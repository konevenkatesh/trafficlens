"""Run-scoped artifacts: where every stage writes, and what survives afterwards.

Two things here exist because getting them wrong is expensive rather than annoying.

**Datasets are fingerprinted.** A training is only as good as the data behind it, and
the failure mode is silent -- a dataset rebuilt with different crops trains a worse model
that still *looks* like a fair comparison against the old one. Every dataset records a
content fingerprint over its label files, so two trainings can be compared only when the
data underneath them is provably the same, and a changed dataset is visible as a changed
hash rather than as an unexplained metric drop.

**Weights are archived, never replaced.** `best.pt` is a moving target: Ultralytics
overwrites it, and the previous version is simply gone. Archiving each finished weight
file under its training id is what makes "is v5 actually better than v4?" answerable next
month, and what makes rolling back a bad training possible at all.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
RUNS_DIR = ROOT / "lab_runs"          # fallback for a run with no station
WEIGHTS_DIR = ROOT / "models" / "archive"

# Every stage writes into its own category folder. Named here rather than spelled
# out at each call site, so "where did the crops go?" has exactly one answer.
CATEGORIES = ("segments", "compressed", "frames", "crops", "context",
              "dataset", "judgments", "reports", "weights")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_datasets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, name TEXT, path TEXT, kind TEXT DEFAULT 'yolo',
  n_train INTEGER, n_val INTEGER, n_images INTEGER, n_boxes INTEGER,
  class_mix TEXT, source_runs TEXT, label_source TEXT,
  fingerprint TEXT, note TEXT, created REAL, archived INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS lab_weights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  training_id INTEGER, dataset_id INTEGER, tag TEXT,
  path TEXT, source_path TEXT, size_mb REAL, sha256 TEXT,
  map50 REAL, map5095 REAL, precision REAL, recall REAL,
  base_model TEXT, epochs INTEGER, note TEXT,
  is_active INTEGER DEFAULT 0, created REAL);

CREATE INDEX IF NOT EXISTS ix_ds_run ON lab_datasets(run_id);
CREATE INDEX IF NOT EXISTS ix_w_training ON lab_weights(training_id);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ───────────────────────────── run-scoped paths ─────────────────────────────
def run_dir(run_id, category=None):
    """lab_runs/{run_id}/{category}/ -- created on demand.

    Everything a run produces lives under one folder, so a run can be inspected,
    archived or deleted as a unit instead of hunting its leavings across the tree.
    """
    # A run belongs to a station, so its artifacts belong in that station's folder.
    # Runs with no station still get the flat fallback rather than failing.
    import organise
    row = db.one("SELECT site_id FROM lab_runs WHERE id=?", run_id)
    if row and row["site_id"]:
        d = organise.station_dir(row["site_id"], "runs") / f"run-{run_id}"
    else:
        d = RUNS_DIR / str(run_id)
    if category:
        if category not in CATEGORIES:
            raise ValueError(f"unknown artifact category {category!r}")
        d = d / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_files(run_id, category):
    d = run_dir(run_id, category)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.name)


def usage(run_id):
    """Bytes on disk per category -- shown per node so a run's footprint is visible."""
    out = {}
    for c in CATEGORIES:
        d = run_dir(run_id, c)
        if d.is_dir():
            n = sum(1 for _ in d.rglob("*") if _.is_file())
            mb = sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1e6
            if n:
                out[c] = {"files": n, "mb": round(mb, 1)}
    return out


# ───────────────────────────── dataset registry ─────────────────────────────
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def fingerprint(dataset_path):
    """Content hash over the labels plus an inventory of the images.

    Labels are hashed in full -- they are the supervision, and they are small.
    Images are hashed by name and byte size rather than by content: hashing tens of
    thousands of JPEGs is far too slow to run on every registration, but name+size
    still separates two sets that share labels while differing in imagery. That case
    is real here -- `round4` and `round4_1280` carry identical annotations over
    differently sized frames, and calling them one dataset would let a resolution
    change masquerade as a like-for-like comparison.
    """
    p = Path(dataset_path)
    h = hashlib.sha256()
    for f in sorted(p.rglob("*.txt")):
        if "labels" in f.parts:
            h.update(f.relative_to(p).as_posix().encode())
            h.update(f.read_bytes())
    for f in sorted(p.rglob("*")):
        if f.suffix.lower() in IMG_EXT and f.is_file():
            h.update(f"{f.relative_to(p).as_posix()}:{f.stat().st_size}".encode())
    return h.hexdigest()[:16]


def scan_dataset(dataset_path):
    """Count images, boxes and per-class boxes by reading the label files."""
    p = Path(dataset_path)
    mix, n_boxes, splits = {}, 0, {}
    for split in ("train", "val", "test"):
        lbl = p / split / "labels"
        if not lbl.is_dir():
            lbl = p / "labels" / split
        if not lbl.is_dir():
            continue
        files = list(lbl.glob("*.txt"))
        splits[split] = len(files)
        for f in files:
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    cid = int(line.split()[0])
                except (ValueError, IndexError):
                    continue
                mix[cid] = mix.get(cid, 0) + 1
                n_boxes += 1
    return {"class_mix": mix, "n_boxes": n_boxes,
            "n_train": splits.get("train", 0), "n_val": splits.get("val", 0),
            "n_images": sum(splits.values())}


def register_dataset(path, name=None, run_id=None, source_runs=None,
                     label_source=None, note=""):
    """Record a dataset. Re-registering the same content returns the existing row.

    Identity is the fingerprint, so rebuilding a dataset that happens to be identical
    does not fork the lineage, while rebuilding it with different data always does.
    """
    init()
    path = str(Path(path).resolve())
    fp = fingerprint(path)
    prior = db.one("SELECT * FROM lab_datasets WHERE fingerprint=?", fp)
    if prior:
        return prior["id"]
    s = scan_dataset(path)
    return db.run(
        """INSERT INTO lab_datasets
           (run_id,name,path,n_train,n_val,n_images,n_boxes,class_mix,
            source_runs,label_source,fingerprint,note,created)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        run_id, name or Path(path).name, path, s["n_train"], s["n_val"],
        s["n_images"], s["n_boxes"], db.jdump(s["class_mix"]),
        db.jdump(source_runs or []), label_source, fp, note, time.time())


def datasets():
    init()
    out = db.rows("SELECT * FROM lab_datasets ORDER BY id DESC")
    for d in out:
        d["class_mix"] = db.jload(d["class_mix"], {})
        d["source_runs"] = db.jload(d["source_runs"], [])
        d["exists"] = Path(d["path"]).is_dir()
        d["trainings"] = _trainings_for(d["path"])
    return out


def _trainings_for(path):
    """Match a dataset to its trainings by folder name, not by the stored string.

    Trainings record whatever path the trainer was given -- usually relative
    (`dataset/round4_1280`) -- while a registered dataset stores an absolute one. A plain
    equality test therefore reports "never trained on" for data that plainly was.
    """
    if not _has_trainings():
        return []
    name = Path(path).name
    return [t for t in db.rows(
        "SELECT id,tag,status,map50,recall,dataset FROM lab_trainings ORDER BY id DESC")
        if t["dataset"] and Path(t["dataset"]).name == name]


def _has_trainings():
    return bool(db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='lab_trainings'"))


# ───────────────────────────── weight archive ─────────────────────────────
def archive_weights(src, training_id=None, tag=None, dataset_id=None, **metrics):
    """Copy a weight file into the archive under its own name and hash it.

    Copy, never move: the trainer keeps working with its own file, and the archive
    holds a version that nothing downstream can overwrite. Re-archiving identical
    bytes is a no-op, so calling this twice is safe.
    """
    init()
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"no weight file at {src}")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    prior = db.one("SELECT * FROM lab_weights WHERE sha256=?", sha)
    if prior:
        return prior["id"]
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{tag or 'model'}_t{training_id or 0}_{sha[:8]}{src.suffix}"
    dst = WEIGHTS_DIR / name
    if not dst.exists():
        shutil.copy2(src, dst)
    return db.run(
        """INSERT INTO lab_weights
           (training_id,dataset_id,tag,path,source_path,size_mb,sha256,
            map50,map5095,precision,recall,base_model,epochs,note,created)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        training_id, dataset_id, tag, str(dst), str(src),
        round(dst.stat().st_size / 1e6, 2), sha,
        metrics.get("map50"), metrics.get("map5095"), metrics.get("precision"),
        metrics.get("recall"), metrics.get("base_model"), metrics.get("epochs"),
        metrics.get("note", ""), time.time())


def weights():
    init()
    out = db.rows("SELECT * FROM lab_weights ORDER BY id DESC")
    for w in out:
        w["exists"] = Path(w["path"]).is_file()
    return out


def verify_weights():
    """Re-hash every archived file. An archive nobody checks is not an archive."""
    bad = []
    for w in weights():
        p = Path(w["path"])
        if not p.is_file():
            bad.append({**w, "problem": "missing"})
        elif hashlib.sha256(p.read_bytes()).hexdigest() != w["sha256"]:
            bad.append({**w, "problem": "hash mismatch"})
    return {"checked": len(weights()), "problems": bad}


# ───────────────────────────── pipeline graph ─────────────────────────────
def _config(run_id):
    r = db.one("SELECT config FROM lab_runs WHERE id=?", run_id)
    return db.jload(r["config"], {}) if r else {}


def save_graph(run_id, graph):
    """The node layout and parameters for a run, as authored in the editor.

    Merged into the run's config rather than replacing it -- the graph is one key
    beside whatever else the run already carries.
    """
    cfg = _config(run_id)
    cfg["graph"] = graph
    db.run("UPDATE lab_runs SET config=? WHERE id=?", db.jdump(cfg), run_id)
    return True


def load_graph(run_id):
    return _config(run_id).get("graph")
