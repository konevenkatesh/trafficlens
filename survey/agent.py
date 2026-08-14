"""The half of extraction that runs ON the rented GPU.

This file never runs on the surveyor's machine. It is base64'd into the pod's start
command by `remote.py`, so the pod needs nothing fetched from anywhere: no registry to
push to, no repository to clone, no credentials on the pod. Change this file and the next
pod runs the new version — there is no second artifact to keep in step.

**Standard library only, plus what the image already has.** The container is
`ultralytics/ultralytics:<pinned>`, which brings CUDA torch and the exact ultralytics the
app pins locally. Anything installed with pip at boot is a minute of billed time and one
more thing that can fail on a machine nobody can log into, so this uses `http.server`
rather than FastAPI. It is a five-endpoint file transfer; it does not need a framework.

**The protocol is deliberately dumb.** Files in, one run, results out, in a shape the
local database can absorb directly. The pod holds no state worth keeping: if it dies
mid-clip the app re-runs the clip somewhere else, which is cheaper than making this
resumable.

Everything is guarded by a per-pod token. The RunPod proxy URL is guessable from a pod id
and is reachable by anyone on the internet, so an unauthenticated agent here would be an
open GPU and an open file-write endpoint.
"""
import gzip
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path("/work")
TOKEN = os.environ.get("TL_TOKEN", "")
PORT = int(os.environ.get("TL_PORT", "8000"))

STATE = {"phase": "idle", "pct": 0.0, "message": "waiting for work",
         "error": None, "result": None, "started": None}
LOCK = threading.Lock()


def _extract(job):
    """Track one video and leave the result in STATE.

    A near-copy of the app's local extract(), minus the database: same model, same
    tracker config, same stride arithmetic, same vote-per-track class. It has to be the
    same, or a count would depend on where it was computed — which would make the cloud
    option a different product rather than a faster one.
    """
    from collections import Counter
    from ultralytics import YOLO

    video = WORK / "video" / job["video"]
    weights = WORK / "models" / job["weights"]
    tracker = WORK / "tracker.yaml"
    stride = int(job.get("stride") or 1)
    frames = int(job.get("frames") or 0)

    with LOCK:
        STATE.update(phase="loading", pct=0.0, message="loading the detector",
                     error=None, result=None, started=time.time())

    model = YOLO(str(weights))
    points, votes, span = [], {}, {}
    t0 = time.time()
    results = model.track(source=str(video), stream=True, persist=True,
                          tracker=str(tracker), conf=float(job.get("conf", 0.12)),
                          imgsz=int(job.get("imgsz", 960)), vid_stride=stride,
                          device=0, verbose=False)
    for n, r in enumerate(results):
        i = n * stride                     # the real frame index, not the seen-frame count
        if r.boxes.id is not None:
            for b, c, tid, cf in zip(r.boxes.xyxy.cpu().numpy(),
                                     r.boxes.cls.cpu().numpy(),
                                     r.boxes.id.cpu().numpy(),
                                     r.boxes.conf.cpu().numpy()):
                tid = int(tid)
                points.append([tid, i, *[round(float(x), 1) for x in b],
                               round(float(cf), 3)])
                votes.setdefault(tid, Counter())[int(c)] += 1
                s = span.get(tid)
                span[tid] = (i if s is None else s[0], i)
        if n % 250 == 0:
            with LOCK:
                STATE.update(phase="running",
                             pct=round(min(100.0, 100.0 * i / max(frames, 1)), 1),
                             message=f"{len(votes)} vehicles so far")
    tracks = [{"track_id": tid, "cls": v.most_common(1)[0][0], "votes": dict(v),
               "t_start": span[tid][0], "t_end": span[tid][1],
               "n_points": sum(v.values())} for tid, v in votes.items()]
    with LOCK:
        STATE.update(phase="done", pct=100.0, error=None,
                     message=f"{len(tracks)} vehicles, {len(points)} boxes",
                     result={"tracks": tracks, "points": points,
                             "seconds": round(time.time() - t0, 1)})


def _run(job):
    try:
        _extract(job)
    except Exception as e:
        with LOCK:
            STATE.update(phase="error", error=f"{type(e).__name__}: {e}",
                         message=traceback.format_exc()[-800:])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                   # the pod's console is billed, not read

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _authed(self):
        if TOKEN and self.headers.get("X-Token") != TOKEN:
            self._json(403, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":            # unauthenticated: it is the readiness probe
            import torch
            return self._json(200, {"ok": True, "cuda": torch.cuda.is_available(),
                                    "gpu": (torch.cuda.get_device_name(0)
                                            if torch.cuda.is_available() else None)})
        if not self._authed():
            return
        if self.path == "/progress":
            with LOCK:
                return self._json(200, {k: STATE[k] for k in
                                        ("phase", "pct", "message", "error")})
        if self.path == "/result":
            with LOCK:
                r = STATE.get("result")
            if not r:
                return self._json(409, {"error": "no result yet"})
            # Gzipped because the boxes dominate: a 15-minute clip is a few hundred
            # thousand of them, and they compress by roughly 4x.
            return self._send(200, gzip.compress(json.dumps(r).encode()),
                              "application/gzip")
        self._json(404, {"error": "no such path"})

    def do_PUT(self):
        """Upload a file. Weights, tracker config and the video all arrive this way."""
        if not self._authed():
            return
        rel = self.path.lstrip("/")
        # The path comes off the wire and decides where bytes land, so containment is
        # checked against the resolved path rather than argued from the string. Testing
        # this with curl proved nothing -- curl collapses "/../.." before sending, so the
        # server never saw the attack the test thought it was making. A raw socket would.
        # Only the three destinations this protocol actually uses are accepted.
        dest = (WORK / rel).resolve()
        if (not rel
                or not str(dest).startswith(str(WORK.resolve()) + os.sep)
                or not (rel.startswith("models/") or rel.startswith("video/")
                        or rel == "tracker.yaml")):
            return self._json(400, {"error": "bad path"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = int(self.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        self._json(200, {"path": rel, "bytes": dest.stat().st_size})

    def do_POST(self):
        if not self._authed():
            return
        if self.path != "/run":
            return self._json(404, {"error": "no such path"})
        with LOCK:
            if STATE["phase"] in ("loading", "running"):
                return self._json(409, {"error": "already running"})
        n = int(self.headers.get("Content-Length") or 0)
        job = json.loads(self.rfile.read(n) or b"{}")
        threading.Thread(target=_run, args=(job,), daemon=True).start()
        self._json(200, {"started": True})


if __name__ == "__main__":
    (WORK / "video").mkdir(parents=True, exist_ok=True)
    (WORK / "models").mkdir(parents=True, exist_ok=True)
    print(f"agent listening on {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
