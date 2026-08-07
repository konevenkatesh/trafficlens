"""Runs the counting app's extraction engine in its own process.

Isolated on purpose: the app package has its own `db` module bound to the same
SQLite file, and a YOLO crash here must not take the Lab server down.
Usage: python _extract_worker.py <video_id> <job_id> [imgsz] [conf] [model_id]
"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))   # app's `db` must win inside this process

import engine  # noqa: E402

if __name__ == "__main__":
    vid, job = int(sys.argv[1]), int(sys.argv[2])
    imgsz = int(sys.argv[3]) if len(sys.argv) > 3 else 960
    conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.12
    # The detector is a per-extraction choice, not a global. Comparing a station model
    # against the global one means running both over the same clip, and engine.extract
    # stamps the id on every track so a count can always name what produced it.
    model_id = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None
    engine.extract(vid, job, imgsz=imgsz, conf=conf, model_id=model_id)
