"""Collecting human answers to the fine-grained questions, for every attribute at once.

The axle audit proved two things worth generalising. Cheap VLMs plateau around 61% on this
kind of question and premium ones do no better, so the answers have to come from a person
and from a model trained on that person's answers. And the labelling itself is fast --
119 answers in a sitting -- provided the screen puts the right crop in front of you and
records the click without ceremony.

So this is the store, deliberately not axle-shaped: one row per (attribute, track), with
the crop chosen by that attribute's own frame policy. `attrspec` says what the questions
are; this says what has been answered.

**A label records the picture it was given.** `crop_path` is kept, not regenerated on
demand, because a label is only meaningful against the image the person actually saw --
re-rendering later with a different frame policy would silently re-point every answer at a
different picture.

**Nothing here ever writes an answer on a person's behalf.** Model predictions live in
their own column and can never overwrite `human`; that separation is what lets the same
table serve as training data and as the honest scoreboard for the thing trained on it.
"""
import sys
import time
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

import attrspec  # noqa: E402
from engine import CLASSES  # noqa: E402

CROP_DIR = ROOT / "lab_gold" / "_attrs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_attr_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attribute TEXT NOT NULL,
  video_id INTEGER, track_id INTEGER, frame INTEGER,
  det_class INTEGER, box_w INTEGER, box_h INTEGER,
  crop_path TEXT, ctx_path TEXT,
  human TEXT, human_at REAL,
  pred TEXT, pred_conf REAL, pred_model TEXT,
  split TEXT,
  created REAL,
  UNIQUE(attribute, video_id, track_id));

CREATE INDEX IF NOT EXISTS ix_attr_human ON lab_attr_samples(attribute, human);
CREATE INDEX IF NOT EXISTS ix_attr_track ON lab_attr_samples(video_id, track_id);
"""

# Added after the first labelling round, when it turned out the training crops already
# existed. Dataset-sourced samples have no track, so the (attribute, video_id, track_id)
# key does not constrain them -- SQLite treats NULLs as distinct -- and they get their own
# partial unique index on the box they came from instead.
SCHEMA_V2 = [
    "ALTER TABLE lab_attr_samples ADD COLUMN source TEXT DEFAULT 'video'",
    "ALTER TABLE lab_attr_samples ADD COLUMN source_ref TEXT",
    # The label the dataset already carries. Kept to STRATIFY sampling -- so the rare
    # classes are actually reachable -- and never shown while answering, because the whole
    # value of a fresh label is that it was not anchored on the old one.
    "ALTER TABLE lab_attr_samples ADD COLUMN prior TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_attr_source "
    "ON lab_attr_samples(attribute, source_ref) WHERE source_ref IS NOT NULL",
]


def init():
    db.conn().executescript(SCHEMA)
    for stmt in SCHEMA_V2:
        try:
            db.conn().execute(stmt)
        except Exception as e:            # already applied
            if "duplicate column" not in str(e).lower():
                raise
    db.conn().commit()


# ───────────────────────────── finding candidates ─────────────────────────────
def candidates(attribute, video_ids=None):
    """Tracks of the right parent class, each with the frame that attribute needs."""
    s = attrspec.spec(attribute)
    parents = {CLASSES.index(p) for p in s["parents"] if p in CLASSES}
    vids = video_ids or [v["id"] for v in db.rows("SELECT id FROM videos")]
    # "widest" finds the most side-on view; "largest" finds the most pixels. Ordering in
    # SQL rather than in Python keeps this to one row per track instead of pulling every
    # point of every track into memory.
    order = ("(x2-x1) DESC" if s["frame"] == attrspec.FRAME_WIDEST
             else "((x2-x1)*(y2-y1)) DESC")
    out = []
    for vid in vids:
        for t in db.rows("SELECT track_id, cls, class_override FROM tracks WHERE video_id=?", vid):
            cls = t["class_override"] if t["class_override"] is not None else t["cls"]
            if cls not in parents:
                continue
            p = db.one(f"""SELECT frame, x1, y1, x2, y2 FROM track_points
                           WHERE video_id=? AND track_id=? ORDER BY {order} LIMIT 1""",
                       vid, t["track_id"])
            if p:
                out.append({"attribute": attribute, "video_id": vid, "track_id": t["track_id"],
                            "frame": p["frame"], "det_class": cls,
                            "box": (p["x1"], p["y1"], p["x2"], p["y2"]),
                            "box_w": int(p["x2"] - p["x1"]), "box_h": int(p["y2"] - p["y1"])})
    return out


def render(c, margin=None):
    """Write the crop and the boxed context frame this label will be given against."""
    import cv2
    s = attrspec.spec(c["attribute"])
    margin = s["margin"] if margin is None else margin
    v = db.one("SELECT path FROM videos WHERE id=?", c["video_id"])
    if not v:
        return None, None
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, c["frame"])
    ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        return None, None

    H, W = img.shape[:2]
    x1, y1, x2, y2 = c["box"]
    mw, mh = (x2 - x1) * margin, (y2 - y1) * margin
    cx1, cy1 = int(max(0, x1 - mw)), int(max(0, y1 - mh))
    cx2, cy2 = int(min(W, x2 + mw)), int(min(H, y2 + mh))
    crop = img[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None, None
    if crop.shape[1] < 480:
        k = 480 / crop.shape[1]
        crop = cv2.resize(crop, (480, max(1, int(crop.shape[0] * k))),
                          interpolation=cv2.INTER_CUBIC)

    d = CROP_DIR / c["attribute"]
    d.mkdir(parents=True, exist_ok=True)
    stem = f"v{c['video_id']}_t{c['track_id']}"
    cp = d / f"{stem}.jpg"
    cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])

    ctx = img.copy()
    cv2.rectangle(ctx, (int(x1), int(y1)), (int(x2), int(y2)), (80, 220, 120), 3)
    k = 1280 / ctx.shape[1]
    if k < 1:
        ctx = cv2.resize(ctx, (1280, int(ctx.shape[0] * k)))
    xp = d / f"{stem}_ctx.jpg"
    cv2.imwrite(str(xp), ctx, [cv2.IMWRITE_JPEG_QUALITY, 86])
    return str(cp), str(xp)


def populate(attribute, video_ids=None, limit=None, min_px=None):
    """Make sure every candidate track has a row and a crop waiting to be answered.

    `min_px` is a floor on box width, and it matters more than it looks. Candidates are
    every track of a parent class, including vehicles at the far end of the road that
    never came near the line: at KDP-01 the widths are bimodal -- a median of 66px against
    a p75 of 528 -- so without a floor two thirds of the queue is specks nobody can answer
    and every one of them still costs a rendered crop on disk. Ask only what can be seen.

    It defaults to the attribute's own `min_box_w` rather than to zero, because the floor
    is a property of the question. Passing it per call meant a crop dropped from the queue
    for being too small came back on the next populate, and the labeller met it again.
    """
    init()
    if min_px is None:
        min_px = attrspec.spec(attribute).get("min_box_w", 0)
    have = {(r["video_id"], r["track_id"]) for r in
            db.rows("SELECT video_id, track_id FROM lab_attr_samples WHERE attribute=?",
                    attribute)}
    todo = [c for c in candidates(attribute, video_ids)
            if (c["video_id"], c["track_id"]) not in have and c["box_w"] >= min_px]
    # Widest first: the clearest evidence gets answered first, so a session cut short
    # still produced the most useful labels rather than an arbitrary slice.
    todo.sort(key=lambda c: -c["box_w"])
    if limit:
        todo = todo[:limit]
    made = 0
    for c in todo:
        cp, xp = render(c)
        if not cp:
            continue
        db.run("""INSERT OR IGNORE INTO lab_attr_samples
                  (attribute,video_id,track_id,frame,det_class,box_w,box_h,
                   crop_path,ctx_path,created) VALUES (?,?,?,?,?,?,?,?,?,?)""",
               attribute, c["video_id"], c["track_id"], c["frame"], c["det_class"],
               c["box_w"], c["box_h"], cp, xp, time.time())
        made += 1
    return {"attribute": attribute, "added": made, "already_present": len(have)}


def populate_from_dataset(attribute, dataset="round4", min_px=200, per_class=250):
    """Harvest labelling candidates from the hand-labelled YOLO dataset.

    The crops needed to train these classifiers already exist: 8,111 frames were labelled
    box by box during the original fine-tuning, and 1,297 of those boxes are heavy
    vehicles. Extracting more footage to obtain what is already on disk would be a waste of
    hours, so this reads the label files and cuts the crops straight out of the frames.

    **`min_px` is not a quality preference, it is the physics.** The audit measured judges
    abstaining on 43% of crops under 150px and 11% over 500px; a person is subject to the
    same limit. A crop too small to resolve wheel groups produces a guess, and a guess
    recorded as ground truth is the thing this subsystem exists to eliminate.

    **`per_class` caps the common class so the rare ones get looked at.** 2Axle outnumbers
    MAV seven to one, so an uncapped queue sorted by size would show hundreds of the same
    common truck before the first multi-axle rig. The cap is applied against the dataset's
    existing label purely to spread the sampling -- that label never becomes an answer.
    """
    import cv2
    init()
    s = attrspec.spec(attribute)
    parents = {CLASSES.index(p) for p in s["parents"] if p in CLASSES}
    base = ROOT / "dataset" / dataset
    lbl_root = base / "labels" if (base / "labels").is_dir() else base
    if not lbl_root.is_dir():
        return {"error": f"no dataset at {base}"}

    found = []
    for f in sorted(lbl_root.rglob("*.txt")):
        img = None
        for cand in (base / "images" / f.parent.name / (f.stem + ".jpg"),
                     base / "images" / (f.stem + ".jpg")):
            if cand.is_file():
                img = cand
                break
        if img is None:
            continue
        for i, line in enumerate(f.read_text().splitlines()):
            p = line.split()
            if len(p) < 5:
                continue
            c = int(p[0])
            if c not in parents:
                continue
            cx, cy, w, h = (float(x) for x in p[1:5])
            found.append({"img": img, "box_i": i, "cls": c, "n": (cx, cy, w, h)})

    # Cap per existing-label class, largest first, so rare classes survive the cut.
    by_cls = {}
    for r in found:
        by_cls.setdefault(r["cls"], []).append(r)
    picked = []
    for c, rows in by_cls.items():
        rows.sort(key=lambda r: -r["n"][2])
        picked += rows[:per_class]

    made = skipped_small = 0
    d = CROP_DIR / attribute
    d.mkdir(parents=True, exist_ok=True)
    for r in picked:
        ref = f"{dataset}:{r['img'].relative_to(base).as_posix()}:{r['box_i']}"
        if db.one("SELECT id FROM lab_attr_samples WHERE attribute=? AND source_ref=?",
                  attribute, ref):
            continue
        im = cv2.imread(str(r["img"]))
        if im is None:
            continue
        H, W = im.shape[:2]
        cx, cy, w, h = r["n"]
        bw, bh = w * W, h * H
        if bw < min_px:
            skipped_small += 1
            continue
        x1, y1 = (cx - w / 2) * W, (cy - h / 2) * H
        x2, y2 = (cx + w / 2) * W, (cy + h / 2) * H
        mw, mh = bw * s["margin"], bh * s["margin"]
        crop = im[int(max(0, y1 - mh)):int(min(H, y2 + mh)),
                  int(max(0, x1 - mw)):int(min(W, x2 + mw))]
        if crop.size == 0:
            continue
        if crop.shape[1] < 480:
            k = 480 / crop.shape[1]
            crop = cv2.resize(crop, (480, max(1, int(crop.shape[0] * k))),
                              interpolation=cv2.INTER_CUBIC)
        stem = f"ds_{r['img'].stem}_{r['box_i']}"
        cp = d / f"{stem}.jpg"
        cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])

        ctx = im.copy()
        cv2.rectangle(ctx, (int(x1), int(y1)), (int(x2), int(y2)), (80, 220, 120), 3)
        xp = d / f"{stem}_ctx.jpg"
        cv2.imwrite(str(xp), ctx, [cv2.IMWRITE_JPEG_QUALITY, 86])

        db.run("""INSERT INTO lab_attr_samples
                  (attribute,det_class,box_w,box_h,crop_path,ctx_path,
                   source,source_ref,prior,created)
                  VALUES (?,?,?,?,?,?,'dataset',?,?,?)""",
               attribute, r["cls"], int(bw), int(bh), str(cp), str(xp),
               ref, CLASSES[r["cls"]], time.time())
        made += 1
    return {"attribute": attribute, "dataset": dataset, "boxes_seen": len(found),
            "considered": len(picked), "added": made,
            "skipped_too_small": skipped_small, "min_px": min_px}


# ───────────────────────────── answering ─────────────────────────────
def set_human(sample_id, value):
    """Record a person's answer AND make it count.

    The write to `tracks.class_override` is the whole point and was missing: answers
    accumulated as training data while the report kept showing the detector's guess, so a
    clip whose every heavy vehicle had been classified by hand still published the wrong
    columns. Doing it here, on the click, rather than in a batch someone has to remember
    to run, is what stops the two from drifting apart again.

    Only for `kind="class"` attributes -- those replace the MoRTH class. An attribute like
    bus operator annotates a vehicle without changing what it is counted as, and writing it
    into `class_override` would corrupt the count it is meant to enrich.
    """
    init()
    r = db.one("""SELECT attribute, video_id, track_id, det_class
                  FROM lab_attr_samples WHERE id=?""", sample_id)
    if not r:
        raise KeyError(f"no sample {sample_id}")
    spec = attrspec.spec(r["attribute"])
    if value not in spec["values"]:
        raise ValueError(f"{value!r} is not a valid answer for {r['attribute']}")
    db.run("UPDATE lab_attr_samples SET human=?, human_at=? WHERE id=?",
           value, time.time(), sample_id)

    applied = False
    if spec.get("kind") == "class" and r["video_id"] is not None:
        name = spec.get("to_class", {}).get(value)
        # `unclear` and answers rejecting the parent class name no class, so they leave
        # the detector's label alone rather than forcing a column.
        if name and name in CLASSES:
            db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
                   CLASSES.index(name), r["video_id"], r["track_id"])
            applied = True
    return {"ok": True, "applied_to_count": applied}


def queue(attribute, limit=200, include_answered=False, video_id=None):
    """What still needs answering, biggest crop first -- the easiest calls come first.

    `video_id` narrows to one clip. That matters when a fresh clip has been extracted to
    test a model: those crops are the only ones the model has not been trained on, so
    labelling exactly them -- and not a hundred already-seen ones alongside -- is what
    turns the exercise into an off-sample measurement.
    """
    init()
    where = "attribute=?" + ("" if include_answered else " AND human IS NULL")
    args = [attribute]
    if video_id is not None:
        where += " AND video_id=?"
        args.append(video_id)
    rows = db.rows(f"""SELECT * FROM lab_attr_samples WHERE {where}
                       ORDER BY box_w DESC LIMIT ?""", *args, limit)
    for r in rows:
        r["det"] = CLASSES[r["det_class"]] if r["det_class"] is not None \
            and 0 <= r["det_class"] < len(CLASSES) else None
    return rows


def stats(attribute=None):
    """Per attribute: how many answered, how the answers are distributed, what's missing.

    `shortfall` is the number that decides whether a model can be trained at all, so it is
    reported per attribute rather than left to be worked out from the counts.
    """
    init()
    # A retired question still reports its numbers when asked for by name -- that is how
    # the evidence for retiring it stays visible -- but never appears in the list of work.
    names = ([attribute] if attribute
             else [n for n, s in attrspec.ATTRIBUTES.items() if s.get("mode") != "off"])
    out = []
    for n in names:
        s = attrspec.spec(n)
        rows = db.rows("SELECT human FROM lab_attr_samples WHERE attribute=?", n)
        dist = {}
        for r in rows:
            if r["human"]:
                dist[r["human"]] = dist.get(r["human"], 0) + 1
        usable = sum(v for k, v in dist.items() if k != attrspec.ABSTAIN)
        out.append({
            "attribute": n, "label": s["label"], "kind": s["kind"],
            "mode": s.get("mode", "human"),
            "parents": s["parents"], "values": s["values"], "hint": s["hint"],
            "total": len(rows), "answered": sum(dist.values()), "usable": usable,
            "distribution": dist, "min_labels": s["min_labels"],
            "shortfall": max(0, s["min_labels"] - usable),
            "trainable": usable >= s["min_labels"],
        })
    return out


# ───────────────────────────── migration ─────────────────────────────
def migrate_axles():
    """Bring the axle audit's human answers into the generic store, without loss.

    Those 119 answers are the only real ground truth the project has for axle class, and
    they were expensive in the one currency that matters here -- a person's attention. The
    crop paths are carried across rather than re-rendered so that each label still refers
    to the exact image it was given against.
    """
    init()
    src = db.rows("""SELECT * FROM lab_axle_checks
                     WHERE human IS NOT NULL""")
    moved = 0
    for r in src:
        db.run("""INSERT INTO lab_attr_samples
                  (attribute,video_id,track_id,frame,det_class,box_w,box_h,
                   crop_path,ctx_path,human,human_at,created)
                  VALUES ('axles',?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(attribute,video_id,track_id) DO UPDATE SET
                    human=excluded.human, human_at=excluded.human_at""",
               r["video_id"], r["track_id"], r["frame"], r["det_class"],
               r["box_w"], r["box_h"], r["crop_path"], r["ctx_path"],
               r["human"], r["created"], r["created"])
        moved += 1
    check = db.one("""SELECT COUNT(*) c FROM lab_attr_samples
                      WHERE attribute='axles' AND human IS NOT NULL""")["c"]
    return {"source_rows": len(src), "written": moved, "now_in_store": check,
            "lossless": check >= len(src)}
