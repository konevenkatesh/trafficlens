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

# There is deliberately NO idle-exit timer in this agent.
#
# One was here, on the theory that a pod nobody is talking to could at least stop itself
# once the app -- and its watchdog -- had died with the laptop. It was measured on a real
# RTX 3090 pod: the agent exited on schedule, RunPod restarted the container within a
# minute, the agent came back with a fresh clock, exited again 90 seconds later, and the
# pod sat RUNNING at $0.22/hr through eleven such cycles until something external called
# terminate. Exiting the process buys nothing. The pod's life is ended only by
# podTerminate from the RunPod API, which the pod itself cannot call without carrying the
# account's API key -- and a key sitting inside a rented container is a worse problem than
# the one it would solve.
#
# So the guards that exist are all on the app side: the idle watchdog, the Stop button,
# termination on normal app exit, and reconcile on the next launch. If the surveyor's
# machine dies with a pod rented, the pod bills until the app is reopened. The Settings
# screen says so.
_SEEN = [time.time()]


def _extract(job):
    """Track one video and leave the result in STATE.

    A near-copy of the app's local extract(), minus the database: same model, same
    tracker config, same stride arithmetic, same vote-per-track class. Kept deliberately
    in step, because the cloud should be the faster way to get the answer rather than a
    different answer.

    Identical code still does not mean an identical number across GPU backends — one
    clip gave 86 tracks on Metal and 83 on CUDA. Any change here widens that gap on
    purpose rather than by accident, so it should be made in engine.extract too.
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

    print(f"loading {weights.name}", flush=True)
    model = YOLO(str(weights))
    print(f"detecting {video.name}: {frames} frames, stride {stride}", flush=True)
    points, votes, span = [], {}, {}
    _last_log = time.time()
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
            # Every ~30s to the pod console, so the RunPod log shows progress rather than
            # going silent for an hour. A container that prints nothing is impossible to
            # tell from one that has hung, which is exactly how a healthy pod got reported
            # as stuck.
            if time.time() - _last_log > 30:
                _last_log = time.time()
                pct = min(100.0, 100.0 * i / max(frames, 1))
                print(f"  {pct:5.1f}%  frame {i}/{frames}  {len(votes)} vehicles",
                      flush=True)
            with LOCK:
                STATE.update(phase="running",
                             pct=round(min(100.0, 100.0 * i / max(frames, 1)), 1),
                             message=f"{len(votes)} vehicles so far")
    tracks = [{"track_id": tid, "cls": v.most_common(1)[0][0], "votes": dict(v),
               "t_start": span[tid][0], "t_end": span[tid][1],
               "n_points": sum(v.values())} for tid, v in votes.items()]
    # The clip is consumed; the results are in memory and about to be collected. Dropping
    # it here rather than waiting to be told means a survey cannot fill the container disk
    # just because the app died between finishing a clip and tidying up after it.
    try:
        video.unlink()
    except OSError:
        pass
    print(f"done: {len(tracks)} vehicles, {len(points)} boxes in "
          f"{time.time() - t0:.0f}s", flush=True)
    with LOCK:
        STATE.update(phase="done", pct=100.0, error=None,
                     message=f"{len(tracks)} vehicles, {len(points)} boxes",
                     result={"tracks": tracks, "points": points,
                             "seconds": round(time.time() - t0, 1)})


def _run(job):
    try:
        _extract(job)
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        with LOCK:
            STATE.update(phase="error", error=f"{type(e).__name__}: {e}",
                         message=traceback.format_exc()[-800:])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        # Per-request logging stays off -- a poll every four seconds would bury everything
        # else. Phase lines are printed explicitly instead, by the handlers below.
        pass

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
        _SEEN[0] = time.time()
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
            import shutil
            with LOCK:
                out = {k: STATE[k] for k in ("phase", "pct", "message", "error")}
            # Reported on every poll because the app now sends the next clip while this
            # one runs. Two recordings at a gigabyte each is comfortable; a leak that
            # keeps every clip of a station day is not, and "no space left on device"
            # three hours in is not a diagnosable message on a machine nobody can log in
            # to. The number makes it one.
            out["free_gb"] = round(shutil.disk_usage(str(WORK)).free / 1e9, 1)
            return self._json(200, out)
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
        _t0 = time.time()
        print(f"receiving {rel} ({n/1e6:.0f} MB)", flush=True)
        with open(dest, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        _sz = dest.stat().st_size
        _el = max(time.time() - _t0, 1e-6)
        print(f"received {rel}: {_sz/1e6:.0f} MB in {_el:.0f}s ({_sz/1e6/_el:.1f} MB/s)",
              flush=True)
        self._json(200, {"path": rel, "bytes": _sz})

    def do_DELETE(self):
        """Drop a file the app is finished with.

        Needed once the app started uploading the next clip while this one detects: two
        recordings at ~1GB each plus the weights fits the container disk, an unbounded
        pile of them does not. The app deletes each video after its results are safely
        ingested, so the pod holds at most the clip it is working on and the one arriving.
        """
        if not self._authed():
            return
        rel = self.path.lstrip("/")
        dest = (WORK / rel).resolve()
        if not str(dest).startswith(str(WORK.resolve()) + os.sep) or not rel.startswith("video/"):
            return self._json(400, {"error": "bad path"})
        existed = dest.is_file()
        if existed:
            dest.unlink()
        return self._json(200, {"deleted": existed, "path": rel})

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
    print(f"agent listening on {PORT} — ready for work", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
