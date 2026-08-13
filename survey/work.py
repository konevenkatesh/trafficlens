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
             "-show_entries", "format=duration:format_tags=creation_time",
             "-of", "json", str(path)],
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
    # Phone and camera recordings carry their real start time in the container, even when
    # the filename is VID_0042.mp4 and says nothing. Far better than the file's mtime,
    # which is when it was copied.
    made = None
    tags = (d.get("format") or {}).get("tags") or {}
    raw = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
    if raw:
        try:
            made = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            made = None
    return {"fps": round(fps, 3), "width": st.get("width"), "height": st.get("height"),
            "frames": frames, "duration_s": round(dur or frames / fps, 1),
            "created_meta": made}


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
    added, skipped, duplicates, report = [], [], [], []
    for f in s["files"]:
        if f["path"] in known:
            report.append({"name": f["name"], "status": "already",
                           "note": "already attached"})
            continue
        if not f.get("fps"):
            skipped.append(f["name"])
            report.append({"name": f["name"], "status": "unreadable",
                           "note": "could not be read — not a video, or the file is damaged"})
            continue

        # Every readable video is accepted. The start time is best-effort and always
        # labelled with where it came from, because a missing clock is no reason to
        # refuse a file: a phone recording is called VID_0042.mp4 and carries nothing in
        # its name, and dropping it silently made the app look broken on the most obvious
        # thing anyone would try first.
        #
        # In order of trustworthiness:
        #   filename    the DVR wrote it — authoritative
        #   metadata    the camera wrote it into the container — right for phone video
        #   file-time   when the file was last written, i.e. usually when it was COPIED
        #   assumed     nothing at all was available; placed after the last known
        #               recording so the timeline still has an order
        clock, source = f.get("clock"), "filename"
        if not clock and f.get("created_meta"):
            clock, source = f["created_meta"], "metadata"
        if not clock:
            try:
                clock = datetime.fromtimestamp(
                    Path(f["path"]).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                source = "file-time"
            except OSError:
                clock, source = None, "assumed"
        if not clock:
            base = max([e for _, e in covered], default=datetime.now())
            clock = base.strftime("%Y-%m-%d %H:%M:%S")
            source = "assumed"

        # A guessed clock that collides with footage already placed gets moved to the end
        # instead. Files copied in one go share an mtime to the second, so a folder of
        # phone clips would otherwise stack on top of each other at one instant -- one
        # hour claiming to hold six simultaneous recordings. Laid end to end they are at
        # least in a sensible order, and the source is labelled so nobody mistakes it for
        # the real time. A filename clock is never moved: that one is authoritative.
        if source != "filename":
            t0 = datetime.strptime(clock, "%Y-%m-%d %H:%M:%S")
            dur = timedelta(seconds=f.get("duration_s") or 0)
            if any(t0 < e and t0 + dur > s0 for s0, e in covered):
                t0 = max(e for _, e in covered)
                clock = t0.strftime("%Y-%m-%d %H:%M:%S")
        f = {**f, "clock": clock}
        # Already on the timeline under a different filename. This happens whenever the
        # footage has been cut into clips: the folder still holds the original 80-minute
        # recording, and registering it alongside the five 15-minute clips made from it
        # puts the same traffic on the timeline twice. Counting both would double every
        # vehicle in that hour, and the total would still look entirely plausible.
        #
        # Only for clocks worth trusting. A guessed one says nothing about what the
        # footage contains, and two phone clips copied in the same second are not the
        # same recording -- rejecting them as duplicates would throw away real footage.
        if source == "filename" and _overlap_fraction(f, covered) >= 0.9:
            duplicates.append(f["name"])
            report.append({"name": f["name"], "status": "duplicate",
                           "note": "this footage is already on the timeline under "
                                   "another name"})
            continue
        db.run("""INSERT INTO videos (path,name,fps,frames,width,height,start_clock,
                                      site_id,clock_source,created)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               f["path"], f["name"], f["fps"], f.get("frames"), f.get("width"),
               f.get("height"), clock, site_id, source, time.time())
        added.append(f["name"])
        try:
            _t0 = datetime.strptime(clock, "%Y-%m-%d %H:%M:%S")
            covered.append((_t0, _t0 + timedelta(seconds=f.get("duration_s") or 0)))
        except (ValueError, TypeError):
            pass
        report.append({"name": f["name"], "status": "added", "clock": clock,
                       "clock_source": source,
                       "minutes": round((f.get("duration_s") or 0) / 60, 1),
                       "note": ("start time taken from the file's own timestamp — check it"
                                if source == "file-time" else "")})
    return {"folder": str(Path(folder)), "added": added, "skipped": skipped,
            "duplicates": duplicates, "report": report,
            "guessed_clock": [r["name"] for r in report
                              if r.get("clock_source") == "file-time"],
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
_WORKERS = []
_CURRENT = {}          # worker index -> the job it is running


def worker_count():
    """How many clips to detect at once.

    Measured, not guessed. On an RTX 4090 pure inference runs at 92 fps but the whole
    pipeline only reaches 67 -- a 27% gap where the GPU sits idle waiting for Python to
    do NMS, run the tracker, copy boxes off the device and buffer them to SQLite. One
    stream cannot fill a card that fast; several can overlap each other's gaps.

    On the M4 that gap did not exist (47 fps pipeline against 46 fps inference), which is
    exactly why this stayed single-threaded until now: a second worker there would only
    have halved the first. So the pool is sized to the device rather than the CPU count.

      cpu   1  -- extraction is already using every core; more workers just thrash
      mps   1  -- one Apple GPU, no idle gap to fill, and 16GB shared with the system
      cuda  by VRAM, capped at 4 -- past that the returns are small and the risk of
            an out-of-memory failure mid-survey is not worth it
    """
    import engine
    dev = engine.device()

    # MPS is capped at 1 and the override cannot lift it. PyTorch's Metal backend does not
    # run models concurrently from several threads: forcing 3 workers here produced three
    # extractions that sat at 0% and never finished -- no error, no progress, just stuck.
    # A configurable knob that can hang the app is not a knob.
    if dev == "mps":
        return 1

    forced = os.environ.get("TRAFFICLENS_WORKERS")
    if forced:
        try:
            return max(1, int(forced))
        except ValueError:
            pass
    # Three, measured on an RTX 4090 -- not derived from VRAM, which turned out to be
    # entirely the wrong model. Peak use was 0.5 GB, so memory was never the constraint;
    # sizing by it would have picked 4 on a 24GB card and made the machine SLOWER.
    #
    #   1 worker   84.7 fps   1.00x
    #   2 workers  91.7 fps   1.08x
    #   3 workers 101.8 fps   1.20x
    #   4 workers  56.4 fps   0.67x   <- reproduced four times, not noise
    #
    # Whatever saturates at three collapses at four (128 vCPUs were available, so it is
    # not core count -- most likely the GIL plus CUDA context switching). The cliff is
    # sharp and the gain past two is small, so this stops at three and does not scale
    # with the card.
    if dev == "cuda":
        return 3
    return 1


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
            busy = {c["video_id"] for c in _CURRENT.values() if c}
            if m["video_id"] in pending or m["video_id"] in busy:
                continue
            _Q.append({"kind": "extract", "video_id": m["video_id"], "name": m["name"],
                       "site_id": site_id, "hour": hour_label, "model_id": model_id})
    _ensure_worker()
    return {"queued": len(todo), "hour": hour_label}


def enqueue_render(video_id):
    """Queue an annotated video for one recording.

    Goes through the SAME single worker as extraction rather than starting its own
    thread. Rendering decodes and re-encodes every frame; running it beside a detection
    means two processes fighting one GPU and a machine that appears hung. Queued, the
    surveyor gets both, just not at once.
    """
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    if not v:
        return {"error": "no such recording"}
    with _QLOCK:
        if any(j["video_id"] == video_id and j["kind"] == "render" for j in _Q):
            return {"queued": 0, "note": "already queued"}
        _Q.append({"kind": "render", "video_id": video_id, "name": v["name"],
                   "site_id": None, "hour": None, "model_id": None})
    _ensure_worker()
    return {"queued": 1, "name": v["name"]}


def cancel(video_id=None):
    """Drop queued work. The clip already running is left alone -- killing it mid-write
    would leave a half-populated trajectory store that looks like a complete one."""
    with _QLOCK:
        before = len(_Q)
        if video_id is None:
            _Q.clear()
        else:
            _Q[:] = [j for j in _Q if j["video_id"] != video_id]
        return {"dropped": before - len(_Q),
                "still_running": [dict(c) for c in _CURRENT.values() if c]}


def queue_state():
    with _QLOCK:
        running = [dict(v) for v in _CURRENT.values() if v]
        return {"running": running[0] if running else None,   # kept: single-job callers
                "running_all": running,
                "workers": worker_count(),
                "waiting": [{"video_id": j["video_id"], "name": j["name"],
                             "hour": j["hour"], "kind": j.get("kind", "extract")}
                            for j in _Q],
                "device": device_note()}


def _ensure_worker():
    """Top the pool up to worker_count(), never above it."""
    global _WORKERS
    _WORKERS = [w for w in _WORKERS if w.is_alive()]
    want = worker_count()
    while len(_WORKERS) < want:
        i = len(_WORKERS)
        t = threading.Thread(target=_drain, args=(i,), daemon=True)
        _WORKERS.append(t)
        t.start()


def _drain(slot):
    import engine
    while True:
        with _QLOCK:
            job = _Q.pop(0) if _Q else None
            _CURRENT.pop(slot, None)
            if job:
                _CURRENT[slot] = {"video_id": job["video_id"], "name": job["name"],
                                  "hour": job["hour"], "kind": job.get("kind", "extract"),
                                  "started": time.time()}
        if not job:
            return
        kind = job.get("kind", "extract")
        jid = db.run("INSERT INTO jobs (video_id,kind,status,progress,started) "
                     "VALUES (?,?,'queued',0,?)", job["video_id"], kind, time.time())
        try:
            if kind == "render":
                import render
                render.render(job["video_id"], jid)
            else:
                engine.extract(job["video_id"], jid,
                               model_id=job["model_id"] or default_model())
                _axle(job["video_id"])
        except Exception as e:                      # one bad file must not stop the queue
            db.run("UPDATE jobs SET status='error', message=? WHERE id=?",
                   str(e)[:300], jid)
        finally:
            with _QLOCK:
                _CURRENT.pop(slot, None)


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
            # No line yet, so nothing has "crossed" and there is nothing to judge. Not an
            # error: the line is normally drawn after the first hour is detected, and the
            # pass runs on the next extraction or when the report is built.
            return
        axle_pass.run(video_id)
    except Exception:
        pass
