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


class _Writer:
    """Frames in, a web-playable H.264 file out.

    OpenCV's own VideoWriter is the obvious way to do this and it is the reason renders
    were broken on Windows. Its bundled FFmpeg has no H.264 encoder compiled in: asking
    for `avc1` makes it try to load `openh264-2.5.0-win64.dll`, which nobody has, and it
    fails with "Unable to create encoder". The fallback, `mp4v`, does write a file — an
    MPEG-4 Part 2 file, which no browser plays and which the transcode step was then
    supposed to fix by shelling out to a bare `ffmpeg` that a frozen Windows build also
    does not have. So the surveyor got either an error or an unplayable download.

    Piping raw frames straight into the bundled ffmpeg removes the whole chain: one
    encode, real H.264, `+faststart` so it plays while downloading, no temp file, no DLL.
    OpenCV is kept only as the fallback for a machine with no ffmpeg at all — where an
    unplayable file still beats no file.
    """

    def __init__(self, path, fps, w, h):
        self.cv = None
        self.proc = None
        import engine
        exe = engine.ffmpeg_bin()
        if exe:
            import subprocess
            self.proc = subprocess.Popen(
                [exe, "-y", "-v", "error",
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                 "-r", f"{max(float(fps) or 25.0, 1.0):.4f}", "-i", "-",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 # yuv420p, not ffmpeg's pick: libx264 defaults to yuv444p for rawvideo
                 # input, which Safari and most phones refuse to decode.
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
            return
        self.cv = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
        if not self.cv.isOpened():
            self.cv = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not self.cv.isOpened():
            raise RuntimeError("no video encoder available — ffmpeg is missing and "
                               "OpenCV cannot write mp4 on this machine")

    def write(self, frame):
        if self.proc:
            # A dead encoder must be reported as itself. Writing on regardless raises
            # BrokenPipeError several hundred frames later, with ffmpeg's actual
            # complaint already discarded.
            try:
                self.proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(f"the encoder stopped: {err[:200] or 'no reason given'}")
        else:
            self.cv.write(frame)

    def release(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait(timeout=300)
            if self.proc.returncode:
                err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(f"encoding failed: {err[:200] or 'no reason given'}")
            self.proc.stderr.close()
        else:
            self.cv.release()

    @property
    def web_ready(self):
        return self.proc is not None


def _writer(path, fps, w, h):
    return _Writer(path, fps, w, h)


# ───────────────────────────── the overlay ─────────────────────────────
# The counts were being drawn in each class's own colour on a dark translucent panel,
# which is how "2Axle_Truck" ended up as dim purple on near-black and "2W" as dark green
# — legible in the palette, invisible in the video. Colour is an identifier, not a
# typeface. So the colour moves into a solid chip and every word is white on a solid
# panel, which is readable at any size, on any footage, after any compression.
FONT = cv2.FONT_HERSHEY_DUPLEX
PANEL_BG = (24, 24, 26)
INK = (255, 255, 255)
DIM = (170, 170, 176)


def _panel_rect(ow, n, k):
    """Where the counts panel sits. Its own function because the boxes need to know:
    the panel is drawn last and would otherwise slice a label in half."""
    pad, row = int(12 * k), int(24 * k)
    w = int(268 * k)
    head = int(50 * k)
    h = head + int(10 * k) + row * max(n, 1) + pad
    x0, y0 = ow - w - int(12 * k), int(12 * k)
    return x0, y0, x0 + w, y0 + h


def _tag(img, text, x1, y1, y2, col, k, avoid=None):
    """The label above a box: white on a filled chip in the class colour.

    Coloured text on the footage itself was the same mistake as the panel — a yellow
    label over a yellow crash barrier is not a label. The chip also gives the eye a
    consistent shape to find, which matters when six boxes overlap.

    `avoid` is the counts panel. A chip drawn under it came out as "2Axle_", a truncation
    that reads as a different vehicle class rather than as a hidden label.
    """
    sc = 0.46 * k
    th = max(1, int(round(k)))
    (tw, tht), _ = cv2.getTextSize(text, FONT, sc, th)
    pad = max(3, int(4 * k))
    top = y1 - tht - 2 * pad
    if top < 0:                     # no room above the box — sit the chip inside it
        top = y1
    # Slide the chip left rather than letting it run off the frame. Clamping only the
    # rectangle left the text hanging past the edge, so a lorry on the right shoulder
    # was labelled "2Axle_" — a truncation that reads as a different vehicle class.
    x1 = max(0, min(x1, img.shape[1] - tw - 2 * pad))
    ch, cw = tht + 2 * pad, tw + 2 * pad
    if avoid:
        ax0, ay0, ax1, ay1 = avoid
        hidden = (lambda t: x1 < ax1 and x1 + cw > ax0 and t < ay1 and t + ch > ay0)
        if hidden(top):
            for cand in (ay1 + int(2 * k),      # just under the panel
                         y2 + int(2 * k),       # under the box
                         y1 + int(2 * k)):      # last resort: inside the box
                if not hidden(cand) and cand + ch < img.shape[0]:
                    top = cand
                    break
    cv2.rectangle(img, (x1, top), (x1 + cw, top + ch), col, -1)
    # Dark ink on a light chip, light ink on a dark one. Half the palette is bright.
    lum = 0.114 * col[0] + 0.587 * col[1] + 0.299 * col[2]
    ink = (20, 20, 20) if lum > 150 else INK
    cv2.putText(img, text, (x1 + pad, top + tht + pad - 1), FONT, sc, ink, th, cv2.LINE_AA)


def _panel(img, ow, running, k):
    """Running counts, top right: the number the whole video exists to justify.

    Solid rather than translucent. A 65%-opacity panel over moving footage means the
    digits change contrast several times a second, which is unreadable in motion and
    worse after compression — and this panel is the one thing a client zooms in on.
    """
    seen = sorted({key[0] for key in running if isinstance(key, tuple)})
    pad, row = int(12 * k), int(24 * k)
    head = int(50 * k)
    x0, y0, x1, y1 = _panel_rect(ow, len(seen), k)
    w = x1 - x0

    cv2.rectangle(img, (x0, y0), (x1, y1), PANEL_BG, -1)
    cv2.rectangle(img, (x0, y0), (x1, y1), (90, 90, 96), max(1, int(k)))

    # The two totals, as big as the panel allows — this is the headline figure.
    cv2.putText(img, "IN", (x0 + pad, y0 + int(22 * k)), FONT, 0.44 * k, DIM,
                max(1, int(k)), cv2.LINE_AA)
    cv2.putText(img, str(running["in"]), (x0 + pad, y0 + int(46 * k)), FONT, 0.86 * k,
                INK, max(1, int(2 * k)), cv2.LINE_AA)
    mid = x0 + w // 2
    cv2.putText(img, "OUT", (mid + pad, y0 + int(22 * k)), FONT, 0.44 * k, DIM,
                max(1, int(k)), cv2.LINE_AA)
    cv2.putText(img, str(running["out"]), (mid + pad, y0 + int(46 * k)), FONT, 0.86 * k,
                INK, max(1, int(2 * k)), cv2.LINE_AA)
    cv2.line(img, (x0 + pad, y0 + head), (x0 + w - pad, y0 + head), (70, 70, 76),
             max(1, int(k)))

    if not seen:
        cv2.putText(img, "nothing has crossed yet", (x0 + pad, y0 + head + int(24 * k)),
                    FONT, 0.42 * k, DIM, max(1, int(k)), cv2.LINE_AA)
        return

    y = y0 + head + int(8 * k)
    chip = int(11 * k)
    for c in seen:
        col = COLORS[CLASSES.index(c) % 15]
        cy = y + row // 2
        cv2.rectangle(img, (x0 + pad, cy - chip // 2),
                      (x0 + pad + chip, cy + chip // 2), col, -1)
        cv2.putText(img, c, (x0 + pad + chip + int(9 * k), cy + int(5 * k)), FONT,
                    0.44 * k, INK, max(1, int(k)), cv2.LINE_AA)
        # Right-aligned counts in fixed columns, so the digits do not jitter sideways
        # as they tick from 9 to 10.
        for val, right in ((running[(c, "in")], mid + int(46 * k)),
                           (running[(c, "out")], x0 + w - pad)):
            s = str(val)
            (tw, _), _b = cv2.getTextSize(s, FONT, 0.44 * k, max(1, int(k)))
            cv2.putText(img, s, (right - tw, cy + int(5 * k)), FONT, 0.44 * k, INK,
                        max(1, int(k)), cv2.LINE_AA)
        y += row


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
        # Everything drawn is sized from this, so the overlay looks the same on a 640-wide
        # phone clip as on a 1280 camera feed. Fixed pixel sizes made the panel swallow a
        # small frame and vanish on a large one.
        k = max(0.55, ow / 1280.0)
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
        out_path = Path(out_path)     # organise may hand back a string
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wr = _writer(out_path, v["fps"], ow, oh)
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
                # Drawn twice: a dark stroke under a white core, so the line stays visible
                # over pale concrete as well as over shadow. A single white line
                # disappears exactly where these surveys are filmed.
                cv2.line(frame, p1, p2, (0, 0, 0), max(2, int(6 * k)), cv2.LINE_AA)
                cv2.line(frame, p1, p2, (255, 255, 255), max(1, int(3 * k)), cv2.LINE_AA)
            pr = _panel_rect(ow, len({key[0] for key in running
                                      if isinstance(key, tuple)}), k)
            for p in by_frame.get(i, []):
                t = tracks.get(p["track_id"])
                if not t or t.get("dup_of") is not None:
                    continue
                cls = t["class_override"] if t["class_override"] is not None else t["cls"]
                col = COLORS[cls % 15]
                x1, y1 = int(p["x1"] * scale), int(p["y1"] * scale)
                x2, y2 = int(p["x2"] * scale), int(p["y2"] * scale)
                cv2.rectangle(frame, (x1, y1), (x2, y2), col, max(1, int(2 * k)))
                _tag(frame, f"{p['track_id']}  {CLASSES[cls]}", x1, y1, y2, col, k, pr)
            for e in ev_by_frame.get(i, []):
                running[e["direction"]] += 1
                running[(e["class"], e["direction"])] += 1
            _panel(frame, ow, running, k)
            wr.write(frame)
            i += 1
            if i % 500 == 0:
                pct = 100.0 * i / max(v["frames"], 1)
                db.run("UPDATE jobs SET progress=?, message=? WHERE id=?",
                       round(pct, 1), f"rendering {((i/v['fps'])/max(time.time()-t0,0.01)):.1f}x", job_id)
        wr.release()
        cap.release()
        if not i:
            raise RuntimeError("no frames could be read from the recording")
        # No transcode pass. The ffmpeg writer already produced faststart H.264, and the
        # OpenCV fallback only runs on a machine with no ffmpeg to transcode with.
        db.run("UPDATE jobs SET status='done', progress=100, finished=?, message=? WHERE id=?",
               time.time(), str(out_path.name), job_id)
    except Exception as e:
        # Delete the half-written file. Left behind, it reads as a finished render
        # everywhere the app looks for one, and the surveyor downloads a truncated video
        # instead of being told the render failed.
        try:
            if out_path.is_file():
                out_path.unlink()
        except (NameError, OSError):
            pass
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)
