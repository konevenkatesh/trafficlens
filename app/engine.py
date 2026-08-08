"""Extraction engine: video -> trajectory store (run ONCE; lines are queries later)."""
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import db

# Bundled read-only files live under sys._MEIPASS in a frozen build, and `__file__` for
# a frozen module points inside it -- so `__file__.parent.parent` lands ABOVE the bundle
# and every packaged path silently misses. Writable paths must NOT use this: they follow
# TRAFFICLENS_DATA instead, because the bundle is a temp directory deleted on exit.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# TRAFFICLENS_MODEL lets a candidate model be extracted into its own video row and
# compared against the live one, without disturbing the trajectories already reviewed.
MODEL_ID = os.environ.get("TRAFFICLENS_MODEL", "yolo26s_morth15_v4")
MODEL = ROOT / "models" / f"{MODEL_ID}.pt"
TRACKER = ROOT / "benchmark" / "bytetrack_low.yaml"
CLASSES = ["2W", "3W_Auto", "Car_Jeep_Van", "LCV", "Mini_Bus", "Bus", "Tractor",
           "Tractor_Trailer", "2Axle_Truck", "3Axle_Truck", "MAV", "Cycle",
           "Cycle_Rickshaw", "Animal_Cart", "Other"]


FALLBACK_CLOCK = "2026-01-01 00:00:00"   # stand-in when the filename carries no timestamp


def real_fps(stream):
    """The rate that actually maps frame numbers to wall-clock seconds.

    DVR recordings are variable-frame-rate, and for those `r_frame_rate` reports the
    stream's *base* rate — the ceiling the encoder was configured for — not the rate
    frames were written at. The FID footage says 20/1 while its 13500 frames span 900
    seconds, i.e. 14.99. Trusting `r_frame_rate` therefore stretches every clock time,
    every 15-minute bin and every duration by 33%, silently.

    `avg_frame_rate` is frames÷duration and is the honest answer, so it wins whenever
    it is present and sane. `nb_frames/duration` is the cross-check.
    """
    def ratio(k):
        try:
            num, den = str(stream.get(k, "")).split("/")
            v = float(num) / float(den)
            return v if 1 < v < 240 else None
        except (ValueError, ZeroDivisionError):
            return None

    avg, base = ratio("avg_frame_rate"), ratio("r_frame_rate")
    try:
        measured = int(stream["nb_frames"]) / float(stream["duration"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        measured = None
    return avg or measured or base or 25.0


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
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,avg_frame_rate,nb_frames,width,height,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    import json
    s = json.loads(out)["streams"][0]
    fps = round(real_fps(s), 3)
    frames = int(s.get("nb_frames") or float(s.get("duration", 0)) * fps)
    m = re.search(r"ch\d+_(\d{14})", Path(path).name)
    clock = (datetime.strptime(m.group(1), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
             if m else FALLBACK_CLOCK)
    return fps, frames, int(s["width"]), int(s["height"]), clock


def extract(video_id, job_id, imgsz=960, conf=0.12, model_id=None):
    """Runs in a worker thread. Stores every tracked box into track_points.

    `model_id` picks a registered detector; without one the app's default is used. The id
    is stored on every track, so counts always name the model that produced them.
    """
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    db.run("UPDATE jobs SET status='running', started=? WHERE id=?", time.time(), job_id)
    try:
        from ultralytics import YOLO
        use_id, use_path = MODEL_ID, MODEL
        if model_id:
            cand = ROOT / "models" / f"{model_id}.pt"
            if not cand.exists():
                raise FileNotFoundError(f"model weights not found: {cand.name}")
            use_id, use_path = model_id, cand
        model = YOLO(str(use_path))
        db.run("DELETE FROM track_points WHERE video_id=?", video_id)
        db.run("DELETE FROM tracks WHERE video_id=?", video_id)
        votes = {}
        span = {}
        buf = []
        t0 = time.time()
        results = model.track(source=v["path"], stream=True, persist=True,
                              tracker=str(TRACKER), conf=conf, imgsz=imgsz,
                              vid_stride=1, device="mps", verbose=False)
        for i, r in enumerate(results):
            if r.boxes.id is not None:
                for b, c, tid, cf in zip(r.boxes.xyxy.cpu().numpy(),
                                         r.boxes.cls.cpu().numpy(),
                                         r.boxes.id.cpu().numpy(),
                                         r.boxes.conf.cpu().numpy()):
                    tid = int(tid)
                    buf.append((video_id, tid, i, *[round(float(x), 1) for x in b], round(float(cf), 3)))
                    votes.setdefault(tid, Counter())[int(c)] += 1
                    s = span.get(tid)
                    span[tid] = (i if s is None else s[0], i)
            if len(buf) >= 2000:
                db.runmany("INSERT INTO track_points VALUES (?,?,?,?,?,?,?,?)", buf)
                buf = []
            if i % 250 == 0:
                pct = 100.0 * i / max(v["frames"], 1)
                rate = (i / v["fps"]) / max(time.time() - t0, 1e-6)
                db.run("UPDATE jobs SET progress=?, message=? WHERE id=?",
                       round(pct, 1), f"{rate:.2f}x realtime", job_id)
        if buf:
            db.runmany("INSERT INTO track_points VALUES (?,?,?,?,?,?,?,?)", buf)
        db.runmany(
            "INSERT INTO tracks (video_id,track_id,cls,cls_votes,class_override,t_start,t_end,n_points,model_id) "
            "VALUES (?,?,?,?,NULL,?,?,?,?)",
            [(video_id, tid, votes[tid].most_common(1)[0][0], db.jdump(dict(votes[tid])),
              span[tid][0], span[tid][1], sum(votes[tid].values()), use_id)
             for tid in votes])
        import dedup as dedup_mod
        d = dedup_mod.dedup(video_id)
        db.run("UPDATE jobs SET status='done', progress=100, finished=?, message=? WHERE id=?",
               time.time(), f"{len(votes)} tracks stored, {d['suppressed']} duplicates suppressed", job_id)
    except Exception as e:
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)
