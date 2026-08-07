"""What a station actually contains: clips, and how far each one has got.

The station page was built around *runs*, which is how footage gets extracted rather than
how anybody thinks about a survey. FID-33 shows seven of them -- including one with no
stages and no output, left over from a test -- with names like `fid33_axletest_p1` that
mean nothing, and no way to see which 15 minutes of road each represents.

The unit people actually work in is the clip: a quarter-hour of footage at a known time,
which either has a count line or does not, has been counted or has not, classified or not,
reviewed or not, rendered or not. One row per clip, in clock order, with every stage
visible and every gap answerable. Runs become provenance -- available when you ask "where
did this clip come from", and invisible otherwise.

The status of a clip is derived from the data every time, never stored. A stored status is
a second source of truth that drifts the moment anything is re-run, and the whole point of
this screen is to be the place you can trust about what state the survey is in.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _fmt_min(v):
    return round(v, 1) if v else 0


def clips(site_id):
    """Every clip of this station in clock order, each with its full pipeline state."""
    import axle_pass
    import counting
    import organise
    import sites

    out = []
    for v in db.rows("""SELECT id, name, path, start_clock, frames, fps, site_id
                        FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0
                        ORDER BY start_clock, id""", site_id):
        mins = (v["frames"] or 0) / (v["fps"] or 25) / 60
        lines, line_src = sites.lines_for(v["id"])
        n_tracks = (db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=?", v["id"]) or {})["n"]

        counted = None
        if lines and n_tracks:
            try:
                counted = counting.count_video(v["id"], lines)["total"]
            except Exception:
                counted = None

        ax = axle_pass.state(v["id"]) if counted is not None else {}
        import verify as _verify
        vs = _verify.state(v["id"])
        render = organise.render_path(v["id"])
        rendered = bool(render and Path(render).exists())
        size_mb = round(Path(render).stat().st_size / 1e6, 1) if rendered else 0

        # Where this clip came from: the run and segment that produced it. Provenance, not
        # navigation -- it answers "why does this file exist" without being the way in.
        seg = db.one("""SELECT s.run_id, s.idx, r.name FROM lab_segments s
                        JOIN lab_runs r ON r.id=s.run_id WHERE s.path=?""", v["path"])

        end = None
        if v["start_clock"]:
            try:
                end = (datetime.strptime(v["start_clock"], "%Y-%m-%d %H:%M:%S")
                       + timedelta(minutes=mins)).strftime("%H:%M")
            except ValueError:
                pass

        out.append({
            "video_id": v["id"], "name": v["name"],
            "start_clock": v["start_clock"],
            "clock": (v["start_clock"] or "")[11:16], "end_clock": end,
            "minutes": _fmt_min(mins),
            "tracks": n_tracks,
            "line": line_src,                       # video | station | none
            "counted": counted,
            "axles": {"total": ax.get("total") or 0, "auto": ax.get("auto") or 0,
                      "human": ax.get("human") or 0, "pending": ax.get("pending") or 0,
                      "too_small": ax.get("too_small") or 0},
            "verify": vs,
            "rendered": rendered, "render_mb": size_mb,
            "from_run": ({"run_id": seg["run_id"], "part": seg["idx"], "name": seg["name"]}
                         if seg else None),
            "state": _state(lines, n_tracks, counted, ax, vs, counted),
        })
    return out


def _state(lines, n_tracks, counted, ax, vs=None, total=None):
    """One word for where this clip is stuck, chosen to name the NEXT action.

    'blocked' would be useless on its own -- the value of this field is that it says what
    to do, so a clip without a line reads `needs line` rather than `incomplete`.
    """
    if not n_tracks:
        return "needs extraction"
    if not lines:
        return "needs line"
    if counted is None:
        return "count failed"
    if (ax.get("pending") or 0):
        return f"{ax['pending']} to review"
    # No denominator here on purpose: the count includes vehicles too small to show a
    # reviewer, so "215 of 233" reads as unfinished work that does not exist. The verify
    # screen owns completeness; this says only how many were settled.
    v = (vs or {}).get("verified") or 0
    return f"{v} verified" if v else "not verified"


def summary(site_id):
    """The station in one line each: footage, progress, spend, model, disk."""
    import axle_pass
    cs = clips(site_id)
    site = db.one("SELECT * FROM sites WHERE id=?", site_id)

    total_min = sum(c["minutes"] for c in cs)
    counted = [c for c in cs if c["counted"] is not None]
    pending = sum(c["axles"]["pending"] for c in cs)

    # Spend attributable to this station, in the currency it was charged in. Converted at
    # display time, never here -- see the note in the UI's money formatter.
    spend = db.one("""SELECT ROUND(COALESCE(SUM(c.usd),0),4) usd FROM lab_costs c
                      LEFT JOIN lab_runs r ON r.id=c.run_id
                      WHERE r.site_id=?""", site_id) or {}

    model = axle_pass.current_model()
    # A station model is one trained on THIS station's labels. Not the same thing as the
    # global axle model the station happens to be using, and conflating them would let a
    # station look trained when nothing of its own has ever been fitted.
    own_labels = (db.one("""SELECT COUNT(*) n FROM lab_attr_samples
                            WHERE attribute='axles' AND human IS NOT NULL
                              AND video_id IN (SELECT id FROM videos WHERE site_id=?)""",
                         site_id) or {}).get("n") or 0

    return {
        "site": dict(site) if site else None,
        "clips": len(cs),
        "minutes": round(total_min, 1),
        "hours": round(total_min / 60, 2),
        "counted_clips": len(counted),
        "vehicles": sum(c["counted"] or 0 for c in counted),
        "needs_line": len([c for c in cs if c["line"] == "none"]),
        "needs_extraction": len([c for c in cs if not c["tracks"]]),
        "pending_review": pending,
        "rendered": len([c for c in cs if c["rendered"]]),
        "render_mb": round(sum(c["render_mb"] for c in cs), 1),
        "spend_usd": spend.get("usd") or 0,
        "labels_from_here": own_labels,
        "model": ({"id": model["id"], "accuracy": model["accuracy"],
                   "macro_f1": model["macro_f1"], "scope": "shared"} if model else None),
    }


def runs(site_id):
    """The extraction runs behind the clips — provenance, and the dead ones flagged.

    A run that produced nothing is not history, it is litter: it makes the list look busy
    and tells the reader nothing. They are listed with `produced: 0` so they can be seen
    and cleared rather than quietly filtered out, which would hide a failed extraction.
    """
    out = []
    for r in db.rows("""SELECT id, name, status, source_path, config, created
                        FROM lab_runs WHERE site_id=? ORDER BY id DESC""", site_id):
        cfg = db.jload(r["config"], {})
        vids = db.rows("SELECT id FROM videos WHERE path LIKE ?", f"%run{r['id']}/segments%")
        done = db.rows("SELECT stage FROM lab_stages WHERE run_id=? AND status='done'", r["id"])
        cost = db.one("SELECT ROUND(COALESCE(SUM(usd),0),4) usd FROM lab_costs WHERE run_id=?",
                      r["id"]) or {}
        out.append({
            "run_id": r["id"], "name": r["name"], "status": r["status"],
            "source": Path(r["source_path"]).name if r["source_path"] else None,
            "parts": cfg.get("extract_segments"),
            "produced": [v["id"] for v in vids],
            "stages_done": len(done),
            "spend_usd": cost.get("usd") or 0,
            "created": r["created"],
            "dead": not vids and len(done) == 0,
        })
    return out


def delete_run(run_id):
    """Remove a run that produced nothing. Refuses if it did."""
    vids = db.rows("SELECT id FROM videos WHERE path LIKE ?", f"%run{run_id}/segments%")
    if vids:
        return {"deleted": False,
                "why": f"run {run_id} produced videos {[v['id'] for v in vids]} — "
                       f"deleting it would orphan them"}
    db.run("DELETE FROM lab_stages WHERE run_id=?", run_id)
    db.run("DELETE FROM lab_segments WHERE run_id=?", run_id)
    db.run("DELETE FROM lab_runs WHERE id=?", run_id)
    return {"deleted": True, "run_id": run_id}


# ───────────────────────── footage → clips → labels ─────────────────────────
def _label_state(video_id):
    """What has been extracted, judged and human-verified for one clip.

    This is the raw dataset, seen per clip. The three numbers that matter are kept
    separate on purpose: crops sampled, crops a judge ensemble settled, and crops a
    person settled. Collapsing them into one "labelled" figure is what let a pipeline
    look finished while every heavy label in it was a fallback constant.
    """
    c = db.one("""SELECT COUNT(*) crops,
                    SUM(state IN ('agreed','reclass')) judged,
                    SUM(human_class IS NOT NULL) human,
                    SUM(state='contested') contested,
                    SUM(state='new') unjudged,
                    SUM(state='needs_axles') needs_axles
                  FROM lab_crops WHERE video_id=?""", video_id) or {}
    a = db.one("""SELECT COUNT(*) n, SUM(human IS NOT NULL) human
                  FROM lab_attr_samples WHERE video_id=?""", video_id) or {}
    g = db.one("SELECT COUNT(*) n FROM lab_gold_frames WHERE video_id=?", video_id) or {}
    return {"crops": c.get("crops") or 0, "judged": c.get("judged") or 0,
            "human": c.get("human") or 0, "contested": c.get("contested") or 0,
            "unjudged": c.get("unjudged") or 0,
            "needs_axles": c.get("needs_axles") or 0,
            "attrs": a.get("n") or 0, "attrs_human": a.get("human") or 0,
            "gold_frames": g.get("n") or 0}


def footage_tree(site_id):
    """The station's footage files, each with the clips cut from it.

    The hierarchy is the point. A survey arrives as 50-minute DVR files; the work happens
    on 15-minute clips; and until now the UI showed the clips flat, so there was no way to
    see which file a clip came from, which files had been segmented, or which had not been
    touched. Segmenting is cheap (a stream copy, no re-encode) and extraction is not, so
    they are separate actions: cut a file into clips first, then choose which clips are
    worth the GPU time.
    """
    from pipeline import clip_name as _clip_name
    code = (db.one("SELECT code FROM sites WHERE id=?", site_id) or {}).get("code")
    by_clip = {c["video_id"]: c for c in clips(site_id)}

    # clip -> the run that produced it -> that run's source file
    src_of = {}
    for r in db.rows("""SELECT s.path, r.source_path FROM lab_segments s
                        JOIN lab_runs r ON r.id=s.run_id WHERE r.site_id=?""", site_id):
        src_of[r["path"]] = r["source_path"]
    clip_src = {}
    for v in db.rows("SELECT id, path FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id):
        if v["path"] in src_of:
            clip_src[v["id"]] = src_of[v["path"]]

    out = []
    for f in db.rows("""SELECT id, name, path, start_clock, dur_s, size_mb, fps
                        FROM lab_footage WHERE site_id=? AND dup_of IS NULL
                          AND (missing IS NULL OR missing=0)
                        ORDER BY start_clock""", site_id):
        mine = [by_clip[vid] for vid, sp in clip_src.items()
                if sp == f["path"] and vid in by_clip]
        mine.sort(key=lambda c: c["start_clock"] or "")
        for c in mine:
            c["labels"] = _label_state(c["video_id"])
        dur_min = (f["dur_s"] or 0) / 60
        # 15-minute cuts, the last one short. Shown so a partly-segmented file is obvious.
        expected = max(1, int(dur_min // 15) + (1 if dur_min % 15 > 0.2 else 0))

        # Every 15-minute window this file contains, whether it has been cut yet or not.
        # The page renders THIS, not two different layouts -- an unsegmented file used to
        # show a button and a paragraph where a segmented one showed a grid, so cutting
        # appeared to change what the file was. It does not: the windows already exist in
        # the footage, segmenting only makes them separately addressable. Names come from
        # the same clip_name() the segmenter will use, so the preview is the result.
        windows, t0 = [], _parse(f["start_clock"])
        for i in range(expected):
            secs = min(900.0, (f["dur_s"] or 0) - i * 900)
            if secs <= 0:
                break
            start = (t0 + timedelta(seconds=i * 900)) if t0 else None
            iso = start.isoformat(sep=" ", timespec="seconds") if start else None
            # Match the real clip by NEAREST start, not by an equal string. ffmpeg cuts on
            # the next keyframe, so a "900 second" part is 899 or 901 and the exact-match
            # version reported an already-cut clip as still planned.
            real = None
            if start:
                near = [(abs((_parse(c["start_clock"]) - start).total_seconds()), c)
                        for c in mine if _parse(c["start_clock"])]
                near = [x for x in near if x[0] <= 120]
                real = min(near, key=lambda x: x[0])[1] if near else None
            windows.append({
                "idx": i, "start_clock": iso,
                "clock": start.strftime("%H:%M") if start else f"part {i}",
                "end_clock": (start + timedelta(seconds=secs)).strftime("%H:%M") if start else "",
                "minutes": _fmt_min(secs / 60),
                "name": _clip_name(code, iso, secs, i),
                "video_id": real["video_id"] if real else None,
                "clip": real,
            })
        out.append({
            "footage_id": f["id"], "name": f["name"], "path": f["path"],
            "start_clock": f["start_clock"], "minutes": _fmt_min(dur_min),
            "size_mb": f["size_mb"],
            "clips": mine, "n_clips": len(mine), "expected_clips": expected,
            "windows": windows,
            "segmented": len(mine) > 0,
            "fully_segmented": len(mine) >= expected,
            "extracted": len([c for c in mine if c["tracks"]]),
            "counted": len([c for c in mine if c["counted"] is not None]),
        })
    return out


def raw_dataset(site_id):
    """Everything labelled at this station, and how much of it a person has settled.

    The raw dataset is not a folder — it is every crop this station has produced with a
    label attached, whoever attached it. Gold is the subset a person has confirmed. Keeping
    the two apart, and showing what is still pending between them, is what stops a model
    being trained on its own guesses.
    """
    vids = [v["id"] for v in db.rows("SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id)]
    if not vids:
        return {"crops": 0, "judged": 0, "human": 0, "pending": 0, "gold_frames": 0}
    q = ",".join("?" * len(vids))
    c = db.one(f"""SELECT COUNT(*) crops,
                     SUM(state IN ('agreed','reclass')) judged,
                     SUM(human_class IS NOT NULL) human,
                     SUM(state='contested') contested,
                     SUM(state='new') unjudged
                   FROM lab_crops WHERE video_id IN ({q})""", *vids) or {}
    a = db.one(f"""SELECT COUNT(*) n, SUM(human IS NOT NULL) human
                   FROM lab_attr_samples WHERE video_id IN ({q})""", *vids) or {}
    g = db.one("SELECT COUNT(*) n, SUM(reviewed IS NOT NULL) done "
               "FROM lab_gold_frames WHERE site_id=?", site_id) or {}
    pending = (c.get("contested") or 0) + (c.get("unjudged") or 0) \
        + ((a.get("n") or 0) - (a.get("human") or 0))
    # Verification has overtaken crop judging as the source of truth: 1,608 verdicts on
    # counted vehicles against 962 sampled crops, and the gold set is now derived from
    # them. The banner has to lead with that or it describes a pipeline nobody uses.
    import verify
    v = db.one(f"""SELECT COUNT(*) n, SUM(kind='class' AND was != answer) corr
                   FROM clip_verdicts WHERE video_id IN ({q})""", *vids) or {}
    # Which run holds the crops still waiting on a person, so the Clips tab can offer a
    # door straight into reviewing them. Without it the review screen exists but the
    # reader has to guess which run to open, which is why nothing linked to it.
    cr = db.one(f"""SELECT run_id, COUNT(*) n FROM lab_crops
                    WHERE video_id IN ({q}) AND state='contested' AND human_class IS NULL
                    GROUP BY run_id ORDER BY n DESC LIMIT 1""", *vids) or {}
    return {
        "contested_run": cr.get("run_id"),
        "contested_open": cr.get("n") or 0,
        "verdicts": v.get("n") or 0,
        "corrections": v.get("corr") or 0,
        "crops": c.get("crops") or 0,
        "judged": c.get("judged") or 0,
        "human": c.get("human") or 0,
        "contested": c.get("contested") or 0,
        "unjudged": c.get("unjudged") or 0,
        "attr_samples": a.get("n") or 0, "attr_human": a.get("human") or 0,
        "pending_review": pending,
        "gold_frames": g.get("n") or 0, "gold_reviewed": g.get("done") or 0,
    }


def class_mix(site_id):
    """Crops sampled per class across the station — what the raw dataset is made of.

    Was buried on a per-run page, where it answered "what did this extraction see".
    At station level it answers the more useful question: which classes this station's
    dataset is thin on, which is what decides where the next labelling hour goes.
    """
    from engine import CLASSES
    rows = db.rows("""SELECT det_class, COUNT(*) n,
                        SUM(human_class IS NOT NULL) human,
                        SUM(state IN ('agreed','reclass')) judged
                      FROM lab_crops
                      WHERE video_id IN (SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0)
                      GROUP BY det_class ORDER BY n DESC""", site_id)
    return [{"class": CLASSES[r["det_class"]] if r["det_class"] is not None
             and 0 <= r["det_class"] < len(CLASSES) else str(r["det_class"]),
             "n": r["n"], "judged": r["judged"] or 0, "human": r["human"] or 0}
            for r in rows]


def spend(site_id):
    """What this station cost, by stage — the ledger stays in USD, display converts."""
    rows = db.rows("""SELECT c.stage, c.provider, ROUND(SUM(c.usd),5) usd, COUNT(*) n
                      FROM lab_costs c JOIN lab_runs r ON r.id=c.run_id
                      WHERE r.site_id=? GROUP BY c.stage, c.provider
                      ORDER BY SUM(c.usd) DESC""", site_id)
    return [{"stage": r["stage"] or "—", "provider": r["provider"],
             "usd": r["usd"] or 0, "calls": r["n"]} for r in rows]


def active(site_id):
    """Any stage running right now at this station, so the page can show live progress.

    The run page was the only place a running extraction was visible. Folding it away
    without this would mean starting a job and having nowhere to watch it.
    """
    rows = db.rows("""SELECT s.run_id, s.stage, s.status, s.progress, s.message,
                             r.name, r.source_path
                      FROM lab_stages s JOIN lab_runs r ON r.id=s.run_id
                      WHERE r.site_id=? AND s.status IN ('running','queued')
                      ORDER BY s.id""", site_id)
    return [{"run_id": r["run_id"], "stage": r["stage"], "status": r["status"],
             "progress": r["progress"] or 0, "message": r["message"],
             "source": Path(r["source_path"]).name if r["source_path"] else None}
            for r in rows]


def _performance(judged):
    """Per-class accuracy, split into the two ways a class can be wrong.

    One accuracy number per station hides the thing you need to act on. 90% overall can
    mean every class is fine, or it can mean LCV is a disaster and 2W is carrying the
    average -- and those call for opposite responses. Worse, a single number cannot
    distinguish the two failure directions, which are not the same problem:

      * **over-called** (low precision): the model says LCV and it is not. Inflates the
        LCV column of the proforma with vehicles taken from another class.
      * **missed** (low recall): the vehicle IS an LCV and the model called it something
        else. Deflates the LCV column.

    A class can be bad at one and fine at the other, and the fix differs -- more negative
    examples versus more positive ones. So both are reported, per class, with the single
    confusion most responsible for each.

    Counted only over vehicles a person actually judged, so the denominators are real.
    """
    said, actual, kept = {}, {}, {}
    as_said, as_actual = {}, {}          # confusion tallies, per direction
    for was, act in judged:
        said[was] = said.get(was, 0) + 1
        actual[act] = actual.get(act, 0) + 1
        if was == act:
            kept[act] = kept.get(act, 0) + 1
        else:
            as_said.setdefault(was, {})[act] = as_said.setdefault(was, {}).get(act, 0) + 1
            as_actual.setdefault(act, {})[was] = as_actual.setdefault(act, {}).get(was, 0) + 1

    def top(d):
        return max(d.items(), key=lambda kv: -(-kv[1]))[0:2] if d else (None, 0)

    out = []
    for cls in sorted(set(said) | set(actual), key=lambda c: -(actual.get(c, 0))):
        s, a, k = said.get(cls, 0), actual.get(cls, 0), kept.get(cls, 0)
        mis_cls, mis_n = top(as_said.get(cls, {}))
        got_cls, got_n = top(as_actual.get(cls, {}))
        out.append({
            "class": cls,
            "said": s, "actual": a, "kept": k,
            "over": s - k, "missed": a - k,
            "precision": round(k / s, 3) if s else None,
            "recall": round(k / a, 3) if a else None,
            # "when it wrongly said LCV, it was usually really a Car"
            "mostly_really": mis_cls, "mostly_really_n": mis_n,
            # "when it missed a real LCV, it usually called it a Car"
            "mistaken_for": got_cls, "mistaken_for_n": got_n,
        })
    return out


def gold_from_verdicts(site_id):
    """The gold set, as it actually exists: every vehicle a person settled.

    The Lab's original gold set was 60 frames exhaustively labelled by hand, of which 3
    were ever reviewed. Clip-level verification has since produced 1,608 verdicts on
    vehicles that were actually counted, each with an image and a provenance trail. That
    is the better gold set on every axis that matters -- size, relevance, and the fact
    that each label is attached to a number in a delivered report.

    `corrections` is the part worth training on. A confirmation teaches a model what it
    already knows; a correction is a measured failure with the picture that caused it.
    """
    import verify
    vids = [v["id"] for v in db.rows("SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id)]
    if not vids:
        # The empty shape must match the populated one. Returning a bare {"verdicts": 0}
        # made the Labels tab throw on `g.confusions.length` for any station with no
        # clips yet -- a fresh station crashed the page it was meant to start on.
        return {"verdicts": 0, "scored": 0, "corrections": 0, "attributes": 0,
                "rejects": 0, "model_accuracy": None, "confusions": [], "by_class": []}
    q = ",".join("?" * len(vids))
    n = db.one(f"""SELECT COUNT(*) total,
                     SUM(kind='class') classes,
                     SUM(kind='class' AND was != answer) corrections,
                     SUM(kind='attribute') attributes,
                     SUM(kind='reject') rejects,
                     SUM(kind='class' AND was = answer) confirmations
                   FROM clip_verdicts WHERE video_id IN ({q})""", *vids) or {}

    # Every verdict, reduced to "what the model said" and "what it actually is".
    #
    # An attribute answer is a class confirmation and was being thrown away. Press
    # "Govt / APSRTC bus" on a bus and the verdict row becomes kind='attribute', which
    # this function filtered out -- so a station could have every one of its buses
    # confirmed by hand and still show no Bus at all in the gold set, and score those
    # buses as unjudged. The person did not decline to answer; they answered "yes, a bus,
    # and a government one". The class they confirmed is `was`, because an attribute
    # refines a class, it never replaces it.
    rows = db.rows(f"""SELECT was, answer, kind FROM clip_verdicts
                       WHERE video_id IN ({q})""", *vids)
    judged = []                     # (model_said, actually) for everything with a class
    for r in rows:
        if r["kind"] == "class":
            judged.append((r["was"], r["answer"]))
        elif r["kind"] == "attribute" and r["was"]:
            judged.append((r["was"], r["was"]))
        # reject = not a vehicle, skip = could not tell. Neither names a class.
    scored = len(judged)
    right = sum(1 for was, act in judged if was == act)
    tally = {}
    for _, act in judged:
        tally[act] = tally.get(act, 0) + 1
    by_class = [{"answer": k, "n": v} for k, v in
                sorted(tally.items(), key=lambda kv: -kv[1])]
    wrong = verify.wrong_calls(site_id=site_id)
    pairs = {}
    for w in wrong:
        pairs[(w["was"], w["answer"])] = pairs.get((w["was"], w["answer"]), 0) + 1
    return {
        "verdicts": n.get("total") or 0,
        "confirmations": right,
        "corrections": n.get("corrections") or 0,
        "attributes": n.get("attributes") or 0,
        "rejects": n.get("rejects") or 0,
        "model_accuracy": round(right / scored, 3) if scored else None,
        "scored": scored,
        "by_class": [{"class": r["answer"], "n": r["n"]} for r in by_class],
        "performance": _performance(judged),
        "confusions": sorted(
            [{"said": a, "actually": b, "n": c} for (a, b), c in pairs.items()],
            key=lambda x: -x["n"]),
    }
