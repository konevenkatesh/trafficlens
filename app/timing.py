"""Which moment of the recording a detector frame number really is.

The detector counts the frames the decoder hands it; the crop, the report's clock and
the 15-minute bins turn that count back into a time by dividing by the file's frame
rate. On a DVR recording those are not the same thing. KDP-01's camera dropped 796 of
47,224 frames while recording clip 13:00 (1.7%), so the decoder returns 46,428 frames
across 3,935 seconds of stream time: the detector's "frame 42,351" is 60 seconds later
than 42,351 / 12 says. Every crop cut by frame number showed an empty road, and every
crossing near the end of a clip was booked a minute early.

The stream carries its own timestamps and those are right (the burnt-in clock agrees
with them to the second). So each recording gets a map, decoder-frame -> stream
millisecond, made by one sequential pass over the file. 22 s per hour of footage here;
run beside detection it costs nothing. Seeking by timestamp then lands on the frame the
detector saw, pixel for pixel -- checked on three vehicles, difference 0.0.
"""
import threading
import time
import zlib

import numpy as np

import db

_CACHE = {}
_LOCKS = {}
_LOCK = threading.Lock()


def _init():
    db.run("""CREATE TABLE IF NOT EXISTS frame_times (
                video_id INTEGER PRIMARY KEY, n INTEGER, ms BLOB, made REAL)""")


def scan(path, stop=None, on_progress=None):
    """Stream millisecond of every frame the decoder returns, in order. Decode-free:
    grab() parses without converting pixels, so this runs at hundreds of fps."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    out = []
    try:
        while True:
            if stop is not None and stop():
                return None
            if not cap.grab():
                break
            out.append(int(round(cap.get(cv2.CAP_PROP_POS_MSEC))))
            if on_progress and len(out) % 5000 == 0:
                on_progress(len(out))
    finally:
        cap.release()
    return out


def save(video_id, ms):
    if not ms:
        return
    _init()
    arr = np.asarray(ms, dtype=np.uint32)
    db.run("INSERT OR REPLACE INTO frame_times (video_id,n,ms,made) VALUES (?,?,?,?)",
           video_id, int(arr.size), zlib.compress(arr.tobytes(), 6), time.time())
    with _LOCK:
        _CACHE[video_id] = arr


def load(video_id):
    with _LOCK:
        if video_id in _CACHE:
            return _CACHE[video_id]
    _init()
    r = db.one("SELECT n, ms FROM frame_times WHERE video_id=?", video_id)
    arr = np.frombuffer(zlib.decompress(r["ms"]), dtype=np.uint32) if r else None
    with _LOCK:
        _CACHE[video_id] = arr
    return arr


def made_at(video_id):
    """When this recording's map was made; 0 if it has none. Anything cut from the
    recording before that -- a cached crop -- was cut at the wrong frame."""
    _init()
    r = db.one("SELECT made FROM frame_times WHERE video_id=?", video_id)
    return float(r["made"]) if r and r["made"] else 0.0


def forget(video_id):
    with _LOCK:
        _CACHE.pop(video_id, None)


def ensure(video_id, stop=None):
    """The map for this recording, made now if it does not exist yet. None if the file
    cannot be read. One scan at a time per video, however many callers ask."""
    arr = load(video_id)
    if arr is not None:
        return arr
    with _LOCK:
        lock = _LOCKS.setdefault(video_id, threading.Lock())
    with lock:
        arr = load(video_id)
        if arr is not None:
            return arr
        v = db.one("SELECT path FROM videos WHERE id=?", video_id)
        if not v:
            return None
        ms = scan(v["path"], stop=stop)
        if not ms:
            return None
        save(video_id, ms)
        return load(video_id)


def seconds(video_id, frame, fps=None):
    """Seconds into the recording for a detector frame number: from the map when there
    is one, frame / fps when there is not (a clip that has not been scanned yet)."""
    arr = load(video_id)
    if arr is not None and arr.size:
        i = min(max(int(frame), 0), arr.size - 1)
        return float(arr[i]) / 1000.0
    if fps is None:
        v = db.one("SELECT fps FROM videos WHERE id=?", video_id)
        fps = (v["fps"] if v else None) or 25.0
    return float(frame) / float(fps or 25.0)


def read_frame(cap, video_id, frame):
    """The frame the detector saw as `frame`, from an open cv2 capture.

    With a map: seek a little before its timestamp and read forward to it. A seek by
    timestamp alone lands one or two frames late on this stream (measured); reading
    forward from 400 ms early lands exactly, in about a tenth of a second.
    Without one: seek by frame number, as before.
    """
    import cv2
    arr = load(video_id)
    if arr is None or not arr.size:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame))
        return cap.read()
    i = min(max(int(frame), 0), arr.size - 1)
    ms = float(arr[i])
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, ms - 400.0))
    ok, img = False, None
    for _ in range(80):
        ok, img = cap.read()
        if not ok:
            return False, None
        if cap.get(cv2.CAP_PROP_POS_MSEC) >= ms - 1.0:
            return True, img
    return ok, img


def missing():
    """Recordings that have detections but no map yet."""
    _init()
    return [r["id"] for r in db.rows("""SELECT DISTINCT v.id FROM videos v
                                        JOIN tracks t ON t.video_id = v.id
                                        LEFT JOIN frame_times f ON f.video_id = v.id
                                        WHERE f.video_id IS NULL ORDER BY v.id""")]


def backfill(stop=None):
    """Map every detected recording that has none, one at a time. For recordings
    detected before maps existed; a minute or two each, in the background."""
    for vid in missing():
        if stop is not None and stop():
            return
        try:
            ensure(vid, stop=stop)
        except Exception:
            pass


def backfill_in_background():
    t = threading.Thread(target=backfill, name="frame-times-backfill", daemon=True)
    t.start()
    return t
