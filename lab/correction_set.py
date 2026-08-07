"""Turn verification corrections into training examples, merged into the existing dataset.

108 corrections is not a training set — it is a patch. Fitting a 15-class detector to 108
images would catastrophically forget everything the 8,111-image set taught it, and produce
a model that is excellent at the eleven classes in the patch and useless at the rest. So
these are added to the existing data, not substituted for it.

**A corrected frame must be labelled completely.** A frame contains many vehicles;
verification corrected one of them. Writing a label file with only that box tells the
trainer every other vehicle in the frame is background — which teaches it to miss exactly
what it currently detects. Every tracked vehicle present in the frame is written, with the
corrected class applied to the one that was fixed and each other box carrying whatever the
detector-plus-verification currently says.

**Only frames where the box is big enough to learn from.** A 60px vehicle contributes a
handful of pixels and a lot of label noise.
"""
import shutil
import sys
from collections import Counter
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
from engine import CLASSES  # noqa: E402

MIN_BOX = 70


def build(site_id, base="round4", out_name=None, min_box=MIN_BOX):
    """Copy `base`, add a labelled frame for every correction, register the result."""
    import artifacts
    import cv2
    import verify

    base_dir = ROOT / "dataset" / base
    if not base_dir.is_dir():
        return {"error": f"no base dataset at {base_dir}"}
    out_name = out_name or f"{base}_verified"
    out = ROOT / "dataset" / out_name

    wrong = verify.wrong_calls(site_id=site_id)
    # The frozen eval clip's corrections NEVER enter a training set — that exclusion is
    # what makes its score a clean number instead of a model grading its own homework.
    from eval_clip import EVAL_VIDEO_ID
    held_out = [w for w in wrong if w["video_id"] == EVAL_VIDEO_ID]
    wrong = [w for w in wrong if w["video_id"] != EVAL_VIDEO_ID]
    if held_out:
        print(f"eval freeze: excluded {len(held_out)} correction(s) from clip {EVAL_VIDEO_ID}")
    if not wrong:
        return {"error": "no corrections to add"}

    # Copy the base once. Cheap in wall-clock next to a training run, and it keeps the
    # original immutable so a comparison between the two is always possible.
    if not out.exists():
        shutil.copytree(base_dir, out)
    img_dir = out / "images" / "train"
    lbl_dir = out / "labels" / "train"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    added, skipped = 0, Counter()
    for w in wrong:
        vid, tid = w["video_id"], w["track_id"]
        p = db.one("""SELECT frame, x1,y1,x2,y2, (x2-x1) wd FROM track_points
                      WHERE video_id=? AND track_id=? ORDER BY wd DESC LIMIT 1""", vid, tid)
        if not p or p["wd"] < min_box:
            skipped["too small"] += 1
            continue
        v = db.one("SELECT path, name FROM videos WHERE id=?", vid)
        if not v:
            continue
        cap = cv2.VideoCapture(v["path"])
        cap.set(cv2.CAP_PROP_POS_FRAMES, p["frame"])
        ok, img = cap.read()
        cap.release()
        if not ok or img is None:
            skipped["frame unreadable"] += 1
            continue
        H, W = img.shape[:2]
        stem = f"verified_v{vid}_f{p['frame']}_t{tid}"
        cv2.imwrite(str(img_dir / f"{stem}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])

        # EVERY vehicle in this frame, not just the corrected one.
        lines = []
        for q in db.rows("""SELECT tp.track_id, tp.x1,tp.y1,tp.x2,tp.y2,
                                   t.cls, t.class_override
                            FROM track_points tp JOIN tracks t
                              ON t.video_id=tp.video_id AND t.track_id=tp.track_id
                            WHERE tp.video_id=? AND tp.frame=?""", vid, p["frame"]):
            cls = q["class_override"] if q["class_override"] is not None else q["cls"]
            if cls is None or cls < 0 or cls >= len(CLASSES):
                continue                    # rejected as not-a-vehicle: omit entirely
            cx = ((q["x1"] + q["x2"]) / 2) / W
            cy = ((q["y1"] + q["y2"]) / 2) / H
            bw = (q["x2"] - q["x1"]) / W
            bh = (q["y2"] - q["y1"]) / H
            if bw <= 0 or bh <= 0:
                continue
            lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        (lbl_dir / f"{stem}.txt").write_text("\n".join(lines) + "\n")
        added += 1

    ds_id = artifacts.register_dataset(
        str(out), name=out_name, label_source="verified corrections",
        note=f"{base} plus {added} frames carrying {len(wrong)} human corrections "
             f"from clip verification at site {site_id}")
    return {"dataset": out_name, "path": str(out), "dataset_id": ds_id,
            "corrections": len(wrong), "frames_added": added,
            "skipped": dict(skipped),
            "note": "base dataset copied, not modified — the two stay comparable"}
