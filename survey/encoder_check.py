"""Prove this machine can produce a video a browser will play.

Its own step in CI because the encoder is the one part of the app the end-to-end run
cannot reach. That run works on a generated test pattern, which contains no vehicles, so
there is nothing to draw boxes on and `/annotate` correctly refuses — which is exactly
how a broken encoder shipped to Windows unnoticed.

What broke: OpenCV's Windows wheel has no H.264 encoder compiled in. Asking it for `avc1`
makes it try to load `openh264-2.5.0-win64.dll`, which is not installed anywhere, and it
fails with "Unable to create encoder". The `mp4v` fallback does write a file — MPEG-4
Part 2, which no browser plays — and the transcode meant to fix that shelled out to a
bare `ffmpeg` that a frozen Windows build does not have either. So this asserts the
result, not the intent: H.264, yuv420p, and the moov atom at the front.
"""
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path[:0] = ["app", str(Path(__file__).resolve().parent.parent / "app")]

import numpy as np                                                    # noqa: E402
import engine                                                         # noqa: E402
import render                                                         # noqa: E402

W, H, N = 320, 240, 30


def main():
    exe = engine.ffmpeg_bin()
    print(f"ffmpeg: {exe or 'NOT FOUND'}")
    if not exe:
        print("FAILED: no ffmpeg, so every annotated video would be unplayable")
        return 1

    out = Path(tempfile.mkdtemp()) / "encoder_check.mp4"
    w = render._writer(out, 12.0, W, H)
    if not w.web_ready:
        print("FAILED: fell back to OpenCV even though ffmpeg is present")
        return 1
    for n in range(N):
        f = np.zeros((H, W, 3), dtype=np.uint8)
        f[:, :, n % 3] = (n * 8) % 256          # something that actually compresses
        w.write(f)
    w.release()

    if not out.is_file() or out.stat().st_size < 512:
        print("FAILED: no usable file came out")
        return 1

    probe = subprocess.run(
        [engine._ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt,nb_frames", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True).stdout.strip()
    print(f"produced {out.stat().st_size} bytes: {probe}")
    bad = []
    if "h264" not in probe:
        bad.append("not H.264 — a browser will not play it")
    if "yuv420p" not in probe:
        bad.append("not yuv420p — Safari and most phones will not decode it")

    # moov at the front, or the video only plays once fully downloaded.
    with open(out, "rb") as fh:
        off = 0
        for _ in range(6):
            fh.seek(off)
            head = fh.read(8)
            if len(head) < 8:
                break
            size, kind = struct.unpack(">I4s", head)
            if kind == b"moov":
                print(f"moov at byte {off} — faststart OK")
                break
            off += size
        else:
            bad.append("moov not near the front — no progressive playback")

    if bad:
        for b in bad:
            print(f"FAILED: {b}")
        return 1
    print("ENCODER OK — H.264, yuv420p, faststart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
