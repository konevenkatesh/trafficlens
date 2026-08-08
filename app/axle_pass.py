"""The axle pass: give every counted truck the axle class the proforma asks for.

The detector cannot do this. Measured on a fresh FID-33 clip, it put 17 of 17 heavy
vehicles in the 2Axle column -- zero 3-axle, zero MAV -- on a road where two thirds are
3-axle. The APRDC workbook has a column for each, so that is three columns wrong on every
report, while the vehicle total looks perfectly fine.

A classifier trained in the Lab on human answers scores 80% against the detector's 11% on
unseen footage. This runs it, and it is deliberately the *only* thing here that is
automatic: the Lab trains, the survey app infers. No weights are made in this process, and
the model it loads is one that has already passed the Lab's promotion gate.

Three rules, each of which cost something to learn:

**Only vehicles that crossed the line.** The report counts crossings, so a truck parked in
the verge for the whole clip is not worth a person's attention or a GPU's.

**The crop must be big enough before anything is asked of it.** A softmax always names a
winner: this model returned 0.93 on an 81px truck a person looked at and could not call.
Confidence measures how cleanly features fall into a class, not whether the features are
there. Width first, confidence second.

**A person's answer is final and immediate.** Verdicts write straight through to the class
the report counts. The Lab shipped for a while with human answers accumulating as training
data while the report kept publishing the detector's guess; that is not a bug worth having
twice.
"""
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import db
from engine import CLASSES

# Bundled read-only files live under sys._MEIPASS in a frozen build, and `__file__` for
# a frozen module points inside it -- so `__file__.parent.parent` lands ABOVE the bundle
# and every packaged path silently misses. Writable paths must NOT use this: they follow
# TRAFFICLENS_DATA instead, because the bundle is a temp directory deleted on exit.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# Runtime output, so it follows the data directory rather than the install directory --
# a packaged build's own folder is temporary and may be read-only.
CROP_DIR = Path(os.environ.get("TRAFFICLENS_DATA") or ROOT / "app") / "axle_crops"

ATTR = "axles"
# Answer -> MoRTH class. `not_a_truck` names no class: it says the detection was wrong
# about the vehicle type, which is the class reviewer's question, not this pass's.
TO_CLASS = {"2_axle": "2Axle_Truck", "3_axle": "3Axle_Truck", "4_or_more_axle": "MAV"}
ANSWERS = ["2_axle", "3_axle", "4_or_more_axle", "not_a_truck", "unclear"]
PARENTS = ("2Axle_Truck", "3Axle_Truck", "MAV")

MIN_BOX_W = 300      # below this the wheels are not in the picture at any confidence
# Raised from 0.55 after the model's first production use got both of its decisions
# wrong, each at exactly 0.70. Measured on labelled crops wide enough to judge:
#
#     confidence 0.40-0.70   n=4    1 correct   (2 on video 8, 2 on video 6)
#     confidence >= 0.75     n=14  14 correct
#
# The band between the old floor and 0.75 is where the model is merely picking a winner
# among features it cannot resolve, and it is exactly where a 3-axle rear bogie and a
# 4-axle rig look alike. Those cases belong to a person, not to an argmax.
CONF_FLOOR = 0.75
IMG_SIZE = 320       # measured optimum; 224 loses the rear-bogie gap, 448 overfits

SCHEMA = """
CREATE TABLE IF NOT EXISTS axle_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER, track_id INTEGER, frame INTEGER,
  det_class INTEGER, box_w INTEGER, crop_path TEXT, ctx_path TEXT,
  pred TEXT, confidence REAL, model_id INTEGER,
  human TEXT, human_at REAL,
  applied TEXT, applied_by TEXT, created REAL,
  UNIQUE(video_id, track_id));
CREATE INDEX IF NOT EXISTS ix_axle_video ON axle_checks(video_id);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()
    _ensure_scope()


# ───────────────────────────── the model ─────────────────────────────
_SCOPED = False


def _ensure_scope():
    """Make sure the station-scoping columns exist, whichever app got here first.

    This module is imported by both apps, and `import db` resolves to a different module
    in each -- app/db.py runs the column migrations, lab/db.py does not run any. So the
    columns appeared only if the survey app happened to open the database first, and the
    Lab would query a column that was not there yet. Ensuring them here removes the
    dependency on boot order entirely.
    """
    global _SCOPED
    if _SCOPED:
        return
    for table, col in (("lab_attr_models", "site_id"),
                       ("lab_attr_promotions", "site_id")):
        if not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table):
            continue          # the Lab creates these lazily; nothing to migrate yet
        have = {r["name"] for r in db.rows(f"PRAGMA table_info({table})")}
        if col not in have:
            db.run(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")
    db.run("""CREATE TABLE IF NOT EXISTS lab_site_settings (
                site_id INTEGER, key TEXT, value TEXT, updated REAL,
                PRIMARY KEY (site_id, key))""")
    _SCOPED = True


def site_of(video_id):
    """Which station this clip belongs to, or None if it is not attached to one."""
    r = db.one("SELECT site_id FROM videos WHERE id=?", video_id)
    return (r or {}).get("site_id")


def setting(key, default, site_id=None):
    """A threshold, resolved station-first then global.

    Thresholds here are camera properties, not universal truths. MIN_BOX_W=760 means "at
    KDP-01's mounting height and lens, a truck narrower than this has its wheels out of
    frame" -- at a station set further back the same truck is 500px and that floor would
    silently switch the head off. So a station may override, and a station that has not
    measured its own keeps the global default.
    """
    _ensure_scope()
    r = None
    if site_id is not None:
        r = db.one("SELECT value FROM lab_site_settings WHERE site_id=? AND key=?",
                   site_id, key)
    if r is None:
        r = db.one("SELECT value FROM lab_site_settings WHERE site_id IS NULL AND key=?", key)
    if r is None:
        return default
    try:
        return type(default)(r["value"])
    except (TypeError, ValueError):
        return default


def current_model(site_id=None):
    """The axle model serving this station: its own if it has one, else the global one.

    Read from the shared database rather than a path in a config file, so the app can
    never be running weights the Lab has since disowned, and so `reliable=0` runs -- the
    ones forced below the label threshold for a smoke test -- are structurally excluded.

    A head trained on one camera is calibrated to that camera. Resolution is therefore
    station-first, falling back to global: training a head at KDP-01 must not change what
    any other station is running, and a station that has trained nothing must keep working
    exactly as it does today. Existing rows carry site_id NULL, so they stay global.
    """
    if not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_attr_models'"):
        return None
    _ensure_scope()

    def pick(scope):
        # The promotion has to be scoped too. A head promoted globally is cleared for every
        # camera; one promoted at a station was gated on that station's held-out crops and
        # says nothing about anywhere else.
        cond = "m.site_id=? AND p.site_id=?" if scope is not None \
            else "m.site_id IS NULL AND p.site_id IS NULL"
        args = (ATTR,) + ((scope, scope) if scope is not None else ())
        return db.one(f"""SELECT m.* FROM lab_attr_models m
                          JOIN lab_attr_promotions p ON p.model_id=m.id AND p.promoted=1
                          WHERE m.attribute=? AND m.reliable=1 AND {cond}
                          ORDER BY m.macro_f1 DESC, m.id DESC LIMIT 1""", *args)

    row = (pick(site_id) if site_id is not None else None) or pick(None)
    if row and Path(row["path"]).is_file():
        row["classes"] = json.loads(row["classes"]) if isinstance(row["classes"], str) \
            else row["classes"]
        row["scope"] = "station" if row.get("site_id") is not None else "global"
        return row
    return None


_CACHE = {}


def _load(site_id=None):
    row = current_model(site_id)
    if not row:
        return None, None, None
    if _CACHE.get("id") == row["id"]:
        return _CACHE["model"], _CACHE["classes"], row
    import torch
    import torch.nn as nn
    import torchvision
    blob = torch.load(row["path"], map_location="cpu", weights_only=False)
    m = getattr(torchvision.models, blob["arch"])(weights=None)
    m.fc = nn.Linear(m.fc.in_features, len(blob["classes"]))
    m.load_state_dict(blob["state_dict"])
    m.eval()
    _CACHE.update({"id": row["id"], "model": m, "classes": blob["classes"]})
    return m, blob["classes"], row


def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ───────────────────────────── what to look at ─────────────────────────────
def candidates(video_id, lines=None):
    """Heavy vehicles that actually crossed the line, with their most side-on frame."""
    import counting
    if lines is None:
        import sites
        lines = sites.lines_for(video_id)[0]
    if not lines:
        return []
    r = counting.count_video(video_id, lines)
    want = {e["track_id"] for e in r["events"] if e["class"] in PARENTS}
    out = []
    for tid in sorted(want):
        p = db.one("""SELECT frame, x1, y1, x2, y2 FROM track_points
                      WHERE video_id=? AND track_id=?
                      ORDER BY (x2-x1) DESC LIMIT 1""", video_id, tid)
        t = db.one("SELECT cls, class_override FROM tracks WHERE video_id=? AND track_id=?",
                   video_id, tid)
        if not p or not t:
            continue
        cls = t["class_override"] if t["class_override"] is not None else t["cls"]
        out.append({"video_id": video_id, "track_id": tid, "frame": p["frame"],
                    "det_class": cls, "box": (p["x1"], p["y1"], p["x2"], p["y2"]),
                    "box_w": int(p["x2"] - p["x1"])})
    return out


def _render(c, margin=0.10):
    import cv2
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
    crop = img[int(max(0, y1 - mh)):int(min(H, y2 + mh)),
               int(max(0, x1 - mw)):int(min(W, x2 + mw))]
    if crop.size == 0:
        return None, None
    if crop.shape[1] < 480:
        k = 480 / crop.shape[1]
        crop = cv2.resize(crop, (480, max(1, int(crop.shape[0] * k))),
                          interpolation=cv2.INTER_CUBIC)
    d = CROP_DIR / str(c["video_id"])
    d.mkdir(parents=True, exist_ok=True)
    cp = d / f"t{c['track_id']}.jpg"
    cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    ctx = img.copy()
    cv2.rectangle(ctx, (int(x1), int(y1)), (int(x2), int(y2)), (80, 220, 120), 3)
    k = 1280 / ctx.shape[1]
    if k < 1:
        ctx = cv2.resize(ctx, (1280, int(ctx.shape[0] * k)))
    xp = d / f"t{c['track_id']}_ctx.jpg"
    cv2.imwrite(str(xp), ctx, [cv2.IMWRITE_JPEG_QUALITY, 86])
    return str(cp), str(xp)


# ───────────────────────────── the pass ─────────────────────────────
def run(video_id, job_id=None, apply=True):
    """Classify every counted heavy vehicle; auto-apply the confident ones."""
    init()
    site_id = site_of(video_id)
    model, classes, row = _load(site_id)
    if not model:
        return {"error": "no promoted axle model available — train and promote one in the Lab"}
    # Resolved per station, defaulting to the module constants. Both are camera
    # properties: how wide a truck has to be before its wheels are readable, and how sure
    # the head must be before it overwrites a counted vehicle's class.
    min_box_w = setting("axle_min_box_w", MIN_BOX_W, site_id)
    conf_floor = setting("axle_conf_floor", CONF_FLOOR, site_id)
    cands = candidates(video_id)
    if not cands:
        return {"checked": 0, "note": "no heavy vehicles crossed the line on this video"}

    import torch
    from PIL import Image
    from torchvision import transforms as T
    dev = _device()
    model = model.to(dev)
    tf = T.Compose([T.Resize((IMG_SIZE, IMG_SIZE)), T.ToTensor(),
                    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

    applied = queued = skipped_small = unchanged = skipped_human = 0
    moves = Counter()
    for i, c in enumerate(cands):
        cp, xp = _render(c)
        if not cp:
            continue
        # Human answers are never re-predicted over, so a re-run is safe to trigger --
        # and that includes answers given in the LAB. The two apps keep their own review
        # tables, so without this check a survey run would quietly overwrite hand-labelled
        # axle classes with model guesses, which is the worst possible direction for an
        # automated pass to move a number.
        if _already_human(video_id, c["track_id"]):
            # Reported, not silently dropped: "0 applied" and "0 applied because a person
            # had already answered all of them" look identical otherwise, and only one of
            # them means the pass is working.
            skipped_human += 1
            continue

        pred, conf = None, None
        if c["box_w"] >= min_box_w:
            with torch.no_grad():
                p = torch.softmax(model(tf(Image.open(cp).convert("RGB"))
                                        .unsqueeze(0).to(dev)), dim=1)[0]
            j = int(p.argmax())
            pred, conf = classes[j], float(p[j])
        else:
            skipped_small += 1

        db.run("""INSERT INTO axle_checks
                  (video_id,track_id,frame,det_class,box_w,crop_path,ctx_path,
                   pred,confidence,model_id,created)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(video_id,track_id) DO UPDATE SET
                    pred=excluded.pred, confidence=excluded.confidence,
                    model_id=excluded.model_id, crop_path=excluded.crop_path,
                    ctx_path=excluded.ctx_path, box_w=excluded.box_w""",
               video_id, c["track_id"], c["frame"], c["det_class"], c["box_w"],
               cp, xp, pred, conf, row["id"], time.time())

        if pred and conf >= conf_floor and pred in TO_CLASS:
            new = CLASSES.index(TO_CLASS[pred])
            if new == c["det_class"]:
                unchanged += 1
            else:
                moves[(CLASSES[c["det_class"]], TO_CLASS[pred])] += 1
                if apply:
                    db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
                           new, video_id, c["track_id"])
                    db.run("""UPDATE axle_checks SET applied=?, applied_by='model'
                              WHERE video_id=? AND track_id=?""",
                           TO_CLASS[pred], video_id, c["track_id"])
                applied += 1
        else:
            queued += 1
        if job_id and i % 5 == 0:
            db.run("UPDATE jobs SET progress=? WHERE id=?",
                   round(100 * (i + 1) / len(cands), 1), job_id)

    # Which head ran, under which thresholds. Once a head can be per-station, a number in
    # a report cannot be reproduced without knowing what produced it -- the same reason
    # model_id is stamped on every track. Reported here and written into axle_checks.
    return {"checked": len(cands), "applied": applied, "needs_review": queued,
            "too_small": skipped_small, "already_right": unchanged,
            "already_answered_by_human": skipped_human,
            "model_id": row["id"], "model_accuracy": row["accuracy"],
            "scope": row["scope"], "site_id": site_id,
            "min_box_w": min_box_w, "conf_floor": conf_floor,
            "moves": [{"from": a, "to": b, "n": n} for (a, b), n in moves.most_common()]}


def _already_human(video_id, track_id):
    """Has a person answered this track, in either app?"""
    r = db.one("SELECT human FROM axle_checks WHERE video_id=? AND track_id=?",
               video_id, track_id)
    if r and r["human"]:
        return True
    if db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_attr_samples'"):
        r = db.one("""SELECT human FROM lab_attr_samples
                      WHERE attribute=? AND video_id=? AND track_id=? AND human IS NOT NULL""",
                   ATTR, video_id, track_id)
        if r:
            return True
    # And the CLIP VERIFICATION screen, which is where most human calls actually land.
    # It writes clip_verdicts + tracks.class_override and leaves axle_checks.human NULL,
    # so a track a person had already settled looked untouched here. Harmless while the
    # pass was manual; now that it runs automatically after every extraction it would
    # silently overwrite a human answer on the next run.
    if db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='clip_verdicts'"):
        if db.one("SELECT 1 FROM clip_verdicts WHERE video_id=? AND track_id=? "
                  "AND kind IN ('class','reject')", video_id, track_id):
            return True
    return False


def queue(video_id):
    """Trucks a person still has to settle -- big enough to judge, model unsure.

    Resolved with this station's thresholds, so the review queue matches what the pass
    actually did here. Reading the global floor while the pass ran a station override
    would list vehicles the pass never looked at, and hide ones it skipped.
    """
    init()
    site_id = site_of(video_id)
    rows = db.rows("""SELECT * FROM axle_checks
                      WHERE video_id=? AND human IS NULL
                        AND box_w >= ?
                        AND (pred IS NULL OR confidence < ?)
                      ORDER BY box_w DESC""", video_id,
                   setting("axle_min_box_w", MIN_BOX_W, site_id),
                   setting("axle_conf_floor", CONF_FLOOR, site_id))
    for r in rows:
        r["det"] = CLASSES[r["det_class"]] if r["det_class"] is not None else None
    return rows


def verdict(video_id, track_id, value):
    """A person's answer: applied to the count, and sent back to the Lab as training data.

    The second half is the point. A surveyor only ever sees the vehicles the model was
    unsure about or got visibly wrong, so their corrections are the highest-value labels
    in the whole system -- and without this they would land in the survey app's own table
    and never reach training, leaving the model to repeat exactly the mistakes a person
    had already fixed. The two vehicles that prompted this were not in the Lab's store at
    all: correcting them would have improved one report and taught nothing.

    The Lab still owns training. This only deposits the label; nothing here decides when
    or whether a new model is built.
    """
    init()
    if value not in ANSWERS:
        raise ValueError(f"answer must be one of {ANSWERS}")
    name = TO_CLASS.get(value)
    db.run("""UPDATE axle_checks SET human=?, human_at=?, applied=?, applied_by='human'
              WHERE video_id=? AND track_id=?""",
           value, time.time(), name, video_id, track_id)
    if name:
        db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
               CLASSES.index(name), video_id, track_id)
    return {"ok": True, "applied": name, "sent_to_lab": _to_lab(video_id, track_id, value)}


def _to_lab(video_id, track_id, value):
    """Deposit this answer in the Lab's label store, crop and all.

    Uses the crop the person actually judged rather than re-rendering one: a label only
    means anything against the image it was given, and a later frame-choice change would
    otherwise silently re-point it at a different picture.
    """
    if not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_attr_samples'"):
        return False
    c = db.one("SELECT * FROM axle_checks WHERE video_id=? AND track_id=?",
               video_id, track_id)
    if not c:
        return False
    db.run("""INSERT INTO lab_attr_samples
              (attribute,video_id,track_id,frame,det_class,box_w,box_h,
               crop_path,ctx_path,human,human_at,source,created)
              VALUES (?,?,?,?,?,?,NULL,?,?,?,?, 'survey', ?)
              ON CONFLICT(attribute,video_id,track_id) DO UPDATE SET
                human=excluded.human, human_at=excluded.human_at,
                crop_path=excluded.crop_path, ctx_path=excluded.ctx_path""",
           ATTR, video_id, track_id, c["frame"], c["det_class"], c["box_w"],
           c["crop_path"], c["ctx_path"], value, time.time(), time.time())
    return True


def state(video_id):
    """What the pipeline node shows: has this run, and what is outstanding."""
    init()
    lab = 0
    if db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_attr_samples'"):
        lab = (db.one("""SELECT COUNT(*) n FROM lab_attr_samples
                         WHERE attribute=? AND video_id=? AND human IS NOT NULL""",
                      ATTR, video_id) or {}).get("n") or 0
    _site = site_of(video_id)
    _mw = setting("axle_min_box_w", MIN_BOX_W, _site)
    _cf = setting("axle_conf_floor", CONF_FLOOR, _site)
    n = db.one("""SELECT COUNT(*) total,
                    SUM(human IS NOT NULL) human,
                    SUM(applied_by='model') auto,
                    SUM(human IS NULL AND box_w >= ? AND
                        (pred IS NULL OR confidence < ?)) pending,
                    SUM(box_w < ?) too_small
                  FROM axle_checks WHERE video_id=?""",
               _mw, _cf, _mw, video_id) or {}
    m = current_model(_site)
    return {"total": n.get("total") or 0,
            "human": (n.get("human") or 0) + lab, "human_in_lab": lab,
            "auto": n.get("auto") or 0, "pending": n.get("pending") or 0,
            "too_small": n.get("too_small") or 0,
            "model": ({"id": m["id"], "accuracy": m["accuracy"],
                       "macro_f1": m["macro_f1"]} if m else None)}
