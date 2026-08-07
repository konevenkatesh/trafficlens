"""Clip-level verification: settle every vehicle the count is about to report.

This is where a survey is made correct. Everything upstream — the detector, the judges,
the axle model — produces a best guess; this is the one screen where a person looks at the
vehicle and says what it is, and the number changes underneath them.

Three things it does that a generic review queue does not:

**It verifies what was COUNTED, not what was detected.** A clip has thousands of
detections and a few hundred crossings. Only the crossings reach the report, so only they
are worth a person's attention, and every verdict here moves a number rather than
adjusting a training set.

**Class and attribute are one question.** A bus is `Bus` plus "is it APSRTC"; a car is
`Car_Jeep_Van` plus "is it a yellow-plate taxi". Asking those in separate passes means
seeing the same vehicle twice, and the second pass is the one that never gets done. Here
the answer list carries both, and the code sorts out which column it lands in.

**Mandatory is a computed set, not a mood.** Some vehicles have to be looked at: the ones
carrying the most PCU, the ones the models disagreed about, the ones a classifier answered
below its own floor. `mandatory()` names them so "review what matters" is a button rather
than a judgement call, and "review everything" is still there when there is time.
"""
import os
import sys
import time
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))

from engine import CLASSES  # noqa: E402

# Written at runtime, so it must never be inside a PyInstaller bundle: that unpacks to a
# temp directory which is deleted on exit, and under Program Files it is read-only.
# TRAFFICLENS_DATA is set by the packaged launcher; unset means running from the repo.
CROP_DIR = Path(os.environ.get("TRAFFICLENS_DATA") or ROOT) / "lab_gold" / "_verify"

# The answer list. Classes replace what the vehicle IS; attributes add a fact about it and
# leave the class alone -- an APSRTC bus is still a Bus in the vehicle count and only
# changes which proforma column it also lands in.
ATTR_ANSWERS = {
    "taxi":   {"label": "Taxi (yellow plate)", "attr": "taxi",   "value": "taxi",
               "parents": ["Car_Jeep_Van"]},
    "apsrtc": {"label": "Govt / APSRTC bus",   "attr": "apsrtc", "value": "apsrtc",
               "parents": ["Bus", "Mini_Bus"]},
    "maxi":   {"label": "7-seater / maxi",     "attr": "maxi",   "value": "maxi",
               "parents": ["3W_Auto"]},
}
OTHER_ANSWERS = {
    "not_a_vehicle": "Not a vehicle",
    "unclear": "Can't tell",
}

# Heavy vehicles carry 3-4.5 PCU against a car's 1.0, so one wrong heavy call moves the
# report as much as three wrong cars. They are reviewed whether or not anything flagged
# them.
HEAVY = {"2Axle_Truck", "3Axle_Truck", "MAV", "Bus", "Mini_Bus", "Tractor_Trailer"}
MIN_BOX_W = 60          # below this there is nothing for a person to see either

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER, track_id INTEGER,
  was TEXT, answer TEXT, kind TEXT,          -- kind: class | attribute | reject | skip
  applied_class INTEGER, applied_attr TEXT,
  created REAL,
  revisions INTEGER DEFAULT 0,
  UNIQUE(video_id, track_id));
CREATE INDEX IF NOT EXISTS ix_cv_video ON clip_verdicts(video_id);

-- Re-verification must leave a trace without duplicating the vehicle. `clip_verdicts`
-- stays exactly one row per (video, track) -- the answer that counts, the thing every
-- report reads -- and every CHANGE to it is appended here instead. So a vehicle is never
-- listed twice in a queue or counted twice in a total, and "what did this used to be, and
-- when did it change" is still answerable.
CREATE TABLE IF NOT EXISTS clip_verdict_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER, track_id INTEGER,
  from_answer TEXT, to_answer TEXT, at REAL);
CREATE INDEX IF NOT EXISTS ix_cvl_track ON clip_verdict_log(video_id, track_id);
"""


def init():
    db.conn().executescript(SCHEMA)
    try:                                   # existing installs predate `revisions`
        db.conn().execute("ALTER TABLE clip_verdicts ADD COLUMN revisions INTEGER DEFAULT 0")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    db.conn().commit()


def _reasons(video_id):
    """Why each track is mandatory, keyed by track_id. Empty set = optional."""
    out = {}
    # the axle classifier declined or answered below its floor
    if db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='axle_checks'"):
        import axle_pass
        for r in db.rows("""SELECT track_id, pred, confidence, box_w FROM axle_checks
                            WHERE video_id=? AND human IS NULL""", video_id):
            if r["box_w"] >= axle_pass.MIN_BOX_W and (
                    r["pred"] is None or (r["confidence"] or 0) < axle_pass.CONF_FLOOR):
                out.setdefault(r["track_id"], []).append("axle model unsure")
    # The judge ensemble could not agree. Guarded, because lab_crops belongs to the Lab
    # and does not exist at all in a survey-only install -- where this module is still
    # very much in use.
    if db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_crops'"):
        for r in db.rows("""SELECT track_id FROM lab_crops
                            WHERE video_id=? AND state='contested'""", video_id):
            out.setdefault(r["track_id"], []).append("judges disagreed")
    return out


def queue(video_id, only_class=None, mandatory_only=False, limit=400, answered=None):
    """Counted vehicles and their verdicts, largest and most consequential first.

    `answered` filters the list: None is everything, True only what a person has already
    ruled on, False only what is left. The answered view is the one that was missing --
    a verdict went in and became unreachable, so a mistake could not be found, let alone
    corrected. Nothing about a verdict is final: `verdict()` upserts, so re-answering a
    vehicle overwrites the previous call and the count moves with it.
    """
    init()
    import counting
    import sites
    lines, _ = sites.lines_for(video_id)
    if not lines:
        return {"items": [], "error": "this clip has no count line"}
    r = counting.count_video(video_id, lines)

    done = {x["track_id"]: x for x in
            db.rows("SELECT * FROM clip_verdicts WHERE video_id=?", video_id)}
    # The attributes already on each track. The card has to show these: an attribute is
    # the one answer that is not visible from the class, so without them a reviewer cannot
    # see that a car is already marked taxi, and has no way to take it back.
    attrs_of = {}
    for a in db.rows("SELECT track_id, attr FROM track_attrs "
                     "WHERE video_id=? AND source='human'", video_id):
        attrs_of.setdefault(a["track_id"], []).append(a["attr"])
    why = _reasons(video_id)

    seen, items = set(), []
    for e in r["events"]:
        tid = e["track_id"]
        if tid in seen:
            continue                       # one row per vehicle, not per crossing
        seen.add(tid)
        cls = e["class"]
        if only_class and cls != only_class:
            continue
        box = db.one("""SELECT frame, x1, y1, x2, y2, (x2-x1) w FROM track_points
                        WHERE video_id=? AND track_id=? ORDER BY w DESC LIMIT 1""",
                     video_id, tid)
        if not box or box["w"] < MIN_BOX_W:
            continue
        reasons = list(why.get(tid, []))
        if cls in HEAVY:
            reasons.append("heavy vehicle — high PCU")
        if mandatory_only and not reasons:
            continue
        prior = done.get(tid)
        if answered is True and not prior:
            continue
        if answered is False and prior:
            continue
        items.append({
            "track_id": tid, "class": cls, "clock": e["clock"],
            "direction": e["direction"], "frame": box["frame"],
            "box": [box["x1"], box["y1"], box["x2"], box["y2"]],
            "box_w": int(box["w"]),
            "mandatory": bool(reasons), "reasons": reasons,
            "verdict": (prior or {}).get("answer"),
            # What the model had said before a person changed it, so a review screen can
            # show "AI said Car, you said LCV" rather than just the final answer.
            "was": (prior or {}).get("was"),
            "kind": (prior or {}).get("kind"),
            "answered_at": (prior or {}).get("created"),
            "revisions": (prior or {}).get("revisions") or 0,
            "attrs": attrs_of.get(tid, []),
        })

    items.sort(key=lambda x: (not x["mandatory"], -x["box_w"]))
    counts = {}
    for e in r["events"]:
        counts[e["class"]] = counts.get(e["class"], 0) + 1
    # Who and what this queue belongs to. A verification screen that only says "clip 4"
    # gives the reviewer no way to check they are on the right footage — and the whole
    # value of their answers depends on that.
    v = db.one("""SELECT v.name, v.start_clock, v.frames, v.fps, v.site_id,
                         s.code, s.name AS station
                  FROM videos v LEFT JOIN sites s ON s.id=v.site_id
                  WHERE v.id=?""", video_id) or {}
    mins = (v.get("frames") or 0) / (v.get("fps") or 25) / 60
    end = None
    if v.get("start_clock"):
        from datetime import datetime, timedelta
        try:
            end = (datetime.strptime(v["start_clock"], "%Y-%m-%d %H:%M:%S")
                   + timedelta(minutes=mins)).strftime("%H:%M")
        except ValueError:
            pass
    clip = {"video_id": video_id, "name": v.get("name"),
            "site_id": v.get("site_id"), "station_code": v.get("code"),
            "station": v.get("station"), "date": (v.get("start_clock") or "")[:10],
            "clock": (v.get("start_clock") or "")[11:16], "end_clock": end,
            "minutes": round(mins, 1)}
    return {"clip": clip, "items": items[:limit], "total": len(items),
            "answered": len([i for i in items if i["verdict"]]),
            "mandatory": len([i for i in items if i["mandatory"]]),
            "mandatory_left": len([i for i in items
                                   if i["mandatory"] and not i["verdict"]]),
            "classes": sorted(counts.items(), key=lambda kv: -kv[1]),
            "answers": answers()}


def answers():
    """What a person may say — classes and attributes in one list, as they see it."""
    return {
        "classes": [c for c in CLASSES],
        "attributes": [{"key": k, **v} for k, v in ATTR_ANSWERS.items()],
        "other": [{"key": k, "label": v} for k, v in OTHER_ANSWERS.items()],
    }


def _extracted_at(video_id):
    """When this video's current tracks were produced — the cache's expiry stamp."""
    r = db.one("""SELECT MAX(COALESCE(finished, started)) t FROM jobs
                  WHERE video_id=? AND kind='extract'""", video_id)
    return (r or {}).get("t") or 0


def crop(video_id, track_id):
    """The clearest frame of this vehicle, cropped, plus the frame it came from."""
    import cv2
    v = db.one("SELECT path FROM videos WHERE id=?", video_id)
    b = db.one("""SELECT frame, x1, y1, x2, y2, (x2-x1) w FROM track_points
                  WHERE video_id=? AND track_id=? ORDER BY w DESC LIMIT 1""",
               video_id, track_id)
    if not v or not b:
        return None, None
    d = CROP_DIR / str(video_id)
    d.mkdir(parents=True, exist_ok=True)
    cp, xp = d / f"t{track_id}.jpg", d / f"t{track_id}_ctx.jpg"
    # A cached crop is only valid for the extraction that produced its track. Both keys
    # get reused -- ByteTrack re-numbers every track on a re-extract, and a video id is
    # reused when a station is reset and re-segmented -- so the cache silently served the
    # WRONG VEHICLE, and after a reset the wrong clip entirely: 13:00-13:15 was showing
    # crops cut from the 20:28 night clip that previously held the same id. Anything older
    # than the latest completed extraction for this video is regenerated.
    if cp.exists() and xp.exists() and cp.stat().st_mtime >= _extracted_at(video_id):
        return str(cp), str(xp)
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, b["frame"])
    ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        return None, None
    H, W = img.shape[:2]
    x1, y1, x2, y2 = b["x1"], b["y1"], b["x2"], b["y2"]
    mw, mh = (x2 - x1) * 0.25, (y2 - y1) * 0.25
    c = img[int(max(0, y1 - mh)):int(min(H, y2 + mh)),
            int(max(0, x1 - mw)):int(min(W, x2 + mw))]
    if c.size:
        if c.shape[1] < 420:
            k = 420 / c.shape[1]
            c = cv2.resize(c, (420, max(1, int(c.shape[0] * k))), interpolation=cv2.INTER_CUBIC)
        cv2.imwrite(str(cp), c, [cv2.IMWRITE_JPEG_QUALITY, 92])
    ctx = img.copy()
    cv2.rectangle(ctx, (int(x1), int(y1)), (int(x2), int(y2)), (80, 240, 120), 3)
    k = 1280 / ctx.shape[1]
    if k < 1:
        ctx = cv2.resize(ctx, (1280, int(ctx.shape[0] * k)))
    cv2.imwrite(str(xp), ctx, [cv2.IMWRITE_JPEG_QUALITY, 86])
    return str(cp), str(xp)


def verdict(video_id, track_id, answer):
    """Record a person's answer and move the count with it, immediately.

    The write is the point. A verification screen whose answers land in a review table and
    nowhere else lets somebody spend an hour correcting a survey that still reports the
    original numbers — which happened here once already, with the axle labels.
    """
    init()
    prior = db.one("SELECT answer, revisions FROM clip_verdicts "
                   "WHERE video_id=? AND track_id=?", video_id, track_id)
    cur = db.one("SELECT cls, class_override FROM tracks WHERE video_id=? AND track_id=?",
                 video_id, track_id)
    was = CLASSES[cur["class_override"] if cur and cur["class_override"] is not None
                  else cur["cls"]] if cur else None

    held = {r["attr"] for r in
            db.rows("SELECT attr FROM track_attrs WHERE video_id=? AND track_id=? "
                    "AND source='human'", video_id, track_id)}

    kind, applied_class, applied_attr = "skip", None, None
    if answer in CLASSES:
        kind, applied_class = "class", CLASSES.index(answer)
        db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
               applied_class, video_id, track_id)
        # An attribute belongs to a class. Reclassify a "taxi" as an LCV and the taxi mark
        # is not merely stale, it is false -- and it stayed in track_attrs, so the report
        # card counted a taxi the reviewer had already taken back. Attributes the new
        # class cannot carry go with the class.
        for at in held:
            if answer not in ATTR_ANSWERS.get(at, {}).get("parents", []):
                db.run("DELETE FROM track_attrs WHERE video_id=? AND track_id=? AND attr=?",
                       video_id, track_id, at)
    elif answer in ATTR_ANSWERS:
        a = ATTR_ANSWERS[answer]
        kind, applied_attr = "attribute", a["attr"]
        if a["attr"] in held:
            # Pressing a set attribute takes it back. Without this the button is one-way:
            # a mis-pressed "Taxi" could never be undone from the screen that made it.
            # What is left is the plain class, so that is what the verdict becomes --
            # recording "you answered taxi" on a track with no taxi mark would be a lie.
            db.run("DELETE FROM track_attrs WHERE video_id=? AND track_id=? AND attr=?",
                   video_id, track_id, a["attr"])
            answer, applied_attr = was, None
            kind, applied_class = "class", (CLASSES.index(was) if was in CLASSES else None)
        else:
            # Columns exactly as the survey app defined them — this table predates the Lab
            # and both apps read it, so inventing a column here breaks the writer, not the
            # schema. (It did: an invented `created` made every attribute verdict fail.)
            db.run("""INSERT INTO track_attrs (video_id,track_id,attr,value,source)
                      VALUES (?,?,?,?, 'human')
                      ON CONFLICT(video_id,track_id,attr) DO UPDATE SET
                        value=excluded.value, source='human'""",
                   video_id, track_id, a["attr"], a["value"])
            # Pressing "Govt / APSRTC bus" also says "and it IS a bus". Record that as a
            # class confirmation, otherwise the dataset falls back to the detector's own
            # guess and cannot tell a Bus a person checked from a Bus nobody looked at.
            # It stays a Bus either way — the attribute never becomes a class.
            if was in CLASSES:
                db.run("UPDATE tracks SET class_override=? "
                       "WHERE video_id=? AND track_id=? AND class_override IS NULL",
                       CLASSES.index(was), video_id, track_id)
    elif answer == "not_a_vehicle":
        # -1 is what counting reads as "do not count this at all".
        kind, applied_class = "reject", -1
        db.run("UPDATE tracks SET class_override=-1 WHERE video_id=? AND track_id=?",
               video_id, track_id)
        # Nothing that is not a vehicle is an APSRTC bus.
        db.run("DELETE FROM track_attrs WHERE video_id=? AND track_id=? AND source='human'",
               video_id, track_id)
    elif answer == "unclear":
        kind = "skip"          # leaves whatever the models decided, and says so
    else:
        raise ValueError(f"unknown answer {answer!r}")

    # A revision is a person changing their mind, and only that. Two writes are NOT a
    # change of mind: re-confirming the same answer, and the second half of the two-step
    # reclassify (press Bus, then press "Govt / APSRTC bus") — that attribute refines the
    # class just set, it does not overturn it. Counting either makes the "changed N
    # time(s)" chip on the card claim a correction that never happened.
    # Taking an attribute back, though, IS a change of mind and is recorded as one.
    refines = (kind == "attribute" and prior
               and prior["answer"] in ATTR_ANSWERS[answer]["parents"])
    revised = bool(prior) and prior["answer"] != answer and not refines

    db.run("""INSERT INTO clip_verdicts
              (video_id,track_id,was,answer,kind,applied_class,applied_attr,created)
              VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(video_id,track_id) DO UPDATE SET
                answer=excluded.answer, kind=excluded.kind,
                applied_class=excluded.applied_class,
                applied_attr=excluded.applied_attr, created=excluded.created,
                revisions=clip_verdicts.revisions+?""",
           video_id, track_id, was, answer, kind, applied_class, applied_attr, time.time(),
           1 if revised else 0)
    if revised:
        db.run("""INSERT INTO clip_verdict_log (video_id,track_id,from_answer,to_answer,at)
                  VALUES (?,?,?,?,?)""",
               video_id, track_id, prior["answer"], answer, time.time())
    return {"ok": True, "was": was, "answer": answer, "kind": kind,
            "changed_from": prior["answer"] if revised else None}


def state(video_id):
    """How much of this clip is settled — and how right the model turned out to be.

    The accuracy figure is the most valuable thing a verification pass produces, and it
    is free: every card already records what the model said (`was`) and what the person
    answered. Measured on the vehicles that were actually counted, it is the detector's
    real-world score for this station, which no validation split can give you.

    `unchanged / total` and not "agreement": a person confirming the model is agreement,
    a person changing it is a measured failure, and the two must not be blurred.
    """
    init()
    n = db.one("""SELECT COUNT(*) done, SUM(kind='class') reclassed,
                    SUM(kind='attribute') attrs, SUM(kind='reject') rejected
                  FROM clip_verdicts WHERE video_id=?""", video_id) or {}
    # Only class verdicts score the detector. An attribute answer adds a fact without
    # contradicting the class, and a "can't tell" is not evidence either way.
    a = db.one("""SELECT COUNT(*) n, SUM(was = answer) ok
                  FROM clip_verdicts WHERE video_id=? AND kind='class'""", video_id) or {}
    judged = a.get("n") or 0
    ok = a.get("ok") or 0
    return {"verified": n.get("done") or 0, "reclassed": (judged - ok),
            "attributes": n.get("attrs") or 0, "rejected": n.get("rejected") or 0,
            "scored": judged, "model_right": ok,
            "model_accuracy": round(ok / judged, 3) if judged else None}


def wrong_calls(video_id=None, site_id=None):
    """Every vehicle a person disagreed with the model about — the failure list.

    This is the training set the station actually needs: not a random sample, but the
    specific vehicles the current model gets wrong, each with the image it got wrong.
    """
    q = """SELECT v.video_id, v.track_id, v.was, v.answer, v.kind
           FROM clip_verdicts v WHERE v.kind='class' AND v.was != v.answer"""
    args = []
    if video_id:
        q += " AND v.video_id=?"
        args.append(video_id)
    elif site_id:
        q += " AND v.video_id IN (SELECT id FROM videos WHERE site_id=?)"
        args.append(site_id)
    return db.rows(q + " ORDER BY v.created DESC", *args)
