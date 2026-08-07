"""Put the filesystem into the same shape as the mental model: one folder per station.

Everything in the UI is organised around a station, and nothing on disk was. Footage sat
on a delivery drive mixed across four surveys, gold frames were keyed by a bare site id,
renders were flat, and run artifacts were written to a `lab_runs/` folder nothing used.
That mismatch is most of why the project felt disorganised.

    stations/FID-33_FID33-PK5/
        footage/    symlinks to the source videos
        runs/       run-7/segments, crops, frames …
        gold/       frozen human-verified frames
        datasets/   datasets built from this station
        models/     station model weights
        renders/    annotated previews
        reports/    report cards and workbooks

**Footage is symlinked, never copied.** The delivery drive holds 31 GB and is removable;
duplicating it would be slow, would double the disk, and would create a second copy that
can drift from the original. A symlink makes the station folder browsable — which is the
whole point — while the delivery drive stays canonical.

**Everything the Lab produces is moved for real**, and every database path it moved is
rewritten in the same transaction-ish pass. A file moved without its path updated is
worse than not moving it: the UI silently shows nothing.
"""
import os
import re
import shutil
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
STATIONS = ROOT / "stations"
SUBDIRS = ("footage", "runs", "gold", "datasets", "models", "renders", "reports")


def slug(site):
    """FID-33 + 'FID33 PK5 (rural)' -> 'FID-33_FID33-PK5-rural'."""
    name = re.sub(r"[^A-Za-z0-9]+", "-", site["name"] or "").strip("-")
    return f"{site['code']}_{name}" if name else site["code"]


def station_dir(site_id, sub=None, create=True):
    site = db.one("SELECT id, code, name FROM sites WHERE id=?", site_id)
    if not site:
        return None
    d = STATIONS / slug(site)
    if sub:
        d = d / sub
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


LEGACY_RENDERS = ROOT / "app" / "annotated"


def render_path(video_id, create=False):
    """Where this video's annotated render lives.

    One resolver, used by both the API and the renderer, so moving renders into station
    folders cannot leave one of them looking in the old place. Falls back to the flat
    legacy folder for anything not yet attached to a station.
    """
    v = db.one("SELECT name, site_id FROM videos WHERE id=?", video_id)
    if not v:
        return None
    fname = f"annotated_{v['name']}.mp4"
    if v["site_id"]:
        d = station_dir(v["site_id"], "renders", create=create)
        if d:
            p = d / fname
            if p.exists() or create:
                return p
    legacy = LEGACY_RENDERS / fname
    if legacy.exists():
        return legacy
    # Nothing yet: name the place it SHOULD go, so a new render lands correctly.
    if v["site_id"]:
        d = station_dir(v["site_id"], "renders", create=True)
        if d:
            return d / fname
    LEGACY_RENDERS.mkdir(parents=True, exist_ok=True)
    return legacy


def all_renders():
    """Every render on disk, wherever it lives."""
    out = []
    for d in list(STATIONS.glob("*/renders")) + [LEGACY_RENDERS]:
        if d.is_dir():
            out += [p for p in d.glob("*.mp4")]
    return out


def plan():
    """What the reorganisation would do, without touching anything."""
    steps = []
    for site in db.rows("SELECT id, code, name FROM sites ORDER BY code"):
        base = STATIONS / slug(site)
        steps.append({"kind": "mkdir", "path": str(base),
                      "detail": f"{len(SUBDIRS)} subfolder(s)"})

        for f in db.rows("""SELECT path, name FROM lab_footage
                            WHERE site_id=? AND dup_of IS NULL ORDER BY start_clock""",
                         site["id"]):
            steps.append({"kind": "link", "src": f["path"],
                          "dst": str(base / "footage" / f["name"]),
                          "detail": "symlink"})

        gold = ROOT / "lab_gold" / str(site["id"])
        if gold.is_dir():
            n = sum(1 for _ in gold.glob("*.jpg"))
            steps.append({"kind": "move", "src": str(gold), "dst": str(base / "gold"),
                          "detail": f"{n} gold frame(s) + database paths"})

        for r in db.rows("SELECT id FROM lab_runs WHERE site_id=?", site["id"]):
            src = ROOT / "lab_runs" / str(r["id"])
            if src.is_dir():
                steps.append({"kind": "move", "src": str(src),
                              "dst": str(base / "runs" / f"run-{r['id']}"), "detail": "run artifacts"})

        vids = db.rows("SELECT name FROM videos WHERE site_id=?", site["id"])
        for v in vids:
            p = ROOT / "app" / "annotated" / f"annotated_{v['name']}.mp4"
            if p.exists():
                steps.append({"kind": "move", "src": str(p),
                              "dst": str(base / "renders" / p.name),
                              "detail": f"{p.stat().st_size / 1e6:.0f} MB render"})
    return steps


def run(dry=True):
    """Do it. Paths in the database are rewritten as each thing moves."""
    done, failed = [], []

    for site in db.rows("SELECT id, code, name FROM sites ORDER BY code"):
        base = STATIONS / slug(site)
        if not dry:
            for sub in SUBDIRS:
                (base / sub).mkdir(parents=True, exist_ok=True)
        done.append(f"mkdir {base.relative_to(ROOT)}/")

        # ── footage: symlink, never copy ──
        for f in db.rows("""SELECT path, name FROM lab_footage
                            WHERE site_id=? AND dup_of IS NULL""", site["id"]):
            src, link = Path(f["path"]), base / "footage" / f["name"]
            if not src.exists():
                failed.append(f"missing source {src}")
                continue
            if not dry:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(src)
            done.append(f"link  {link.relative_to(ROOT)} -> {src}")

        # ── gold frames: move, then rewrite the stored paths ──
        gsrc = ROOT / "lab_gold" / str(site["id"])
        gdst = base / "gold"
        if gsrc.is_dir():
            n = 0
            for p in sorted(gsrc.glob("*.jpg")):
                if not dry:
                    shutil.move(str(p), str(gdst / p.name))
                    db.run("UPDATE lab_gold_frames SET image_path=? WHERE image_path=?",
                           str(gdst / p.name), str(p))
                n += 1
            if not dry and not any(gsrc.iterdir()):
                gsrc.rmdir()
            done.append(f"move  {n} gold frame(s) -> {gdst.relative_to(ROOT)}/")

        # ── run artifacts ──
        for r in db.rows("SELECT id FROM lab_runs WHERE site_id=?", site["id"]):
            rsrc = ROOT / "lab_runs" / str(r["id"])
            rdst = base / "runs" / f"run-{r['id']}"
            if not rsrc.is_dir():
                continue
            if not dry:
                rdst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(rsrc), str(rdst))
                for tbl, col in (("lab_crops", "crop_path"), ("lab_crops", "ctx_path"),
                                 ("lab_segments", "path"), ("lab_segments", "compressed_path")):
                    db.run(f"UPDATE {tbl} SET {col} = REPLACE({col}, ?, ?) "
                           f"WHERE {col} LIKE ?", str(rsrc), str(rdst), f"{rsrc}%")
            done.append(f"move  run {r['id']} -> {rdst.relative_to(ROOT)}/")

        # ── renders ──
        for v in db.rows("SELECT id, name FROM videos WHERE site_id=?", site["id"]):
            p = ROOT / "app" / "annotated" / f"annotated_{v['name']}.mp4"
            dst = base / "renders" / p.name
            if not p.exists():
                continue
            if not dry:
                shutil.move(str(p), str(dst))
            done.append(f"move  render {p.name} -> {dst.relative_to(ROOT)}/")

    if not dry:
        db.log(None, "organised", "station folders",
               f"{len(done)} action(s), {len(failed)} problem(s)")
    return {"actions": done, "problems": failed, "dry": dry}


def verify():
    """Every stored path must point at something that exists."""
    bad = []
    for r in db.rows("SELECT id, image_path FROM lab_gold_frames WHERE image_path IS NOT NULL"):
        if not Path(r["image_path"]).exists():
            bad.append(("gold_frame", r["id"], r["image_path"]))
    for r in db.rows("SELECT id, crop_path FROM lab_crops WHERE crop_path IS NOT NULL LIMIT 5000"):
        if not Path(r["crop_path"]).exists():
            bad.append(("crop", r["id"], r["crop_path"]))
    for r in db.rows("SELECT id, path FROM lab_segments WHERE path IS NOT NULL"):
        if not Path(r["path"]).exists():
            bad.append(("segment", r["id"], r["path"]))
    links = 0
    for d in STATIONS.glob("*/footage/*") if STATIONS.is_dir() else []:
        links += 1
        if d.is_symlink() and not d.resolve().exists():
            bad.append(("dangling link", d.name, str(d)))
    return {"checked_links": links, "broken": bad}


def tree():
    """The layout as it stands, for showing back."""
    out = []
    if not STATIONS.is_dir():
        return out
    for st in sorted(STATIONS.iterdir()):
        if not st.is_dir():
            continue
        row = {"station": st.name, "subs": {}}
        for sub in SUBDIRS:
            d = st / sub
            if not d.is_dir():
                continue
            files = [p for p in d.rglob("*") if p.is_file() or p.is_symlink()]
            if files:
                mb = sum(p.stat().st_size for p in files
                         if not p.is_symlink() and p.exists()) / 1e6
                row["subs"][sub] = {"files": len(files), "mb": round(mb, 1)}
        out.append(row)
    return out
