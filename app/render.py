"""Annotated video renderer: draws boxes/IDs/classes/lines/running counts from the
trajectory store onto the source video. No model inference - pure playback of the DB.
Output downscaled to 1280w for size; ~2-4x realtime on CPU."""
import os
import time
from collections import defaultdict
from pathlib import Path

import cv2

import db
from counting import count_video
from engine import CLASSES

# Written at run time, so it follows the data directory. Inside a frozen bundle this is
# a temp folder deleted on exit -- the render would finish and then disappear.
OUT_DIR = Path(os.environ.get("TRAFFICLENS_DATA")
               or Path(__file__).resolve().parent) / "annotated"
COLORS = [(118, 230, 0), (40, 202, 255), (246, 182, 41), (80, 83, 239), (188, 71, 171),
          (218, 198, 38), (99, 110, 141), (67, 112, 255), (192, 107, 92), (122, 64, 236),
          (136, 150, 0), (38, 166, 255), (51, 202, 192), (127, 133, 161), (174, 164, 144)]


def render(video_id, job_id):
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    try:                                   # station default applies unless overridden
        import sys as _s
        _s.path.insert(0, str(Path(__file__).parent.parent / "lab"))
        import stations as _st
        lines = _st.lines_for(video_id)[0]
    except Exception:
        import sites
        s = {"lines": db.jdump(sites.lines_for(video_id)[0])}
        lines = db.jload(s["lines"], []) if s else []
    db.run("UPDATE jobs SET status='running', started=? WHERE id=?", time.time(), job_id)
    try:
        tracks = {t["track_id"]: t for t in db.rows("SELECT * FROM tracks WHERE video_id=?", video_id)}
        by_frame = defaultdict(list)
        for p in db.rows("SELECT track_id, frame, x1, y1, x2, y2 FROM track_points WHERE video_id=?", video_id):
            by_frame[p["frame"]].append(p)
        ev = count_video(video_id, lines)["events"] if lines else []
        ev_by_frame = defaultdict(list)
        for e in ev:
            ev_by_frame[e["frame"]].append(e)

        cap = cv2.VideoCapture(v["path"])
        W, H = v["width"], v["height"]
        scale = min(1.0, 1280 / W)
        ow, oh = int(W * scale), int(H * scale)
        # Ask the Lab where this video's render belongs — station folder if it has a
        # station, the legacy flat folder otherwise. One resolver, so a render is never
        # written somewhere the reader does not look.
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "lab"))
            import organise as _org
            out_path = _org.render_path(video_id, create=True)
        except Exception:
            OUT_DIR.mkdir(exist_ok=True)
            out_path = OUT_DIR / f"annotated_{v['name']}.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wr = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"avc1"), v["fps"], (ow, oh))
        if not wr.isOpened():
            wr = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), v["fps"], (ow, oh))
        running = defaultdict(int)
        i = 0
        t0 = time.time()
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if scale < 1.0:
                frame = cv2.resize(frame, (ow, oh))
            for ln in lines:
                p1 = (int(ln["start"][0] * scale), int(ln["start"][1] * scale))
                p2 = (int(ln["end"][0] * scale), int(ln["end"][1] * scale))
                cv2.line(frame, p1, p2, (255, 255, 255), 2)
            for p in by_frame.get(i, []):
                t = tracks.get(p["track_id"])
                if not t or t.get("dup_of") is not None:
                    continue
                cls = t["class_override"] if t["class_override"] is not None else t["cls"]
                col = COLORS[cls % 15]
                x1, y1 = int(p["x1"] * scale), int(p["y1"] * scale)
                x2, y2 = int(p["x2"] * scale), int(p["y2"] * scale)
                cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
                cv2.putText(frame, f"{p['track_id']} {CLASSES[cls]}", (x1, max(12, y1 - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
            for e in ev_by_frame.get(i, []):
                running[e["direction"]] += 1
                running[(e["class"], e["direction"])] += 1
            # sidebar: per-class running counts (only classes seen so far)
            seen = sorted({k[0] for k in running if isinstance(k, tuple)})
            top = 8
            px0 = ow - 226  # user prefers the panel on the right side
            panel_h = 58 + 22 * len(seen)
            ov = frame.copy()
            cv2.rectangle(ov, (px0, top), (ow, top + panel_h), (20, 20, 20), -1)
            cv2.addWeighted(ov, 0.65, frame, 0.35, 0, frame)
            cv2.putText(frame, f"in: {running['in']}   out: {running['out']}", (px0 + 12, top + 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.line(frame, (px0 + 10, top + 38), (ow - 10, top + 38), (120, 120, 120), 1)
            y = top + 58
            for c in seen:
                col = COLORS[CLASSES.index(c) % 15]
                cv2.putText(frame, f"{c}", (px0 + 12, y), cv2.FONT_HERSHEY_SIMPLEX,
                            0.48, col, 1, cv2.LINE_AA)
                cv2.putText(frame, f"{running[(c,'in')]} | {running[(c,'out')]}", (px0 + 152, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA)
                y += 22
            wr.write(frame)
            i += 1
            if i % 500 == 0:
                pct = 100.0 * i / max(v["frames"], 1)
                db.run("UPDATE jobs SET progress=?, message=? WHERE id=?",
                       round(pct, 1), f"rendering {((i/v['fps'])/max(time.time()-t0,0.01)):.1f}x", job_id)
        wr.release()
        cap.release()
        # transcode to h264 for in-browser playback (opencv's mp4v is not web-playable)
        db.run("UPDATE jobs SET message='transcoding for web playback' WHERE id=?", job_id)
        import subprocess
        tmp = out_path.with_suffix(".tmp.mp4")
        out_path.rename(tmp)
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp), "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart",
                            "-an", str(out_path)], capture_output=True)
        if r.returncode == 0:
            tmp.unlink()
        else:
            tmp.rename(out_path)
        db.run("UPDATE jobs SET status='done', progress=100, finished=?, message=? WHERE id=?",
               time.time(), str(out_path.name), job_id)
    except Exception as e:
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)
