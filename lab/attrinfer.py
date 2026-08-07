"""Run a trained attribute model over real tracks, behind a gate it has to earn.

The axle classifier scores 75% against 35% for the detector it would replace, so applying
it should improve the report. "Should" is the problem: the same sentence was true of the
judge layer that turned out to be laundering the detector's own guesses back into the
training set, and nothing in the pipeline noticed for four rounds.

So predictions do not become counts by being produced. Three things stand between:

**A promotion gate.** The model must beat the detector on a held-out slice of human labels
it never trained on, by a stated margin, before its output is allowed anywhere near a
count. `promote()` reports the comparison and refuses if it fails.

**A confidence floor with a human queue.** A softmax over three classes always names a
winner. Below the floor the prediction is recorded but not applied, and the track joins a
review queue -- which is the same mechanism that made the axle labels in the first place.

**Human answers reach the count too.** `apply_human` writes what a person decided into
the class the report uses. Without it the labelling screen builds training data and
silently leaves the survey wrong, which is the more embarrassing half of the same bug.

**Predictions never overwrite a human.** `class_override` is where a person's decision
lives; a model writes `attr_class` and counting prefers the human whenever there is one.
An automated pass that can quietly erase a person's correction is a system nobody can
trust twice.
"""
import sys
import time
from collections import Counter
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
import attrspec  # noqa: E402
from engine import CLASSES  # noqa: E402

# How much better than the incumbent the model has to be before it may touch a count.
# Not zero: a model ahead by a point or two on a 120-crop validation slice is inside the
# noise, and swapping a known-wrong number for an unknown-wrong one is not progress.
PROMOTION_MARGIN = 0.10
# Raised from 0.55 after the model's first production use got both of its decisions
# wrong, each at exactly 0.70. Measured on labelled crops wide enough to judge:
#
#     confidence 0.40-0.70   n=4    1 correct   (2 on video 8, 2 on video 6)
#     confidence >= 0.75     n=14  14 correct
#
# The band between the old floor and 0.75 is where the model is merely picking a winner
# among features it cannot resolve, and it is exactly where a 3-axle rear bogie and a
# 4-axle rig look alike. Those cases belong to a person, not to an argmax.
CONFIDENCE_FLOOR = 0.75

# The size floor matters more than the confidence floor, which is not what I expected.
# Measured on 30 crops from a clip nobody had labelled, against a person's answers:
#
#     crop width        answerable by eye: median 536px   unanswerable: median 50px
#     model confidence  answerable: 0.84                  unanswerable: 0.64
#
# A softmax over three classes is not a detector of "there is nothing here to see": the
# model returned 0.93 on an 81px crop and 0.87 on a 52px one, both of which a person
# looked at and said were too far away to call. Confidence measures how cleanly the
# features it found fall into a class, not whether those features exist.
#
#     confidence >= 0.55          24 kept,  67% readable, 81% correct
#     width >= 400px              15 kept, 100% readable, 80% correct
#     width >= 300 & conf >= .55  14 kept, 100% readable, 86% correct
#
# So both, with width leading. This is the same threshold the VLM audit found and I
# failed to carry across: below roughly 150-200px there is no evidence in the picture,
# and every answer given about it -- by a model or a person -- is a guess.
MIN_BOX_W = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_attr_preds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attribute TEXT, model_id INTEGER, video_id INTEGER, track_id INTEGER,
  pred TEXT, confidence REAL, applied INTEGER DEFAULT 0,
  det_class INTEGER, new_class INTEGER, created REAL,
  UNIQUE(attribute, video_id, track_id));

CREATE TABLE IF NOT EXISTS lab_attr_promotions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attribute TEXT, model_id INTEGER, promoted INTEGER,
  model_accuracy REAL, detector_accuracy REAL, margin REAL, n_val INTEGER,
  reason TEXT, created REAL,
  site_id INTEGER);        -- NULL = promoted globally; set = promoted at one station

-- Thresholds that gate a head, per station. lab_settings cannot carry these: its key is
-- the PRIMARY KEY, so it holds exactly one value per name for the whole system.
-- NULL site_id is the global default; a row with a site_id overrides it for that station.
CREATE TABLE IF NOT EXISTS lab_site_settings (
  site_id INTEGER, key TEXT, value TEXT, updated REAL,
  PRIMARY KEY (site_id, key));
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


def _load(model_id=None, attribute="axles"):
    """The archived weights, plus the class order they were trained with.

    The class list is stored *inside* the checkpoint rather than recomputed from the label
    table. Recomputing it would silently re-map every prediction the day a new class
    appears in the data -- the model's third output would keep meaning what it meant, and
    everything reading it would disagree.
    """
    import torch
    import torchvision
    import torch.nn as nn
    q = "SELECT * FROM lab_attr_models WHERE reliable=1"
    args = []
    if model_id:
        q += " AND id=?"
        args = [model_id]
    else:
        q += " AND attribute=?"
        args = [attribute]
    row = db.one(q + " ORDER BY macro_f1 DESC, id DESC", *args)
    if not row:
        return None, None, None
    blob = torch.load(row["path"], map_location="cpu", weights_only=False)
    classes = blob["classes"]
    model = getattr(torchvision.models, blob["arch"])(weights=None)
    model.fc = nn.Linear(model.fc.in_features, len(classes))
    model.load_state_dict(blob["state_dict"])
    model.eval()
    return model, classes, row


def _predict(model, classes, paths, img_size=320, device=None):
    import torch
    from PIL import Image
    from torchvision import transforms as T
    import attrtrain
    dev = device or attrtrain._device()
    model = model.to(dev)
    tf = T.Compose([T.Resize((img_size, img_size)), T.ToTensor(),
                    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
    out = []
    with torch.no_grad():
        for i in range(0, len(paths), 32):
            batch = torch.stack([tf(Image.open(p).convert("RGB"))
                                 for p in paths[i:i + 32]]).to(dev)
            prob = torch.softmax(model(batch), dim=1).cpu()
            for row in prob:
                j = int(row.argmax())
                out.append((classes[j], float(row[j])))
    return out


# ───────────────────────────── the gate ─────────────────────────────
def promote(attribute="axles", model_id=None, margin=PROMOTION_MARGIN):
    """Does the model beat the detector on labels it never saw? Decided, then recorded.

    Both are scored on the SAME held-out rows, which is the only comparison that means
    anything: the detector's number quoted from a different sample would be a different
    experiment wearing the same units.
    """
    init()
    import attrtrain
    model, classes, row = _load(model_id, attribute)
    if not model:
        return {"promoted": False, "reason": "no reliable trained model for this attribute"}

    spec = attrspec.spec(attribute)
    to_cls = spec.get("to_class", {})
    va = [r for r in attrtrain.labelled(attribute) if r["split"] == "val"]
    if not va:
        return {"promoted": False, "reason": "no validation split assigned; train first"}

    preds = _predict(model, classes, [r["crop_path"] for r in va])
    m_ok = sum(1 for r, (p, _c) in zip(va, preds) if p == r["human"])

    # The incumbent: whatever class the detector gave this crop, expressed as an answer.
    d_ok = 0
    for r in va:
        det = CLASSES[r["det_class"]] if r["det_class"] is not None else None
        d_ok += to_cls.get(r["human"]) == det
    n = len(va)
    m_acc, d_acc = m_ok / n, d_ok / n
    ok = (m_acc - d_acc) >= margin
    reason = (f"model {100*m_acc:.0f}% vs detector {100*d_acc:.0f}% on {n} held-out "
              f"labels; margin required {100*margin:.0f}pts, actual "
              f"{100*(m_acc-d_acc):.0f}pts")
    # One row per DECISION, not per call. `promote()` is safe to re-run -- the survey app
    # calls it to check a model is still cleared -- and logging every call turned the
    # audit trail into a call log: eight rows for two models, none of them a decision.
    # A promotion inherits the model's scope. A head trained on one station's crops was
    # gated on that station's held-out data and says nothing about any other camera, so
    # clearing it globally would be claiming evidence that was never gathered. Heads with
    # site_id NULL -- everything trained before this existed -- stay global as before.
    site_id = row.get("site_id")
    prior = db.one("""SELECT id, promoted, model_accuracy, n_val FROM lab_attr_promotions
                      WHERE attribute=? AND model_id=? ORDER BY id DESC LIMIT 1""",
                   attribute, row["id"])
    changed = (not prior or prior["promoted"] != int(ok)
               or abs((prior["model_accuracy"] or 0) - m_acc) > 1e-9
               or prior["n_val"] != n)
    if changed:
        db.run("""INSERT INTO lab_attr_promotions
                  (attribute,model_id,promoted,model_accuracy,detector_accuracy,margin,
                   n_val,reason,created,site_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
               attribute, row["id"], int(ok), m_acc, d_acc, margin, n, reason, time.time(),
               site_id)
    return {"promoted": ok, "attribute": attribute, "model_id": row["id"],
            "model_accuracy": round(m_acc, 3), "detector_accuracy": round(d_acc, 3),
            "n_val": n, "reason": reason,
            "scope": "station" if site_id is not None else "global", "site_id": site_id}


# ───────────────────────────── applying ─────────────────────────────
def apply(attribute="axles", video_ids=None, model_id=None, dry=True,
          floor=CONFIDENCE_FLOOR, min_box_w=MIN_BOX_W):
    """Predict on every eligible track and, if promoted, write the corrected class.

    `dry=True` by default: this rewrites the class of counted vehicles, which changes a
    deliverable, so the caller has to say so explicitly.
    """
    init()
    import attrlabel
    gate = promote(attribute, model_id)
    if not gate["promoted"] and not dry:
        return {"applied": 0, "blocked": True, **gate}

    model, classes, row = _load(model_id, attribute)
    if not model:
        return {"error": "no model"}
    spec = attrspec.spec(attribute)
    to_cls = spec.get("to_class", {})

    cands = attrlabel.candidates(attribute, video_ids)
    # Human answers win outright and are never re-predicted over.
    human = {(r["video_id"], r["track_id"]): r["human"] for r in db.rows(
        "SELECT video_id,track_id,human FROM lab_attr_samples "
        "WHERE attribute=? AND human IS NOT NULL", attribute)}

    todo, paths = [], []
    for c in cands:
        key = (c["video_id"], c["track_id"])
        s = db.one("""SELECT crop_path FROM lab_attr_samples
                      WHERE attribute=? AND video_id=? AND track_id=?""",
                   attribute, *key)
        if not s or not s["crop_path"] or not Path(s["crop_path"]).is_file():
            continue
        todo.append(c)
        paths.append(s["crop_path"])
    if not todo:
        return {"applied": 0, "reason": "no crops; run attrlabel.populate first", **gate}

    preds = _predict(model, classes, paths)
    n_applied = n_low = n_human = n_same = n_small = 0
    changes = Counter()
    for c, (p, conf) in zip(todo, preds):
        key = (c["video_id"], c["track_id"])
        db.run("""INSERT INTO lab_attr_preds
                  (attribute,model_id,video_id,track_id,pred,confidence,det_class,created)
                  VALUES (?,?,?,?,?,?,?,?)
                  ON CONFLICT(attribute,video_id,track_id) DO UPDATE SET
                    pred=excluded.pred, confidence=excluded.confidence,
                    model_id=excluded.model_id, created=excluded.created""",
               attribute, row["id"], c["video_id"], c["track_id"], p, conf,
               c["det_class"], time.time())
        if key in human:
            n_human += 1
            continue
        if c["box_w"] < min_box_w:
            # Too far away for the evidence to be in the picture at all. Checked before
            # confidence, because the model is happy to be certain about a 50px truck.
            n_small += 1
            continue
        if conf < floor:
            n_low += 1
            continue
        new_name = to_cls.get(p)
        if new_name is None or new_name not in CLASSES:
            continue                       # e.g. not_a_truck: not this stage's decision
        new_id = CLASSES.index(new_name)
        if new_id == c["det_class"]:
            n_same += 1
            continue
        changes[(CLASSES[c["det_class"]], new_name)] += 1
        if not dry:
            db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
                   new_id, c["video_id"], c["track_id"])
            db.run("""UPDATE lab_attr_preds SET applied=1, new_class=?
                      WHERE attribute=? AND video_id=? AND track_id=?""",
                   new_id, attribute, *key)
        n_applied += 1

    return {"dry": dry, "promoted": gate["promoted"], "gate": gate["reason"],
            "candidates": len(todo), "would_change" if dry else "changed": n_applied,
            "left_to_human": n_human, "below_confidence": n_low,
            "too_small": n_small, "min_box_w": min_box_w, "unchanged": n_same,
            "moves": [{"from": a, "to": b, "n": n} for (a, b), n in changes.most_common()]}


def apply_human(attribute="axles", video_ids=None, dry=True):
    """Write the answers a PERSON gave into the classes the report counts.

    This existed as a hole rather than a decision. `apply()` refuses to overwrite a human
    answer, which is right, but nothing else wrote those answers into `class_override`
    either -- so a clip whose every heavy vehicle had been classified by hand still
    reported the detector's guesses. The labelling screen looked like it was correcting
    the survey and was only ever building a training set.

    No gate and no confidence floor here, and no size floor: a person looking at the crop
    is the best evidence this system has, and it does not need to out-argue a model to be
    used. `unclear` and answers that reject the parent class are skipped -- neither names
    a class to count it as.
    """
    init()
    spec = attrspec.spec(attribute)
    to_cls = spec.get("to_class", {})
    # Join to the track so "already correct" means the class the COUNT currently uses,
    # not the detector's original guess. Comparing against the stored `det_class` made
    # this report the same backlog forever, however many times it had been applied.
    q = """SELECT s.video_id, s.track_id, s.human, s.det_class,
                  COALESCE(t.class_override, t.cls) AS current_class
           FROM lab_attr_samples s
           LEFT JOIN tracks t ON t.video_id=s.video_id AND t.track_id=s.track_id
           WHERE s.attribute=? AND s.human IS NOT NULL AND s.video_id IS NOT NULL"""
    args = [attribute]
    if video_ids:
        q += f" AND s.video_id IN ({','.join('?' * len(video_ids))})"
        args += list(video_ids)

    changed, same, skipped = 0, 0, 0
    moves = Counter()
    for r in db.rows(q, *args):
        name = to_cls.get(r["human"])
        if name is None or name not in CLASSES:
            skipped += 1                      # unclear, or "not a truck": no class to give
            continue
        new_id = CLASSES.index(name)
        cur = r["current_class"]
        if new_id == cur:
            same += 1
            continue
        moves[(CLASSES[cur] if cur is not None and cur < len(CLASSES) else "?", name)] += 1
        changed += 1
        if not dry:
            db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
                   new_id, r["video_id"], r["track_id"])
    return {"dry": dry, "attribute": attribute,
            "would_change" if dry else "changed": changed,
            "already_correct": same, "no_class_to_apply": skipped,
            "moves": [{"from": a, "to": b, "n": n} for (a, b), n in moves.most_common()]}


def review_queue(attribute="axles", floor=CONFIDENCE_FLOOR, limit=200):
    """Tracks the model was not confident enough about -- the ones worth a person's time."""
    init()
    # Only crops a person could actually answer. A queue full of 50px trucks wastes the
    # scarcest resource in this system -- somebody's attention -- on pictures with no
    # evidence in them, and every answer it collects would be a guess.
    rows = db.rows("""SELECT p.*, s.crop_path, s.box_w FROM lab_attr_preds p
                      LEFT JOIN lab_attr_samples s
                        ON s.attribute=p.attribute AND s.video_id=p.video_id
                       AND s.track_id=p.track_id
                      WHERE p.attribute=? AND p.confidence < ?
                        AND s.human IS NULL AND s.box_w >= ?
                      ORDER BY s.box_w DESC LIMIT ?""",
                   attribute, floor, MIN_BOX_W, limit)
    for r in rows:
        r["det"] = CLASSES[r["det_class"]] if r["det_class"] is not None else None
    return rows
