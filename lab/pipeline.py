"""The Lab pipeline: every stage a run passes through, start to finish.

Stages are explicit rows in lab_stages so the UI can show exactly where a run is,
what it cost, and what it produced. One worker thread per run; stages run in order
and a failure stops the chain with the reason recorded.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
APP = ROOT / "app"
WORK = ROOT / "lab_work"
PY = ROOT / ".venv" / "bin" / "python"
CLASSES = ["2W", "3W_Auto", "Car_Jeep_Van", "LCV", "Mini_Bus", "Bus", "Tractor",
           "Tractor_Trailer", "2Axle_Truck", "3Axle_Truck", "MAV", "Cycle",
           "Cycle_Rickshaw", "Animal_Cart", "Other"]

# `existence` sits between sample and judge on purpose: it is cheaper to drop a junk
# crop than to have three class judges confidently agree it is a vehicle.
STAGES = ["probe", "segment", "compress", "extract", "sample", "existence", "judge",
          "complete", "review", "dataset", "train", "eval"]
_workers = {}


# ─────────────────────────────── helpers ───────────────────────────────
def sh(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def ffprobe(path):
    code, out, _ = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
                       "-show_entries",
                       "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,"
                       "duration,codec_name",
                       "-show_entries", "format=duration,size", "-of", "json", str(path)])
    if code != 0:
        return None
    d = json.loads(out)
    s, f = d["streams"][0], d["format"]
    dur = float(f.get("duration") or 0)
    # One implementation of "what is this file's real frame rate", shared with the
    # counting app — see engine.real_fps for why r_frame_rate cannot be trusted.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    from engine import real_fps
    fps = real_fps({**s, "duration": s.get("duration") or dur})
    return {"width": s["width"], "height": s["height"], "fps": round(fps, 3),
            "codec": s.get("codec_name"), "duration_s": dur,
            "frames": int(s.get("nb_frames") or dur * fps),
            "size_mb": round(int(f.get("size", 0)) / 1e6, 1)}


def clock_from_name(name):
    """Camera files carry their start time; fall back to file mtime."""
    m = re.search(r"(\d{14})", name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    m = re.search(r"(\d{8})_(\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def stage_set(run_id, stage, **kw):
    cur = db.one("SELECT id FROM lab_stages WHERE run_id=? AND stage=?", run_id, stage)
    if not cur:
        db.run("INSERT INTO lab_stages (run_id,stage,status) VALUES (?,?,'pending')", run_id, stage)
    sets, vals = [], []
    for k, v in kw.items():
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        db.run(f"UPDATE lab_stages SET {','.join(sets)} WHERE run_id=? AND stage=?",
               *vals, run_id, stage)


def stage_begin(run_id, stage, msg=""):
    stage_set(run_id, stage, status="running", started=time.time(), progress=0, message=msg)
    db.log(run_id, "started", stage, msg)


def stage_done(run_id, stage, msg="", meta=None):
    stage_set(run_id, stage, status="done", finished=time.time(), progress=100,
              message=msg, meta=db.jdump(meta or {}))
    db.log(run_id, "completed", stage, msg)


def stage_fail(run_id, stage, msg):
    stage_set(run_id, stage, status="error", finished=time.time(), message=msg[:400])
    db.log(run_id, "failed", stage, msg[:200])


def run_dir(run_id):
    d = WORK / f"run{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cfg(run_id, key, default=None):
    r = db.one("SELECT config FROM lab_runs WHERE id=?", run_id)
    return (db.jload(r["config"], {}) if r else {}).get(key, default)


# ─────────────────────────────── stages ───────────────────────────────
def probe(run_id):
    r = db.one("SELECT * FROM lab_runs WHERE id=?", run_id)
    src = Path(r["source_path"])
    stage_begin(run_id, "probe", f"probing {src.name}")
    info = ffprobe(src)
    if not info:
        raise RuntimeError(f"ffprobe failed on {src}")
    sys.path.append(str(APP))
    try:
        import quality
        q = quality.assess(str(src), samples=6)
    except Exception as e:                                   # quality is advisory
        q = {"grade": "?", "error": str(e)[:150]}
    info["quality"] = q
    stage_done(run_id, "probe",
               f"{info['width']}x{info['height']} {info['fps']}fps "
               f"{info['duration_s']/60:.1f}min {info['size_mb']}MB grade {q.get('grade','?')}",
               info)
    return info


def segment(run_id):
    """Cut into fixed-length parts with stream copy -- no re-encode, no quality loss."""
    r = db.one("SELECT * FROM lab_runs WHERE id=?", run_id)
    src = Path(r["source_path"])
    minutes = cfg(run_id, "segment_minutes", 15)
    stage_begin(run_id, "segment", f"cutting {minutes}-minute parts")
    out = run_dir(run_id) / "segments"
    out.mkdir(exist_ok=True)
    for old in out.glob("part*.mp4"):
        old.unlink()
    db.run("DELETE FROM lab_segments WHERE run_id=?", run_id)
    code, _, err = sh(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                       "-c", "copy", "-map", "0:v:0", "-f", "segment",
                       "-segment_time", str(minutes * 60), "-reset_timestamps", "1",
                       str(out / "part%02d.mp4")], timeout=3600)
    parts = sorted(out.glob("part*.mp4"))
    if code != 0 and not parts:
        raise RuntimeError(f"ffmpeg segment failed: {err[-300:]}")
    base_clock = clock_from_name(src.name)
    made = []
    for i, p in enumerate(parts):
        info = ffprobe(p) or {}
        start_s = sum(m["dur_s"] for m in made)
        db.run("""INSERT INTO lab_segments
              (run_id,idx,name,path,start_s,dur_s,size_mb,frames,fps,width,height,status)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,'ready')""",
               run_id, i, p.stem, str(p), start_s, info.get("duration_s", 0),
               info.get("size_mb", 0), info.get("frames", 0), info.get("fps", 0),
               info.get("width", 0), info.get("height", 0))
        made.append({"dur_s": info.get("duration_s", 0)})
        stage_set(run_id, "segment", progress=100.0 * (i + 1) / max(len(parts), 1))
    stage_done(run_id, "segment", f"{len(parts)} parts of ≤{minutes} min",
               {"parts": len(parts), "base_clock": str(base_clock) if base_clock else None})
    return len(parts)


def compress(run_id):
    """Storage/upload copy. The ORIGINAL segment stays the analysis input --
    we never measure traffic on a re-compressed file."""
    segs = db.rows("SELECT * FROM lab_segments WHERE run_id=? ORDER BY idx", run_id)
    if not segs:
        raise RuntimeError("no segments to compress")
    crf = cfg(run_id, "compress_crf", 30)
    maxw = cfg(run_id, "compress_width", 1280)
    ratio = cfg(run_id, "compress_target_ratio", 0.5)   # aim for half the source bitrate
    stage_begin(run_id, "compress", f"h264 crf{crf} ≤{maxw}px")
    saved, kept, skipped = 0.0, 0, 0
    for i, s in enumerate(segs):
        src_kbps = (s["size_mb"] or 0) * 8000 / max(s["dur_s"] or 1, 1)
        dst = Path(s["path"]).with_name(Path(s["path"]).stem + "_c.mp4")
        cap = max(180, int(src_kbps * ratio))           # never re-encode upward
        code, _, err = sh(["ffmpeg", "-y", "-v", "error", "-i", s["path"],
                           "-vf", f"scale='min({maxw},iw)':-2", "-c:v", "libx264",
                           "-preset", "veryfast", "-crf", str(crf),
                           "-maxrate", f"{cap}k", "-bufsize", f"{cap*2}k",
                           "-movflags", "+faststart", "-an", str(dst)], timeout=3600)
        if code == 0 and dst.exists():
            mb = round(dst.stat().st_size / 1e6, 1)
            if mb < (s["size_mb"] or 0) * 0.92:         # only keep a real win
                saved += (s["size_mb"] or 0) - mb
                kept += 1
                db.run("UPDATE lab_segments SET compressed_path=?, compressed_mb=? WHERE id=?",
                       str(dst), mb, s["id"])
            else:
                dst.unlink(missing_ok=True)             # source was already efficient
                skipped += 1
        stage_set(run_id, "compress", progress=100.0 * (i + 1) / len(segs),
                  message=f"part {i+1}/{len(segs)}")
    msg = (f"{kept} part(s) shrunk, saved {saved:.0f} MB" if kept else "no gain available")
    if skipped:
        msg += f" · {skipped} left as-is (source already efficient)"
    stage_done(run_id, "compress", msg,
               {"saved_mb": round(saved, 1), "kept": kept, "skipped": skipped})


def clip_name(site_code, start_clock, dur_s, idx=None):
    """The name a clip is known by, everywhere.

    `ch03_20260704202846_p0` told you the source file and an index and nothing else --
    every clip in a survey looked alike, and two stations' clips looked alike too. A
    survey is reported per station, per date, per time band, so the name carries exactly
    that: KDP-01_20260704_1300-1315. Sorts chronologically, unique by construction, and
    readable in a filename, a report cell and a spoken sentence.
    """
    code = (site_code or "CLIP").replace(" ", "")
    try:
        t0 = datetime.fromisoformat(start_clock)
    except (TypeError, ValueError):
        return f"{code}_part{idx:02d}" if idx is not None else f"{code}_clip"
    t1 = t0 + timedelta(seconds=float(dur_s or 0))
    return f"{code}_{t0.strftime('%Y%m%d')}_{t0.strftime('%H%M')}-{t1.strftime('%H%M')}"


def _register_video(seg, base_clock):
    """Give a segment a row in the counting app's videos table so the shared
    engine, counting and render code all work on it unchanged."""
    existing = db.one("SELECT id FROM videos WHERE path=?", seg["path"])
    if existing:
        return existing["id"]
    clock = "2026-01-01 00:00:00"
    if base_clock:
        clock = (base_clock + timedelta(seconds=seg["start_s"])).strftime("%Y-%m-%d %H:%M:%S")
    r = db.one("SELECT name, site_id FROM lab_runs WHERE id=?", seg["run_id"]) or {}
    code = (db.one("SELECT code FROM sites WHERE id=?", r.get("site_id")) or {}).get("code")
    # The run knows its station; the video must inherit it. Without this the video has no
    # station, so `stations.lines_for` finds no default count line and the clip reports
    # zero vehicles -- which looks like a counting failure rather than a missing link.
    return db.run("""INSERT INTO videos (path,name,fps,frames,width,height,start_clock,
                                         quality,created,site_id)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  seg["path"],
                  clip_name(code, clock if base_clock else None, seg["dur_s"], seg["idx"]),
                  seg["fps"], seg["frames"],
                  seg["width"], seg["height"], clock, "{}", time.time(), r.get("site_id"))


def extract(run_id):
    """Detector + tracker over the chosen segments -> trajectory store."""
    r = db.one("SELECT * FROM lab_runs WHERE id=?", run_id)
    which = cfg(run_id, "extract_segments", [0])
    imgsz, conf = cfg(run_id, "imgsz", 960), cfg(run_id, "conf", 0.12)
    model_id = cfg(run_id, "model_id", None)      # None = the registry's default
    segs = [s for s in db.rows("SELECT * FROM lab_segments WHERE run_id=? ORDER BY idx", run_id)
            if s["idx"] in which]
    if not segs:
        raise RuntimeError("no segments selected for extraction")
    stage_begin(run_id, "extract",
                f"{len(segs)} segment(s) @ {imgsz}px conf {conf}"
                + (f" · {model_id}" if model_id else ""))
    base_clock = clock_from_name(Path(r["source_path"]).name)
    total_tracks = 0
    for n, s in enumerate(segs):
        vid = _register_video(s, base_clock)
        db.run("UPDATE lab_segments SET video_id=? WHERE id=?", vid, s["id"])
        job = db.run("INSERT INTO jobs (video_id,kind,status,progress,started) "
                     "VALUES (?,'extract','queued',0,?)", vid, time.time())
        proc = subprocess.Popen([str(PY), str(Path(__file__).parent / "_extract_worker.py"),
                                 str(vid), str(job), str(imgsz), str(conf),
                                 model_id or "-"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while proc.poll() is None:                     # mirror engine progress into the Lab
            j = db.one("SELECT progress,message,status FROM jobs WHERE id=?", job)
            if j:
                span = 100.0 / len(segs)
                stage_set(run_id, "extract",
                          progress=round(n * span + (j["progress"] or 0) * span / 100, 1),
                          message=f"part {s['idx']}: {j.get('message') or 'starting'}")
            time.sleep(2)
        j = db.one("SELECT status,message FROM jobs WHERE id=?", job)
        if proc.returncode != 0 or (j and j["status"] == "error"):
            err = (proc.stderr.read().decode()[-300:] if proc.stderr else "") or (j or {}).get("message", "")
            raise RuntimeError(f"extraction failed on part {s['idx']}: {err}")
        n_tracks = db.one("SELECT COUNT(*) c FROM tracks WHERE video_id=?", vid)["c"]
        total_tracks += n_tracks
        db.run("UPDATE lab_segments SET status='extracted' WHERE id=?", s["id"])
        _axle_pass(vid)
    stage_done(run_id, "extract", f"{total_tracks} tracks from {len(segs)} part(s)",
               {"tracks": total_tracks, "videos": [s["id"] for s in segs]})


def _axle_pass(video_id):
    """Run the axle head over this clip's heavy vehicles, right after extraction.

    The detector calls 2-axle / 3-axle / MAV itself, and it is measurably bad at it: on
    KDP clip 1 it split 30 heavy vehicles 70/20/10 against a training prior of 75/12/13 --
    reproducing what it saw most often rather than reading the axles. The ResNet-18 head
    exists to correct exactly that, and leaving it as a manual pass meant every clip
    reached the report carrying the detector's raw guess in three separate proforma
    columns. It is a local classifier over a handful of crops -- seconds, no GPU booking --
    so there is no reason for it not to be part of extracting.

    Needs a count line, because it only judges vehicles that actually crossed. Without one
    it is skipped and picked up next time the clip is extracted or the pass is run by hand.
    Never fatal: a missing or unpromoted model must not fail an extraction that succeeded.
    """
    try:
        import axle_pass
        import sites
        if not sites.lines_for(video_id)[0]:
            return
        r = axle_pass.run(video_id)
        if r.get("error"):
            db.log(None, "skipped", f"axle pass on clip {video_id}", r["error"])
        elif r.get("checked"):
            db.log(None, "axles", f"clip {video_id}",
                   f"{r.get('checked')} heavy vehicle(s) classified, "
                   f"{r.get('applied', 0)} applied, {r.get('queued', 0)} left for review")
    except Exception as e:                      # advisory: never fail a good extraction
        db.log(None, "failed", f"axle pass on clip {video_id}", str(e)[:200])


def sample(run_id):
    """Cut one crop per track at its largest, clearest moment -- plus a context
    frame, because a 60px crop alone is not enough to classify a vehicle."""
    import cv2
    n_target = cfg(run_id, "sample_n", 300)
    min_h = cfg(run_id, "sample_min_h", 40)
    segs = db.rows("SELECT * FROM lab_segments WHERE run_id=? AND video_id IS NOT NULL", run_id)
    if not segs:
        raise RuntimeError("nothing extracted yet")
    stage_begin(run_id, "sample", f"selecting up to {n_target} crops")
    out = run_dir(run_id) / "crops"
    out.mkdir(exist_ok=True)
    db.run("DELETE FROM lab_crops WHERE run_id=?", run_id)

    picks = []
    for s in segs:
        rows = db.rows("""SELECT p.track_id, p.frame, p.x1,p.y1,p.x2,p.y2, p.conf,
                                 t.cls, (p.y2-p.y1) h
                          FROM track_points p JOIN tracks t
                            ON t.video_id=p.video_id AND t.track_id=p.track_id
                          WHERE p.video_id=? AND t.dup_of IS NULL AND (p.y2-p.y1) >= ?""",
                       s["video_id"], min_h)
        best = {}
        for r in rows:                                  # largest box wins per track
            k = r["track_id"]
            if k not in best or r["h"] > best[k]["h"]:
                best[k] = r
        for r in best.values():
            r["segment_id"], r["video_id"] = s["id"], s["video_id"]
            picks.append(r)

    by_cls = {}
    for p in picks:
        by_cls.setdefault(p["cls"], []).append(p)
    for v in by_cls.values():
        v.sort(key=lambda r: -r["h"])
    chosen, i = [], 0
    while len(chosen) < n_target and any(len(v) > i for v in by_cls.values()):
        for c in sorted(by_cls):                        # round-robin keeps rare classes in
            if len(by_cls[c]) > i and len(chosen) < n_target:
                chosen.append(by_cls[c][i])
        i += 1

    caps, made = {}, 0
    for p in chosen:
        vid = p["video_id"]
        if vid not in caps:
            path = db.one("SELECT path FROM videos WHERE id=?", vid)["path"]
            caps[vid] = cv2.VideoCapture(path)
        cap = caps[vid]
        cap.set(cv2.CAP_PROP_POS_FRAMES, p["frame"])
        ok, frame = cap.read()
        if not ok:
            continue
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = (max(0, int(p["x1"])), max(0, int(p["y1"])),
                          min(W, int(p["x2"])), min(H, int(p["y2"])))
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        pad = int(0.12 * max(x2 - x1, y2 - y1))
        crop = frame[max(0, y1 - pad):min(H, y2 + pad), max(0, x1 - pad):min(W, x2 + pad)]
        cpath = out / f"c{vid}_{p['track_id']}_{p['frame']}.jpg"
        cv2.imwrite(str(cpath), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        ctx = frame.copy()
        cv2.rectangle(ctx, (x1, y1), (x2, y2), (0, 230, 118), 3)
        scale = min(1.0, 900 / W)
        ctx = cv2.resize(ctx, (int(W * scale), int(H * scale)))
        xpath = out / f"x{vid}_{p['track_id']}_{p['frame']}.jpg"
        cv2.imwrite(str(xpath), ctx, [cv2.IMWRITE_JPEG_QUALITY, 78])
        db.run("""INSERT INTO lab_crops
              (run_id,segment_id,video_id,track_id,frame,x1,y1,x2,y2,det_class,det_conf,
               crop_path,ctx_path,state,created)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?)""",
               run_id, p["segment_id"], vid, p["track_id"], p["frame"],
               p["x1"], p["y1"], p["x2"], p["y2"], p["cls"], p["conf"],
               str(cpath), str(xpath), time.time())
        made += 1
        if made % 20 == 0:
            stage_set(run_id, "sample", progress=100.0 * made / max(len(chosen), 1),
                      message=f"{made} crops")
    for c in caps.values():
        c.release()
    # Gold frames are held out in dataset(), which is the stage that writes training
    # data -- sampling one only spends a judge call, it cannot leak into a model. The
    # gold-count log lives there for the same reason.
    dist = db.rows("""SELECT det_class, COUNT(*) n FROM lab_crops WHERE run_id=?
                      GROUP BY det_class ORDER BY n DESC""", run_id)
    stage_done(run_id, "sample", f"{made} crops across {len(dist)} classes",
               {"n": made, "dist": {CLASSES[d["det_class"]]: d["n"] for d in dist}})


def complete_frames(run_id):
    """Make every training frame fully verified.

    Sampling judges one track per frame, but a training image needs a trusted
    label on EVERY vehicle in it -- otherwise the un-judged boxes are just the
    detector grading its own homework, which reinforces the errors we are trying
    to fix. This adds a crop for each un-judged track sharing a frame with an
    already-judged one, so the next judge pass covers them.
    """
    import cv2
    stage_begin(run_id, "complete", "finding un-judged boxes in training frames")
    judged = {(c["video_id"], c["track_id"])
              for c in db.rows("SELECT video_id, track_id FROM lab_crops WHERE run_id=?", run_id)}
    frames = db.rows("""SELECT DISTINCT video_id, frame FROM lab_crops
                        WHERE run_id=? AND (final_class IS NOT NULL OR human_class IS NOT NULL)""",
                     run_id)
    if not frames:
        raise RuntimeError("nothing judged yet -- run the judges first")
    out = run_dir(run_id) / "crops"
    out.mkdir(parents=True, exist_ok=True)
    caps, made = {}, 0
    for i, f in enumerate(frames):
        vid, fr = f["video_id"], f["frame"]
        rows = db.rows("""SELECT p.track_id, p.x1,p.y1,p.x2,p.y2, p.conf, t.cls
                          FROM track_points p JOIN tracks t
                            ON t.video_id=p.video_id AND t.track_id=p.track_id
                          WHERE p.video_id=? AND p.frame=? AND t.dup_of IS NULL""", vid, fr)
        todo = [r for r in rows if (vid, r["track_id"]) not in judged]
        if not todo:
            continue
        if vid not in caps:
            caps[vid] = cv2.VideoCapture(db.one("SELECT path FROM videos WHERE id=?", vid)["path"])
        cap = caps[vid]
        cap.set(cv2.CAP_PROP_POS_FRAMES, fr)
        ok, frame = cap.read()
        if not ok:
            continue
        H, W = frame.shape[:2]
        for r in todo:
            x1, y1 = max(0, int(r["x1"])), max(0, int(r["y1"]))
            x2, y2 = min(W, int(r["x2"])), min(H, int(r["y2"]))
            if x2 - x1 < 10 or y2 - y1 < 10:
                continue
            pad = int(0.12 * max(x2 - x1, y2 - y1))
            crop = frame[max(0, y1 - pad):min(H, y2 + pad), max(0, x1 - pad):min(W, x2 + pad)]
            cp = out / f"n{vid}_{r['track_id']}_{fr}.jpg"
            cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
            ctx = frame.copy()
            cv2.rectangle(ctx, (x1, y1), (x2, y2), (0, 230, 118), 3)
            sc = min(1.0, 900 / W)
            xp = out / f"nx{vid}_{r['track_id']}_{fr}.jpg"
            cv2.imwrite(str(xp), cv2.resize(ctx, (int(W * sc), int(H * sc))),
                        [cv2.IMWRITE_JPEG_QUALITY, 78])
            db.run("""INSERT INTO lab_crops
                  (run_id,segment_id,video_id,track_id,frame,x1,y1,x2,y2,det_class,det_conf,
                   crop_path,ctx_path,state,created)
                  VALUES (?,NULL,?,?,?,?,?,?,?,?,?,?,?,'new',?)""",
                   run_id, vid, r["track_id"], fr, r["x1"], r["y1"], r["x2"], r["y2"],
                   r["cls"], r["conf"], str(cp), str(xp), time.time())
            judged.add((vid, r["track_id"]))
            made += 1
        stage_set(run_id, "complete", progress=100.0 * (i + 1) / len(frames),
                  message=f"{made} un-judged boxes queued")
    for c in caps.values():
        c.release()
    # See sample(): the gold hold-out is enforced in dataset(), not here.
    stage_done(run_id, "complete", f"{made} extra crops queued for judging",
               {"added": made, "frames": len(frames)})


def _val_clip(video_id):
    """Split train/val by CLIP, never by frame.

    Neighbouring frames of one clip are near-identical, so splitting by frame index puts
    almost the same picture in both halves and the validation score measures memorisation.
    That mistake already invalidated one v3-vs-v4 comparison here. Hashing the video id
    keeps the choice stable across rebuilds, so a dataset does not reshuffle itself.
    """
    return hashlib.md5(str(video_id).encode()).digest()[0] % 5 == 0    # ~20% of clips


def dataset(run_id):
    """Materialise a YOLO dataset from the judged + human-confirmed crops."""
    import cv2
    stage_begin(run_id, "dataset", "building YOLO dataset")
    crops = db.rows("""SELECT * FROM lab_crops WHERE run_id=?
                       AND (human_class IS NOT NULL OR final_class IS NOT NULL)""", run_id)
    if not crops:
        raise RuntimeError("no confirmed labels yet -- run judge/review first")
    out = run_dir(run_id) / "dataset"
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    # A training image must carry EVERY vehicle in it. If only the sampled box is
    # written, the others become background and teach the model to miss them --
    # so each chosen frame is re-read in full and its judged classes applied.
    confirmed = {}
    for c in crops:
        cls = c["human_class"] if c["human_class"] is not None else c["final_class"]
        confirmed[(c["video_id"], c["track_id"])] = cls
    by_frame = {}
    for c in crops:
        by_frame.setdefault((c["video_id"], c["frame"]), [])
    for (vid, frame) in list(by_frame):
        by_frame[(vid, frame)] = db.rows(
            """SELECT p.track_id, p.x1, p.y1, p.x2, p.y2, t.cls, t.class_override
               FROM track_points p JOIN tracks t
                 ON t.video_id=p.video_id AND t.track_id=p.track_id
               WHERE p.video_id=? AND p.frame=? AND t.dup_of IS NULL""", vid, frame)
    # Gold frames are the measuring stick. Training on them turns every later score
    # into a memorisation test, so they are excluded here rather than by convention --
    # a note in a README cannot stop a rebuild six months from now.
    import goldset
    frozen = goldset.frozen_video_frames()
    skipped_gold = 0

    caps, n_tr, n_va = {}, 0, 0
    for i, ((vid, frame), items) in enumerate(sorted(by_frame.items())):
        if (vid, frame) in frozen:
            skipped_gold += 1
            continue
        if vid not in caps:
            caps[vid] = cv2.VideoCapture(db.one("SELECT path FROM videos WHERE id=?", vid)["path"])
        cap = caps[vid]
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, img = cap.read()
        if not ok:
            continue
        H, W = img.shape[:2]
        split = "val" if _val_clip(vid) else "train"
        stem = f"v{vid}_f{frame}"
        cv2.imwrite(str(out / f"images/{split}/{stem}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        lines = []
        for c in items:
            key = (vid, c["track_id"])
            cls = confirmed.get(key, c["class_override"] if c["class_override"] is not None
                                else c["cls"])
            if cls is None or cls < 0:          # judged not-a-vehicle -> no box
                continue
            cx, cy = (c["x1"] + c["x2"]) / 2 / W, (c["y1"] + c["y2"]) / 2 / H
            bw, bh = (c["x2"] - c["x1"]) / W, (c["y2"] - c["y1"]) / H
            lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        if not lines:
            continue                            # never write an empty label file
        (out / f"labels/{split}/{stem}.txt").write_text("\n".join(lines))
        n_va += split == "val"
        n_tr += split == "train"
        stage_set(run_id, "dataset", progress=100.0 * (i + 1) / len(by_frame))
    for c in caps.values():
        c.release()
    if skipped_gold:
        db.log(run_id, "excluded", f"{skipped_gold} gold frame(s) held out",
               "frames reserved for evaluation are never trained on")
    (out / "data.yaml").write_text(
        f"path: {out}\ntrain: images/train\nval: images/val\n"
        f"nc: {len(CLASSES)}\nnames: {json.dumps(CLASSES)}\n")
    stage_done(run_id, "dataset", f"{n_tr} train / {n_va} val frames",
               {"train": n_tr, "val": n_va, "path": str(out)})


def _existence(run_id):
    # Imported here, not at module scope: existence imports back into pipeline for the
    # stage helpers, and a top-level import would make that circular.
    import existence
    existence.run_gate(run_id)


ORDER = {"probe": probe, "segment": segment, "compress": compress,
         "extract": extract, "sample": sample, "existence": _existence,
         "complete": complete_frames, "dataset": dataset}


def start(run_id, stages):  # noqa: D401 - ORDER must exist before this is called
    """Run the named stages in pipeline order on a background worker."""
    if _workers.get(run_id) and _workers[run_id].is_alive():
        return False, "this run is already working"
    # judge/review/train/eval are driven by their own endpoints, not this worker
    todo = [s for s in STAGES if s in stages and s in ORDER]
    if not todo:
        return False, "no runnable stage in that request"

    def work():
        db.run("UPDATE lab_runs SET status='running' WHERE id=?", run_id)
        for s in todo:
            fn = ORDER.get(s)
            if not fn:
                continue
            try:
                fn(run_id)
            except Exception as e:
                stage_fail(run_id, s, str(e))
                db.run("UPDATE lab_runs SET status='error' WHERE id=?", run_id)
                return
        db.run("UPDATE lab_runs SET status='done', finished=? WHERE id=?", time.time(), run_id)

    for s in todo:
        stage_set(run_id, s, status="queued", progress=0, message="waiting")
    t = threading.Thread(target=work, daemon=True)
    _workers[run_id] = t
    t.start()
    return True, f"{len(todo)} stage(s) queued"
