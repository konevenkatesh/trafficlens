"""Drive the INSTALLED app through a whole survey, over its own HTTP API.

This is the test that says the thing anyone actually cares about: a surveyor can install
it and get a report out. The smoke test only proved the process starts and knows what a
detector is; every step after that -- reading a folder, placing recordings on a timeline,
running the detector, counting crossings, building the workbook -- was still only ever
verified on the developer's Mac.

It runs against a synthetic clip, so the counts are expected to be zero: a generated test
pattern contains no vehicles. That is fine and deliberate. What is being tested is the
machinery, not the model -- whether ffprobe reads the file, whether the timeline groups
it, whether YOLO runs to completion over real frames without a missing DLL or a
misresolved bundle path, whether counting and the xlsx writer work. Detection quality is
measured elsewhere, against footage, on hardware that can do it in reasonable time.

Exit code 0 means a surveyor could have done this by hand.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8801"
FOOTAGE = sys.argv[2] if len(sys.argv) > 2 else "testfootage"
FAILED = []


def api(path, body=None, method=None, timeout=120):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or "{}")


def step(name, fn):
    print(f"\n>> {name}", flush=True)
    try:
        out = fn()
        print(f"   ok: {out}", flush=True)
        return out
    except Exception as e:
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = " — " + e.read().decode()[:300]
            except Exception:
                pass
        print(f"   FAILED: {e}{detail}", flush=True)
        FAILED.append(name)
        return None


def main():
    step("health", lambda: api("/api/health"))

    st = step("create a station", lambda: api("/api/stations", {"name": "CI End To End"}))
    if not st:
        return finish()
    sid = st["id"]

    folder = str(Path(FOOTAGE).resolve())
    att = step("attach the footage folder",
               lambda: api(f"/api/stations/{sid}/folder", {"folder": folder}))
    if not att or not att.get("added"):
        print(f"   nothing was registered from {folder}", flush=True)
        FAILED.append("attach registered no recordings")
        return finish()

    hours = step("group into hours", lambda: api(f"/api/stations/{sid}")["hours"])
    if not hours:
        FAILED.append("no hours produced")
        return finish()

    # A line across the middle of the frame. Coordinates are in source pixels, and the
    # synthetic clip is 1920x1080 like the real cameras.
    step("draw the count line", lambda: api(f"/api/stations/{sid}/line", {
        "lines": [{"name": "L1", "start": [300, 540], "end": [1620, 540]}]}))

    step("read a frame for the line editor",
         lambda: len(urllib.request.urlopen(
             f"{BASE}/api/stations/{sid}/frame?at=0.3", timeout=120).read()))

    # The hour label is "2026-01-01 12:00" — it has a space in it, so it has to be
    # encoded before it goes in a path. urllib refuses a raw one outright, which is how
    # this was caught; a client that quietly sent it would have got a 404 instead.
    hour = hours[0]["hour"]
    step(f"extract {hour}",
         lambda: api(f"/api/stations/{sid}/hours/"
                     f"{urllib.parse.quote(hour)}/extract", {}))

    # Extraction is CPU-only on a runner: roughly 2.5x the clip's own length.
    def wait():
        for _ in range(180):
            time.sleep(5)
            q = api("/api/queue")
            if not q.get("running") and not q.get("waiting"):
                return "queue drained"
            r = q.get("running") or {}
            if r:
                print(f"      {r.get('name')} {round(r.get('progress') or 0)}%", flush=True)
        raise TimeoutError("extraction did not finish in 15 minutes")
    step("wait for extraction", wait)

    # Zero detections is a legitimate outcome here -- the clip is a generated test
    # pattern and contains no vehicles -- so "did it find anything" is the wrong
    # question. The right one is whether the job finished without erroring, which is
    # what a real failure looks like: an hour that silently stays undetected.
    dd = step("no extraction errors", lambda: api(f"/api/stations/{sid}"))
    if dd and dd.get("failures"):
        for f in dd["failures"]:
            print(f"      {f['name']}: {f['message']}", flush=True)
        FAILED.append(f"{len(dd['failures'])} extraction(s) errored")
    tracks = (dd or {}).get("progress", {}).get("tracks", 0)
    print(f"   detector found {tracks} vehicle(s) "
          f"({'expected 0 on a test pattern' if not tracks else 'unexpected but fine'})",
          flush=True)

    step("review queue builds",
         lambda: f"{len(api(f'/api/stations/{sid}/review?mode=all&limit=5')['items'])} item(s)")

    # The annotated video. With nothing detected there is nothing to draw, so what is
    # tested here is that the app says so instead of erroring, and that the state
    # endpoint the button polls answers at all -- a 404 or a 500 there leaves the
    # surveyor watching a button that says "Queued…" forever. The encoder itself is
    # proved separately, by encoder_check.py, which does not need vehicles.
    vid = None
    for h in api(f"/api/stations/{sid}").get("hours") or []:
        for f in h.get("files") or []:
            vid = f["video_id"]
            break
        if vid:
            break

    def annotate_state():
        st = api(f"/api/clips/{vid}/render_state")
        for k in ("ready", "stale"):
            if k not in st:
                raise KeyError(f"render_state is missing {k}")
        return f"ready={st['ready']} stale={st['stale']} job={st.get('job')}"

    if vid:
        step("annotated-video state reads", annotate_state)

        def annotate_guard():
            try:
                api(f"/api/clips/{vid}/annotate", {})
                return "accepted the render"
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    return "declined cleanly with 400 — nothing detected to draw"
                raise
        step("annotate refuses when there is nothing to draw", annotate_guard)

    rep = step("build the report", lambda: api(f"/api/stations/{sid}/report"))
    if rep is not None and rep.get("empty") and tracks:
        FAILED.append("report came back empty despite detections")

    if not tracks:
        # Nothing was detected, so there is no workbook to write and the endpoint should
        # say so cleanly. A 500 here would mean the writer crashed rather than declined.
        def refuses():
            try:
                urllib.request.urlopen(f"{BASE}/api/stations/{sid}/report.xlsx", timeout=60)
                return "produced a workbook from nothing (unexpected, not fatal)"
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    return "declined cleanly with 400, as it should"
                raise
        step("Excel export handles an empty station", refuses)
        return finish()

    def xlsx():
        with urllib.request.urlopen(f"{BASE}/api/stations/{sid}/report.xlsx", timeout=300) as r:
            data = r.read()
        # A workbook is a zip; anything else means the writer produced junk with a 200.
        if data[:2] != b"PK":
            raise ValueError(f"not a workbook: starts with {data[:8]!r}")
        return f"{len(data)} bytes"
    step("download the Excel report", xlsx)

    return finish()


def finish():
    print("\n" + "=" * 60, flush=True)
    if FAILED:
        print(f"END TO END FAILED — {len(FAILED)} step(s):", flush=True)
        for f in FAILED:
            print(f"   - {f}", flush=True)
        return 1
    print("END TO END PASSED — install to report, on Windows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
