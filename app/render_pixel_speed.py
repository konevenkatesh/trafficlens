"""Annotated video showing PIXEL speed — what the tracker measures before any calibration.

Separate from render.py on purpose. This claims nothing about km/h: it draws px/s, which
is what the trajectory store actually contains and is true regardless of where the camera
was, how high, or whether anybody has been to the site with a tape measure.

It is worth looking at because every real-world speed figure is this number multiplied by
a scale, so if this is wrong or noisy no calibration can rescue it. Watching a lorry cross
the frame with a steady reading, and a motorcycle weaving with a jumpy one, says more
about what the system can and cannot measure than any table of error percentages.

Pixel speed also falls naturally as a vehicle recedes, because the same metre of road is
fewer pixels further away. That is perspective, not deceleration, and seeing it happen is
the clearest possible argument for why a scale is needed at all.

    python app/render_pixel_speed.py <video_id> <out.mp4> [max_seconds]
"""
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db                                                          # noqa: E402
from engine import CLASSES                                         # noqa: E402
from render import COLORS, FONT, INK, PANEL_BG, _Writer, _tag      # noqa: E402

# Speed is fitted over a window rather than taken from one frame gap. Detection jitter was
# measured at 9.35 px against 8.3 px/frame of real motion, so a single frame difference is
# more noise than signal; a second of context makes it readable without smoothing away the
# accelerations that are actually there.
WIN = 7


def speeds_by_frame(video_id):
    """Pixel speed per (track, frame), from a local straight-line fit."""
    tracks = {t["track_id"]: t for t in db.rows(
        "SELECT track_id, cls, class_override, dup_of FROM tracks WHERE video_id=?",
        video_id)}
    paths = defaultdict(list)
    for p in db.rows("""SELECT track_id, frame, x1, y1, x2, y2 FROM track_points
                        WHERE video_id=? ORDER BY track_id, frame""", video_id):
        t = tracks.get(p["track_id"])
        if not t or t.get("dup_of") is not None:
            continue
        paths[p["track_id"]].append((p["frame"], (p["x1"] + p["x2"]) / 2.0, p["y2"]))

    out = {}
    for tid, pl in paths.items():
        f = np.array([q[0] for q in pl], float)
        x = np.array([q[1] for q in pl])
        y = np.array([q[2] for q in pl])
        for i in range(len(pl)):
            lo, hi = max(0, i - WIN // 2), min(len(pl), i + WIN // 2 + 1)
            if hi - lo < 3 or f[hi - 1] - f[lo] < 2:
                continue
            # Slope of x and y against frame number: px per frame, combined.
            vx = np.polyfit(f[lo:hi], x[lo:hi], 1)[0]
            vy = np.polyfit(f[lo:hi], y[lo:hi], 1)[0]
            out[(tid, int(f[i]))] = float(np.hypot(vx, vy))
    return out, tracks, paths


def render(video_id, out_path, max_seconds=None):
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    sp, tracks, paths = speeds_by_frame(video_id)
    by_frame = defaultdict(list)
    for p in db.rows("""SELECT track_id, frame, x1, y1, x2, y2 FROM track_points
                        WHERE video_id=? ORDER BY frame""", video_id):
        t = tracks.get(p["track_id"])
        if not t or t.get("dup_of") is not None:
            continue
        by_frame[p["frame"]].append(p)
    hist = defaultdict(list)
    for tid, pl in paths.items():
        for f, x, y in pl:
            hist[tid].append((f, x, y))

    cap = cv2.VideoCapture(v["path"])
    W, H = v["width"], v["height"]
    scale = min(1.0, 1280 / W)
    ow, oh = int(W * scale), int(H * scale)
    k = max(0.55, ow / 1280.0)
    fps = float(v["fps"] or 15)
    limit = int(max_seconds * fps) if max_seconds else None

    wr = _Writer(Path(out_path), fps, ow, oh)
    i = 0
    fastest = 0.0
    while True:
        ok, frame = cap.read()
        if not ok or (limit and i >= limit):
            break
        if scale < 1.0:
            frame = cv2.resize(frame, (ow, oh))

        for p in by_frame.get(i, []):
            t = tracks[p["track_id"]]
            cls = t["class_override"] if t["class_override"] is not None else t["cls"]
            col = COLORS[cls % 15]
            x1, y1 = int(p["x1"] * scale), int(p["y1"] * scale)
            x2, y2 = int(p["x2"] * scale), int(p["y2"] * scale)
            trail = [(int(x * scale), int(y * scale))
                     for f, x, y in hist[p["track_id"]] if i - 30 <= f <= i]
            if len(trail) > 1:
                cv2.polylines(frame, [np.array(trail, np.int32).reshape(-1, 1, 2)],
                              False, col, max(1, int(2 * k)), cv2.LINE_AA)
            cv2.rectangle(frame, (x1, y1), (x2, y2), col, max(1, int(2 * k)))
            pxs = sp.get((p["track_id"], i))
            if pxs is not None:
                pxs_per_s = pxs * fps * scale
                fastest = max(fastest, pxs_per_s)
                label = f"{CLASSES[cls]}  {pxs_per_s:.0f} px/s"
            else:
                label = CLASSES[cls]
            _tag(frame, label, x1, y1, y2, col, k)

        # A standing caption, because a still pulled out of this video will travel without
        # the video. Anyone who sees the frame must see that these are not km/h.
        bh = int(52 * k)
        cv2.rectangle(frame, (0, oh - bh), (ow, oh), PANEL_BG, -1)
        cv2.putText(frame, "PIXEL SPEED - what the tracker measures. Not km/h: no "
                    "calibration applied.", (int(14 * k), oh - int(30 * k)),
                    FONT, 0.52 * k, INK, max(1, int(k)), cv2.LINE_AA)
        cv2.putText(frame, "The same vehicle reads slower further away - that is "
                    "perspective, not braking. Converting to km/h needs one measured "
                    "distance on the road.",
                    (int(14 * k), oh - int(12 * k)), FONT, 0.42 * k, (170, 170, 176),
                    max(1, int(k)), cv2.LINE_AA)
        wr.write(frame)
        i += 1
    wr.release()
    cap.release()
    return {"frames": i, "fastest_px_per_s": round(fastest, 1), "out": str(out_path)}


if __name__ == "__main__":
    vid = int(sys.argv[1])
    out = sys.argv[2]
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else None
    print(render(vid, out, secs))
