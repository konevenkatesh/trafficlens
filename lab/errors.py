"""Where the count error actually comes from.

A classified count can be wrong in four independent ways, and they have completely
different fixes and completely different prices:

  1. **Detection** -- the vehicle was never found. Costs a count outright, and nothing
     downstream can recover it. Fixed by training.
  2. **Classification** -- found, but put in the wrong proforma column. The total stays
     right while the report is wrong. Fixed by training.
  3. **Tracking** -- found in frames, but broken into fragments or merged with a
     neighbour. Fixed by tuning the tracker, which today means RE-EXTRACTING: tracking is
     coupled to detection (`model.track(...)` in engine.py), so a tracker parameter change
     costs a full pass over the footage at ~3.4x real time.
  4. **Counting logic** -- tracked fine, but the line geometry, the debounce or the
     duplicate guard threw the crossing away. Genuinely free: trajectories are already
     stored, so a line moves and the count is re-read in milliseconds.

Getting this order wrong is expensive in the wrong direction: every large counting error
found in this project so far was (3) or (4) -- parked bikes crossing an infinite line,
convoy vehicles swallowed by the duplicate guard, a video counted twice -- and all of
them were free to fix. Renting a GPU to fix a line-geometry bug buys nothing.

So layers 1-2 are measured against the gold set (the only thing that can see a miss) and
layers 3-4 are read from the counting code's own decisions, recorded as it runs rather
than re-derived here where they could drift.
"""
import json
import sys
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))


def _lines(video_id):
    """The line for this video: its own override, else its station's default."""
    import stations
    return stations.lines_for(video_id)[0]


def counting_diagnostics(video_id):
    """Replay counting on a video and collect why each track did or did not count."""
    import counting
    lines = _lines(video_id)
    if not lines:
        return {"error": "no count line for this video or its station"}
    r = counting.count_video(video_id, lines)
    return {"total": r["total"], "per_class": r["per_class"], **r["diagnostics"]}


def decompose(site_id=None, video_id=None, model_id=None):
    """Combine the frame layer and the counting layer into one attribution.

    Returns the measurements plus a ranked list of where the effort is worth spending.
    Nothing here is inferred silently: every finding names the number it came from, and
    layers with no evidence say so instead of scoring zero.
    """
    out = {"video_id": video_id, "site_id": site_id, "findings": []}

    # ── layers 1-2: only the gold set can see a missed vehicle ──
    frame = None
    if site_id:
        import goldset
        val = goldset.validation(site_id)
        out["gold_validation"] = val
        frame = goldset.score(site_id, model_id)
        if frame.get("error"):
            out["frame_level"] = {"error": frame["error"]}
            frame = None
        else:
            out["frame_level"] = frame
            # A gold set nobody corrected scores 100% by construction. Reporting the
            # number is fine; drawing a conclusion from it is not.
            if not val["trustworthy"]:
                frame = None

    # ── layers 3-4: the counting code's own record of its decisions ──
    count = counting_diagnostics(video_id) if video_id else None
    if count and not count.get("error"):
        out["count_level"] = count
    elif count:
        out["count_level"] = count

    F = out["findings"]

    if frame:
        rec, cls = frame.get("recall"), frame.get("class_accuracy")
        if rec is not None and rec < 0.95:
            F.append({
                "layer": "detection", "severity": "high" if rec < 0.85 else "medium",
                "fix": "train", "cost": "GPU + labelling",
                "what": f"{frame['missed']} of {frame['gold_boxes']} vehicles were never "
                        f"detected ({rec * 100:.1f}% recall).",
                "why": "A missed vehicle is lost for good — no downstream step can "
                       "recover it, so this is a straight under-count.",
            })
        if cls is not None and cls < 0.95:
            F.append({
                "layer": "classification", "severity": "high" if cls < 0.85 else "medium",
                "fix": "train", "cost": "GPU + labelling",
                "what": f"{frame['wrong_class']} vehicle(s) were found but put in the "
                        f"wrong class ({cls * 100:.1f}% correct).",
                "why": "The total stays right while the proforma columns are wrong, so "
                       "this is invisible in a headline count.",
            })
        if frame.get("false_positives"):
            F.append({
                "layer": "detection", "severity": "medium", "fix": "threshold",
                "cost": "free",
                "what": f"{frame['false_positives']} box(es) were found where the human "
                        f"marked nothing.",
                "why": "Raising the confidence floor removes these, but costs recall — "
                       "check the recall number above before touching it.",
            })

    if count and not count.get("error"):
        tracks = count.get("tracks_total", 0) or 1
        short = count.get("dropped_too_short", 0)
        dup = count.get("dropped_duplicate", 0)
        offseg = count.get("crossings_off_segment", 0)
        frag = count.get("fragments_merged", 0)
        counted = count.get("counted_crossings", 0) or 1

        if short / tracks > 0.12:
            F.append({
                "layer": "tracking", "severity": "high" if short / tracks > 0.25 else "medium",
                "fix": "tracker", "cost": "re-extract",
                "what": f"{short} of {tracks} tracks ({short / tracks * 100:.0f}%) were "
                        f"too short to count.",
                "why": "Short tracks are usually one vehicle broken into pieces by an "
                       "occlusion. Loosening the tracker's match distance or raising its "
                       "buffer needs no retraining, but tracking is currently bundled into "
                       "detection, so it does need a re-extraction pass.",
            })
        if offseg / counted > 0.20:
            F.append({
                "layer": "counting", "severity": "high" if offseg / counted > 0.5 else "medium",
                "fix": "line", "cost": "free",
                "what": f"{offseg} crossings were rejected for happening outside the "
                        f"drawn line segment, against {counted} counted.",
                "why": "Either the line is too short for the carriageway, or real traffic "
                       "is passing beyond its ends. Redrawing the line is free and "
                       "instant — trajectories are already stored.",
            })
        if dup / tracks > 0.10:
            F.append({
                "layer": "counting", "severity": "medium", "fix": "dedup", "cost": "free",
                "what": f"{dup} of {tracks} tracks ({dup / tracks * 100:.0f}%) were "
                        f"suppressed as duplicates.",
                "why": "Some of these are genuine rider-boxes. But the duplicate guard "
                       "has swallowed real convoy vehicles here before, so a high rate "
                       "is worth eyeballing before it is trusted.",
            })
        if frag / tracks > 0.15:
            F.append({
                "layer": "tracking", "severity": "low", "fix": "tracker", "cost": "re-extract",
                "what": f"{frag} of {tracks} tracks were joined back together as "
                        f"fragments of one vehicle.",
                "why": "The joiner is working, but a high rate means the tracker is "
                       "fragmenting a lot — worth tuning at the source.",
            })

    # Free fixes first: they are reversible, instant, and historically where the large
    # errors actually were.
    rank = {"free": 0, "threshold": 1, "re-extract": 2, "GPU + labelling": 3}
    sev = {"high": 0, "medium": 1, "low": 2}
    F.sort(key=lambda f: (rank.get(f["cost"], 3), sev.get(f["severity"], 3)))

    out["verdict"] = _verdict(F, frame, count, out.get("gold_validation"))
    return out


def _verdict(findings, frame, count, val=None):
    """One sentence answering the question the user actually asked."""
    if val and val.get("circular"):
        return ("Detection and classification are UNMEASURED: " + val["note"] +
                " The counting-layer findings below are still valid.")
    if val and val.get("thin"):
        return ("Detection and classification are only weakly measured — " + val["note"] +
                " The counting-layer findings below are unaffected.")
    if not frame and not count:
        return ("Nothing measured yet. Label a gold set for the station and draw a count "
                "line on one of its videos.")
    if not frame:
        return ("Counting-layer decisions measured, but detection and classification are "
                "unmeasured — a gold set is needed before a training decision can be made.")
    train = [f for f in findings if f["fix"] == "train"]
    free = [f for f in findings if f["cost"] == "free"]
    reextract = [f for f in findings if f["cost"] == "re-extract"]
    if not train and not free:
        return ("No error source stands out. Detection, classification and the counting "
                "logic are all within tolerance on what has been measured.")
    if train and free:
        return (f"{len(free)} free fix(es) and {len(train)} model-level problem(s). Do the "
                f"free ones first and re-measure — they are instant and have historically "
                f"been the larger error here.")
    if free or reextract:
        bits = []
        if free:
            bits.append(f"{len(free)} free fix(es)")
        if reextract:
            bits.append(f"{len(reextract)} needing a re-extraction pass")
        return (" and ".join(bits) + ", and no model-level problem detected. Training a "
                "station model would not address what is actually wrong.")
    return (f"{len(train)} model-level problem(s) and no free fix outstanding. This is the "
            f"case where a station-specific model earns its cost.")
