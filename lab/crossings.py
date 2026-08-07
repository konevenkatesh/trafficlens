"""Look at the vehicles a counting change added or removed.

A count is a number, and a number cannot be argued with — you can only agree or disagree
with it. This turns a change in the total into a set of specific vehicles you can look at
and judge one at a time.

The picture that matters is not the crop. It is the frame where the track was first seen,
with the count line drawn, the vehicle boxed, and the **back-projected path** it is being
credited with drawn as a dashed line to where it must have crossed. That is the whole
claim the implied-birth rule makes, made visible: "this vehicle came from over there, so
it crossed before we saw it."

If that dashed line runs along the road and meets the line where a vehicle plainly would
have, the count is right. If it runs through a wall, it is not.
"""
import sys
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))


def _joined(video_id):
    """Trajectories with fragments merged, exactly as counting sees them."""
    from collections import defaultdict
    tracks = {t["track_id"]: t for t in
              db.rows("SELECT * FROM tracks WHERE video_id=?", video_id)}

    def root(tid, d=0):
        t = tracks.get(tid)
        if not t or t.get("join_to") is None or d > 20:
            return tid
        return root(t["join_to"], d + 1)

    traj = defaultdict(list)
    for p in db.rows("""SELECT track_id, frame, x1, y1, x2, y2 FROM track_points
                        WHERE video_id=? ORDER BY track_id, frame""", video_id):
        traj[root(p["track_id"])].append(p)
    for v in traj.values():
        v.sort(key=lambda p: p["frame"])
    return traj, tracks


def diff(video_id, lookback_a=0.0, lookback_b=None):
    """Which vehicles the change added, and what each one's evidence is.

    The default compares against `lookback_a = 0` -- counting with the implied-birth
    rule switched off entirely -- because that is the question worth reviewing: not
    "what did tuning a constant change", but "which vehicles am I being asked to
    believe in without ever having seen them cross".
    """
    import counting
    import stations
    lines = stations.lines_for(video_id)[0]
    if not lines:
        return {"error": "no count line for this video or its station"}
    if lookback_b is None:
        lookback_b = counting.BIRTH_LOOKBACK_S

    a = counting.count_video(video_id, lines, birth_lookback_s=lookback_a)
    b = counting.count_video(video_id, lines, birth_lookback_s=lookback_b)

    ev_a = {e["track_id"]: e for e in a["events"]}
    ev_b = {e["track_id"]: e for e in b["events"]}
    traj, tracks = _joined(video_id)

    added = []
    for tid in sorted(set(ev_b) - set(ev_a)):
        p = traj.get(tid, [])
        if not p:
            continue
        e = ev_b[tid]
        span = p[-1]["frame"] - p[0]["frame"] + 1
        # The unbroken opening run is the evidence the heading rests on -- shown
        # because a short run beside a long track is the signature of a spurious
        # birth (a shadow) joined onto a real vehicle.
        run = 1
        while run < len(p) and p[run]["frame"] - p[run - 1]["frame"] <= 2:
            run += 1
        j = min(6, run - 1)
        f0 = p[0]["frame"]
        vx = ((p[j]["x1"] + p[j]["x2"]) / 2 - (p[0]["x1"] + p[0]["x2"]) / 2) / max(p[j]["frame"] - f0, 1)
        vy = (p[j]["y2"] - p[0]["y2"]) / max(p[j]["frame"] - f0, 1)
        added.append({
            "track_id": tid, "frame": f0, "clock": e["clock"], "direction": e["direction"],
            "class": e["class"], "frames": len(p), "span": span, "run": run,
            "speed_px_per_frame": round((vx * vx + vy * vy) ** 0.5, 1),
            "max_box_h": round(max(q["y2"] - q["y1"] for q in p)),
        })
    return {"video_id": video_id, "before": a["total"], "after": b["total"],
            "added": added, "removed": sorted(set(ev_a) - set(ev_b)),
            "lookback_a": lookback_a, "lookback_b": lookback_b}


def review_image(video_id, track_id, out=None):
    """The frame where this track was first seen, with the claim drawn on it.

    Green box = the vehicle. Blue = the count line. Dashed orange = the back-projected
    path, ending at the point where the crossing is being credited.
    """
    import cv2
    import stations
    lines = stations.lines_for(video_id)[0]
    traj, _ = _joined(video_id)
    p = traj.get(track_id)
    v = db.one("SELECT path, fps FROM videos WHERE id=?", video_id)
    if not p or not v:
        return None

    f0 = p[0]["frame"]
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        return None

    # Same velocity estimate counting uses, or the drawn claim is not the made claim.
    run = 1
    while run < len(p) and p[run]["frame"] - p[run - 1]["frame"] <= 2:
        run += 1
    j = min(6, run - 1)
    x0 = (p[0]["x1"] + p[0]["x2"]) / 2
    y0 = p[0]["y2"]
    vx = ((p[j]["x1"] + p[j]["x2"]) / 2 - x0) / max(p[j]["frame"] - f0, 1)
    vy = (p[j]["y2"] - y0) / max(p[j]["frame"] - f0, 1)

    for ln in lines:
        (lx1, ly1), (lx2, ly2) = ln["start"], ln["end"]
        cv2.line(img, (int(lx1), int(ly1)), (int(lx2), int(ly2)), (230, 130, 40), 3)
        dx, dy = lx2 - lx1, ly2 - ly1
        cross = dx * vy - dy * vx
        if abs(cross) > 1e-9:
            s = (dx * (y0 - ly1) - dy * (x0 - lx1)) / cross
            if 0 < s <= 6 * (v["fps"] or 25):
                cx, cy = x0 - vx * s, y0 - vy * s
                # dashed back-projection, birth -> credited crossing point
                n = 22
                for k in range(0, n, 2):
                    a = (int(x0 + (cx - x0) * k / n), int(y0 + (cy - y0) * k / n))
                    b = (int(x0 + (cx - x0) * (k + 1) / n), int(y0 + (cy - y0) * (k + 1) / n))
                    cv2.line(img, a, b, (40, 150, 240), 2)
                cv2.circle(img, (int(cx), int(cy)), 8, (40, 150, 240), -1)
                cv2.circle(img, (int(cx), int(cy)), 8, (255, 255, 255), 2)

    b0 = p[0]
    cv2.rectangle(img, (int(b0["x1"]), int(b0["y1"])), (int(b0["x2"]), int(b0["y2"])),
                  (80, 220, 120), 3)
    # the rest of the track, so its real direction of travel is visible
    for k in range(1, len(p)):
        a = (int((p[k - 1]["x1"] + p[k - 1]["x2"]) / 2), int(p[k - 1]["y2"]))
        c = (int((p[k]["x1"] + p[k]["x2"]) / 2), int(p[k]["y2"]))
        cv2.line(img, a, c, (80, 220, 120), 1)

    out = Path(out or (ROOT / "lab_gold" / "_crossings" /
                       f"v{video_id}_t{track_id}.jpg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    h, w = img.shape[:2]
    k = 900 / w
    cv2.imwrite(str(out), cv2.resize(img, (900, int(h * k))), [cv2.IMWRITE_JPEG_QUALITY, 86])
    return str(out)
