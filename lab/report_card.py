"""The report card: everything the deliverable says, as data you can look at.

The xlsx is what the client receives. This is what tells you whether the xlsx is any good
*before* it is sent -- and a spreadsheet is a poor instrument for that, because the
things that go wrong are shapes rather than cells: an hour with no traffic at three in the
afternoon, a class that suddenly stops appearing, a direction split that drifts to 90/10,
a quarter-hour with four times its neighbours.

So this returns the same numbers the workbook contains plus the checks a reader would
have to do by eye, already done.

PCU factors are IRC:64-1990. They are included because volume in PCU, not vehicle count,
is what capacity analysis actually uses -- a report giving only headcount is only half
the deliverable.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import db

# IRC:64-1990 passenger-car-unit equivalents.
PCU = {
    "2W": 0.5, "3W_Auto": 1.0, "Car_Jeep_Van": 1.0, "LCV": 1.5, "Mini_Bus": 1.5,
    "Bus": 3.0, "Tractor": 1.5, "Tractor_Trailer": 4.5, "2Axle_Truck": 3.0,
    "3Axle_Truck": 3.0, "MAV": 4.5, "Cycle": 0.5, "Cycle_Rickshaw": 2.0,
    "Animal_Cart": 6.0, "Other": 1.0,
}


def _lines(video_id):
    """The line for this video: its own override, else its station's default.

    Asked of `sites`, which both apps have, rather than of the Lab's `stations`. The two
    carry the same implementation, but reaching for `stations` pulled in the whole Lab
    pipeline -- training, pods, judges -- for a single lookup, which put this module out
    of reach of any third app that only wants to produce a report.
    """
    import sites
    return sites.lines_for(video_id)[0]


def build(video_id):
    import counting
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    if not v:
        return {"error": "no such video"}
    lines = _lines(video_id)
    if not lines:
        return {"error": "no count line drawn on this video"}

    r = counting.count_video(video_id, lines)
    events = r["events"]
    per_class = r["per_class"]

    # ── totals and PCU ──
    totals = {k: c.get("total", 0) for k, c in per_class.items()}
    n = sum(totals.values())
    pcu_by_class = {k: round(vv * PCU.get(k, 1.0), 1) for k, vv in totals.items()}
    pcu_total = round(sum(pcu_by_class.values()), 1)

    # ── direction split, per line ──
    directions = defaultdict(Counter)
    for e in events:
        directions[e["line"]][e["direction"]] += 1
    dir_split = {ln: {"in": c.get("in", 0), "out": c.get("out", 0),
                      "total": c.get("in", 0) + c.get("out", 0)}
                 for ln, c in directions.items()}

    # ── the 15-minute grid, ZERO-FILLED across the whole covered span ──
    # An absent quarter-hour and a quarter-hour with no traffic are different facts, and a
    # report that simply omits the empty ones silently changes the shape of the day.
    bins, hours = _grid(v, events)

    out = {
        "video": {"id": v["id"], "name": v["name"], "start_clock": v["start_clock"],
                  "fps": v["fps"], "frames": v["frames"],
                  "duration_min": round((v["frames"] or 0) / (v["fps"] or 25) / 60, 1)},
        "lines": [ln.get("name") for ln in lines],
        "total": n,
        "per_class": totals,
        "pcu_by_class": pcu_by_class,
        "pcu_total": pcu_total,
        "pcu_per_vehicle": round(pcu_total / n, 2) if n else None,
        "direction": dir_split,
        "bins_15min": bins,
        "hourly": hours,
        "peak": _peak(hours),
        "composition": _composition(totals, n),
        "attributes": _attributes(video_id, events),
        "checks": _checks(totals, n, bins, dir_split, r.get("diagnostics", {})),
        "diagnostics": r.get("diagnostics", {}),
    }
    return out


def _grid(v, events):
    """Zero-filled 15-minute bins and hourly rollup across the covered span."""
    if not v.get("start_clock"):
        return [], []
    try:
        start = datetime.strptime(v["start_clock"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return [], []
    dur_s = (v["frames"] or 0) / (v["fps"] or 25)
    end = start + timedelta(seconds=dur_s)

    counts = Counter()
    for e in events:
        ts = start + timedelta(seconds=e["time_s"])
        counts[ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)] += 1

    bins, t = [], start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    while t < end:
        # Partial coverage is flagged rather than silently reported as a low count.
        covered = max(0.0, min((t + timedelta(minutes=15)).timestamp(), end.timestamp())
                      - max(t.timestamp(), start.timestamp())) / 900
        bins.append({"t": t.strftime("%Y-%m-%d %H:%M"), "label": t.strftime("%H:%M"),
                     "n": counts.get(t, 0), "coverage": round(covered, 2),
                     "partial": covered < 0.98})
        t += timedelta(minutes=15)

    hourly = defaultdict(lambda: {"n": 0, "coverage": 0.0})
    for b in bins:
        h = b["t"][:13] + ":00"
        hourly[h]["n"] += b["n"]
        hourly[h]["coverage"] += b["coverage"] / 4
    hours = [{"t": k, "label": k[11:16], "n": vv["n"],
              "coverage": round(vv["coverage"], 2), "partial": vv["coverage"] < 0.98}
             for k, vv in sorted(hourly.items())]
    return bins, hours


def _attributes(video_id, events):
    """The sub-splits inside a class: govt vs private bus, taxi vs private car, maxi auto.

    These are answers a person gave on the Verify screen, and until now they went into
    track_attrs and stopped there -- the card said "Bus 8" whether or not every one of
    those eight had been marked APSRTC. That is a real omission, not cosmetic: the
    operator split is a line item in a MoRTH classified count, and it is the one number
    the counter cannot get from the detector.

    It is a *breakdown*, never an extra class. An APSRTC bus is a Bus, carries a Bus's
    3.0 PCU, and is already inside per_class -- adding it again would double-count.
    """
    counted = {e["track_id"] for e in events}
    if not counted:
        return []
    cls_of = {e["track_id"]: e["class"] for e in events}
    rows = db.rows("SELECT track_id, attr, value FROM track_attrs WHERE video_id=?", video_id)

    LABELS = {"apsrtc": ("Govt / APSRTC bus", "private / other operator"),
              "taxi":   ("Taxi (yellow plate)", "private car"),
              "maxi":   ("7-seater / maxi",     "standard auto")}
    PARENTS = {"apsrtc": ("Bus", "Mini_Bus"), "taxi": ("Car_Jeep_Van",), "maxi": ("3W_Auto",)}

    out = []
    for attr, (yes_label, no_label) in LABELS.items():
        parents = PARENTS[attr]
        pool = [t for t in counted if cls_of.get(t) in parents]
        if not pool:
            continue
        marked = {r["track_id"] for r in rows if r["attr"] == attr and r["track_id"] in counted}
        yes = len(marked & set(pool))
        # Unasked is not the same as "no". A card that reports 2 govt buses out of 8 when
        # six were never reviewed is stating a split it does not have.
        answered = len({r["track_id"] for r in rows if r["track_id"] in pool})
        out.append({
            "attr": attr, "of_class": " + ".join(parents),
            "yes_label": yes_label, "no_label": no_label,
            "yes": yes, "no": max(0, answered - yes),
            "unreviewed": len(pool) - answered, "pool": len(pool),
        })
    return out


def _peak(hours):
    full = [h for h in hours if not h["partial"]] or hours
    if not full:
        return None
    p = max(full, key=lambda h: h["n"])
    return {"hour": p["label"], "n": p["n"],
            "note": "busiest fully-covered hour" if not p["partial"] else "coverage is partial"}


def _composition(totals, n):
    if not n:
        return []
    return [{"class": k, "n": vv, "share": round(100 * vv / n, 1)}
            for k, vv in sorted(totals.items(), key=lambda kv: -kv[1])]


def _checks(totals, n, bins, dir_split, diag):
    """The eyeball checks, done. Each returns a fact, not a score."""
    out = []
    if not n:
        out.append({"level": "bad", "what": "No vehicles counted at all.",
                    "why": "Either the line is in the wrong place or nothing was extracted."})
        return out

    full = [b for b in bins if not b["partial"]]
    if full:
        vals = sorted(b["n"] for b in full)
        med = vals[len(vals) // 2]
        empty = [b for b in full if b["n"] == 0]
        if empty:
            out.append({"level": "warn",
                        "what": f"{len(empty)} fully-covered quarter-hour(s) counted zero vehicles.",
                        "why": "Real on a quiet rural road at night; suspicious in daylight — "
                               "check the line still sits on the carriageway there."})
        spikes = [b for b in full if med and b["n"] > 4 * med]
        if spikes:
            out.append({"level": "warn",
                        "what": f"{len(spikes)} quarter-hour(s) counted more than 4x the median "
                                f"({med}).",
                        "why": "A spike this sharp is usually double-counting rather than traffic."})

    for ln, d in dir_split.items():
        if d["total"] >= 30:
            share = d["in"] / d["total"]
            if share > 0.85 or share < 0.15:
                out.append({"level": "warn",
                            "what": f"Line '{ln}' is {share * 100:.0f}% one direction "
                                    f"({d['in']} in / {d['out']} out).",
                            "why": "A near-one-way split on a two-way road usually means the "
                                   "line only covers one carriageway."})

    absent = [k for k in ("2W", "Car_Jeep_Van") if not totals.get(k)]
    if absent:
        out.append({"level": "warn",
                    "what": f"No {', '.join(absent)} counted anywhere.",
                    "why": "These are the commonest classes on an Indian road; zero of them "
                           "points at a classification or line problem, not at the traffic."})

    off = diag.get("crossings_off_segment", 0)
    if off > 0.2 * n:
        out.append({"level": "warn",
                    "what": f"{off} crossings were rejected for falling outside the drawn line, "
                            f"against {n} counted.",
                    "why": "The line is probably too short for the carriageway."})

    if not out:
        out.append({"level": "ok", "what": "Nothing anomalous in the shape of this count.",
                    "why": "Bins, direction split and class mix all look ordinary."})
    return out
