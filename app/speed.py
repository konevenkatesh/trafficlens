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


def save_trap(site_id, a, b, metres, expected_kmh=None, width_m=None):
    """`expected_kmh` is what the surveyor believes traffic actually does here.

    It exists because the generic sanity check was useless in practice. "Flag anything
    above 110 km/h" passes a rural road reading 90 when the person who has stood there
    knows it never exceeds 60 -- and the cause, a mismeasured distance, is exactly what a
    check is for. Nobody can set that threshold from the footage; the surveyor can set it
    from memory in five seconds.
    """
    if not (a and b and metres):
        raise ValueError("a speed trap needs two lines and the distance between them")
    metres = float(metres)
    if not 2.0 <= metres <= 500.0:
        raise ValueError("the distance between the lines should be between 2 and 500 m")
    trap = {"a": a, "b": b, "metres": metres}
    if width_m:
        width_m = float(width_m)
        if not 2.0 <= width_m <= 60.0:
            raise ValueError("the carriageway width should be between 2 and 60 m")
        trap["width_m"] = width_m
    if expected_kmh:
        expected_kmh = float(expected_kmh)
        if not 10.0 <= expected_kmh <= 150.0:
            raise ValueError("the expected speed should be between 10 and 150 km/h")
        trap["expected_kmh"] = expected_kmh
    db.run("UPDATE sites SET speed_trap=? WHERE id=?", db.jdump(trap), site_id)
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


def summary(rows, trap=None):
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
        expected = (trap or {}).get("expected_kmh")
        metres = (trap or {}).get("metres")
        if expected and p85:
            # Speed is exactly proportional to the trap distance, so a reading that is
            # 2x too fast means a distance 2x too long. Rather than say "this looks
            # wrong", say what the distance would have to be -- that is a checkable claim
            # the surveyor can take back to the road, and it is how the 18m guess on this
            # station was caught: expecting 60 implied 8.6m, and the road turned out to
            # have 9m dash spacing.
            if p85 > expected * 1.15:
                msg = (f"the 85th percentile is {p85:.0f} km/h but you expect about "
                       f"{expected:.0f} here.")
                if metres:
                    msg += (f" That points at the distance: {metres * expected / p85:.1f} m "
                            f"would give {expected:.0f}, against the {metres:.1f} m entered.")
                warnings.append(msg + " Re-check the measurement before quoting these.")
            elif p85 < expected * 0.6:
                msg = (f"the 85th percentile is {p85:.0f} km/h against the "
                       f"{expected:.0f} you expect.")
                if metres:
                    msg += (f" {metres * expected / p85:.1f} m would give {expected:.0f}.")
                warnings.append(msg + " Either the distance is short or the trap is "
                                "catching vehicles slowing for something.")
        elif p85 and p85 > 110:
            # No expectation set, so all that can be said is that this is fast for any
            # road a survey like this is run on.
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


# ───────────────────────── speed from the whole trajectory ─────────────────────────
# The trap times two crossings. That wastes almost everything the tracker produced: a
# vehicle is seen at twenty-odd positions, and only two of them are used. Worse, only the
# vehicles that happen to cross BOTH lines are measured at all -- 18% of traffic on the
# rural test footage, and 12% of the motorcycles, which are half of it. An overall speed
# from that sample is a car speed wearing a mixed-traffic label.
#
# With the carriageway width as well as the distance between the lines, the four line
# endpoints are the corners of a rectangle of known size on the road, and that is a full
# image-to-ground mapping. Every tracked position becomes metres; speed is the slope of a
# straight-line fit of position against time over the whole track. Measured on the same
# footage: 65% of vehicles instead of 18%, motorcycles at 65% instead of 12%, and a
# per-vehicle error of about 6% instead of 7.4% -- less than the naive N^1.5 gain
# promises, because detection jitter is strongly correlated frame to frame (lag-1
# autocorrelation 0.85), but better on every axis and without the sampling bias.
#
# The trap is kept as the cross-check: a vehicle that both methods measure should agree.

# Ground points outside this margin around the calibrated rectangle are not used. A
# homography from four points is exact inside them and extrapolates badly beyond, and
# far-distance points also carry the most jitter in ground terms.
# 0.35, not 1.0. With a full rectangle-width of extrapolation allowed, motorcycles riding
# the shoulder -- outside the calibrated quad -- were being measured through the part of
# the mapping that is least trustworthy, and read four times faster than the cars beside
# them. A homography is exact inside its four points and degrades quickly past them.
MARGIN = 0.35           # times the rectangle's own size, each side
MIN_GROUND_M = 4.0      # a track must cover this much road to give a speed
MIN_SECONDS = 0.5


def _homography(trap):
    """Image -> ground metres, or None if the trap has no width."""
    import cv2
    import numpy as np
    W, D = trap.get("width_m"), trap.get("metres")
    if not W or not D:
        return None
    a0, a1 = np.array(trap["a"]["start"], float), np.array(trap["a"]["end"], float)
    b0, b1 = np.array(trap["b"]["start"], float), np.array(trap["b"]["end"], float)
    # The surveyor may have drawn B right-to-left. Pair each B endpoint with the A
    # endpoint it is nearest to in the image, so the rectangle does not fold over.
    if np.linalg.norm(b0 - a0) + np.linalg.norm(b1 - a1) > \
            np.linalg.norm(b1 - a0) + np.linalg.norm(b0 - a1):
        b0, b1 = b1, b0
    src = np.array([a0, a1, b1, b0], np.float32)
    dst = np.array([[0, 0], [W, 0], [W, D], [0, D]], np.float32)
    try:
        return cv2.getPerspectiveTransform(src, dst)
    except cv2.error:
        return None


def speeds_by_trajectory(video_id, trap):
    """One reading per vehicle whose track covers enough calibrated road, in km/h."""
    import cv2
    import numpy as np
    H = _homography(trap)
    v = db.one("SELECT fps FROM videos WHERE id=?", video_id)
    if H is None or not v or not v["fps"]:
        return []
    fps = float(v["fps"])
    W, D = float(trap["width_m"]), float(trap["metres"])
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
        pts = np.array([[x, y] for _f, x, y in path], np.float32).reshape(-1, 1, 2)
        g = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
        t = np.array([f for f, _x, _y in path], float) / fps
        keep = ((g[:, 0] > -MARGIN * W) & (g[:, 0] < (1 + MARGIN) * W)
                & (g[:, 1] > -MARGIN * D) & (g[:, 1] < (1 + MARGIN) * D))
        if keep.sum() < MIN_POINTS:
            continue
        g, t = g[keep], t[keep]
        span = t[-1] - t[0]
        covered = float(np.hypot(*(g[-1] - g[0])))
        if span < MIN_SECONDS or covered < MIN_GROUND_M:
            continue
        # Straight-line fit of each ground axis against time. Its slope is a velocity
        # component; the fit uses every point rather than the two ends.
        vx = np.polyfit(t, g[:, 0], 1)[0]
        vy = np.polyfit(t, g[:, 1], 1)[0]
        kmh = float(np.hypot(vx, vy)) * 3.6
        if not (MIN_KMH <= kmh <= MAX_KMH):
            continue
        tr = tracks[tid]
        out.append({
            "track_id": tid,
            "cls": tr["class_override"] if tr["class_override"] is not None else tr["cls"],
            "kmh": round(kmh, 1),
            "seconds": round(float(span), 3),
            "metres": round(covered, 1),
            "points": int(keep.sum()),
            "direction": "a_to_b" if vy > 0 else "b_to_a",
        })
    return out


def cross_check(traj_rows, trap_rows):
    """How well the two methods agree on the vehicles both of them measured.

    A systematic gap here is a geometry problem -- most likely the width, which only the
    trajectory method uses -- and it is caught before anyone quotes a number.
    """
    a = {r["track_id"]: r["kmh"] for r in traj_rows}
    b = {r["track_id"]: r["kmh"] for r in trap_rows}
    both = [(a[k], b[k]) for k in a if k in b and b[k] > 0]
    if len(both) < 8:
        return {"n": len(both)}
    ratios = sorted(x / y for x, y in both)
    med = ratios[len(ratios) // 2]
    out = {"n": len(both), "trajectory_over_trap": round(med, 3)}
    if abs(med - 1.0) > 0.12:
        out["warning"] = (
            f"on {len(both)} vehicles both methods measured, the trajectory speed is "
            f"{med:.2f}x the two-line speed. They should agree within a few percent. The "
            f"width you entered is the usual cause: check the carriageway width and that "
            f"each line spans exactly that width.")
    return out
