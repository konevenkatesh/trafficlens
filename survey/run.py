"""Entry point for the packaged app: start the server, open a browser, stay out of the way.

A surveyor double-clicks TrafficLens.exe and expects a window. What actually happens is a
local web server on 127.0.0.1 with the default browser pointed at it -- the same app that
runs in development, so there is no second UI to keep in sync and no Electron to ship.

Three things this has to get right, and each one is a support call if it does not:

  * **Find a free port.** 8801 is often taken. Binding port 0 lets the OS pick and then
    reports which, rather than failing with an error nobody can act on.
  * **Keep the data next to the user, not inside the bundle.** A PyInstaller onefile
    unpacks to a temp directory that is deleted on exit, so a database written there
    vanishes with the app. It goes in the user's home instead.
  * **Say something while it loads.** Importing torch takes several seconds; a console
    that prints nothing looks hung, and the user closes it.
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _base():
    """Where the code lives, bundled or not."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _data_dir():
    """Where the user's work lives. Never inside the bundle -- that is temporary.

    An explicit TRAFFICLENS_DATA wins, so the app can be pointed at a clean directory --
    which is the only way to test what a first-ever run actually does.
    """
    forced = os.environ.get("TRAFFICLENS_DATA")
    if forced:
        d = Path(forced)
        d.mkdir(parents=True, exist_ok=True)
        return d
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = root / "TrafficLens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _free_port(preferred=8801):
    # An explicit port makes the app checkable by something other than a human: the CI
    # smoke test has to know where to look, and "whatever was free" is not an address.
    forced = os.environ.get("TRAFFICLENS_PORT")
    if forced:
        return int(forced)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    base = _base()
    sys.path.insert(0, str(base / "survey"))
    sys.path.insert(0, str(base / "app"))
    sys.path.append(str(base / "lab"))

    data = _data_dir()
    # Read by verify and axle_pass to place their crop caches. Set BEFORE those modules
    # are imported, because both resolve it at import time — and their default is inside
    # the bundle, which is a temp directory that disappears when the app closes.
    os.environ.setdefault("TRAFFICLENS_DATA", str(data))
    # ultralytics writes its settings file here rather than into the bundle, which is a
    # temp directory in a frozen build and read-only under Program Files.
    os.environ.setdefault("YOLO_CONFIG_DIR", str(data))
    print(f"TrafficLens Survey\n  data: {data}\n  loading (this takes a few seconds)…",
          flush=True)

    import db
    db.DB_PATH = data / "trafficlens.db"     # before any connection is opened
    db.conn()

    # A fresh database knows nothing about the weights shipped alongside it, and the rows
    # in a copied one point at paths from the machine that built it. Recompute both from
    # where this copy is actually installed, every start.
    import seed
    try:
        s = seed.run()
        print(f"  detector: {s['detectors'].get('default')}"
              f"  ·  axle head: {'yes' if s['axles'].get('model_id') else s['axles'].get('skipped')}",
              flush=True)
    except Exception as e:
        print(f"  WARNING: could not register the bundled models: {e}", flush=True)

    import api
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    def open_when_up():
        for _ in range(120):
            time.sleep(0.5)
            try:
                with socket.create_connection(("127.0.0.1", port), 0.4):
                    break
            except OSError:
                continue
        webbrowser.open(url)

    if os.environ.get("TRAFFICLENS_NO_BROWSER") != "1":
        threading.Thread(target=open_when_up, daemon=True).start()
    print(f"  ready: {url}\n  keep this window open while you work.", flush=True)

    import uvicorn
    uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
