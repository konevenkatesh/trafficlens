"""The detector versions available to run, and which one is current.

Every `tracks` row already records the `model_id` that produced it, so a video's counts
can always be traced to a specific detector. This turns that into something choosable:
the registry lists the weights on disk with their measured accuracy, and extraction takes
a model id rather than whatever the environment happened to hold.

Metrics come from the training run's own results.csv, not from anything retyped here.
"""
import csv
import time
import sys
from pathlib import Path

import db

# Bundled read-only files live under sys._MEIPASS in a frozen build, and `__file__` for
# a frozen module points inside it -- so `__file__.parent.parent` lands ABOVE the bundle
# and every packaged path silently misses. Writable paths must NOT use this: they follow
# TRAFFICLENS_DATA instead, because the bundle is a temp directory deleted on exit.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
MODELS_DIR = ROOT / "models"
SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY,          -- 'yolo26s_morth15_v4', matches tracks.model_id
  label TEXT, file TEXT,
  map50 REAL, map5095 REAL, precision REAL, recall REAL,
  epochs INTEGER, note TEXT, is_default INTEGER DEFAULT 0,
  size_mb REAL, discovered REAL);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


def _metrics(model_id):
    """Best epoch from the training run's results.csv, chosen by mAP50-95 — the same
    metric Ultralytics uses to pick best.pt, so the number matches the shipped weights."""
    p = MODELS_DIR / f"{model_id}_results.csv"
    if not p.exists():
        return {}
    try:
        rows = list(csv.DictReader(open(p)))
    except OSError:
        return {}
    if not rows:
        return {}

    def val(row, needle):
        for k, v in row.items():
            if needle in k:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    best = max(rows, key=lambda r: val(r, "mAP50-95(B)") or 0)
    return {"map50": val(best, "mAP50(B)"), "map5095": val(best, "mAP50-95(B)"),
            "precision": val(best, "precision(B)"), "recall": val(best, "recall(B)"),
            "epochs": len(rows)}


def discover():
    """Pick up any weights sitting in models/. Cheap enough to run on every boot."""
    init()
    seen = []
    for p in sorted(MODELS_DIR.glob("yolo26s_morth15_v*.pt")):
        mid = p.stem
        seen.append(mid)
        m = _metrics(mid)
        if db.one("SELECT id FROM models WHERE id=?", mid):
            db.run("""UPDATE models SET file=?, size_mb=?, map50=?, map5095=?,
                      precision=?, recall=?, epochs=? WHERE id=?""",
                   str(p), round(p.stat().st_size / 1e6, 1), m.get("map50"),
                   m.get("map5095"), m.get("precision"), m.get("recall"),
                   m.get("epochs"), mid)
        else:
            db.run("""INSERT INTO models (id,label,file,map50,map5095,precision,recall,
                      epochs,size_mb,discovered) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   mid, mid.replace("yolo26s_morth15_", "").upper(), str(p),
                   m.get("map50"), m.get("map5095"), m.get("precision"), m.get("recall"),
                   m.get("epochs"), round(p.stat().st_size / 1e6, 1), time.time())
    if seen and not db.one("SELECT id FROM models WHERE is_default=1"):
        set_default(seen[-1])                       # newest version wins on a fresh install
    return seen


def listing():
    init()
    out = db.rows("SELECT * FROM models ORDER BY id DESC")
    for m in out:
        m["in_use"] = (db.one("SELECT COUNT(DISTINCT video_id) n FROM tracks WHERE model_id=?",
                              m["id"]) or {})["n"]
    return out


def default_id():
    init()
    r = db.one("SELECT id FROM models WHERE is_default=1")
    if r:
        return r["id"]
    r = db.one("SELECT id FROM models ORDER BY id DESC LIMIT 1")
    return r["id"] if r else None


def set_default(model_id):
    init()
    if not db.one("SELECT id FROM models WHERE id=?", model_id):
        raise ValueError(f"unknown model {model_id}")
    db.run("UPDATE models SET is_default=0")
    db.run("UPDATE models SET is_default=1 WHERE id=?", model_id)
    return model_id


def path_for(model_id):
    r = db.one("SELECT file FROM models WHERE id=?", model_id)
    return Path(r["file"]) if r else None
