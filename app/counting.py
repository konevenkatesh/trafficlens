"""Counting = queries over stored trajectories. Redraw lines -> instant recount."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import db
from engine import CLASSES


MIN_TRACK_FRAMES = 12  # tracks shorter than ~0.5s cannot count (phantom filter)
# How far back a track's own velocity may be projected to credit a crossing it was
# detected too late to witness. Measured on FID-33: 1.2s recovered 7 vehicles per clip,
# 3s recovers ~30. Longer than this and the evidence gets too thin to trust.
BIRTH_LOOKBACK_S = 3.0
BIRTH_MIN_RUN = 6      # consecutive frames of the same object before its heading is trusted


def count_video(video_id, lines, birth_lookback_s=None):
    """lines: [{name, start:[x,y], end:[x,y]}] in source-pixel coords.
    Returns events + per-class/direction summary + 15-min bins.

    `birth_lookback_s` overrides BIRTH_LOOKBACK_S for one call. It is a parameter and
    not a module global anyone reassigns: the review screen counts the same video twice
    with different settings, and doing that by patching the global would let a report
    card requested at the same moment be counted with the review's setting instead of
    its own -- a wrong number, produced silently, in the deliverable."""
    lookback = BIRTH_LOOKBACK_S if birth_lookback_s is None else birth_lookback_s
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    tracks = {t["track_id"]: t for t in db.rows(
        "SELECT * FROM tracks WHERE video_id=?", video_id)}
    # resolve fragment chains: follow join_to to the root track
    def root(tid, _depth=0):
        t = tracks.get(tid)
        if not t or t.get("join_to") is None or _depth > 20:
            return tid
        return root(t["join_to"], _depth + 1)
    pts = db.rows(
        "SELECT track_id, frame, x1, y1, x2, y2 FROM track_points "
        "WHERE video_id=? ORDER BY track_id, frame", video_id)
    traj = defaultdict(list)
    boxes = defaultdict(dict)
    for p in pts:
        r = root(p["track_id"])
        traj[r].append((p["frame"], (p["x1"] + p["x2"]) / 2, p["y2"]))
        boxes[r][p["frame"]] = (p["x1"], p["y1"], p["x2"], p["y2"])
    for path in traj.values():
        path.sort(key=lambda x: x[0])

    def same_vehicle(a_tid, b_tid):
        """Split-on-line fragments are either sequential or their boxes sit glued
        while both exist; two convoy vehicles coexist with near-zero overlap."""
        A, B = boxes.get(a_tid, {}), boxes.get(b_tid, {})
        shared = set(A) & set(B)
        if len(shared) < 3:
            return True
        s = 0.0
        for f in shared:
            ax1, ay1, ax2, ay2 = A[f]
            bx1, by1, bx2, by2 = B[f]
            ix = max(0, min(ax2, bx2) - max(ax1, bx1))
            iy = max(0, min(ay2, by2) - max(ay1, by1))
            sm = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
            s += ix * iy / max(sm, 1)
        return s / len(shared) >= 0.3

    start = datetime.strptime(v["start_clock"], "%Y-%m-%d %H:%M:%S")
    events = []
    # Why a track did or did not produce a count. Recorded here rather than
    # re-derived elsewhere, so the diagnosis can never drift from the real logic.
    diag = Counter()
    diag["tracks_total"] = len(tracks)
    diag["fragments_merged"] = sum(1 for t in tracks.values() if t.get("join_to") is not None)
    for ln in lines:
        (lx1, ly1), (lx2, ly2) = ln["start"], ln["end"]
        dx, dy = lx2 - lx1, ly2 - ly1
        L2 = dx * dx + dy * dy

        def on_segment(px, py):
            # crossings only count within the drawn segment (parked vehicles past
            # the endpoints jitter across the infinite line and phantom-count)
            t = ((px - lx1) * dx + (py - ly1) * dy) / L2
            return -0.08 <= t <= 1.08
        # How many source frames apart the stored points are. The detector may have run
        # with a stride -- 30fps phone video is sampled down to ~15 -- so a track that
        # lasted a full second holds fewer points than it would have at stride 1.
        # Thresholds counted in POINTS have to be divided by that, or striding silently
        # drops real vehicles for being "too short" when they were nothing of the kind.
        # Measured from the data rather than passed in, so it is right for footage
        # extracted before this existed.
        gaps = []
        for _p in traj.values():
            for a, b in zip(_p, _p[1:]):
                if b[0] > a[0]:
                    gaps.append(b[0] - a[0])
                if len(gaps) > 400:
                    break
            if len(gaps) > 400:
                break
        stride = min(gaps) if gaps else 1
        min_points = max(2, int(round(MIN_TRACK_FRAMES / stride)))
        diag["frame_stride"] = stride

        for tid, path in traj.items():
            t = tracks.get(tid)
            if not t or len(path) < min_points:
                diag["dropped_too_short"] += 1
                continue
            ov = t["class_override"]
            if ov == -1:
                diag["dropped_not_vehicle"] += 1
                continue  # human marked not-a-vehicle
            if t.get("dup_of") is not None:
                diag["dropped_duplicate"] += 1
                continue  # duplicate track (rider-box etc.) - suppressed
            diag["eligible"] += 1
            cls = ov if ov is not None else t["cls"]
            raw = []
            # Implied birth-crossing: the detector often picks a vehicle up only AFTER it
            # has passed the line, so the track never changes side and would never count.
            # Back-project the track's initial velocity and, if it came from the other
            # side, credit the crossing.
            #
            # Two things here were measured, not guessed:
            #   * the window was 1.2s, which caught 7 of ~30 recoverable vehicles on a
            #     15-minute clip. 3s catches most of the rest.
            #   * the on-segment test was applied to where the track was BORN, which is
            #     the wrong point -- a vehicle can appear off to the side and still have
            #     crossed within the drawn segment. It now tests the back-projected
            #     CROSSING point, which is the thing that actually has to be on the line.
            # The >1 px/frame speed gate is deliberately unchanged: it rejects 48 tracks
            # per clip that never move at all -- parked vehicles, and shadows of branches
            # the detector reads as cars.
            # The birth end needs its own evidence: a crossing is being credited that
            # nobody witnessed, so it must rest on a real, sustained track rather than a
            # one-frame stub that happened to be joined onto something longer.
            # The velocity must come from CONSECUTIVE observations of the same object.
            # Reading it from path[0] to path[6] regardless of gaps is what let a
            # three-frame shadow on the verge, joined onto a van that appeared 19 frames
            # later, back-project a crossing that never happened: the "velocity" was the
            # displacement between two different things. So the estimate uses only the
            # unbroken run the track opens with, and refuses to guess from a stub.
            span = path[-1][0] - path[0][0] + 1
            run = 1
            while run < len(path) and path[run][0] - path[run - 1][0] <= 2 * stride:
                run += 1
            # span stays in FRAMES (a real duration); the point counts are scaled.
            if (len(path) >= min_points and span >= MIN_TRACK_FRAMES
                    and run >= max(2, int(round(BIRTH_MIN_RUN / stride)))):
                f0, x0, y0 = path[0]
                f1_, x1_, y1_ = path[min(6, run - 1)]
                vx = (x1_ - x0) / max(f1_ - f0, 1)
                vy = (y1_ - y0) / max(f1_ - f0, 1)
                speed = (vx * vx + vy * vy) ** 0.5
                cross = dx * vy - dy * vx          # 0 when travelling parallel to the line
                if speed > 1.0 and abs(cross) > 1e-9:
                    side_val = dx * (y0 - ly1) - dy * (x0 - lx1)
                    s = side_val / cross           # frames back to the line
                    if 0 < s <= lookback * v["fps"]:
                        cx_, cy_ = x0 - vx * s, y0 - vy * s
                        if on_segment(cx_, cy_):
                            side0 = 1 if side_val > 0 else -1
                            raw.append((f0, "in" if side0 < 0 else "out"))
                            diag["implied_birth_crossings"] += 1
                        else:
                            diag["implied_birth_off_segment"] += 1
            prev = None
            for f, px, py in path:
                side = 1 if (dx * (py - ly1) - dy * (px - lx1)) > 0 else -1
                if prev is not None and side != prev:
                    if on_segment(px, py):
                        raw.append((f, "in" if side < 0 else "out"))
                    else:
                        # crossed the infinite line but outside the drawn segment
                        diag["crossings_off_segment"] += 1
                prev = side
            # debounce: adjacent opposite crossings within ~2s are line-jitter -> cancel pair
            JIT = int(2.0 * v["fps"])
            stack = []
            for f, d in raw:
                if stack and stack[-1][1] != d and f - stack[-1][0] <= JIT:
                    stack.pop()
                    diag["crossings_debounced"] += 2
                else:
                    stack.append((f, d))
            pos = {f: (px, py) for f, px, py in path}
            for f, d in stack:
                ts = start + timedelta(seconds=f / v["fps"])
                px, py = pos.get(f, (0, 0))
                events.append({"frame": f, "clock": ts.strftime("%H:%M:%S"),
                               "time_s": round(f / v["fps"], 2), "track_id": tid,
                               "class": CLASSES[cls], "line": ln["name"],
                               "direction": d, "px": round(px), "py": round(py)})
    events.sort(key=lambda e: e["frame"])
    guard = []
    for e in events:
        dupe = False
        for g in reversed(guard):
            if e["frame"] - g["frame"] > 1.5 * v["fps"]:
                break
            if (g["line"] == e["line"] and g["direction"] == e["direction"]
                    and g["class"] == e["class"] and g["track_id"] != e["track_id"]
                    and abs(g.get("px", 0) - e.get("px", 0)) < 140
                    and abs(g.get("py", 0) - e.get("py", 0)) < 140
                    and same_vehicle(g["track_id"], e["track_id"])):
                dupe = True
                break
        if dupe:
            diag["crossings_deduped"] += 1
        else:
            guard.append(e)
    events = guard

    summary = defaultdict(Counter)
    bins = defaultdict(Counter)
    for e in events:
        summary[e["class"]][e["line"] + "_" + e["direction"]] += 1
        summary[e["class"]]["total"] += 1
        ts = start + timedelta(seconds=e["time_s"])
        b = ts.replace(minute=(ts.minute // 15) * 15, second=0).strftime("%H:%M")
        bins[b][e["class"]] += 1
    diag["counted_crossings"] = len(events)
    diag["tracks_that_counted"] = len({e["track_id"] for e in events})
    return {"events": events, "total": len(events),
            "per_class": {k: dict(c) for k, c in sorted(summary.items())},
            "bins_15min": {k: dict(c) for k, c in sorted(bins.items())},
            "diagnostics": dict(diag)}
