"""Assemble a delivery folder for one station: annotated clips + the APRDC workbook.

Everything the client receives, produced in one pass so the workbook and the videos can
never describe different numbers. The order matters and is not arbitrary:

    count -> classify axles -> (human review) -> render -> workbook

Rendering happens AFTER classification because the annotated video draws each vehicle's
class on it. Render first and the clips show trucks labelled 2-Axle that the workbook
counts as 3-Axle, and the person checking the delivery has no way to tell which is right.

The run refuses to build the workbook while trucks are still sitting in the review queue,
unless told otherwise. A survey that ships with unreviewed low-confidence classifications
looks exactly like one that shipped clean.
"""
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def videos_for(site_id):
    """Every clip of this station that can be counted, in wall-clock order."""
    import sites
    out = []
    for v in db.rows("""SELECT id,name,start_clock,frames,fps FROM videos
                        WHERE site_id=? ORDER BY start_clock""", site_id):
        lines, src = sites.lines_for(v["id"])
        if not lines:
            log(f"  video {v['id']} {v['name']}: no count line — excluded")
            continue
        out.append(v["id"])
    return out


def classify(video_ids):
    """Run the axle model over every clip. Returns what still needs a person."""
    import axle_pass
    m = axle_pass.current_model()
    if not m:
        return {"error": "no promoted axle model"}
    log(f"axle model #{m['id']} — {100*m['accuracy']:.0f}% on held-out labels")
    summary, pending = [], 0
    for vid in video_ids:
        r = axle_pass.run(vid)
        q = len(axle_pass.queue(vid))
        pending += q
        summary.append({"video_id": vid, **{k: r.get(k) for k in
                        ("checked", "applied", "needs_review", "too_small",
                         "already_answered_by_human")}, "queue": q})
        log(f"  video {vid}: {r.get('checked',0)} heavy · {r.get('applied',0)} auto · "
            f"{q} to review · {r.get('too_small',0)} too far")
    return {"model_id": m["id"], "per_video": summary, "pending_review": pending}


def render_all(video_ids, out_dir):
    """One annotated clip per video, named by its clock time so they sort as a survey."""
    import render as render_mod
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for vid in video_ids:
        v = db.one("SELECT name, start_clock FROM videos WHERE id=?", vid)
        job = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) "
                     "VALUES (?,?,?,?,?)", vid, "render", "running", 0, "")
        t = time.time()
        try:
            render_mod.render(vid, job)
        except Exception as e:
            log(f"  video {vid}: render FAILED — {e}")
            continue
        src = _find_render(vid)
        if not src:
            log(f"  video {vid}: render produced no file")
            continue
        clock = (v["start_clock"] or "").replace(":", "").replace("-", "").replace(" ", "_")
        dst = out_dir / f"{clock}_video{vid}_annotated.mp4"
        shutil.copy2(src, dst)
        made.append(str(dst))
        log(f"  video {vid}: rendered in {time.time()-t:.0f}s -> {dst.name}")
    return made


def _find_render(video_id):
    try:
        sys.path.insert(0, str(ROOT / "lab"))
        import organise
        p = organise.render_path(video_id)
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    p = ROOT / "app" / "annotated" / f"annotated_{v['name']}.mp4"
    return p if p.exists() else None


def deliver(site_id, out_dir, meta=None, allow_pending=False, skip_render=False):
    import aprdc_workbook
    out_dir = Path(out_dir)
    site = db.one("SELECT * FROM sites WHERE id=?", site_id)
    meta = {"road": (site or {}).get("name", ""), "location_id": (site or {}).get("code", ""),
            **(meta or {})}

    log(f"station {meta['location_id']} — {meta['road']}")
    vids = videos_for(site_id)
    log(f"{len(vids)} clip(s) with a count line")

    cls = classify(vids)
    if cls.get("error"):
        return cls
    if cls["pending_review"] and not allow_pending:
        return {"blocked": True, "pending_review": cls["pending_review"],
                "why": "trucks are still waiting for a human decision. Review them, or "
                       "pass allow_pending=True to ship with the model's fallback classes.",
                "classify": cls}

    data = aprdc_workbook.build(vids, meta)
    if data.get("error"):
        return data
    wb = aprdc_workbook.write(data, out_dir / f"APRDC_{meta['location_id']}_"
                                              f"{data['window']['from'][:10]}.xlsx", meta)
    log(f"workbook -> {Path(wb).name}  ({data['total']} vehicles, {data['pcu']} PCU)")

    videos = []
    if not skip_render:
        log("rendering annotated clips (the slow part)")
        videos = render_all(vids, out_dir / "annotated")

    (out_dir / "run_summary.json").write_text(json.dumps({
        "station": meta, "produced": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clips": len(vids), "workbook": wb, "annotated": videos,
        "window": data["window"], "total": data["total"], "pcu": data["pcu"],
        "per_class": data["totals"], "classify": cls,
        "unreviewed_columns": data["unreviewed_columns"],
    }, indent=1))
    return {"ok": True, "dir": str(out_dir), "workbook": wb, "videos": videos,
            "total": data["total"], "pcu": data["pcu"],
            "unreviewed_columns": data["unreviewed_columns"], "classify": cls}
