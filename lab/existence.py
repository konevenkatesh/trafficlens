"""The vehicle / not-a-vehicle gate.

Measured fact this exists to fix: the three class judges agree unanimously on some
vehicle type for a junk box -- a tree, a shadow, a lane marking, half an object -- and
are right only 24% of the time. About 20% of detections are junk, so unanimity on the
class question lets roughly one bad label in six into a dataset, and no amount of extra
judge agreement closes it. Agreement is not the problem; the question is.

The hypothesis here is that a model offered thirteen vehicle classes and one escape
hatch will reach for a class, while the same model asked a plain yes/no about existence
will not. That is measurable, so `bakeoff()` measures it before anything depends on it.
"""
import json
import re
import time

import db
import providers

PROMPT = """You are checking detection quality for an Indian road traffic survey.

The first image is a crop. The second is the full frame with the same region boxed in green.

Answer ONE question: is a real road vehicle the subject of that box?

Answer NO if the box contains any of these instead of a vehicle:
- a tree, bush or foliage
- a shadow cast on the road
- a lane marking, zebra crossing, kerb or road paint
- a building, wall, hoarding, pole or sign
- a pedestrian or animal with no vehicle
- bare road surface
- a fragment too small or too blurred to identify as a vehicle

Answer YES only if a road vehicle is clearly the subject of the box, even if it is
partly cut off or distant, and even if you are unsure which type it is.

Reply with ONLY this JSON, nothing else:
{"vehicle": true, "confidence": 0.8}"""


def parse(text):
    """Recover the boolean even when the model wanders outside JSON."""
    if not text:
        return None, None
    m = re.search(r"\{.*?\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            v = d.get("vehicle")
            if isinstance(v, bool):
                return v, float(d.get("confidence", 0) or 0)
            if isinstance(v, str) and v.lower() in ("true", "false", "yes", "no"):
                return v.lower() in ("true", "yes"), float(d.get("confidence", 0) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    t = text.lower()
    if re.search(r'"?vehicle"?\s*[:=]\s*(true|yes)', t) or re.search(r"\byes\b", t):
        return True, None
    if re.search(r'"?vehicle"?\s*[:=]\s*(false|no)', t) or re.search(r"\bno\b", t):
        return False, None
    return None, None


def check_one(model, crop_path, ctx_path=None, timeout=90):
    imgs = [crop_path] + ([ctx_path] if ctx_path else [])
    txt, usage, cost, ms, err = providers.or_vision(
        model, PROMPT, imgs, max_tokens=40, timeout=timeout)
    is_veh, conf = parse(txt)
    return {"model": model, "vehicle": is_veh, "conf": conf,
            "cost": cost, "ms": ms, "error": err, "raw": txt}


def policy_eval(models, run_id=0, n=40):
    """Collect every judge's vote per crop once, then score the combining rules offline.

    The asymmetry that picks the rule: this gate guards a TRAINING SET, not a count.
    A junk box that gets through teaches the detector that a tree is a vehicle. A real
    vehicle wrongly dropped just means one fewer crop, and there are plenty. So the
    right rule is whichever removes the most junk while keeping real losses tolerable
    -- precision over recall, the opposite of what counting would want.
    """
    import judge as J
    junk = J.build_gold(n, kind="reject", balanced=False)
    real = J.build_gold(n, kind="clean", balanced=False)
    votes, cost = {"junk": [], "real": []}, 0.0
    for label, items in (("junk", junk), ("real", real)):
        for g in items:
            row = {}
            for m in models:
                r = check_one(m, g["crop_path"], g["ctx_path"])
                cost += r["cost"] or 0
                row[m] = r["vehicle"]
            votes[label].append(row)

    def score(rule):
        out = {}
        for label in ("junk", "real"):
            kept = 0
            for row in votes[label]:
                said_no = sum(1 for m in models if row.get(m) is False)
                said_any = sum(1 for m in models if row.get(m) is not None)
                out.setdefault("_n_" + label, 0)
                out["_n_" + label] += 1
                kept += not rule(said_no, said_any)
            out[label + "_kept"] = kept
        n_junk, n_real = out["_n_junk"], out["_n_real"]
        tp, fp = out["real_kept"], out["junk_kept"]
        return {"junk_rejected": round(1 - fp / n_junk, 4),
                "real_kept": round(tp / n_real, 4),
                "keep_precision": round(tp / (tp + fp), 4) if (tp + fp) else None}

    rules = {
        "any judge says no":       lambda no, tot: no >= 1,
        "two of three say no":     lambda no, tot: no >= 2,
        "all three say no":        lambda no, tot: tot and no == tot,
    }
    results = {name: score(fn) for name, fn in rules.items()}
    db.run("""INSERT INTO lab_evals
              (run_id,kind,model,n,accuracy,cost_usd,detail,created)
              VALUES (?,'existence_policy',?,?,?,?,?,?)""",
           run_id, " + ".join(m.split("/")[-1] for m in models),
           len(junk) + len(real), None, round(cost, 5), db.jdump(results), time.time())
    if cost:
        db.charge(run_id, "openrouter", "eval", "existence policy",
                  len(junk) + len(real), "crop", cost, {"models": models})
    return {"results": results, "cost": round(cost, 5)}


def bakeoff(models, run_id=0, n=40):
    """Score the binary gate on both halves of the problem.

    Junk crops come from boxes the human marked `delete`; real ones from boxes marked
    `correct`/`reclass`. Reported separately on purpose: a gate that calls everything a
    vehicle scores 100% on the real half and is worthless, and one that rejects
    everything scores 100% on the junk half and destroys the dataset. Only the pair of
    numbers says whether the gate is usable.
    """
    import judge as J
    junk = J.build_gold(n, kind="reject", balanced=False)
    real = J.build_gold(n, kind="clean", balanced=False)
    out = []
    for model in models:
        r = {"model": model, "cost": 0.0, "lat": []}
        for label, items, want in (("junk", junk, False), ("real", real, True)):
            hit = miss = unparsed = 0
            for g in items:
                res = check_one(model, g["crop_path"], g["ctx_path"])
                r["cost"] += res["cost"] or 0
                if res["ms"]:
                    r["lat"].append(res["ms"])
                if res["vehicle"] is None:
                    unparsed += 1
                    continue
                hit += (res["vehicle"] == want)
                miss += (res["vehicle"] != want)
            n_ok = hit + miss
            r[label] = {"n": n_ok, "correct": hit,
                        "accuracy": round(hit / n_ok, 4) if n_ok else None,
                        "unparsed": unparsed}
        # Precision/recall of "keep this crop", which is what the pipeline actually does.
        tp = r["real"]["correct"]                       # real kept
        fn = r["real"]["n"] - tp                        # real wrongly dropped
        fp = r["junk"]["n"] - r["junk"]["correct"]      # junk wrongly kept
        r["keep_precision"] = round(tp / (tp + fp), 4) if (tp + fp) else None
        r["keep_recall"] = round(tp / (tp + fn), 4) if (tp + fn) else None
        r["latency_ms"] = int(sum(r["lat"]) / len(r["lat"])) if r["lat"] else 0
        r.pop("lat")
        db.run("""INSERT INTO lab_evals
                  (run_id,kind,model,n,accuracy,cost_usd,usd_per_1k,latency_ms,detail,created)
                  VALUES (?,'existence_gate',?,?,?,?,?,?,?,?)""",
               run_id, model, r["junk"]["n"] + r["real"]["n"],
               r["keep_precision"], round(r["cost"], 5),
               round(r["cost"] / max(1, len(junk) + len(real)) * 1000, 4),
               r["latency_ms"], db.jdump(r), time.time())
        if r["cost"]:
            db.charge(run_id, "openrouter", "eval", f"existence {model}",
                      len(junk) + len(real), "crop", r["cost"], {"model": model})
        out.append(r)
    out.sort(key=lambda x: -(x["keep_precision"] or 0))
    return out


# ═════════════════════════ production gate ═════════════════════════
# Measured on 40 junk + 40 real crops, three judges:
#
#   rule                  junk rejected   real kept   keep-precision
#   any judge says no         75.0%         97.5%         79.6%
#   two of three say no       50.0%        100.0%         66.7%
#   all three say no          27.5%        100.0%         58.0%
#   (13-way class judges,
#    unanimous, for scale)    24.0%           —             —
#
# "Any judge says no" wins because the two errors are not equal. A junk box that gets
# through teaches the detector that a tree is a vehicle; a real vehicle dropped costs
# one crop out of thousands. Note this gate guards the DATASET, never a count -- a
# dropped crop is not a missed vehicle.
GATE_MODELS = ["qwen/qwen3-vl-32b-instruct", "meta-llama/llama-4-scout",
               "google/gemini-2.5-flash-lite"]
GATE_RULE_NOTE = "rejected if any judge says not-a-vehicle (75% of junk, keeps 97.5% of real)"


def gate_crop(crop, models=None):
    """Ask each model the yes/no question. Returns (keep, votes)."""
    models = models or GATE_MODELS
    votes = []
    for m in models:
        r = check_one(m, crop["crop_path"], crop.get("ctx_path"))
        votes.append(r)
        db.run("""INSERT INTO lab_judgments
                  (crop_id,run_id,model,verdict,verdict_name,confidence,raw,
                   cost_usd,latency_ms,error,created)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               crop["id"], crop["run_id"], "gate:" + m,
               (-1 if r["vehicle"] is False else None),
               ("Not_A_Vehicle" if r["vehicle"] is False else
                "Vehicle" if r["vehicle"] is True else None),
               r["conf"], (r["raw"] or "")[:300], r["cost"], r["ms"], r["error"],
               time.time())
    spent = sum(v["cost"] or 0 for v in votes)
    if spent:
        db.charge(crop["run_id"], "openrouter", "existence", f"{len(models)} gate calls",
                  1, "crop", spent, {"crop": crop["id"]})
    keep = not any(v["vehicle"] is False for v in votes)
    return keep, votes


def run_gate(run_id, limit=None):
    """Drop junk detections before any class judging happens.

    Running first is the point: it is both cheaper (a rejected crop never reaches the
    class judges) and safer (a junk box can no longer collect three confident votes for
    a vehicle class it never was).
    """
    from pipeline import stage_begin, stage_done, stage_set
    crops = db.rows("SELECT * FROM lab_crops WHERE run_id=? AND state='new' ORDER BY id",
                    run_id)
    if limit:
        crops = crops[:limit]
    if not crops:
        raise RuntimeError("no unjudged crops -- run the sample stage first")
    stage_begin(run_id, "existence", f"{len(crops)} crops x {len(GATE_MODELS)} checks")
    kept = dropped = 0
    for i, c in enumerate(crops):
        keep, _ = gate_crop(c)
        if keep:
            kept += 1
        else:
            dropped += 1
            # final_class = -1, not just a state string. The dataset builder falls back
            # to the DETECTOR's class for any track it has no confirmed label for, so a
            # crop marked only by state would be written back into the dataset carrying
            # the very class the gate just rejected.
            db.run("UPDATE lab_crops SET state='not_vehicle', final_class=-1 WHERE id=?",
                   c["id"])
        if i % 10 == 0 or i == len(crops) - 1:
            stage_set(run_id, "existence", progress=100 * (i + 1) / len(crops),
                      message=f"{kept} kept, {dropped} dropped")
    db.log(run_id, "gated", f"{dropped} junk crop(s) removed", GATE_RULE_NOTE)
    stage_done(run_id, "existence", f"{kept} kept, {dropped} dropped as not-a-vehicle",
               {"kept": kept, "dropped": dropped, "rule": GATE_RULE_NOTE,
                "models": GATE_MODELS})
    return {"kept": kept, "dropped": dropped}
