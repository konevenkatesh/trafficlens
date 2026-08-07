"""The gold set: frames labelled exhaustively by a human, frozen, never trained on.

Everything else in the Lab measures the model against crops the model itself proposed.
That can never reveal a vehicle it missed -- and a count is exactly as wrong as the
vehicles it missed. So the one artifact that can honestly score a model is a set of
frames where a person has marked EVERY vehicle, including the ones no model found.

Two rules make it worth the hours it costs:

**Frozen and excluded from training.** A gold frame that leaks into a training set stops
being a measurement and becomes a memorisation test. `frozen` is checked by the dataset
builder, not merely documented.

**Anchoring is fought deliberately.** If the reviewer is shown the model's boxes first,
they will confirm them and stop looking; the misses -- the whole point -- go unseen. So
a frame opens clean, the reviewer sweeps it themselves, and only then reveals what the
model proposed. The extra ten seconds per frame is what makes the recall number real.
"""
import json
import time
from pathlib import Path

import db
from pipeline import CLASSES, run_dir

GOLD_DIR = Path(__file__).parent.parent / "lab_gold"
IOU_MATCH = 0.5            # standard detection-matching threshold
MIN_FRAMES_FOR_VERDICT = 10   # below this the recall estimate is too noisy to act on

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_gold_frames (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER, video_id INTEGER, footage_path TEXT,
  frame INTEGER, clock TEXT, band TEXT,
  image_path TEXT, width INTEGER, height INTEGER,
  status TEXT DEFAULT 'pending', seed_model TEXT, seeded_n INTEGER,
  revealed INTEGER DEFAULT 0, seconds REAL,
  reviewed REAL, created REAL, note TEXT);

CREATE TABLE IF NOT EXISTS lab_gold_boxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  frame_id INTEGER, x1 REAL, y1 REAL, x2 REAL, y2 REAL,
  cls INTEGER, source TEXT, verdict TEXT,
  track_id INTEGER, conf REAL, created REAL);

CREATE INDEX IF NOT EXISTS ix_gold_site ON lab_gold_frames(site_id, status);
CREATE INDEX IF NOT EXISTS ix_goldbox_frame ON lab_gold_boxes(frame_id);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ───────────────────────────── building the set ─────────────────────────────
def _band(hour):
    for name, lo, hi in (("night", 0, 6), ("morning", 6, 11), ("midday", 11, 16),
                         ("evening", 16, 20), ("late", 20, 24)):
        if lo <= hour < hi:
            return name
    return "night"


def build(site_id, n_frames=60, seed_model=None, stride_s=37):
    """Cut frames for review, spread across time of day and across the footage.

    `stride_s` is deliberately not a round number: sampling every 30 or 60 seconds tends
    to land on the same phase of a signal cycle over and over, so the set fills up with
    the same queue at the same red light. A prime-ish stride walks through the cycle.
    """
    import cv2
    init()
    vids = db.rows("""SELECT f.path, f.start_clock, f.dur_s, v.id video_id
                      FROM lab_footage f LEFT JOIN videos v ON v.path = f.path
                      WHERE f.site_id=? AND f.dup_of IS NULL AND f.dur_s > 0
                      ORDER BY f.start_clock""", site_id)
    if not vids:
        raise RuntimeError("this station has no usable footage")
    from datetime import datetime, timedelta

    # Round-robin across time-of-day bands so a 24h delivery does not produce a gold set
    # that is 80% daylight.
    bands = {}
    for v in vids:
        try:
            h = datetime.fromisoformat(v["start_clock"]).hour
        except (TypeError, ValueError):
            continue
        bands.setdefault(_band(h), []).append(v)
    order = [b for b in ("morning", "midday", "evening", "late", "night") if b in bands]
    if not order:
        raise RuntimeError("no footage with a readable start clock")

    # Gold frames live with their station, like everything else it owns.
    import organise
    out_dir = organise.station_dir(site_id, "gold") or (GOLD_DIR / str(site_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    seeder = None
    try:
        from ultralytics import YOLO
        weights, seed_model = resolve_model(seed_model)
        seeder = YOLO(str(weights))
    except Exception:
        seed_model = None          # no model available: reviewer draws from scratch
    made, cursor = 0, {b: 0 for b in order}
    existing = {(r["footage_path"], r["frame"])
                for r in db.rows("SELECT footage_path, frame FROM lab_gold_frames WHERE site_id=?",
                                 site_id)}
    i = 0
    while made < n_frames and any(cursor[b] < len(bands[b]) * 400 for b in order):
        b = order[i % len(order)]
        i += 1
        pool = bands[b]
        if not pool:
            continue
        v = pool[(cursor[b] // 8) % len(pool)]
        offset = stride_s * (cursor[b] + 1)
        cursor[b] += 1
        if offset >= (v["dur_s"] or 0):
            continue
        cap = cv2.VideoCapture(v["path"])
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        fno = int(offset * fps)
        if (v["path"], fno) in existing:
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, fno)
        ok, img = cap.read()
        cap.release()
        if not ok or img is None:
            continue
        H, W = img.shape[:2]
        stem = f"{Path(v['path']).stem}_{fno}"
        ip = out_dir / f"{stem}.jpg"
        cv2.imwrite(str(ip), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        try:
            clk = (datetime.fromisoformat(v["start_clock"])
                   + timedelta(seconds=offset)).isoformat(" ")
        except (TypeError, ValueError):
            clk = None
        fid = db.run("""INSERT INTO lab_gold_frames
                        (site_id,video_id,footage_path,frame,clock,band,image_path,
                         width,height,status,seed_model,created)
                        VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                     site_id, v["video_id"], v["path"], fno, clk, b, str(ip),
                     W, H, seed_model, time.time())
        n_seed = _seed_boxes(fid, str(ip), seeder)
        db.run("UPDATE lab_gold_frames SET seeded_n=? WHERE id=?", n_seed, fid)
        made += 1
    return {"frames": made, "site_id": site_id}


def build_night(site_id, n_frames=60, seed_model=None, stride_s=37,
                dark_from=19, dark_to=6, luma_max=105, act_min=2.0, empty_share=0.1):
    """A gold set restricted to darkness — the regime where the detector fails differently.

    `build()` bands by each FILE's start hour, which puts a 20:28 file in 'late' and lets
    an 18:23 file that runs well past sunset count as 'evening'. Night sampling instead
    computes the clock AT EACH OFFSET and keeps only offsets inside the dark window, so a
    dusk-spanning file contributes exactly its dark portion. Every frame is tagged
    band='night' regardless of the 5-band day taxonomy: this set is a separate focus,
    filterable in scoring, and re-running the call after a new night delivery extends it
    (existing (path, frame) pairs are never re-cut).
    """
    import cv2
    from datetime import datetime, timedelta
    init()
    vids = db.rows("""SELECT f.path, f.start_clock, f.dur_s, v.id video_id
                      FROM lab_footage f LEFT JOIN videos v ON v.path = f.path
                      WHERE f.site_id=? AND f.dup_of IS NULL AND f.missing=0
                        AND f.dur_s > 0 ORDER BY f.start_clock""", site_id)
    dark = lambda h: h >= dark_from or h < dark_to
    pools = []
    for v in vids:
        try:
            t0 = datetime.fromisoformat(v["start_clock"])
        except (TypeError, ValueError):
            continue
        offs = [o for o in range(0, int(v["dur_s"]), stride_s)
                if dark((t0 + timedelta(seconds=o)).hour)]
        # Bit-reversal order: the first k picks are spread across the WHOLE dark span for
        # any k, instead of walking the window front-to-back and clustering at its start.
        if offs:
            bits = max(1, (len(offs) - 1).bit_length())
            rev = sorted(range(len(offs)),
                         key=lambda j: int(format(j, f"0{bits}b")[::-1], 2))
            offs = [offs[j] for j in rev]
            pools.append({**v, "t0": t0, "offsets": offs, "cursor": 0})
    if not pools:
        raise RuntimeError("no footage covering the dark hours at this station")

    import organise
    out_dir = organise.station_dir(site_id, "gold") or (GOLD_DIR / str(site_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    seeder = None
    try:
        from ultralytics import YOLO
        weights, seed_model = resolve_model(seed_model)
        seeder = YOLO(str(weights))
    except Exception:
        seed_model = None
    existing = {(r["footage_path"], r["frame"])
                for r in db.rows("SELECT footage_path, frame FROM lab_gold_frames WHERE site_id=?",
                                 site_id)}
    made, i, empties_kept = 0, 0, 0
    # Round-robin across files so one long night file does not become the whole set.
    while made < n_frames and any(p["cursor"] < len(p["offsets"]) for p in pools):
        p = pools[i % len(pools)]
        i += 1
        if p["cursor"] >= len(p["offsets"]):
            continue
        offset = p["offsets"][p["cursor"]]
        p["cursor"] += 1
        cap = cv2.VideoCapture(p["path"])
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        fno = int(offset * fps)
        if (p["path"], fno) in existing:
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, fno)
        ok, img = cap.read()
        if not ok or img is None:
            cap.release()
            continue
        # The clock window is only a prefilter — measured darkness decides. KDP July:
        # 19:00 is still full daylight, dark by 19:15; a clock rule alone rots seasonally.
        # Glare-flooded night frames sit ≈100 luma, dusk starts ≈105+, day 130+.
        small = cv2.cvtColor(cv2.resize(img, (320, 180)), cv2.COLOR_BGR2GRAY)
        if small.mean() > luma_max:
            cap.release()
            continue
        # Motion gate: sparse night traffic makes uniform sampling ~90% empty road
        # (measured: the first KDP round gave 9 occupied frames in 100). Frame-diff vs
        # +0.7s is detector-independent — a missed 2W's headlight still moves — so the
        # miss measurement stays honest. Calibrated on KDP: empty ≈0.3%, vehicles ≈8%.
        # A small quota of empty frames stays in as false-positive controls.
        cap.set(cv2.CAP_PROP_POS_FRAMES, fno + max(1, int(fps * 0.7)))
        ok2, img2 = cap.read()
        cap.release()
        act = None
        if ok2 and img2 is not None:
            s2 = cv2.cvtColor(cv2.resize(img2, (320, 180)), cv2.COLOR_BGR2GRAY)
            import numpy as _np
            act = float((_np.abs(s2.astype(_np.int16) - small.astype(_np.int16)) > 25).mean() * 100)
        if act is not None and act < act_min:
            if empties_kept >= int(n_frames * empty_share):
                continue
            empties_kept += 1
        H, W = img.shape[:2]
        ip = out_dir / f"{Path(p['path']).stem}_{fno}.jpg"
        cv2.imwrite(str(ip), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        clk = (p["t0"] + timedelta(seconds=offset)).isoformat(" ")
        fid = db.run("""INSERT INTO lab_gold_frames
                        (site_id,video_id,footage_path,frame,clock,band,image_path,
                         width,height,status,seed_model,created)
                        VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                     site_id, p["video_id"], p["path"], fno, clk, "night", str(ip),
                     W, H, seed_model, time.time())
        n_seed = _seed_boxes(fid, str(ip), seeder)
        db.run("UPDATE lab_gold_frames SET seeded_n=? WHERE id=?", n_seed, fid)
        made += 1
    return {"frames": made, "site_id": site_id, "band": "night",
            "files_used": [Path(p["path"]).name for p in pools if p["cursor"]]}


def _seed_boxes(frame_id, image_path, model, conf=0.12, imgsz=960):
    """Pre-load the model's own boxes -- hidden until the reviewer reveals them.

    Inference is run on the frame rather than read from stored detections: gold frames
    are cut from the original footage while extraction runs on segments, so the frame
    numbers never line up. Seeding matters for the time budget -- confirming boxes is far
    faster than drawing them -- and it stays honest because the reviewer sweeps the frame
    before anything is revealed.
    """
    if model is None:
        return 0
    try:
        res = model.predict(image_path, imgsz=imgsz, conf=conf, verbose=False)[0]
    except Exception:
        return 0
    n = 0
    for b, c, p in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(),
                       res.boxes.conf.tolist()):
        db.run("""INSERT INTO lab_gold_boxes
                  (frame_id,x1,y1,x2,y2,cls,source,verdict,conf,created)
                  VALUES (?,?,?,?,?,?,'model',NULL,?,?)""",
               frame_id, float(b[0]), float(b[1]), float(b[2]), float(b[3]),
               int(c), float(p), time.time())
        n += 1
    return n


# ───────────────────────────── review ─────────────────────────────
def next_frame(site_id):
    init()
    f = db.one("""SELECT * FROM lab_gold_frames WHERE site_id=? AND status='pending'
                  ORDER BY id LIMIT 1""", site_id)
    # `finished`, not `done`: stats() already carries a `done` COUNT, and spreading it
    # over a `done` boolean made the very first saved frame report the set complete.
    if not f:
        return {"finished": True, **stats(site_id)}
    f["boxes"] = db.rows("SELECT * FROM lab_gold_boxes WHERE frame_id=? ORDER BY id", f["id"])
    return {"finished": False, "frame": f, "classes": CLASSES, **stats(site_id)}


def save_frame(frame_id, boxes, seconds=None, revealed=None):
    """Replace a frame's boxes with what the reviewer confirmed.

    Whole-frame replace rather than per-box edits: the claim being recorded is
    "these are ALL the vehicles in this frame", and that is only true if the set is
    written as a set.
    """
    init()
    db.run("DELETE FROM lab_gold_boxes WHERE frame_id=?", frame_id)
    for b in boxes:
        cls = b.get("cls")
        if cls is None or int(cls) < 0:
            continue                                   # deleted / not a vehicle
        db.run("""INSERT INTO lab_gold_boxes
                  (frame_id,x1,y1,x2,y2,cls,source,verdict,track_id,conf,created)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               frame_id, b["x1"], b["y1"], b["x2"], b["y2"], int(cls),
               b.get("source", "human"), b.get("verdict", "confirmed"),
               b.get("track_id"), b.get("conf"), time.time())
    db.run("""UPDATE lab_gold_frames SET status='done', reviewed=?, seconds=?, revealed=?
              WHERE id=?""", time.time(), seconds, 1 if revealed else 0, frame_id)
    return {"ok": True, "boxes": len(boxes)}


def validation(site_id):
    """Has a human actually touched this gold set, or just confirmed the model?

    A gold set built by revealing the model's boxes and pressing save is not ground
    truth -- it is the model's own output wearing a different hat, and scoring against it
    returns 100% by construction. That number is worse than no number, because it looks
    like evidence. So the edit count is tracked and any model-level conclusion is
    withheld until a human has demonstrably changed something.
    """
    init()
    f = db.one("""SELECT COUNT(*) done, COALESCE(SUM(seeded_n),0) seeded,
                         SUM(revealed) revealed
                  FROM lab_gold_frames WHERE site_id=? AND status='done'""", site_id) or {}
    b = db.one("""SELECT COUNT(*) kept, SUM(gb.source='human') added
                  FROM lab_gold_boxes gb JOIN lab_gold_frames gf ON gf.id=gb.frame_id
                  WHERE gf.site_id=? AND gf.status='done'""", site_id) or {}
    done = f.get("done") or 0
    seeded = f.get("seeded") or 0
    kept = b.get("kept") or 0
    added = b.get("added") or 0
    removed = max(0, seeded - (kept - added))
    edits = added + removed
    circular = edits == 0                  # nothing was ever changed
    thin = done < MIN_FRAMES_FOR_VERDICT    # too few frames to mean much
    if circular:
        note = ("Every box came from the model and none was changed, so scoring against "
                "this set is circular — it reads 100% whatever the model does. Sweep "
                "some frames properly before trusting any model-level number.")
    elif thin:
        note = (f"Only {done} frame(s) reviewed. The numbers are real but noisy; "
                f"{MIN_FRAMES_FOR_VERDICT}+ frames make a model-level conclusion safe.")
    else:
        note = (f"{done} frames reviewed with {edits} human correction(s) — scores "
                f"against this set are meaningful.")
    return {"frames_reviewed": done, "seeded_boxes": seeded, "kept_boxes": kept,
            "added": added, "removed": removed, "edits": edits,
            "circular": circular, "thin": thin,
            "trustworthy": not circular and not thin, "note": note}


def stats(site_id):
    init()
    s = db.one("""SELECT COUNT(*) total, SUM(status='done') done,
                         ROUND(AVG(CASE WHEN status='done' THEN seconds END),1) avg_s
                  FROM lab_gold_frames WHERE site_id=?""", site_id) or {}
    b = db.one("""SELECT COUNT(*) boxes, SUM(source='human') added
                  FROM lab_gold_boxes gb JOIN lab_gold_frames gf ON gf.id=gb.frame_id
                  WHERE gf.site_id=? AND gf.status='done'""", site_id) or {}
    done = s.get("done") or 0
    est = None
    if s.get("avg_s") and (s.get("total") or 0) > done:
        est = round(s["avg_s"] * (s["total"] - done) / 60, 1)
    return {"total": s.get("total") or 0, "done": done,
            "avg_seconds": s.get("avg_s"), "minutes_left": est,
            "boxes": b.get("boxes") or 0, "human_added": b.get("added") or 0}


# ───────────────────────────── scoring ─────────────────────────────
def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def resolve_model(model_id=None):
    """model id -> weight file. Falls back to whatever the app currently uses."""
    if model_id and Path(model_id).is_file():
        return model_id, Path(model_id).stem
    row = (db.one("SELECT id,file FROM models WHERE id=?", model_id) if model_id
           else db.one("SELECT id,file FROM models WHERE is_default=1"))
    if not row:
        row = db.one("SELECT id,file FROM models ORDER BY map50 DESC LIMIT 1")
    if not row:
        raise RuntimeError("no model available to score with")
    return row["file"], row["id"]


def score(site_id, model_id=None, imgsz=960, conf=0.12, band=None):
    """Run a model over the gold frames and score it, recall included.

    Inference is run here rather than read from stored detections. Gold frames are cut
    from the ORIGINAL footage while extraction runs on segments, so frame numbers never
    line up -- and more importantly, the models worth comparing (a station fine-tune
    against the global model) have usually never been run on this footage at all. Running
    the weights against the frames makes any model scorable against any station.

    Three outcomes are counted apart because they mean different things to a count:
    a MISS loses a vehicle for good, a FALSE POSITIVE invents one, and a WRONG CLASS
    keeps the total right while moving the vehicle into the wrong proforma column.
    """
    from ultralytics import YOLO
    init()
    frames = db.rows("""SELECT * FROM lab_gold_frames
                        WHERE site_id=? AND status='done'""" +
                     (" AND band=?" if band else ""),
                     *([site_id, band] if band else [site_id]))
    if not frames:
        return {"error": "no reviewed gold frames for this station yet"
                         + (f" in band '{band}'" if band else "")}
    weights, mid = resolve_model(model_id)
    model = YOLO(str(weights))

    tp = fp = fn = wrong_cls = 0
    per_class = {}
    for f in frames:
        gold = db.rows("SELECT * FROM lab_gold_boxes WHERE frame_id=?", f["id"])
        if not Path(f["image_path"]).is_file():
            continue
        res = model.predict(f["image_path"], imgsz=imgsz, conf=conf, verbose=False)[0]
        pred = [{"x1": float(b[0]), "y1": float(b[1]), "x2": float(b[2]), "y2": float(b[3]),
                 "cls": int(c)}
                for b, c in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist())]
        used = set()
        for g in gold:
            gb = (g["x1"], g["y1"], g["x2"], g["y2"])
            best, bi = 0.0, None
            for i, p in enumerate(pred):
                if i in used:
                    continue
                v = _iou(gb, (p["x1"], p["y1"], p["x2"], p["y2"]))
                if v > best:
                    best, bi = v, i
            c = per_class.setdefault(g["cls"], {"gold": 0, "tp": 0, "miss": 0, "wrong": 0})
            c["gold"] += 1
            if best >= IOU_MATCH:
                used.add(bi)
                if pred[bi]["cls"] == g["cls"]:
                    tp += 1
                    c["tp"] += 1
                else:
                    wrong_cls += 1
                    c["wrong"] += 1
            else:
                fn += 1
                c["miss"] += 1
        fp += len(pred) - len(used)

    found = tp + wrong_cls                      # located, whatever class was said
    n_gold = tp + wrong_cls + fn
    out = {
        "model_id": mid, "weights": str(weights), "band": band,
        "frames": len(frames), "gold_boxes": n_gold,
        "located": found, "missed": fn, "false_positives": fp, "wrong_class": wrong_cls,
        "recall": round(found / n_gold, 4) if n_gold else None,
        "precision": round(found / (found + fp), 4) if (found + fp) else None,
        "class_accuracy": round(tp / found, 4) if found else None,
        "count_error_pct": round(100 * ((found + fp) - n_gold) / n_gold, 2) if n_gold else None,
        "per_class": {(CLASSES[k] if 0 <= k < len(CLASSES) else str(k)): v
                      for k, v in sorted(per_class.items())},
    }
    db.run("""INSERT INTO lab_evals (run_id,kind,model,n,accuracy,detail,created)
              VALUES (0,'goldset',?,?,?,?,?)""",
           mid, n_gold, out["recall"], db.jdump(out), time.time())
    return out


def frozen_video_frames(window_s=1.5):
    """(video_id, frame) pairs the dataset builder must refuse to train on.

    Gold frames are cut from the ORIGINAL footage, while training frames come from the
    segments that footage was cut into -- so a gold frame and a training frame can be the
    same picture under two different numbers. The mapping is done through wall-clock
    position: a gold frame at t seconds into the source file is the same moment as the
    frame at (t - segment.start_s) inside the segment that covers it.

    A window is used rather than an exact frame because neighbouring frames are visually
    identical; training on the frame either side of a gold frame leaks just as badly.
    """
    init()
    out = set()
    segs = db.rows("""SELECT s.video_id, s.start_s, s.dur_s, s.fps, r.source_path
                      FROM lab_segments s JOIN lab_runs r ON r.id = s.run_id
                      WHERE s.video_id IS NOT NULL""")
    golds = db.rows("""SELECT g.footage_path, g.frame, f.fps
                       FROM lab_gold_frames g
                       LEFT JOIN lab_footage f ON f.path = g.footage_path""")
    for g in golds:
        fps = g["fps"] or 25.0
        t = g["frame"] / fps
        for s in segs:
            if s["source_path"] != g["footage_path"]:
                continue
            start = s["start_s"] or 0
            if not (start <= t < start + (s["dur_s"] or 0)):
                continue
            sfps = s["fps"] or fps
            centre = int((t - start) * sfps)
            span = max(1, int(window_s * sfps))
            for d in range(-span, span + 1):
                out.add((s["video_id"], centre + d))
    return out
