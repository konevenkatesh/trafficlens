"""Training records: one row per fine-tune, with everything needed to explain
or reproduce it -- what data went in, what settings, what came out, what it cost.

Metrics are read straight from Ultralytics' own results.csv and args.yaml, so the
report is the training's own output rather than a re-typed summary.
"""
import csv
import json
import time
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
CLASSES = ["2W", "3W_Auto", "Car_Jeep_Van", "LCV", "Mini_Bus", "Bus", "Tractor",
           "Tractor_Trailer", "2Axle_Truck", "3Axle_Truck", "MAV", "Cycle",
           "Cycle_Rickshaw", "Animal_Cart", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_trainings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT, run_id INTEGER, status TEXT DEFAULT 'running',
  base_model TEXT, dataset TEXT, dataset_detail TEXT,
  n_train INTEGER, n_val INTEGER, config TEXT,
  pod_id TEXT, gpu TEXT, hourly REAL,
  started REAL, finished REAL, epochs_done INTEGER, epochs_planned INTEGER,
  map50 REAL, map5095 REAL, precision REAL, recall REAL,
  per_class TEXT, curve TEXT, cost_usd REAL DEFAULT 0,
  weights_path TEXT, notes TEXT);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


def create(tag, **kw):
    init()
    existing = db.one("SELECT id FROM lab_trainings WHERE tag=?", tag)
    if existing:
        return existing["id"]
    cols = ["tag", "started"] + list(kw)
    vals = [tag, time.time()] + [db.jdump(v) if isinstance(v, (dict, list)) else v
                                 for v in kw.values()]
    q = f"INSERT INTO lab_trainings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    return db.run(q, *vals)


def update(tid, **kw):
    if not kw:
        return
    sets, vals = [], []
    for k, v in kw.items():
        sets.append(f"{k}=?")
        vals.append(db.jdump(v) if isinstance(v, (dict, list)) else v)
    db.run(f"UPDATE lab_trainings SET {','.join(sets)} WHERE id=?", *vals, tid)


def _f(row, needle, default=None):
    for k, v in row.items():
        if needle in k:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
    return default


def ingest(tid, results_csv, args_yaml=None):
    """Pull Ultralytics' own numbers into the record."""
    p = Path(results_csv)
    if not p.exists():
        return None
    rows = list(csv.DictReader(open(p)))
    if not rows:
        return None
    # best epoch by mAP50-95, the metric Ultralytics itself selects best.pt on
    best = max(rows, key=lambda r: _f(r, "mAP50-95(B)", 0) or 0)
    curve = [{"epoch": i + 1,
              "map50": _f(r, "mAP50(B)", 0),
              "map5095": _f(r, "mAP50-95(B)", 0),
              "precision": _f(r, "precision(B)", 0),
              "recall": _f(r, "recall(B)", 0),
              "box_loss": _f(r, "val/box_loss", 0)}
             for i, r in enumerate(rows)]
    cfg = None
    if args_yaml and Path(args_yaml).exists():
        cfg = {}
        for line in Path(args_yaml).read_text().splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                cfg[k.strip()] = v.strip()
    fields = {
        "epochs_done": len(rows),
        "map50": _f(best, "mAP50(B)", 0),
        "map5095": _f(best, "mAP50-95(B)", 0),
        "precision": _f(best, "precision(B)", 0),
        "recall": _f(best, "recall(B)", 0),
        "curve": curve,
    }
    if cfg:
        fields["config"] = cfg
        if cfg.get("epochs"):
            fields["epochs_planned"] = int(float(cfg["epochs"]))
    update(tid, **fields)
    return fields


def attach_cost(tid, pod_id):
    p = db.one("SELECT cost_usd, gpu, hourly FROM lab_pods WHERE pod_id=? ORDER BY id DESC LIMIT 1",
               pod_id)
    if p:
        update(tid, cost_usd=round(p["cost_usd"] or 0, 4), gpu=p["gpu"], hourly=p["hourly"])


def listing():
    init()
    out = db.rows("SELECT * FROM lab_trainings ORDER BY id DESC")
    for t in out:
        t["config"] = db.jload(t["config"], {})
        t["dataset_detail"] = db.jload(t["dataset_detail"], {})
        t["curve"] = db.jload(t["curve"], [])
        t["per_class"] = db.jload(t["per_class"], {})
    return out


def get(tid):
    init()
    t = db.one("SELECT * FROM lab_trainings WHERE id=?", tid)
    if not t:
        return None
    t["config"] = db.jload(t["config"], {})
    t["dataset_detail"] = db.jload(t["dataset_detail"], {})
    t["curve"] = db.jload(t["curve"], [])
    t["per_class"] = db.jload(t["per_class"], {})
    t["pod"] = db.one("SELECT * FROM lab_pods WHERE pod_id=? ORDER BY id DESC LIMIT 1",
                      t["pod_id"]) if t["pod_id"] else None
    return t
