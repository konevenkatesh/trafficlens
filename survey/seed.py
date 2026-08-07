"""First-run setup for a packaged install.

The app ships its weights, but a fresh database knows nothing about them. On this
developer machine the `models` table and the axle head's `lab_attr_models` /
`lab_attr_promotions` rows were built up over months of Lab work; a surveyor's laptop has
an empty file. Without seeding, a packaged build starts, accepts footage, extracts
nothing (no registered detector) and silently skips the axle head (no promoted model) --
producing a report whose heavy-vehicle columns are the detector's raw guess.

Worse, the rows that *do* exist here store **absolute paths from this Mac**. Copying the
database would be no better than having none.

So: paths are always recomputed from where the app is actually installed, and this runs
on every start rather than once. It is idempotent -- re-registering the same weights is a
no-op -- which matters because the alternative is a "first run" flag that goes stale the
first time somebody moves the folder.
"""
import json
import sys
import time
from pathlib import Path

import db


def _root():
    """Where the bundled files live: the PyInstaller temp dir, or the repo."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def detectors():
    """Register every detector .pt that shipped with this build."""
    import models_registry
    models_registry.init()
    models_registry.MODELS_DIR = _root() / "models"
    found = models_registry.discover()
    if not models_registry.default_id():
        # Prefer the version this build was validated against; otherwise best mAP.
        have = {m["id"] for m in models_registry.listing()}
        pick = ("yolo26s_morth15_v5" if "yolo26s_morth15_v5" in have
                else next(iter(sorted(have)), None))
        if pick:
            models_registry.set_default(pick)
    return {"found": found, "default": models_registry.default_id()}


def axle_head():
    """Register and promote the axle head that shipped with this build.

    The checkpoint carries its own architecture and class order, so those are read from
    the file rather than hard-coded -- a mismatch between the two is how a head silently
    starts predicting the wrong class for every crop.
    """
    # Named, not "whichever sorts last". The filenames are content hashes, so a sort
    # picks an arbitrary checkpoint -- and this directory holds nine the promotion gate
    # REJECTED alongside the one it blessed. Shipping a rejected head that scores 0.70
    # instead of the promoted 0.823 would be invisible: it produces numbers, just worse
    # ones. This must match KEEP_HEADS in TrafficLens.spec.
    PROMOTED = "axles_resnet18_4d598166.pt"
    d = _root() / "models" / "attrs"
    p = d / PROMOTED
    if not p.is_file():
        avail = sorted(x.name for x in d.glob("axles_*.pt")) if d.is_dir() else []
        return {"skipped": f"promoted axle head {PROMOTED} not bundled", "found": avail}
    path = str(p)

    import axle_pass
    axle_pass.init()                     # also ensures the site_id scoping columns

    # These two tables belong to the Lab's trainer, which this app deliberately does not
    # bundle -- so on a fresh install they simply do not exist and every read of them
    # fails. Created here with IF NOT EXISTS and only the columns the survey app reads:
    # where the Lab made them first, its fuller definition is already in place and this
    # is a no-op.
    db.conn().executescript("""
        CREATE TABLE IF NOT EXISTS lab_attr_models (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          attribute TEXT, arch TEXT, classes TEXT,
          accuracy REAL, macro_f1 REAL, path TEXT, sha256 TEXT, size_mb REAL,
          reliable INTEGER DEFAULT 1, note TEXT, created REAL, site_id INTEGER);
        CREATE TABLE IF NOT EXISTS lab_attr_promotions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          attribute TEXT, model_id INTEGER, promoted INTEGER,
          model_accuracy REAL, detector_accuracy REAL, margin REAL, n_val INTEGER,
          reason TEXT, created REAL, site_id INTEGER);
    """)
    db.conn().commit()

    have = db.one("SELECT id FROM lab_attr_models WHERE path=?", path)
    if have:
        mid = have["id"]
    else:
        try:
            import torch
            blob = torch.load(path, map_location="cpu", weights_only=False)
            arch = blob.get("arch", "resnet18")
            classes = blob.get("classes") or ["2_axle", "3_axle", "4_or_more_axle"]
        except Exception as e:
            return {"skipped": f"could not read the axle checkpoint: {e}"}
        mid = db.run(
            """INSERT INTO lab_attr_models
               (attribute,arch,classes,accuracy,macro_f1,path,reliable,note,created,site_id)
               VALUES ('axles',?,?,?,?,?,1,?,?,NULL)""",
            arch, json.dumps(classes), 0.823, 0.823, path,
            "shipped with this build", time.time())

    # Promoted globally (site_id NULL): it is the universal head every station falls back
    # to until one trains its own.
    ok = db.one("""SELECT 1 FROM lab_attr_promotions
                   WHERE model_id=? AND promoted=1 AND site_id IS NULL""", mid)
    if not ok:
        db.run("""INSERT INTO lab_attr_promotions
                  (attribute,model_id,promoted,model_accuracy,reason,created,site_id)
                  VALUES ('axles',?,1,?,?,?,NULL)""",
               mid, 0.823, "shipped and promoted with this build", time.time())
    return {"model_id": mid, "path": path}


def run():
    out = {"detectors": detectors(), "axles": axle_head()}
    return out
