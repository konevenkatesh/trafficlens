"""EXPERIMENT — DOES NOT WORK RELIABLY. NOT WIRED INTO THE APP.

Kept because the idea is sound and the failure is informative, not because it is usable.
Do not import this expecting a date.

THE IDEA. A burnt-in clock counts, so the video can teach the app its own font: sample the
seconds cell for a dozen seconds and every digit walks past in order, and the tens cell
changing marks the 9->0 rollover, which says which shape is "0". No OCR engine, no bundled
model, works in any font the camera happens to use.

WHY IT DOES NOT WORK YET. Locating and segmenting the text beat it, not the recognition:

  * Automatic location fails. Scoring rows by per-second change puts tree canopy against
    bright sky first and the road second; the clock came sixth on real footage. Adding
    "must also look like text" did not separate them -- thin bright branches are
    text-shaped. Given a hand-drawn box the problem goes away, which is why the shipped
    feature asks for the time instead.
  * Segmentation is unstable. The overlay sits on a background that spans bright sky and
    shadow within one crop, so no single threshold holds across its width. A fixed
    threshold merges glyphs into 68px blobs where the background is bright; a top-hat
    inverts over the dark half; cv2.adaptiveThreshold produced four "characters" for
    nineteen. Cell boundaries then move frame to frame and the learned templates are
    garbage.

WHAT WOULD PROBABLY WORK. A tight box around the digits only (not the whole line, gaps
included), per-cell thresholding rather than per-crop, and cells located once from a
frame-averaged mask. Or simply bundle tesseract: 50MB against an installer already at
412MB, and these overlays are high-contrast and monospaced once cropped tightly.

The shipped path is manual: the app shows the first frame beside the input box, and the
surveyor types the time they can read in it. That is five seconds of work, it is right
every time, and it does not silently file a survey under a date invented from foliage.
"""

import re
from datetime import datetime

import cv2
import numpy as np

# Sampling one frame per second. Enough seconds to see every digit twice and to catch a
# tens rollover wherever it happens to fall.
SAMPLE_SECONDS = 26
# A glyph must match a learned template this well to be read as that digit. Overlays sit
# on moving traffic and sky, so the bar is deliberately high: refusing to read is fine
# (the surveyor is asked), inventing a date is not.
MATCH_MIN = 0.62


def _samples(path, fps, n=SAMPLE_SECONDS):
    """One greyscale frame per second, plus the frame index each came from."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return [], []
    step = max(int(round(fps or 12)), 1)
    out, idx = [], []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
        ok, f = cap.read()
        if not ok:
            break
        out.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
        idx.append(i * step)
    cap.release()
    return out, idx


def _text_mask(crop):
    """The overlay text inside a clock crop, as a 0/1 mask.

    A threshold set from the crop's own statistics, which works because the crop is mostly
    one thing (whatever the clock sits on) plus a little very bright text. Two cleverer
    methods were tried on this footage and both lost: a fixed brightness threshold selects
    the sky as readily as the digits, and a morphological top-hat inverts over the dark
    half of a crop that spans sky and shadow. The floor of 140 stops a crop containing no
    text at all from resolving itself into noise.
    """
    c = crop.astype(np.float32)
    t = max(140.0, float(c.mean() + 1.2 * c.std()))
    return (crop > t).astype(np.uint8)


def find_clock_line(frames):
    """The row band and column span holding the clock, or None.

    Located from a per-ROW profile of how often pixels change between one second and the
    next, not from 2-D blobs. The blob version failed on this footage and failed
    convincingly: trees moving against bright sky form a far larger changing region than a
    line of small digits, so picking the biggest one put the clock in the middle of the
    canopy. A whole row of digits changing together is a sharp, narrow peak in the row
    profile; foliage is broad and low.

    Camera makers put the overlay wherever they like, so nothing here assumes a corner --
    only that the clock occupies one horizontal line.
    """
    if len(frames) < 6:
        return None
    st = np.stack([f.astype(np.int16) for f in frames])
    flips = (np.abs(np.diff(st, axis=0)) > 40).mean(axis=0)
    # Rows scored on pixels that are BOTH text in nearly every frame and changing between
    # seconds. Change alone is not enough: on this footage the strongest changing row was
    # tree canopy against sky, and the clock came sixth. An overlay is the only thing that
    # is permanently text-shaped and also different every second.
    pers = np.stack([_text_mask(f) for f in frames]).mean(axis=0) > 0.6
    rows = ((flips > 0.25) & pers).sum(axis=1).astype(float)
    peak = int(np.argmax(rows))
    if rows[peak] < 3:
        return None
    # Grow out from the peak while the row is still clearly part of the same line.
    thr = max(1.0, rows[peak] * 0.25)
    y0, y1 = peak, peak
    while y0 > 0 and rows[y0 - 1] > thr:
        y0 -= 1
    while y1 < len(rows) - 1 and rows[y1 + 1] > thr:
        y1 += 1
    # Digits are taller than the few rows that happen to differ between two glyphs.
    pad = max(6, (y1 - y0))
    y0, y1 = max(0, y0 - pad), min(frames[0].shape[0], y1 + pad + 1)
    if y1 - y0 < 8 or y1 - y0 > 120:
        return None

    band = _text_mask(frames[0])[y0:y1]
    cols = np.where(band.sum(axis=0) > 0)[0]
    if len(cols) < 10:
        return None
    # Keep the run of columns around the changing part: the date sits beside the seconds
    # and never changes, so it is invisible to a change detector, but unrelated bright
    # objects elsewhere on the same row must not drag the span across the whole frame.
    cflip = flips[y0:y1].mean(axis=0)
    hot = np.where(cflip > cflip.max() * 0.2)[0]
    if not len(hot):
        return None
    lo, hi = int(hot.min()), int(hot.max())
    gap = 0
    while lo > 0 and gap < 60:
        lo -= 1
        gap = 0 if band[:, lo].any() else gap + 1
    gap = 0
    while hi < band.shape[1] - 1 and gap < 60:
        hi += 1
        gap = 0 if band[:, hi].any() else gap + 1
    return y0, y1, max(0, lo), min(band.shape[1], hi + 1)


def _cells(mask):
    """Split a text line into character boxes, left to right."""
    colsum = mask.sum(axis=0)
    on = colsum > 0
    out, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 3:
                out.append((start, i))
            start = None
    if start is not None and len(on) - start >= 3:
        out.append((start, len(on)))
    return out


def _norm(patch):
    """A glyph, scaled to one size so shapes can be compared regardless of the cell."""
    ys, xs = np.where(patch > 0)
    if len(ys) < 6:
        return None
    p = patch[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    return cv2.resize(p, (16, 24), interpolation=cv2.INTER_AREA)


def _score(a, b):
    if a is None or b is None:
        return -1.0
    num = float((a * b).sum())
    den = float(np.sqrt((a * a).sum() * (b * b).sum())) or 1.0
    return num / den


def learn_digits(crops):
    """Label the ten digit shapes by watching the clock count.

    Returns {digit: template} or None. The seconds cell walks 0-9 on its own; the tens
    cell changing is the 9->0 rollover, which fixes which shape is which. Without that
    anchor the shapes are known but nameless.
    """
    masks = [_text_mask(c) for c in crops]
    cells = _cells(masks[0])
    if len(cells) < 6:
        return None
    ones, tens = cells[-1], cells[-2]

    glyphs = [_norm(m[:, ones[0]:ones[1]]) for m in masks]
    tens_g = [_norm(m[:, tens[0]:tens[1]]) for m in masks]
    if any(g is None for g in glyphs[:4]):
        return None

    # Where did the tens digit change? That sample is the one whose ones-digit is 0.
    zero_at = None
    for i in range(1, len(tens_g)):
        if _score(tens_g[i], tens_g[i - 1]) < 0.93:
            zero_at = i
            break
    if zero_at is None:
        return None

    tpl = {}
    for i, g in enumerate(glyphs):
        if g is None:
            continue
        tpl.setdefault((i - zero_at) % 10, []).append(g)
    if len(tpl) < 10:
        return None
    # Average the repeats: the same digit seen three times, averaged, is steadier than any
    # single frame where a lorry happened to be behind the text.
    return {d: np.mean(np.stack(v), axis=0) for d, v in tpl.items()}


def _read_line(mask, templates):
    """Read one clock line into a string of digits and separators."""
    out = []
    for a, b in _cells(mask):
        g = _norm(mask[:, a:b])
        if g is None:
            out.append(" ")
            continue
        best, bd = -1.0, None
        for d, t in templates.items():
            sc = _score(g, t)
            if sc > best:
                best, bd = sc, d
        out.append(str(bd) if best >= MATCH_MIN else " ")
    return "".join(out)


def _parse(digits):
    """Turn a run of digits into a datetime, trying the orders cameras actually use."""
    ds = re.sub(r"\D", "", digits)
    if len(ds) < 12:
        return None
    for fmt, take in (("%Y%m%d%H%M%S", 14), ("%d%m%Y%H%M%S", 14),
                      ("%m%d%Y%H%M%S", 14), ("%y%m%d%H%M%S", 12)):
        if len(ds) < take:
            continue
        try:
            t = datetime.strptime(ds[:take], fmt)
        except ValueError:
            continue
        # A camera clock can be wrong, but not by centuries. This rejects a misread far
        # more often than it rejects a real date.
        if 2000 <= t.year <= 2100:
            return t
    return None


def read(path, fps, box):
    """The clock burnt into this video, as a datetime for its first frame.

    `box` is (y0, y1, x0, x1) around the timestamp, drawn once per station -- the camera
    does not move, so it holds for every recording from that site. Automatic location was
    tried and abandoned: on this footage the strongest per-second change in the frame was
    tree canopy against bright sky, and the road came second. A detector that is usually
    right about which part of the picture is the clock is worse than asking, because when
    it is wrong it reads a plausible time off the scenery.

    Returns {"clock", "confidence", "text", "samples"} or None. Verified by arithmetic
    rather than accepted on faith: the time read from a later frame must be exactly as far
    ahead as the frame count says. A misread digit almost never survives that.
    """
    y0, y1, x0, x1 = [int(v) for v in box]
    frames, idx = _samples(path, fps)
    if len(frames) < 8:
        return None
    crops = [f[y0:y1, x0:x1] for f in frames]
    if not crops or crops[0].size < 200:
        return None
    tpl = learn_digits(crops)
    if not tpl:
        return None

    reads = []
    for c, i in zip(crops, idx):
        txt = _read_line(_text_mask(c), tpl)
        t = _parse(txt)
        if t:
            reads.append((i, t, txt))
    if len(reads) < 6:
        return None

    base = {}
    for i, t, _txt in reads:
        b = t.timestamp() - i / float(fps or 12)
        base[round(b)] = base.get(round(b), 0) + 1
    best_b, votes = max(base.items(), key=lambda kv: kv[1])
    conf = votes / len(reads)
    if conf < 0.7:
        return None
    return {"clock": datetime.fromtimestamp(best_b).strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": round(conf, 2),
            "text": reads[0][2].strip(),
            "samples": len(reads)}
