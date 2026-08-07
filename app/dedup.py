"""Duplicate-track suppression: kills rider-box double counting.

A rider box and its bike box form two tracks of the same class that travel
together, one mostly contained in the other. For every same-class track pair
with enough temporal overlap, we measure per-frame containment (IoA of the
smaller box in the larger). If they are 'glued' for most of their shared
frames, the smaller-area track is marked dup_of the larger and excluded from
counting and rendering. Retroactive - runs on stored trajectories.
"""
from collections import defaultdict

import db

MIN_SHARED = 8          # frames of temporal overlap (3 for newborn phantoms)
MEAN_IOA_THR = 0.30     # mean containment over shared frames (real pairs: 0.45-0.66, separate bikes: ~0.0)
CTR_DIST_FRAC = 0.45    # mean center distance < this fraction of big-box diagonal
OFF_STD_MAX = 60        # rigid co-movement: offset std (px)


def _boxes(video_id):
    out = defaultdict(dict)
    for p in db.rows("SELECT track_id, frame, x1, y1, x2, y2 FROM track_points "
                     "WHERE video_id=?", video_id):
        out[p["track_id"]][p["frame"]] = (p["x1"], p["y1"], p["x2"], p["y2"])
    return out


def dedup(video_id):
    # ensure column exists (migration for older DBs)
    cols = [c["name"] for c in db.rows("PRAGMA table_info(tracks)")]
    if "dup_of" not in cols:
        db.run("ALTER TABLE tracks ADD COLUMN dup_of INTEGER")
    db.run("UPDATE tracks SET dup_of=NULL WHERE video_id=?", video_id)

    tracks = db.rows("SELECT * FROM tracks WHERE video_id=?", video_id)
    boxes = _boxes(video_id)
    info = {}
    for t in tracks:
        tid = t["track_id"]
        bx = boxes.get(tid, {})
        if not bx:
            continue
        areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in bx.values()]
        cls = t["class_override"] if t["class_override"] is not None else t["cls"]
        info[tid] = {"cls": cls, "t0": t["t_start"], "t1": t["t_end"],
                     "median_area": sorted(areas)[len(areas) // 2], "bx": bx}

    # keepers: longest-lived first (stable real tracks), then larger area
    tids = sorted(info, key=lambda k: (-len(info[k]["bx"]), -info[k]["median_area"]))
    suppressed = {}
    for i, big in enumerate(tids):
        if big in suppressed:
            continue
        B = info[big]
        for small in tids[i + 1:]:
            if small in suppressed:
                continue
            S = info[small]
            if S["cls"] != B["cls"]:
                continue
            lo, hi = max(B["t0"], S["t0"]), min(B["t1"], S["t1"])
            shared = [f for f in S["bx"] if lo <= f <= hi and f in B["bx"]]
            s_len = len(S["bx"])
            newborn = s_len <= 20 and len(shared) >= 3 and len(shared) / s_len >= 0.7
            if len(shared) < MIN_SHARED and not newborn:
                continue
            ioas, dists, offs, diags = [], [], [], []
            for f in shared:
                sx1, sy1, sx2, sy2 = S["bx"][f]
                bx1, by1, bx2, by2 = B["bx"][f]
                iw = max(0, min(sx2, bx2) - max(sx1, bx1))
                ih = max(0, min(sy2, by2) - max(sy1, by1))
                sa = max((sx2 - sx1) * (sy2 - sy1), 1e-6)
                ioas.append(iw * ih / sa)
                cs = ((sx1 + sx2) / 2, (sy1 + sy2) / 2)
                cb = ((bx1 + bx2) / 2, (by1 + by2) / 2)
                dists.append(((cs[0]-cb[0])**2 + (cs[1]-cb[1])**2) ** 0.5)
                offs.append((cs[0]-cb[0], cs[1]-cb[1]))
                diags.append(((bx2-bx1)**2 + (by2-by1)**2) ** 0.5)
            n = len(shared)
            mean_ioa = sum(ioas) / n
            mean_dist = sum(dists) / n
            mean_diag = sum(diags) / n
            mox = sum(o[0] for o in offs) / n
            moy = sum(o[1] for o in offs) / n
            off_std = (sum((o[0]-mox)**2 + (o[1]-moy)**2 for o in offs) / n) ** 0.5
            overlapping = mean_ioa >= MEAN_IOA_THR
            comoving = (mean_dist <= CTR_DIST_FRAC * mean_diag and off_std <= OFF_STD_MAX
                        and mean_ioa >= 0.05)  # parallel separate bikes have ~zero overlap
            if overlapping or comoving:
                suppressed[small] = big
    for small, big in suppressed.items():
        db.run("UPDATE tracks SET dup_of=? WHERE video_id=? AND track_id=?",
               big, video_id, small)

    # --- pass 2: sequential fragment linking (same bike, broken track) ---
    if "join_to" not in cols and "join_to" not in [c["name"] for c in db.rows("PRAGMA table_info(tracks)")]:
        db.run("ALTER TABLE tracks ADD COLUMN join_to INTEGER")
    db.run("UPDATE tracks SET join_to=NULL WHERE video_id=?", video_id)
    MAX_GAP = 20       # frames between fragment end and next start
    MAX_PRED_DIST = 60 # px from velocity-extrapolated position
    alive = [t for t in info if t not in suppressed]
    ends, starts, vels = {}, {}, {}
    for tid in alive:
        bx = info[tid]["bx"]
        fs = sorted(bx)
        ends[tid] = (fs[-1], bx[fs[-1]])
        starts[tid] = (fs[0], bx[fs[0]])
        # velocity from the last few points (px/frame of the anchor)
        tail = fs[-min(6, len(fs)):]
        if len(tail) >= 2 and tail[-1] > tail[0]:
            b0, b1 = bx[tail[0]], bx[tail[-1]]
            dt = tail[-1] - tail[0]
            vels[tid] = (((b1[0]+b1[2])/2 - (b0[0]+b0[2])/2) / dt,
                         (b1[3] - b0[3]) / dt)
        else:
            vels[tid] = (0.0, 0.0)
    joins = {}
    by_start = sorted(alive, key=lambda t: starts[t][0])
    for a in alive:
        fe, be = ends[a]
        aex, aey = (be[0] + be[2]) / 2, be[3]
        best = None
        for b in by_start:
            fsb, bs = starts[b]
            if b == a or b in joins or fsb <= fe or fsb - fe > MAX_GAP:
                continue
            if info[b]["cls"] != info[a]["cls"]:
                continue
            bx_, by_ = (bs[0] + bs[2]) / 2, bs[3]
            gap = fsb - fe
            vx, vy = vels[a]
            px_, py_ = aex + vx * gap, aey + vy * gap   # where A should be now
            d = ((px_ - bx_) ** 2 + (py_ - by_) ** 2) ** 0.5
            speed = (vx * vx + vy * vy) ** 0.5
            allow = MAX_PRED_DIST + 0.35 * speed * gap   # radius grows with speed
            if d <= allow and (best is None or d < best[1]):
                best = (b, d)
        if best:
            joins[best[0]] = a   # b continues a
    for b, a in joins.items():
        db.run("UPDATE tracks SET join_to=? WHERE video_id=? AND track_id=?",
               a, video_id, b)
    return {"tracks": len(tracks), "suppressed": len(suppressed), "joined": len(joins)}
