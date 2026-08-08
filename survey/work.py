"""The survey app's whole model of work: a folder of footage, grouped into hours.

This is the operator-facing product, not the Lab. A surveyor has a camera, a folder of
files off its SD card, and a proforma to fill in. They do not have a dataset, a training
budget, or an opinion about detector architectures, and every screen that asks them for
one is a screen they can get wrong.

So the unit here is **the hour**, not the clip. A MoRTH classified count reports in
15-minute bins rolled up hourly, the surveyor thinks in "I need 07:00 to 08:00 done", and
an hour is also about the right amount of work to commit a machine to in one go. Clips
still exist underneath -- the counting, verification and report code all work per clip --
but the app never makes anyone choose one.

Three things this module owns:

  * reading a folder into recordings, using the clock in each filename
  * grouping those recordings into clock hours, with honest coverage per hour
  * running extraction for a whole hour as one queued job that survives the UI closing

Everything else is borrowed from the modules the Lab already proved: engine.extract for
detection, counting.count_video for crossings, verify for the review queue, report_card
for the deliverable.
"""
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.append(str(ROOT / "lab"))

# ── offline by construction ──
# Set before anything imports ultralytics, because it reads all of these at import time.
# These machines sit in site offices with no connection, and each default here is a way
# for the app to hang or fail there:
#
#   YOLO_OFFLINE     ultralytics probes Cloudflare and Google DNS on import to decide if
#                    it is online. With no network that is a DNS timeout on every start,
#                    before the app shows anything.
#   YOLO_AUTOINSTALL a missing dependency makes it run `pip install` at RUN TIME. In a
#                    frozen build there is no pip and no index; it would fail loudly at
#                    the worst moment instead of never being attempted.
#   YOLO_CONFIG_DIR  it writes a settings file next to the package by default, which in
#                    a bundle is a temp dir, and under Program Files is read-only.
#
# Nothing this app does needs the internet. The one exception is voice recognition, which
# is opt-in, off by default, and disabled in the UI when the browser reports no network.
# matplotlib is pulled in by ultralytics. Agg is the non-interactive backend: without
# this it hunts for a GUI toolkit that a frozen build deliberately does not ship.
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("YOLO_OFFLINE", "1")
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("YOLO_CONFIG_DIR",
                      os.environ.get("TRAFFICLENS_DATA") or str(ROOT / "app"))

import db  # noqa: E402

VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".ts", ".dav"}

# Camera filenames carry the start clock, and that is the only reliable way to place a
# recording on a timeline -- file mtime is the COPY time and is wrong by days. These are
# the shapes seen on the DVRs in use; an unrecognised name is reported as undated rather
# than guessed at, because a guessed clock silently puts traffic in the wrong hour.
CLOCK_PATTERNS = [
    r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})",          # ch03_20260704130000
    r"(20\d{2})-(\d{2})-(\d{2})[ _T](\d{2})[-.:](\d{2})[-.:](\d{2})",
    r"(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})",
]


def clock_of(name):
    """The start time written into a filename, or None if it does not carry one."""
    for pat in CLOCK_PATTERNS:
        m = re.search(pat, name)
        if m:
            try:
                return datetime(*(int(g) for g in m.groups()))
            except ValueError:
                continue
    return None


def _ffprobe():
    """ffprobe, from the bundle if this is a packaged build, else from PATH.

    A frozen Windows build has no system ffprobe and no PATH entry for one, so the plain
    name fails and every recording looks unreadable. sys._MEIPASS is where PyInstaller
    unpacks bundled binaries.
    """
    import shutil
    base = Path(getattr(sys, "_MEIPASS", ""))
    for c in (base / "ffprobe.exe", base / "ffprobe"):
        if c.is_file():
            return str(c)
    return shutil.which("ffprobe") or "ffprobe"


def probe(path):
    """Duration, fps and dimensions.

    JSON output, not `-of default=nw=1:nk=1`. The flat form prints fields in ffprobe's
    own order rather than the order they were asked for, so reading them positionally
    silently mislabels every value -- width arrives where fps was expected. Named fields
    cannot drift like that.

    `nb_frames` is routinely N/A on DVR recordings (no index in the container), so the
    frame count falls back to duration x fps. Everything downstream divides by fps to get
    a clock, so a missing frame count must not make the file unreadable.
    """
    import json as _json
    try:
        out = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,nb_frames,width,height",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        d = _json.loads(out.stdout or "{}")
    except Exception:
        return None
    st = (d.get("streams") or [{}])[0]
    try:
        num, den = (str(st.get("avg_frame_rate", "0/1")).split("/") + ["1"])[:2]
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    if fps <= 0:
        return None
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    nb = st.get("nb_frames")
    frames = int(nb) if str(nb).isdigit() else (int(dur * fps) if dur else None)
    if not frames:
        return None
    return {"fps": round(fps, 3), "width": st.get("width"), "height": st.get("height"),
            "frames": frames, "duration_s": round(dur or frames / fps, 1)}


# ───────────────────────────── the folder ─────────────────────────────
def scan(folder):
    """Every video file in a folder, dated and measured.

    Not recursive on purpose. A surveyor points at the folder they copied off the camera;
    walking into subfolders would sweep in last month's job sitting one level down, and
    the resulting report would silently cover two surveys.
    """
    p = Path(folder)
    if not p.is_dir():
        return {"error": f"not a folder: {folder}", "files": []}
    files = []
    for f in sorted(p.iterdir()):
        if f.suffix.lower() not in VIDEO_EXT or f.name.startswith("."):
            continue
        info = probe(f) or {}
        files.append({
            "name": f.name, "path": str(f), "size_mb": round(f.stat().st_size / 1e6, 1),
            "clock": (clock_of(f.name).strftime("%Y-%m-%d %H:%M:%S")
                      if clock_of(f.name) else None),
            **info,
        })
    return {"folder": str(p), "files": files,
            "undated": [f["name"] for f in files if not f["clock"]]}


def attach(site_id, folder):
    """Point a station at its footage folder and register what is in it.

    Re-runnable by design: a surveyor copies more files off the camera and presses the
    same button. Files already known are left exactly as they are -- re-registering would
    orphan every count and verdict already attached to them.
    """
    s = scan(folder)
    if s.get("error"):
        return s
    db.run("UPDATE sites SET footage_dir=? WHERE id=?", str(Path(folder)), site_id)
    known = {r["path"] for r in db.rows("SELECT path FROM videos WHERE site_id=?", site_id)}
    covered = _covered_spans(site_id)
    added, skipped, duplicates = [], [], []
    for f in s["files"]:
        if f["path"] in known:
            continue
        if not f.get("clock"):
            skipped.append(f["name"])       # undated: cannot be placed on the timeline
            continue
        if not f.get("fps"):
            skipped.append(f["name"])       # unreadable: ffprobe could not measure it
            continue
        # Already on the timeline under a different filename. This happens whenever the
        # footage has been cut into clips: the folder still holds the original 80-minute
        # recording, and registering it alongside the five 15-minute clips made from it
        # puts the same traffic on the timeline twice. Counting both would double every
        # vehicle in that hour, and the total would still look entirely plausible.
        if _overlap_fraction(f, covered) >= 0.9:
            duplicates.append(f["name"])
            continue
        db.run("""INSERT INTO videos (path,name,fps,frames,width,height,start_clock,
                                      site_id,clock_source,created)
                  VALUES (?,?,?,?,?,?,?,?,'filename',?)""",
               f["path"], f["name"], f["fps"], f.get("frames"), f.get("width"),
               f.get("height"), f["clock"], site_id, time.time())
        added.append(f["name"])
    return {"folder": str(Path(folder)), "added": added, "skipped": skipped,
            "duplicates": duplicates,
            "total_files": len(s["files"]), "undated": s["undated"]}


def _covered_spans(site_id):
    """Clock intervals this station already has footage for."""
    out = []
    for v in db.rows("""SELECT start_clock, frames, fps FROM videos
                        WHERE site_id=? AND COALESCE(excluded,0)=0
                          AND start_clock IS NOT NULL""", site_id):
        try:
            t0 = datetime.strptime(v["start_clock"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        d = (v["frames"] or 0) / (v["fps"] or 1)
        if d > 0:
            out.append((t0, t0 + timedelta(seconds=d)))
    return out


def _overlap_fraction(f, spans):
    """How much of this file's running time is already covered by known footage."""
    try:
        a = datetime.strptime(f["clock"][:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, KeyError):
        return 0.0
    dur = f.get("duration_s") or 0
    if dur <= 0:
        return 0.0
    b = a + timedelta(seconds=dur)
    # Merge first: five consecutive clips each covering a fifth of the file must add up
    # to "fully covered", not be judged one at a time and each dismissed as partial.
    merged = []
    for s0, s1 in sorted(spans):
        if merged and s0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s1)
        else:
            merged.append([s0, s1])
    hit = sum(max(0.0, (min(b, s1) - max(a, s0)).total_seconds()) for s0, s1 in merged)
    return hit / dur


# ───────────────────────────── hours ─────────────────────────────
def _dur_s(v):
    if v.get("frames") and v.get("fps"):
        return v["frames"] / v["fps"]
    return 0.0


def hours(site_id):
    """The footage laid out as clock hours -- the unit the surveyor works in.

    A recording rarely lines up with the hour: a 50-minute file starting 13:05 belongs
    partly to 13:00 and partly to 14:00. Rather than force it into one, each hour reports
    how many seconds of footage actually cover it. `coverage` below 1.0 is not an error;
    it is the honest statement that this hour is partly unfilmed, and a proforma that
    hides it reports a quiet hour that was never recorded.
    """
    vids = db.rows("""SELECT id,name,path,fps,frames,start_clock FROM videos
                      WHERE site_id=? AND COALESCE(excluded,0)=0
                        AND start_clock IS NOT NULL ORDER BY start_clock""", site_id)
    if not vids:
        return []
    spans = []
    for v in vids:
        try:
            t0 = datetime.strptime(v["start_clock"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        d = _dur_s(v)
        if d <= 0:
            continue
        spans.append((t0, t0 + timedelta(seconds=d), v))
    if not spans:
        return []

    counted = {r["video_id"]: r["n"] for r in db.rows(
        """SELECT video_id, COUNT(*) n FROM tracks
           WHERE video_id IN (SELECT id FROM videos WHERE site_id=?) GROUP BY video_id""",
        site_id)}

    lo = min(s[0] for s in spans).replace(minute=0, second=0, microsecond=0)
    hi = max(s[1] for s in spans)
    out, t = [], lo
    while t < hi:
        nxt = t + timedelta(hours=1)
        covered, members = 0.0, []
        for a, b, v in spans:
            ov = (min(b, nxt) - max(a, t)).total_seconds()
            if ov > 1:
                covered += ov
                members.append({"video_id": v["id"], "name": v["name"],
                                "seconds_here": round(ov),
                                "tracks": counted.get(v["id"], 0)})
        if members:
            done = sum(1 for m in members if m["tracks"])
            out.append({
                "hour": t.strftime("%Y-%m-%d %H:00"),
                "label": t.strftime("%H:00"),
                "date": t.strftime("%Y-%m-%d"),
                "coverage": round(min(covered / 3600.0, 1.0), 3),
                "minutes": round(covered / 60),
                "files": members,
                "extracted": done,
                "total": len(members),
                "state": "done" if done == len(members) else
                         "part" if done else "todo",
                "night": _is_night(t),
            })
        t = nxt
    return out


def _is_night(t):
    """Rough daylight flag, for warning that night footage counts differently.

    Deliberately a fixed window rather than a solar calculation: it only drives a caption,
    and a wrong sunrise from a missing coordinate would be worse than an approximate one.
    Wide enough to cover Indian sunset through the year -- 18:00 in July is broad daylight
    and flagging it as night would train the surveyor to ignore the flag.
    """
    return not (6 <= t.hour < 19)


# ───────────────────────────── extracting an hour ─────────────────────────────
# One worker, one clip at a time, process-wide. Extraction is GPU-bound and every parallel
# worker shares the same device: two at once do not go twice as fast, they halve each
# other and double the memory. On a CPU-only laptop a second worker is actively harmful.
_Q = []
_QLOCK = threading.Lock()
_WORKER = None
_CURRENT = {}


def default_model():
    """The detector to use when the surveyor has not picked one.

    Asked of the registry rather than left to engine.MODEL_ID. Those two disagree -- the
    registry's default is v5, the module constant is v4 -- so passing None quietly ran a
    different detector from the one the app displays as default. On a survey that is a
    silent change of instrument halfway through.
    """
    try:
        import models_registry
        models_registry.init()
        return models_registry.default_id()
    except Exception:
        return None


def device_note():
    """What this machine will extract on, and how slow that is -- said before committing.

    A surveyor on a plain Windows laptop is about to start something that takes forty
    minutes per clip, and the app that does not say so up front looks broken ten minutes
    in. The multiplier is measured: ~3.8x realtime on Apple GPU, similar on CUDA, and
    roughly 0.4x on CPU.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return {"device": "cuda", "name": torch.cuda.get_device_name(0),
                    "speed": 3.8, "note": "NVIDIA GPU"}
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return {"device": "mps", "name": "Apple GPU", "speed": 3.8,
                    "note": "Apple GPU"}
    except Exception:
        pass
    return {"device": "cpu", "name": f"CPU ({os.cpu_count()} cores)", "speed": 0.4,
            "note": "no GPU found — extraction runs on the processor and is slow"}


def estimate_s(seconds_of_footage):
    return seconds_of_footage / max(device_note()["speed"], 0.01)


def enqueue_hour(site_id, hour_label, model_id=None):
    """Queue every unextracted file covering one hour."""
    hrs = [h for h in hours(site_id) if h["hour"] == hour_label]
    if not hrs:
        return {"error": "no such hour"}
    todo = [m for m in hrs[0]["files"] if not m["tracks"]]
    if not todo:
        return {"queued": 0, "note": "this hour is already extracted"}
    with _QLOCK:
        pending = {j["video_id"] for j in _Q}
        for m in todo:
            if m["video_id"] in pending or _CURRENT.get("video_id") == m["video_id"]:
                continue
            _Q.append({"video_id": m["video_id"], "name": m["name"],
                       "site_id": site_id, "hour": hour_label, "model_id": model_id})
    _ensure_worker()
    return {"queued": len(todo), "hour": hour_label}


def cancel(video_id=None):
    """Drop queued work. The clip already running is left alone -- killing it mid-write
    would leave a half-populated trajectory store that looks like a complete one."""
    with _QLOCK:
        before = len(_Q)
        if video_id is None:
            _Q.clear()
        else:
            _Q[:] = [j for j in _Q if j["video_id"] != video_id]
        return {"dropped": before - len(_Q), "still_running": dict(_CURRENT)}


def queue_state():
    with _QLOCK:
        return {"running": dict(_CURRENT) or None,
                "waiting": [{"video_id": j["video_id"], "name": j["name"],
                             "hour": j["hour"]} for j in _Q],
                "device": device_note()}


def _ensure_worker():
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return
    _WORKER = threading.Thread(target=_drain, daemon=True)
    _WORKER.start()


def _drain():
    import engine
    while True:
        with _QLOCK:
            job = _Q.pop(0) if _Q else None
            _CURRENT.clear()
            if job:
                _CURRENT.update({"video_id": job["video_id"], "name": job["name"],
                                 "hour": job["hour"], "started": time.time()})
        if not job:
            return
        jid = db.run("INSERT INTO jobs (video_id,kind,status,progress,started) "
                     "VALUES (?,'extract','queued',0,?)", job["video_id"], time.time())
        try:
            engine.extract(job["video_id"], jid, model_id=job["model_id"] or default_model())
            _axle(job["video_id"])
        except Exception as e:                      # one bad file must not stop the queue
            db.run("UPDATE jobs SET status='error', message=? WHERE id=?",
                   str(e)[:300], jid)
        finally:
            with _QLOCK:
                _CURRENT.clear()


def _axle(video_id):
    """Run the universal axle head straight after extraction.

    The detector cannot read axles and the proforma has a column for each, so this is not
    an optional extra -- leaving it manual meant every report shipped the detector's raw
    guess in three columns. Advisory: never fail an extraction that otherwise worked.
    """
    try:
        import axle_pass
        import sites
        if not sites.lines_for(video_id)[0]:
            return                                  # no line: nothing crossed, nothing to judge
        axle_pass.run(video_id)
    except Exception:
        pass
