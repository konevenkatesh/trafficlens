"""Count the axles, instead of inheriting a guess about them.

The 2Axle / 3Axle / MAV split is the one classification the existing judge layer cannot
touch. `judge.PROMPT` tells every judge "do NOT try to count axles", and `to_class_id`
then resolves a `Heavy_Truck` verdict to `det_class if det_class in AXLE_CLASSES else
HEAVY_DEFAULT`. So a judged heavy truck carries either the detector's own axle call or the
constant 8 -- with a judge's stamp on it either way. Of the 82 heavy crops the Lab has
labelled, 45 echoed the detector and 37 were the fallback constant. None were counted.

That is not a bias the training data happens to have; it is a bias the labelling pipeline
manufactures, and it compounds every round, because next round's model learns this round's
manufactured labels.

This module asks the question that was being skipped. Three things make it a measurement
rather than another rubber stamp:

**The judge never sees the detector's class.** A verdict that can be anchored is not
independent evidence, and the whole point is to find out whether the detector is wrong.

**"Not a truck" is an available answer.** 37 of the current `2Axle_Truck` labels are LCVs,
autos, cars and buses that were promoted by the fallback. A pass that can only choose
between 2, 3 and 4+ axles would relabel every one of them as some kind of truck and call
the job done.

**The frame is chosen for axle visibility, not for size.** Axles are only countable when
the vehicle is side-on, so the widest box in the track wins -- a head-on truck at twice the
pixel height shows one axle no matter how big it is.

Unanimity auto-accepts; anything less goes to a human, because a split vote on "how many
wheels does this have" means the picture does not actually show it.
"""
import json
import re
import time
from pathlib import Path

import db
import providers
from engine import CLASSES

AXLE_CLASSES = {8: "2Axle_Truck", 9: "3Axle_Truck", 10: "MAV"}
JUDGES = [
    "qwen/qwen3-vl-32b-instruct",
    "google/gemini-2.5-flash-lite",
    "mistralai/mistral-small-3.2-24b-instruct",
]

# What an answer may be. "unclear" is deliberately available: a judge that must pick a
# number will pick one, and a forced guess recorded as a measurement is worse than a gap.
VOCAB = ("2_axle", "3_axle", "4_or_more_axle", "not_a_truck", "unclear")

TO_CLASS = {"2_axle": 8, "3_axle": 9, "4_or_more_axle": 10}

PROMPT = """You are auditing axle counts for an Indian road traffic survey.

The first image is a cropped vehicle; the second is the full frame for context.

Count the axles you can actually see — an axle is one position along the vehicle where wheels touch the road. Count the tractor and its trailer together as one vehicle.

- 2_axle — two wheel positions: one front, one rear. Most rigid lorries, tippers and small trucks.
- 3_axle — three wheel positions, usually a single front and a closely-spaced rear pair.
- 4_or_more_axle — four or more wheel positions, or an articulated tractor-trailer / multi-axle rig.
- not_a_truck — this is not a large goods truck at all: a car, van, pickup, small goods carrier (Tata Ace / 407 style), auto-rickshaw, bus, tractor, motorcycle, or not a vehicle.
- unclear — it is a large truck but the axles genuinely cannot be counted from these images (occluded, head-on, too dark, cut off).

Do not guess. If you cannot see the wheels, answer unclear.

Reply with ONLY this JSON, nothing else:
{"answer": "<one option>", "axles_seen": <integer or null>, "confidence": <0.0-1.0>}"""


# Version 2, written after scoring version 1 against 107 human-settled tracks.
#
# v1 asked for "axles" and got 4_or_more_axle for 84 vehicles a person called 3-axle. The
# cause is that the abstraction does not survive contact with the picture: an Indian
# 10-wheeler's rear bogie is two axles of dual tyres sitting close together, which reads
# as four or five separate tyres from the side. Asked to count axles, the models counted
# what they could actually see -- tyres -- and the extra ones came from the duals.
#
# So v2 asks for the thing that is visible, then states the arithmetic that turns it into
# an axle count, instead of expecting the model to do that conversion silently. The
# wheel-count vocabulary (6-wheeler, 10-wheeler) is how these trucks are described locally
# and is likely to be well represented in the models' training data.
PROMPT_V2 = """You are counting axles on Indian trucks for a road traffic survey.

The first image is a cropped vehicle; the second is the full frame for context.

Look at the side of the vehicle and find each place where wheels touch the road, from the front bumper to the back. Work along the vehicle and count those GROUPS.

Critical: a rear axle almost always carries TWO tyres side by side (a "dual"). Both tyres sit at the same place along the vehicle, so they are ONE axle, not two. Count positions along the length of the truck — not the number of round tyres you can see.

Indian trucks are usually one of these:
- 6-wheeler = 2 axles — one wheel group at the front, one at the back. A single rear group.
- 10-wheeler = 3 axles — one at the front, then TWO rear groups sitting close together (a rear bogie). This is the most common medium/heavy truck and tipper on these roads.
- 12-wheeler = 4 axles — one or two at the front, plus a rear bogie of two or three groups.
- Articulated tractor-trailer — a separate cab pulling a trailer on its own wheels: usually 5 or 6 axles.

Two rear groups close together is 3 axles, NOT 4 or more. Only answer 4_or_more_axle if you can count four or more distinct wheel groups along the length, or the vehicle is clearly an articulated tractor-trailer.

Answer with one of:
- 2_axle — two wheel groups (6-wheeler)
- 3_axle — three wheel groups (10-wheeler, incl. most tippers)
- 4_or_more_axle — four or more groups, or an articulated tractor-trailer rig
- not_a_truck — a car, van, pickup, small goods carrier (Tata Ace / 407), auto-rickshaw, bus, tractor, motorcycle, or not a vehicle at all
- unclear — a large truck whose wheels genuinely cannot be made out (occluded, head-on, too dark, cut off)

State how many wheel groups you counted in axles_seen. Do not guess: if the wheels are not visible, answer unclear.

Reply with ONLY this JSON, nothing else:
{"answer": "<one option>", "axles_seen": <integer or null>, "confidence": <0.0-1.0>}"""

# Version 3. v2 fixed the tyre/axle confusion and gained 21 points on the best judge, but
# it also carried a sentence calling the 10-wheeler "the most common truck on these roads".
# That is a prior, not evidence, and the models obediently applied it: `2_axle` misread as
# `3_axle` went to 63, the new top error. A frequency hint in a counting prompt is just a
# thumb on the scale, so v3 drops it and spends the words on the actual 2-vs-3 decision --
# whether there is one wheel group behind the body or two.
PROMPT_V3 = """You are counting axles on Indian trucks for a road traffic survey.

The first image is a cropped vehicle; the second is the full frame for context.

Look along the side of the vehicle, front to back, and count the places where wheels meet the road.

Critical: a rear axle usually carries TWO tyres side by side (a "dual"). Both sit at the same point along the truck's length, so they are ONE axle. Count positions along the length — not round tyres.

The decision that matters most is at the REAR. Look behind the cargo body:
- ONE wheel group at the rear → 2 axles total (one front, one rear).
- TWO wheel groups at the rear, close together → 3 axles total.
- THREE or more groups at the rear, or a separate cab towing a trailer on its own wheels → 4 or more.

Do not assume any of these is more common than the others — count what is in the picture.

Answer with one of:
- 2_axle — one front group, one rear group
- 3_axle — one front group, two rear groups
- 4_or_more_axle — four or more groups in total, or an articulated tractor-trailer rig
- not_a_truck — a car, van, pickup, small goods carrier (Tata Ace / 407), auto-rickshaw, bus, tractor, motorcycle, or not a vehicle at all
- unclear — a large truck whose wheels genuinely cannot be made out (occluded, head-on, too dark, cut off)

State the total number of wheel groups you counted in axles_seen. Do not guess: if the wheels are not visible, answer unclear.

Reply with ONLY this JSON, nothing else:
{"answer": "<one option>", "axles_seen": <integer or null>, "confidence": <0.0-1.0>}"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_axle_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER, track_id INTEGER, frame INTEGER,
  det_class INTEGER, crop_path TEXT, ctx_path TEXT,
  box_w INTEGER, box_h INTEGER,
  verdict TEXT, verdict_class INTEGER, agree_n INTEGER, state TEXT,
  human TEXT, human_class INTEGER, cost_usd REAL, created REAL,
  UNIQUE(video_id, track_id));

CREATE TABLE IF NOT EXISTS lab_axle_votes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id INTEGER, model TEXT, answer TEXT, axles_seen INTEGER,
  confidence REAL, raw TEXT, cost_usd REAL, latency_ms INTEGER, error TEXT,
  created REAL);

CREATE INDEX IF NOT EXISTS ix_axle_check ON lab_axle_votes(check_id);
"""

ROOT = Path(__file__).parent.parent
CROP_DIR = ROOT / "lab_gold" / "_axles"


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ───────────────────────────── choosing what to look at ─────────────────────────────
def candidates(video_ids=None):
    """Every track whose class claims an axle count, with its most side-on frame.

    The population is defined by the *claim*, not by what the vehicle turns out to be:
    the question is "of the tracks currently counted as 2Axle/3Axle/MAV, how many really
    are?", and a track that turns out to be an LCV is the most interesting answer there is.
    """
    vids = video_ids or [v["id"] for v in db.rows("SELECT id FROM videos")]
    out = []
    for vid in vids:
        rows = db.rows("""SELECT t.track_id, t.cls, t.class_override
                          FROM tracks t WHERE t.video_id=?""", vid)
        for t in rows:
            cls = t["class_override"] if t["class_override"] is not None else t["cls"]
            if cls not in AXLE_CLASSES:
                continue
            # Widest box, not biggest: axles are only countable side-on, and a head-on
            # truck is tall and narrow no matter how close it is.
            p = db.one("""SELECT frame, x1, y1, x2, y2, (x2-x1) AS w
                          FROM track_points WHERE video_id=? AND track_id=?
                          ORDER BY w DESC LIMIT 1""", vid, t["track_id"])
            if not p:
                continue
            out.append({"video_id": vid, "track_id": t["track_id"], "frame": p["frame"],
                        "det_class": cls, "box": (p["x1"], p["y1"], p["x2"], p["y2"]),
                        "box_w": int(p["x2"] - p["x1"]), "box_h": int(p["y2"] - p["y1"])})
    return out


def render(c, margin=0.10):
    """A crop with room under the vehicle, plus the context frame.

    The margin matters more than usual here: a box drawn tight to the bodywork clips the
    tyres, and clipped tyres are exactly the evidence being asked for.
    """
    import cv2
    v = db.one("SELECT path FROM videos WHERE id=?", c["video_id"])
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
    # Upscale small crops: the judges resize to a fixed budget anyway, and handing them
    # a 120px truck to count wheels on is asking for the "unclear" answer by construction.
    if crop.shape[1] < 480:
        k = 480 / crop.shape[1]
        crop = cv2.resize(crop, (480, int(crop.shape[0] * k)), interpolation=cv2.INTER_CUBIC)

    CROP_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"v{c['video_id']}_t{c['track_id']}"
    cp = CROP_DIR / f"{stem}_crop.jpg"
    cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])

    ctx = img.copy()
    cv2.rectangle(ctx, (int(x1), int(y1)), (int(x2), int(y2)), (80, 220, 120), 3)
    k = 1280 / ctx.shape[1]
    if k < 1:
        ctx = cv2.resize(ctx, (1280, int(ctx.shape[0] * k)))
    xp = CROP_DIR / f"{stem}_ctx.jpg"
    cv2.imwrite(str(xp), ctx, [cv2.IMWRITE_JPEG_QUALITY, 86])
    return str(cp), str(xp)


# ───────────────────────────── asking ─────────────────────────────
def parse(text):
    if not text:
        return None, None, None
    m = re.search(r"\{.*?\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            a = str(d.get("answer", "")).strip().lower()
            if a in VOCAB:
                seen = d.get("axles_seen")
                return a, (int(seen) if isinstance(seen, (int, float)) else None), \
                    float(d.get("confidence") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    for a in sorted(VOCAB, key=len, reverse=True):
        if re.search(rf"\b{re.escape(a)}\b", text, re.I):
            return a, None, None
    return None, None, None


def ask(check_id, crop_path, ctx_path, models=None):
    """Fan one vehicle at every judge. Returns the votes."""
    from judge import _POOL
    models = models or JUDGES
    imgs = [crop_path] + ([ctx_path] if ctx_path else [])

    def call(model):
        txt, usage, cost, ms, err = providers.or_vision(model, PROMPT, imgs, max_tokens=80)
        a, seen, conf = parse(txt)
        return {"model": model, "answer": a, "axles_seen": seen, "confidence": conf,
                "txt": txt, "cost": cost, "ms": ms, "error": err}

    votes = list(_POOL.map(call, models))
    for r in votes:
        db.run("""INSERT INTO lab_axle_votes
                  (check_id,model,answer,axles_seen,confidence,raw,cost_usd,latency_ms,
                   error,created) VALUES (?,?,?,?,?,?,?,?,?,?)""",
               check_id, r["model"], r["answer"], r["axles_seen"], r["confidence"],
               (r["txt"] or "")[:400], r["cost"], r["ms"], r["error"], time.time())
    return votes


def verdict(votes):
    """Unanimity or nothing.

    A split vote on "how many wheels touch the road" is not a close call between two
    defensible readings -- it means the images do not show it. Averaging that into a
    majority would produce exactly the confident-but-unfounded label this module exists
    to remove.
    """
    answers = [v["answer"] for v in votes if v["answer"]]
    if len(answers) < 2:
        return None, "no_answer", 0
    uniq = set(answers)
    if len(uniq) == 1 and len(answers) == len(votes):
        a = answers[0]
        return a, "unanimous", len(answers)
    return None, "split", max(answers.count(a) for a in uniq)


def run(video_ids=None, limit=None, models=None):
    """Measure the axle claim on every heavy track. Idempotent per track."""
    init()
    cands = candidates(video_ids)
    done = {(r["video_id"], r["track_id"]) for r in
            db.rows("SELECT video_id, track_id FROM lab_axle_checks WHERE state IS NOT NULL")}
    todo = [c for c in cands if (c["video_id"], c["track_id"]) not in done]
    if limit:
        todo = todo[:limit]

    spent = 0.0
    for c in todo:
        cp, xp = render(c)
        if not cp:
            continue
        cid = db.run("""INSERT OR REPLACE INTO lab_axle_checks
                        (video_id,track_id,frame,det_class,crop_path,ctx_path,
                         box_w,box_h,created)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     c["video_id"], c["track_id"], c["frame"], c["det_class"],
                     cp, xp, c["box_w"], c["box_h"], time.time())
        votes = ask(cid, cp, xp, models)
        ans, state, agree = verdict(votes)
        cost = sum(v["cost"] or 0 for v in votes)
        spent += cost
        db.run("""UPDATE lab_axle_checks SET verdict=?, verdict_class=?, agree_n=?,
                  state=?, cost_usd=? WHERE id=?""",
               ans, TO_CLASS.get(ans), agree, state, cost, cid)
    if spent:
        db.charge(None, "openrouter", "axle-audit", f"{len(todo)} heavy tracks",
                  len(todo), "track", spent, {})
    return {"checked": len(todo), "already_done": len(cands) - len(todo),
            "cost_usd": round(spent, 4)}


# ───────────────────────────── what it found ─────────────────────────────
def truth_of(r):
    """The best available answer for one track: a human's if there is one."""
    if r["human_class"] is not None:
        return r["human_class"], "human"
    if r["human"] == "not_a_truck":
        return -1, "human"
    if r["state"] == "unanimous":
        if r["verdict"] == "not_a_truck":
            return -1, "judges"
        if r["verdict"] in TO_CLASS:
            return TO_CLASS[r["verdict"]], "judges"
    return None, r["state"]


def matrix(video_ids=None):
    """The confusion matrix the retrain decision should rest on.

    Rows are what the detector claimed; columns are what the axles turned out to be.
    `unresolved` is reported rather than hidden -- a matrix that quietly drops every case
    nobody could settle overstates how much is known.
    """
    init()
    q = "SELECT * FROM lab_axle_checks WHERE state IS NOT NULL"
    args = []
    if video_ids:
        q += f" AND video_id IN ({','.join('?' * len(video_ids))})"
        args = list(video_ids)
    rows = db.rows(q, *args)

    names = {**AXLE_CLASSES, -1: "not_a_truck"}
    cm, unresolved, by_src = {}, [], {"human": 0, "judges": 0}
    for r in rows:
        t, src = truth_of(r)
        if t is None:
            unresolved.append({"video_id": r["video_id"], "track_id": r["track_id"],
                               "det": AXLE_CLASSES.get(r["det_class"]), "why": src})
            continue
        by_src[src] = by_src.get(src, 0) + 1
        key = (AXLE_CLASSES.get(r["det_class"], r["det_class"]), names.get(t, t))
        cm[key] = cm.get(key, 0) + 1

    resolved = sum(cm.values())
    right = sum(n for (d, t), n in cm.items() if d == t)
    # Counted from the table, not from whatever the page happens to be showing: answered
    # tracks drop out of both the queue and the unanimous list, so a UI-side tally of the
    # visible cards silently under-reports the work already done.
    answered = sum(1 for r in rows if r["human"])
    return {
        "total": len(rows), "answered": answered,
        "remaining": len(rows) - answered,
        "resolved": resolved, "unresolved": len(unresolved),
        "unresolved_detail": unresolved[:40], "resolved_by": by_src,
        "accuracy": round(right / resolved, 3) if resolved else None,
        "matrix": [{"det": d, "truth": t, "n": n} for (d, t), n in
                   sorted(cm.items(), key=lambda kv: -kv[1])],
        "claimed": _tally(rows, lambda r: AXLE_CLASSES.get(r["det_class"])),
        "actual": _tally([r for r in rows if truth_of(r)[0] is not None],
                         lambda r: names.get(truth_of(r)[0])),
    }


def _tally(rows, key):
    out = {}
    for r in rows:
        k = key(r)
        if k:
            out[k] = out.get(k, 0) + 1
    return out


def queue():
    """Tracks a human still has to settle: split votes, unclear, and no-answers."""
    init()
    rows = db.rows("""SELECT * FROM lab_axle_checks
                      WHERE human IS NULL AND (state != 'unanimous' OR verdict='unclear')
                      ORDER BY box_w DESC""")
    for r in rows:
        r["votes"] = db.rows("""SELECT model, answer, axles_seen, confidence, error
                                FROM lab_axle_votes WHERE check_id=?""", r["id"])
        r["det"] = AXLE_CLASSES.get(r["det_class"])
    return rows


def resolved():
    """Everything already settled, for spot-checking what the judges decided alone."""
    init()
    rows = db.rows("""SELECT * FROM lab_axle_checks WHERE state='unanimous'
                      ORDER BY box_w DESC""")
    for r in rows:
        r["det"] = AXLE_CLASSES.get(r["det_class"])
        r["truth"], r["truth_src"] = truth_of(r)
        r["truth_name"] = {**AXLE_CLASSES, -1: "not_a_truck"}.get(r["truth"])
        r["changed"] = r["truth"] != r["det_class"]
    return rows


def bakeoff(prompt, models=None, tag="v2", limit=None):
    """Score a prompt against the tracks a person has already settled.

    The human answers are the only ground truth here, and they were given without seeing
    the judges' votes on the same screen -- so a prompt that scores well is agreeing with
    a person, not with a machine that shares its blind spots.

    Nothing is written to `lab_axle_checks`: a bake-off must never be able to overwrite the
    labels it is being scored against.
    """
    from judge import _POOL
    init()
    models = models or JUDGES
    rows = db.rows("""SELECT * FROM lab_axle_checks
                      WHERE human IS NOT NULL AND human != 'unclear' ORDER BY id""")
    if limit:
        rows = rows[:limit]

    def one(args):
        r, m = args
        imgs = [r["crop_path"]] + ([r["ctx_path"]] if r["ctx_path"] else [])
        txt, _u, cost, _ms, err = providers.or_vision(m, prompt, imgs, max_tokens=90)
        a, seen, _c = parse(txt)
        return {"id": r["id"], "model": m, "human": r["human"], "answer": a,
                "seen": seen, "cost": cost or 0, "error": err}

    jobs = [(r, m) for r in rows for m in models]
    out = list(_POOL.map(one, jobs))

    per, cm = {}, {}
    for o in out:
        s = per.setdefault(o["model"], {"ok": 0, "n": 0, "cost": 0.0})
        s["cost"] += o["cost"]
        if not o["answer"]:
            continue
        s["n"] += 1
        s["ok"] += o["answer"] == o["human"]
        if o["answer"] != o["human"]:
            cm[(o["human"], o["answer"])] = cm.get((o["human"], o["answer"]), 0) + 1

    # Ensemble: unanimity, the rule the real pass uses.
    by_check = {}
    for o in out:
        by_check.setdefault(o["id"], []).append(o)
    uok = un = 0
    for cid, vs in by_check.items():
        ans = [v["answer"] for v in vs if v["answer"]]
        if len(ans) == len(vs) and len(set(ans)) == 1:
            un += 1
            uok += ans[0] == vs[0]["human"]

    spent = sum(s["cost"] for s in per.values())
    if spent:
        db.charge(None, "openrouter", f"axle-prompt-{tag}",
                  f"{len(rows)} labelled tracks", len(rows), "track", spent, {})
    return {
        "tag": tag, "tracks": len(rows), "cost_usd": round(spent, 4),
        "per_model": {m: {**s, "pct": round(100 * s["ok"] / s["n"]) if s["n"] else None}
                      for m, s in per.items()},
        "unanimous": {"n": un, "correct": uok,
                      "pct": round(100 * uok / un) if un else None,
                      "coverage": round(100 * un / len(rows)) if rows else 0},
        "errors": sorted([{"truth": t, "said": a, "n": n} for (t, a), n in cm.items()],
                         key=lambda x: -x["n"])[:10],
    }


def set_human(check_id, answer):
    """A person's answer, which overrides the judges and is never overwritten by a re-run."""
    init()
    if answer not in VOCAB:
        raise ValueError(f"answer must be one of {VOCAB}")
    db.run("UPDATE lab_axle_checks SET human=?, human_class=? WHERE id=?",
           answer, TO_CLASS.get(answer) if answer != "not_a_truck" else -1, check_id)
    return True
