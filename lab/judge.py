"""VLM judging: a 3-model ensemble votes on every sampled crop.

Two design decisions come straight from the model evidence:

1. **Diverse families, not the cheapest three.** Qwen3-VL (native-resolution ViT),
   Gemini Flash-Lite (separate tiling pipeline, strict JSON), Mistral Small
   (best instruction-following of the cheap open set). Three pretraining
   corpora -> errors decorrelate. Gemma is excluded: structured output is
   broken for it on OpenRouter.

2. **Judges never count axles.** Axle counting is the weakest measured skill of
   every cheap VLM (10-47% correct). The judge vocabulary collapses 2-axle,
   3-axle and multi-axle into one Heavy_Truck family.

   A Heavy_Truck verdict therefore settles the vehicle type and leaves the axle
   class genuinely unknown, so it resolves to None and the crop moves to the
   `needs_axles` state. It used to resolve to the detector's own axle guess, or
   to the constant `2Axle_Truck` when the detector had no axle opinion -- which
   meant the training set learned the model's axle bias back from itself, harder
   every round. `axles.py` answers the question properly, on purpose-built crops
   with a prompt that does nothing else.
"""
import concurrent.futures as cf
import json
import re
import time
from pathlib import Path

import db
import providers
from pipeline import CLASSES, run_dir, stage_begin, stage_done, stage_set

DEFAULT_JUDGES = [
    "qwen/qwen3-vl-32b-instruct",
    "google/gemini-2.5-flash-lite",
    "mistralai/mistral-small-3.2-24b-instruct",
]

# What a judge is allowed to say. Heavy_Truck deliberately spans the axle classes.
JUDGE_VOCAB = ["2W", "3W_Auto", "Car_Jeep_Van", "LCV", "Mini_Bus", "Bus", "Tractor",
               "Tractor_Trailer", "Heavy_Truck", "Cycle", "Animal_Cart", "Other",
               "Not_A_Vehicle"]
AXLE_CLASSES = {8, 9, 10}          # 2Axle_Truck, 3Axle_Truck, MAV

PROMPT = """You are grading vehicle classification for an Indian road traffic survey.

The first image is a cropped vehicle. The second is the full frame with that vehicle boxed in green — use it only for scale and context.

Choose exactly one class:
- 2W — motorcycle, scooter, moped (riders included)
- 3W_Auto — three-wheeled auto-rickshaw, e-rickshaw, tempo/Magic style passenger three-wheeler
- Car_Jeep_Van — car, jeep, SUV, van, taxi
- LCV — light commercial: small goods pickup, Tata Ace/407, mini goods carrier
- Mini_Bus — small bus, ~20-30 seats, school van bus
- Bus — full-size bus, state transport or private coach
- Tractor — farm tractor with no trailer
- Tractor_Trailer — farm tractor pulling a trailer
- Heavy_Truck — any large goods truck or lorry (do NOT try to count axles)
- Cycle — bicycle, pedal cart
- Animal_Cart — bullock cart, horse cart
- Other — a vehicle that fits none of the above
- Not_A_Vehicle — pedestrian, building, tree, road marking, shadow, or empty road

Reply with ONLY this JSON, nothing else:
{"class": "<one class name>", "confidence": <0.0-1.0>}"""


def judges_for(run_id=None):
    saved = db.get_setting("judge_models")
    if saved:
        try:
            v = json.loads(saved)
            if isinstance(v, list) and v:
                return v
        except json.JSONDecodeError:
            pass
    return DEFAULT_JUDGES


def parse_verdict(text):
    """Models wander outside JSON; recover the class name either way."""
    if not text:
        return None, None
    m = re.search(r"\{.*?\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            c = str(d.get("class", "")).strip()
            if c in JUDGE_VOCAB:
                return c, float(d.get("confidence", 0) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    for c in sorted(JUDGE_VOCAB, key=len, reverse=True):   # longest name first
        if re.search(rf"\b{re.escape(c)}\b", text, re.I):
            return c, None
    return None, None


def to_class_id(name, det_class):
    """Judge vocabulary -> MoRTH class id.

    `Heavy_Truck` deliberately returns None rather than an axle class. The judges are told
    not to count axles, so a Heavy_Truck verdict carries no information about which of
    2Axle / 3Axle / MAV this is -- and the two things this used to return instead were both
    fabrications. `det_class` echoed the detector's own guess back as though a judge had
    confirmed it, and `HEAVY_DEFAULT` invented `2Axle_Truck` out of nothing: of the 82
    heavy crops labelled this way, 45 were echoes and 37 were the constant.

    Both paths fed straight into the training set, so the model learned its own axle bias
    back from its own predictions, more strongly each round. Returning None routes the crop
    to `axles.py`, which counts them, instead.
    """
    if name is None:
        return None
    if name == "Not_A_Vehicle":
        return -1
    if name == "Heavy_Truck":
        return None
    return CLASSES.index(name) if name in CLASSES else None


def is_heavy_verdict(name):
    """A judge said 'large truck' without saying which axle class -- needs axles.run()."""
    return name == "Heavy_Truck"


# One long-lived pool. Creating a pool per crop churned threads, and every new
# thread opened its own SQLite connection to a large WAL database -- that
# contention, not the API, was dominating the loop.
_POOL = cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="judge")


def judge_one(crop, models):
    """Fan the same crop at every judge in parallel.

    Worker threads do HTTP only; every database write happens on the caller's
    thread so the judges never contend for the SQLite writer lock.
    """
    imgs = [crop["crop_path"]] + ([crop["ctx_path"]] if crop.get("ctx_path") else [])

    def call(model):
        txt, usage, cost, ms, err = providers.or_vision(model, PROMPT, imgs, max_tokens=60)
        name, confv = parse_verdict(txt)
        return {"model": model, "class_id": to_class_id(name, crop["det_class"]),
                "name": name, "conf": confv, "txt": txt, "usage": usage or {},
                "cost": cost, "ms": ms, "error": err}

    out = list(_POOL.map(call, models))
    for r in out:
        db.run("""INSERT INTO lab_judgments
              (crop_id,run_id,model,verdict,verdict_name,confidence,raw,
               in_tokens,out_tokens,cost_usd,latency_ms,error,created)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               crop["id"], crop["run_id"], r["model"], r["class_id"], r["name"],
               r["conf"], (r["txt"] or "")[:400], r["usage"].get("prompt_tokens", 0),
               r["usage"].get("completion_tokens", 0), r["cost"], r["ms"],
               r["error"], time.time())
    spent = sum(r["cost"] or 0 for r in out)
    if spent:                       # one ledger row per crop, not one per call
        db.charge(crop["run_id"], "openrouter", "judge", f"{len(models)} judges",
                  1, "crop", spent, {"crop": crop["id"]})
    return out


def consensus(verdicts, det_class):
    """Unanimity is the bar for auto-accepting a label -- but only for the CLASS.

    Re-measured on clean ground truth (`correct`/`reclass` verdicts only, 40 crops,
    3 judges). The earlier figures -- 72% unanimous -- were wrong: that gold set fed
    the judges boxes the human had marked `delete`, labelled with whatever class the
    detector had guessed, so a judge was penalised for correctly saying Not_A_Vehicle.

        WHICH CLASS IS THIS VEHICLE?            unanimous  majority
          class-balanced (rare types)              92.6%     18.2%     n=27
          natural mix (closer to production)      100.0%     45.5%     n=28

        IS THIS A VEHICLE AT ALL?               unanimous  majority
          boxes the human deleted                  23.8%     23.5%     n=21

    So unanimity is an excellent filter for vehicle TYPE and close to worthless for
    vehicle EXISTENCE: on junk boxes -- tree, shadow, lane marking, half an object --
    all three judges cheerfully agree on some vehicle class about half the time. Since
    ~20% of reviewed detections were junk, that is the one path by which bad labels
    still reach a dataset, and no amount of judge agreement will close it. It has to be
    closed before judging, or by a human asked "is this a vehicle?" rather than "what
    type is it?".

    A two-of-three majority stays below 50% either way, so it is not a consensus --
    it is a flag that a human needs to look.
    """
    # "All three said Heavy_Truck" is agreement, not failure: they settled the vehicle
    # type and were explicitly forbidden from settling the axle count. Marking it failed
    # would hide a well-judged crop; the honest state names the question still open.
    heavy = [v for v in verdicts if is_heavy_verdict(v.get("name"))]
    if heavy and len(heavy) == len([v for v in verdicts if v.get("name")]):
        return None, len(heavy), "needs_axles"

    votes = [v["class_id"] for v in verdicts if v["class_id"] is not None]
    if not votes:
        return None, 0, "failed"
    if len(votes) < 2:
        return None, len(votes), "contested"
    tally = {}
    for v in votes:
        tally[v] = tally.get(v, 0) + 1
    best, n = max(tally.items(), key=lambda kv: kv[1])
    if n == len(votes) and len(votes) >= 3:
        return best, n, ("agreed" if best == det_class else "reclass")
    return None, n, "contested"


def run_judge(run_id, limit=None):
    models = judges_for(run_id)
    crops = db.rows("SELECT * FROM lab_crops WHERE run_id=? AND state='new' ORDER BY id",
                    run_id)
    if limit:
        crops = crops[:limit]
    if not crops:
        raise RuntimeError("no unjudged crops -- run the sample stage first")
    bal = providers.or_balance()
    if bal.get("ok") and bal["remaining"] < 0.10:
        raise RuntimeError(f"OpenRouter balance too low (${bal['remaining']:.2f})")
    stage_begin(run_id, "judge", f"{len(crops)} crops x {len(models)} judges")
    spent, agreed, reclass, contested, failed, heavy = 0.0, 0, 0, 0, 0, 0
    for i, c in enumerate(crops):
        verdicts = judge_one(c, models)
        spent += sum(v["cost"] or 0 for v in verdicts)
        final, n, state = consensus(verdicts, c["det_class"])
        db.run("UPDATE lab_crops SET final_class=?, agree_n=?, state=? WHERE id=?",
               final, n, state, c["id"])
        agreed += state == "agreed"
        reclass += state == "reclass"
        contested += state == "contested"
        failed += state == "failed"
        heavy += state == "needs_axles"
        if i % 3 == 0 or i == len(crops) - 1:
            stage_set(run_id, "judge", progress=100.0 * (i + 1) / len(crops),
                      message=f"{i+1}/{len(crops)} · ${spent:.3f} · "
                              f"{agreed} agreed, {reclass} reclassed, {contested} contested, "
                              f"{heavy} awaiting axle count")
        if spent > float(db.get_setting("judge_budget_usd", "1.5")):
            stage_set(run_id, "judge", message=f"stopped at budget cap ${spent:.2f}")
            break
    stage_done(run_id, "judge",
               f"${spent:.3f} · {agreed} agreed · {reclass} reclassed · "
               f"{contested} to human · {heavy} awaiting axle count · {failed} failed",
               {"cost": round(spent, 4), "models": models, "agreed": agreed,
                "reclass": reclass, "contested": contested, "failed": failed,
                "needs_axles": heavy})
    return {"cost": spent, "agreed": agreed, "reclass": reclass, "contested": contested,
            "needs_axles": heavy}


# ────────────────────────── gold-set bake-off ──────────────────────────
GOLD_DIR = Path(__file__).parent.parent / "dataset"


def build_gold(n=60, kind="clean", balanced=True):
    """Ground truth from the 1,661 hand-graded box verdicts.

    `kind` picks which question is being asked, because they are different skills and
    mixing them produced a badly misleading score:

      "clean"  — only boxes the human marked `correct` or `reclass`, i.e. real vehicles
                 with a confirmed class. Measures classification.
      "reject" — only boxes the human marked `delete`, truth = Not_A_Vehicle. Measures
                 whether a judge will refuse a tree, shadow or lane marking.

    The earlier version took `delete` and `ignore` rows as truth too, keeping whatever
    class the DETECTOR had guessed. Half the gold set was therefore not ground truth at
    all, and a judge that correctly answered "Not_A_Vehicle" on a junk box was marked
    wrong for it. That one line is most of why the judges looked barely better than
    chance.

    `balanced` caps each class so rare types are visible. That is right for choosing a
    judge and wrong for predicting pipeline accuracy -- the real footage is mostly 2W
    and cars, which are easy, so a balanced set understates production accuracy. Pass
    balanced=False for the number that reflects what the pipeline will actually do.
    """
    import csv
    import cv2
    out = run_dir(0) / f"gold_{kind}{'' if balanced else '_nat'}"
    out.mkdir(parents=True, exist_ok=True)
    idx_path = out / "index.json"
    if idx_path.exists():
        items = json.loads(idx_path.read_text())
        if len(items) >= n:
            return items[:n]
    verdicts = []
    with open(GOLD_DIR / "box_verdicts.csv") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            stem, bi, orig, verdict = row[0], row[1], row[2], row[3]
            new = row[4] if len(row) > 4 else ""
            if kind == "reject":
                if verdict != "delete":
                    continue
                truth = "Not_A_Vehicle"
            else:
                if verdict not in ("correct", "reclass"):
                    continue          # `delete`/`ignore` are not a confirmed class
                truth = new if (verdict == "reclass" and new) else orig
            # Not_A_Vehicle is a judge answer, not a MoRTH class, so it is not in CLASSES.
            if truth in CLASSES or truth == "Not_A_Vehicle":
                verdicts.append((stem, int(bi), truth))
    items, per_class = [], {}
    cap = max(2, n // 8) if balanced else n
    for stem, bi, truth in verdicts:
        if per_class.get(truth, 0) >= cap:
            continue
        img_p = GOLD_DIR / "frames_raw" / f"{stem}.jpg"
        lab_p = GOLD_DIR / "labels_v3" / f"{stem}.txt"
        if not img_p.exists() or not lab_p.exists():
            continue
        lines = [l for l in lab_p.read_text().splitlines() if l.strip()]
        if bi >= len(lines):
            continue
        parts = lines[bi].split()
        det_cls = int(parts[0])
        cx, cy, bw, bh = (float(x) for x in parts[1:5])
        img = cv2.imread(str(img_p))
        if img is None:
            continue
        H, W = img.shape[:2]
        x1, y1 = int((cx - bw / 2) * W), int((cy - bh / 2) * H)
        x2, y2 = int((cx + bw / 2) * W), int((cy + bh / 2) * H)
        if x2 - x1 < 12 or y2 - y1 < 12:
            continue
        pad = int(0.12 * max(x2 - x1, y2 - y1))
        crop = img[max(0, y1 - pad):min(H, y2 + pad), max(0, x1 - pad):min(W, x2 + pad)]
        cp = out / f"{stem}_{bi}.jpg"
        cv2.imwrite(str(cp), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        ctx = img.copy()
        cv2.rectangle(ctx, (x1, y1), (x2, y2), (0, 230, 118), 3)
        xp = out / f"{stem}_{bi}_ctx.jpg"
        cv2.imwrite(str(xp), cv2.resize(ctx, (min(900, W), int(H * min(900, W) / W))),
                    [cv2.IMWRITE_JPEG_QUALITY, 78])
        items.append({"crop_path": str(cp), "ctx_path": str(xp),
                      "truth": truth, "truth_id": (-1 if truth == "Not_A_Vehicle" else CLASSES.index(truth)),
                      "det_class": det_cls, "id": f"{stem}_{bi}"})
        per_class[truth] = per_class.get(truth, 0) + 1
        if len(items) >= n:
            break
    idx_path.write_text(json.dumps(items, indent=1))
    return items


def bakeoff(models, n=40, run_id=0, kind="clean", balanced=True):
    """Score candidate judges on the gold set: accuracy, cost, latency.
    This is what makes 'which judge' a measured decision instead of a guess."""
    gold = build_gold(n, kind=kind, balanced=balanced)
    if not gold:
        raise RuntimeError("could not build a gold set from box_verdicts.csv")
    results = []
    picks = {}                       # model -> {gold id: class id}, for ensemble scoring
    for model in models:
        hit = miss = err_n = 0
        cost = 0.0
        lat = []
        confusion = {}
        picks[model] = {}
        for g in gold:
            imgs = [g["crop_path"], g["ctx_path"]]
            txt, usage, c, ms, err = providers.or_vision(model, PROMPT, imgs, max_tokens=60)
            cost += c
            lat.append(ms)
            if err:
                err_n += 1
                continue
            name, _ = parse_verdict(txt)
            got = to_class_id(name, g["det_class"])
            picks[model][g["id"]] = got
            truth = g["truth_id"]
            # Heavy_Truck spans the axle classes: family-level match is a hit,
            # because the judge is not being asked to count axles.
            ok = (got == truth) or (got in AXLE_CLASSES and truth in AXLE_CLASSES)
            hit += ok
            miss += not ok
            if not ok:
                k = f"{g['truth']}→{name or 'unparsed'}"
                confusion[k] = confusion.get(k, 0) + 1
        n_scored = hit + miss
        acc = hit / n_scored if n_scored else 0
        per_1k = (cost / len(gold)) * 1000 if gold else 0
        db.run("""INSERT INTO lab_evals
              (run_id,kind,model,n,accuracy,cost_usd,usd_per_1k,latency_ms,detail,created)
              VALUES (?,'judge_bakeoff',?,?,?,?,?,?,?,?)""",
               run_id, model, n_scored, round(acc, 4), round(cost, 5), round(per_1k, 4),
               sum(lat) / len(lat) if lat else 0,
               db.jdump({"errors": err_n, "confusion": confusion}), time.time())
        if cost:
            db.charge(run_id, "openrouter", "eval", f"bakeoff {model}", len(gold), "crop",
                      cost, {"model": model})
        results.append({"model": model, "n": n_scored, "accuracy": round(acc, 4),
                        "cost": round(cost, 5), "usd_per_1k": round(per_1k, 4),
                        "latency_ms": int(sum(lat) / len(lat)) if lat else 0,
                        "errors": err_n, "confusion": confusion})
    results.sort(key=lambda r: -r["accuracy"])

    # The decision that actually matters: does a majority vote beat the best
    # single judge, and does it beat it by enough to justify 3x the cost?
    def family(m):
        return m.split("/")[0]

    reliable = [r["model"] for r in results if r["errors"] <= max(1, len(gold) // 10)]
    trio, seen = [], set()
    for m in reliable:                     # best-first, one model per vendor
        if family(m) not in seen:
            trio.append(m)
            seen.add(family(m))
        if len(trio) == 3:
            break
    ens = None
    if len(trio) >= 2:
        # Split by how much the judges agreed. An ensemble's real job is not to
        # out-guess the best model -- it is to say which crops a human must see.
        tiers = {"unanimous": [0, 0], "majority": [0, 0], "split": [0, 0]}
        for g in gold:
            votes = [v for v in (picks[m].get(g["id"]) for m in trio) if v is not None]
            if not votes:
                continue
            tally = {}
            for v in votes:
                tally[v] = tally.get(v, 0) + 1
            top = max(tally.values())
            winners = [k for k, c in tally.items() if c == top]
            tier = ("unanimous" if top == len(trio) else
                    "split" if (len(winners) > 1 or top < 2) else "majority")
            got, truth = winners[0], g["truth_id"]
            ok = (got == truth) or (got in AXLE_CLASSES and truth in AXLE_CLASSES)
            tiers[tier][0] += bool(ok)
            tiers[tier][1] += 1
        def acc(t):
            h, n = tiers[t]
            return {"n": n, "correct": h, "accuracy": round(h / n, 4) if n else None,
                    "share": round(n / len(gold), 3)}
        hit = tiers["unanimous"][0] + tiers["majority"][0]
        scored = tiers["unanimous"][1] + tiers["majority"][1]
        per_1k = sum(r["usd_per_1k"] for r in results if r["model"] in trio)
        ens = {"models": trio, "decided": scored, "to_human": tiers["split"][1],
               "accuracy": round(hit / scored, 4) if scored else 0,
               "usd_per_1k": round(per_1k, 4),
               "auto_rate": round(scored / len(gold), 3),
               "tiers": {t: acc(t) for t in tiers}}
        db.run("""INSERT INTO lab_evals
              (run_id,kind,model,n,accuracy,cost_usd,usd_per_1k,latency_ms,detail,created)
              VALUES (?,'ensemble',?,?,?,0,?,0,?,?)""",
               run_id, " + ".join(m.split("/")[-1] for m in trio), scored,
               ens["accuracy"], ens["usd_per_1k"], db.jdump(ens), time.time())
    return {"gold_n": len(gold), "results": results, "ensemble": ens}
