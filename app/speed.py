"""Vehicle speed, from two lines and a tape measure.

**Why a trap and not a perspective model.** The obvious approach is to map pixels to the
ground plane and differentiate a trajectory. I tried that on rural footage and it produced
motorcycles at 70 km/h and cars at 36 on the same road, which is not a calibration error
you can tune away -- vehicles at different lateral offsets were being mapped through a
single road axis, so the far lane read slow. Worse, the calibration itself was ambiguous:
the lane markings I detected turned out not to be consecutive (a cross-ratio test put four
apparently-adjacent dashes at gaps of 1, 2 and 2), and assuming they were would have made
every speed 40% low with nothing in the output to suggest it.

A trap has none of that. Two lines, one measured distance, and the time between crossings.
It is what a spot-speed study does with a stopwatch, it needs no camera model, and its
error budget is small enough to write down:

  * the distance, which the surveyor measures once -- a 0.5m error over 30m is 1.7%
  * the timing, which is bounded by the frame interval

**Sub-frame timing matters more than it looks.** At 12 fps a frame is 83ms. A vehicle
crossing a 30m trap at 50 km/h takes 2.16s, so rounding each crossing to the nearest frame
is up to 7.7% of the answer. Both crossings are interpolated to the fraction of a frame at
which the path actually intersects the line, which removes almost all of it.

What this cannot do is give an instantaneous speed, or measure a vehicle that changes lane
out of the trap. It reports the mean speed over the measured stretch, which is what a spot
speed study reports anyway.
"""
import math

import db

# A vehicle must be seen on both sides of both lines. Below this many points there is not
# enough of a path to interpolate a crossing from, and a two-point "track" that clips a
# line corner produces a confident nonsense speed.
MIN_POINTS = 6

# Physically impossible readings are dropped rather than reported. These are not tuning
# knobs for making the distribution look nice -- they are the range outside which the
# measurement is certainly a tracking failure (an id swapped between two vehicles, or a
# detection that jumped across the frame).
MIN_KMH = 3.0
MAX_KMH = 150.0


def trap_for(site_id):
    """The saved trap: two lines and the distance between them, or None."""
    r = db.one("SELECT speed_trap FROM sites WHERE id=?", site_id)
    t = db.jload(r["speed_trap"], None) if r and r.get("speed_trap") else None
    if not t or not t.get("a") or not t.get("b") or not t.get("metres"):
        return None
    return t


def save_trap(site_id, a, b, metres):
    if not (a and b and metres):
        raise ValueError("a speed trap needs two lines and the distance between them")
    metres = float(metres)
    if not 2.0 <= metres <= 500.0:
        raise ValueError("the distance between the lines should be between 2 and 500 m")
    db.run("UPDATE sites SET speed_trap=? WHERE id=?",
           db.jdump({"a": a, "b": b, "metres": metres}), site_id)
    return trap_for(site_id)


def _cross_time(path, line):
    """The fractional frame at which this path crosses the line, or None.

    Returns the FIRST crossing. A vehicle that wanders back over a line -- which happens
    when a line is drawn along the direction of travel rather than across it -- would
    otherwise give whichever crossing came last, and a negative or absurd transit time.
    """
    (lx1, ly1), (lx2, ly2) = line["start"], line["end"]
    dx, dy = lx2 - lx1, ly2 - ly1
    seg2 = dx * dx + dy * dy
    if seg2 <= 0:
        return None

    def side(px, py):
        return dx * (py - ly1) - dy * (px - lx1)

    def within(px, py):
        # Only between the drawn endpoints. The infinite line runs off across the verge,
        # and a vehicle on the far shoulder must not register as a crossing.
        t = ((px - lx1) * dx + (py - ly1) * dy) / seg2
        return -0.05 <= t <= 1.05

    prev = None
    for f, px, py in path:
        s = side(px, py)
        if prev is not None:
            ps, pf, ppx, ppy = prev
            if (s > 0) != (ps > 0) and s != ps:
                # Linear interpolation between the two observations: the fraction of the
                # way from the previous point to this one where side() passes zero.
                r = ps / (ps - s)
                cx, cy = ppx + (px - ppx) * r, ppy + (py - ppy) * r
                if within(cx, cy):
                    return pf + (f - pf) * r
        prev = (s, f, px, py)
    return None


def speeds_for(video_id, trap):
    """One reading per vehicle that crossed both lines, in km/h.

    The ground point is the bottom centre of the box, because that is where the vehicle
    touches the road; the centre of the box rises and falls with the vehicle's height as
    perspective changes, which puts a metre or two of phantom movement into every path.
    """
    v = db.one("SELECT fps FROM videos WHERE id=?", video_id)
    if not v or not v["fps"] or not trap:
        return []
    fps = float(v["fps"])
    metres = float(trap["metres"])

    tracks = {t["track_id"]: t for t in db.rows(
        "SELECT track_id, cls, class_override, dup_of FROM tracks WHERE video_id=?",
        video_id)}
    paths = {}
    for p in db.rows("""SELECT track_id, frame, x1, y1, x2, y2 FROM track_points
                        WHERE video_id=? ORDER BY track_id, frame""", video_id):
        t = tracks.get(p["track_id"])
        if not t or t.get("dup_of") is not None:
            continue
        paths.setdefault(p["track_id"], []).append(
            (p["frame"], (p["x1"] + p["x2"]) / 2.0, p["y2"]))

    out = []
    for tid, path in paths.items():
        if len(path) < MIN_POINTS:
            continue
        fa = _cross_time(path, trap["a"])
        fb = _cross_time(path, trap["b"])
        if fa is None or fb is None:
            continue
        dt = abs(fb - fa) / fps
        if dt <= 0:
            continue
        kmh = metres / dt * 3.6
        if not (MIN_KMH <= kmh <= MAX_KMH):
            continue
        t = tracks[tid]
        out.append({
            "track_id": tid,
            "cls": t["class_override"] if t["class_override"] is not None else t["cls"],
            "kmh": round(kmh, 1),
            "seconds": round(dt, 3),
            # Which way through the trap, so the two directions can be reported apart --
            # they routinely differ, and averaging them hides a one-way problem.
            "direction": "a_to_b" if fb > fa else "b_to_a",
        })
    return out


def summary(rows):
    """The numbers a speed study actually reports.

    The 85th percentile is the one that matters and the one people forget: design speed
    and enforcement thresholds are set from it, not from the mean. The 15th is reported
    with it because the pair describes the spread that a single average destroys.
    """
    from engine import CLASSES
    if not rows:
        return {"n": 0}
    vals = sorted(r["kmh"] for r in rows)

    def pct(p):
        if not vals:
            return None
        k = (len(vals) - 1) * p / 100.0
        lo, hi = math.floor(k), math.ceil(k)
        return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1) if hi > lo \
            else round(vals[lo], 1)

    by_class, by_dir = {}, {}
    warnings = []
    for r in rows:
        by_class.setdefault(CLASSES[r["cls"]], []).append(r["kmh"])
        by_dir.setdefault(r["direction"], []).append(r["kmh"])
    # The two directions are the built-in check on whether the lines were drawn correctly.
    #
    # Two lines that look parallel on screen are NOT parallel on the ground: perspective
    # makes the gap between them wider on the far side of the road than the near side. So
    # one direction of travel crosses a longer trap than the other and reads faster, by a
    # lot -- a synthetic pair drawn parallel in image space gave 74 km/h one way and 114
    # the other on the same road in the same minute. Real traffic does not do that, so a
    # large split means the geometry is wrong rather than the traffic being interesting.
    #
    # Each line has to be drawn ACROSS the carriageway, square to the direction of travel.
    # Done that way the two readings converge, and this warning is what tells the surveyor
    # they have not done it.
    meds = [sorted(x)[len(x) // 2] for x in by_dir.values() if len(x) >= 8]
    if len(meds) == 2 and max(meds) > 0:
        split = abs(meds[0] - meds[1]) / max(meds) * 100
        if split > 15:
            warnings.append(
                f"the two directions disagree by {split:.0f}% ({min(meds):.0f} vs "
                f"{max(meds):.0f} km/h). Real traffic does not split like that — the two "
                f"lines are probably not square across the road. Redraw each one along "
                f"the carriageway rather than parallel to the other on screen.")
    if len(vals) >= 20:
        p85 = pct(85)
        if p85 and p85 > 110:
            warnings.append(
                f"an 85th percentile of {p85:.0f} km/h is higher than this kind of road "
                f"carries. The measured distance is the most likely cause — check it "
                f"before quoting any of these numbers.")
    return {
        "n": len(vals),
        "warnings": warnings,
        "mean": round(sum(vals) / len(vals), 1),
        "median": pct(50), "p15": pct(15), "p85": pct(85),
        "min": vals[0], "max": vals[-1],
        "by_class": {k: {"n": len(x), "median": round(sorted(x)[len(x) // 2], 1)}
                     for k, x in sorted(by_class.items(), key=lambda kv: -len(kv[1]))},
        "by_direction": {k: {"n": len(x), "median": round(sorted(x)[len(x) // 2], 1)}
                         for k, x in by_dir.items()},
    }


def accuracy_note(trap, fps, typical_kmh=50.0):
    """What this measurement can and cannot claim, in the units of this site.

    Stated rather than implied. A speed with no error bar gets quoted as exact, and the
    dominant term here is the surveyor's tape measure, not anything the software does.
    """
    metres = float(trap["metres"])
    transit = metres / (typical_kmh / 3.6)
    # Sub-frame interpolation leaves roughly a tenth of a frame at each end.
    timing = (0.2 / float(fps or 12)) / transit * 100
    return {
        "transit_s": round(transit, 2),
        "timing_error_pct": round(timing, 1),
        "distance_error_pct_per_half_metre": round(0.5 / metres * 100, 1),
        "note": (f"Over {metres:.0f} m at {typical_kmh:.0f} km/h a vehicle is in the trap "
                 f"for {transit:.1f} s. Frame timing costs about {timing:.1f}%. "
                 f"Every half metre of error in the measured distance costs "
                 f"{0.5 / metres * 100:.1f}%, so measure it once, carefully."),
    }
