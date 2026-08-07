"""One station, end to end: footage in, classified counts out.

Everything before this was a stage tested on its own. This is the whole chain on footage
nobody has looked at -- segment, extract, count, classify axles, and report what needed a
person -- which is the only way to find out what the pipeline actually costs per station
hour and where a real survey would stall.

Run it, then read the summary rather than the model's confidence: the numbers that matter
are how many vehicles were counted, how many the model settled on its own, and how many
are waiting for somebody. A pass that classifies everything and queues nothing has not
succeeded, it has stopped checking.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path[:0] = [str(ROOT / "lab"), str(ROOT / "app")]

import db          # noqa: E402
import pipeline    # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_run(footage_id, parts, name):
    f = db.one("SELECT path, site_id FROM lab_footage WHERE id=?", footage_id)
    cfg = {"segment_minutes": 15, "extract_segments": parts,
           "imgsz": 960, "conf": 0.12, "sample_n": 0}
    rid = db.run("""INSERT INTO lab_runs (name,source_path,status,config,created,site_id)
                    VALUES (?,?,'draft',?,?,?)""",
                 name, f["path"], db.jdump(cfg), time.time(), f["site_id"])
    for s in pipeline.STAGES:
        db.run("INSERT INTO lab_stages (run_id,stage,status) VALUES (?,?,'pending')", rid, s)
    return rid


def run_station(jobs):
    """jobs: [(footage_id, [part indexes], run name)]"""
    t0 = time.time()
    made = []
    for fid, parts, name in jobs:
        rid = make_run(fid, parts, name)
        log(f"run {rid}: segmenting {name}")
        pipeline.segment(rid)
        log(f"run {rid}: extracting parts {parts} — the slow stage")
        t = time.time()
        pipeline.extract(rid)
        log(f"run {rid}: extract took {(time.time()-t)/60:.1f} min")
        for v in db.rows("""SELECT id,name,frames,fps FROM videos
                            WHERE path LIKE ? ORDER BY id""",
                         f"%run{rid}/segments%"):
            db.run("UPDATE videos SET site_id=? WHERE id=?",
                   db.one("SELECT site_id FROM lab_runs WHERE id=?", rid)["site_id"], v["id"])
            made.append(v["id"])
    return made, time.time() - t0


def finish(video_ids):
    """Count, classify, and report what a person still has to do."""
    import counting
    import stations
    import axle_pass
    from report_card import PCU

    out = []
    for vid in video_ids:
        v = db.one("SELECT id,name,frames,fps,start_clock FROM videos WHERE id=?", vid)
        lines, src = stations.lines_for(vid)
        if not lines:
            log(f"video {vid}: no count line — skipped")
            continue
        r = counting.count_video(vid, lines)
        before = {k: c.get("total", 0) for k, c in r["per_class"].items()}
        log(f"video {vid} {v['name']}: {r['total']} vehicles counted")

        ax = axle_pass.run(vid)
        log(f"video {vid}: axles — {json.dumps({k: ax[k] for k in ax if k != 'moves'})}")

        r2 = counting.count_video(vid, lines)
        after = {k: c.get("total", 0) for k, c in r2["per_class"].items()}
        out.append({
            "video_id": vid, "name": v["name"], "clock": v["start_clock"],
            "minutes": round((v["frames"] or 0) / (v["fps"] or 25) / 60, 1),
            "total": r2["total"],
            "pcu": round(sum(PCU.get(k, 1.0) * n for k, n in after.items()), 1),
            "heavy_before": {k: before.get(k, 0) for k in ("2Axle_Truck", "3Axle_Truck", "MAV")},
            "heavy_after": {k: after.get(k, 0) for k in ("2Axle_Truck", "3Axle_Truck", "MAV")},
            "axles": {k: ax.get(k) for k in
                      ("checked", "applied", "needs_review", "too_small",
                       "already_answered_by_human", "model_id")},
        })
    return out


if __name__ == "__main__":
    JOBS = [(33, [2, 3], "fid33_full_tp12_p23"),
            (44, [2, 3], "fid33_full_tp13_p23")]
    vids, secs = run_station(JOBS)
    log(f"extraction complete: {len(vids)} new videos in {secs/60:.1f} min")
    res = finish(vids)
    Path("/tmp/station_run.json").write_text(json.dumps(
        {"videos": res, "extract_minutes": round(secs / 60, 1)}, indent=1))
    log("DONE")
    print(json.dumps(res, indent=1))
