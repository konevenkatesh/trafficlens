"""The frozen evaluation clip: the one yardstick no model is ever allowed to train on.

Every number this project has produced so far carries an asterisk, because every verified
clip contributed its corrections to v5's training set — so v5 was partly scored on
vehicles it had been taught. This module ends that for good.

Clip 9 (video 9, FID-33 10:27–10:42) is frozen: 258 human verdicts, 24 heavy vehicles,
14 buses, and only 6 of its corrections ever reached a training set — the least
contaminated rich clip available. From now on `correction_set.build` refuses to include
its corrections, so from v6 onward its score is a clean, comparable number.

Scoring method matches the promotion test: for each human-verdict vehicle, run the model
on its clearest frame and take the class of the best-overlapping detection (IoU ≥ 0.45).
Never re-extract the clip — new track ids would orphan the verdicts.
"""
import sys, time
from collections import Counter
from pathlib import Path
sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent / "app")]
import db
from engine import CLASSES

EVAL_VIDEO_ID = 9          # enforced by correction_set.build; change only with a re-freeze

SCHEMA = """CREATE TABLE IF NOT EXISTS eval_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model TEXT, weights TEXT, video_id INTEGER,
  n INTEGER, correct INTEGER, missed INTEGER, accuracy REAL,
  confusions TEXT, created REAL);"""


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - i
    return i / u if u else 0


def score(weights, tag=None, video_id=EVAL_VIDEO_ID):
    """One model's accuracy on the frozen clip's human verdicts. Recorded, always."""
    import cv2
    from ultralytics import YOLO
    db.conn().executescript(SCHEMA); db.conn().commit()
    vp = db.one("SELECT path FROM videos WHERE id=?", video_id)["path"]
    gt = []
    for r in db.rows("""SELECT track_id, answer FROM clip_verdicts
                        WHERE video_id=? AND kind='class'""", video_id):
        b = db.one("""SELECT frame,x1,y1,x2,y2,(x2-x1) w FROM track_points
                      WHERE video_id=? AND track_id=? ORDER BY w DESC LIMIT 1""",
                   video_id, r["track_id"])
        if b and b["w"] >= 60:
            gt.append((b["frame"], (b["x1"], b["y1"], b["x2"], b["y2"]), r["answer"]))
    m = YOLO(weights)
    cap = cv2.VideoCapture(vp)
    ok_n = miss = 0
    pairs = Counter()
    for frame, box, truth in gt:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, img = cap.read()
        if not ok:
            continue
        det = m.predict(img, imgsz=960, conf=0.12, verbose=False)[0]
        best, bc = 0.45, None
        for bx, cl in zip(det.boxes.xyxy.tolist(), det.boxes.cls.tolist()):
            i = _iou(box, bx)
            if i > best:
                best, bc = i, int(cl)
        if bc is None:
            miss += 1; pairs[(truth, "(missed)")] += 1
        elif CLASSES[bc] == truth:
            ok_n += 1
        else:
            pairs[(truth, CLASSES[bc])] += 1
    cap.release()
    n = len(gt)
    acc = ok_n / n if n else 0
    db.run("""INSERT INTO eval_scores (model,weights,video_id,n,correct,missed,accuracy,
              confusions,created) VALUES (?,?,?,?,?,?,?,?,?)""",
           tag or Path(weights).stem, str(weights), video_id, n, ok_n, miss, acc,
           db.jdump([[a, b, c] for (a, b), c in pairs.most_common(8)]), time.time())
    return {"model": tag or Path(weights).stem, "n": n, "correct": ok_n,
            "missed": miss, "accuracy": round(acc, 4),
            "top_confusions": pairs.most_common(5)}
