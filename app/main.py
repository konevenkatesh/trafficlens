"""TrafficLens personal edition - FastAPI backend serving the complete workflow:
register video -> quality check -> draw lines -> extract trajectories -> counts -> reports.
Run: .venv/bin/python app/main.py   (http://localhost:8799)
"""
import json
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import axle_pass
import counting
import db
import engine
import geocode
import models_registry
import quality
import render as render_mod
import reports
import review_api
import sites

HERE = Path(__file__).parent
app = FastAPI(title="TrafficLens")


@app.middleware("http")
async def no_html_cache(request, call_next):
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct or request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


class VideoIn(BaseModel):
    path: str


class SceneIn(BaseModel):
    lines: list


@app.get("/api/videos")
def list_videos():
    vids = db.rows("SELECT * FROM videos ORDER BY id DESC")
    for v in vids:
        v["quality"] = db.jload(v["quality"], {})
        v["scene"] = (db.one("SELECT lines FROM scenes WHERE video_id=?", v["id"]) or {}).get("lines")
        v["scene"] = db.jload(v["scene"], []) if v["scene"] else []
        v["tracks"] = (db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=?", v["id"]) or {}).get("n", 0)
        job = db.one("SELECT * FROM jobs WHERE video_id=? ORDER BY id DESC LIMIT 1", v["id"])
        v["job"] = job
    return vids


@app.post("/api/videos")
def add_video(inp: VideoIn):
    p = Path(inp.path).expanduser()
    if not p.exists():
        raise HTTPException(404, f"file not found: {p}")
    existing = db.one("SELECT id FROM videos WHERE path=?", str(p))
    if existing:                      # INSERT OR IGNORE would return a meaningless id 0
        return {"id": existing["id"], "existing": True}
    fps, frames, w, h, clock = engine.probe(p)
    q = quality.assess(p)
    vid = db.run(
        "INSERT INTO videos (path,name,fps,frames,width,height,start_clock,quality,created) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        str(p), p.stem, fps, frames, w, h, clock, db.jdump(q), time.time())
    return {"id": vid, "quality": q, "fps": fps, "frames": frames, "start_clock": clock}


class SiteIn(BaseModel):
    name: str
    code: str | None = None
    road_name: str | None = None
    road_ref: str | None = None
    chainage: str | None = None
    district: str | None = None
    state: str | None = None
    camera_id: str | None = None
    carriageway: str | None = None
    notes: str | None = None
    lat: float | None = None
    lon: float | None = None
    bearing: float | None = None     # compass degrees the camera looks along
    geo_source: str | None = None    # gps | manual | map


class VideoPatch(BaseModel):
    site_id: int | None = None
    start_clock: str | None = None
    excluded: bool | None = None
    excluded_reason: str | None = None


@app.get("/api/geocode")
def geocode_search(q: str = "", country: str = "in"):
    """Find a place by name so a station can be located by typing rather than panning."""
    return {"results": geocode.search(q, country=country)}


@app.get("/api/sites")
def get_sites():
    return sites.list_sites()


@app.post("/api/sites")
def add_site(s: SiteIn):
    try:
        sid = sites.create(**s.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    z = sites.refresh_imagery_zoom(sid)      # how sharp the imagery is *here*
    return {"id": sid, "imagery_zoom": z}


@app.patch("/api/sites/{site_id}")
def edit_site(site_id: int, s: SiteIn):
    if not db.one("SELECT id FROM sites WHERE id=?", site_id):
        raise HTTPException(404, "no such site")
    fields = {k: v for k, v in s.model_dump().items() if v is not None}
    sites.update(site_id, **fields)
    z = None
    if "lat" in fields or "lon" in fields:   # moved: re-probe, coverage is local
        z = sites.refresh_imagery_zoom(site_id)
    return {"ok": True, "imagery_zoom": z}


@app.patch("/api/videos/{video_id}")
def patch_video(video_id: int, p: VideoPatch):
    """Assign a video to a location and/or set its wall-clock start by hand.

    The clock matters as much as the location: every 15-minute bin in the report is
    derived from it, so a video whose filename carries no timestamp must be given one
    here rather than silently counted at a made-up hour.
    """
    if not db.one("SELECT id FROM videos WHERE id=?", video_id):
        raise HTTPException(404, "no such video")
    out = {}
    if p.site_id is not None:
        if p.site_id and not db.one("SELECT id FROM sites WHERE id=?", p.site_id):
            raise HTTPException(400, "no such site")
        sites.assign(video_id, p.site_id or None)
        out["site_id"] = p.site_id
    if p.start_clock:
        try:
            out["start_clock"] = sites.set_clock(video_id, p.start_clock)
        except ValueError:
            raise HTTPException(400, "use YYYY-MM-DD HH:MM:SS")
    if p.excluded is not None:
        db.run("UPDATE videos SET excluded=?, excluded_reason=? WHERE id=?",
               1 if p.excluded else 0, p.excluded_reason, video_id)
        out["excluded"] = p.excluded
    return {"ok": True, **out}


@app.get("/api/pipeline/{video_id}")
def pipeline(video_id: int):
    """The state of one video's journey, as a graph.

    Each node reports what it actually knows, so a stalled video says which step it is
    waiting on rather than just failing to produce a report.
    """
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such video")
    q = db.jload(v["quality"], {})
    lines, line_src = sites.lines_for(video_id)
    n_tracks = (db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=?", video_id) or {})["n"]
    model = (db.one("SELECT model_id FROM tracks WHERE video_id=? LIMIT 1", video_id) or {}).get("model_id")
    job = db.one("SELECT * FROM jobs WHERE video_id=? AND kind='extract' ORDER BY id DESC LIMIT 1",
                 video_id)
    site = db.one("SELECT * FROM sites WHERE id=?", v.get("site_id")) if v.get("site_id") else None
    clock_ok = bool(v["start_clock"]) and v["start_clock"] != engine.FALLBACK_CLOCK
    reviewed = (db.one("SELECT COUNT(*) n FROM box_reviews WHERE video_id=?", video_id) or {})["n"]

    counts = None
    if lines and n_tracks:
        try:
            counts = counting.count_video(video_id, lines)
        except Exception:
            counts = None

    def attr_state(attr, parent):
        judged = (db.one("SELECT COUNT(*) n FROM track_attrs WHERE video_id=? AND attr=?",
                         video_id, attr) or {})["n"]
        pool = 0
        if counts:
            pool = len({e["track_id"] for e in counts["events"] if e["class"] == parent})
        return {"judged": judged, "pool": pool}

    extracting = job and job["status"] in ("running", "queued")
    nodes = [
        {"id": "source", "title": "Footage", "subtitle": v["name"],
         "status": "done", "meta": [
             {"k": "length", "v": f"{round((v['frames'] or 0)/max(v['fps'] or 1,1)/60,1)} min"},
             {"k": "station", "v": site["code"] if site else "unassigned"},
             {"k": "start", "v": (v["start_clock"] or "")[:16] if clock_ok else "not set"}]},
        {"id": "quality", "title": "Quality", "subtitle": q.get("condition") or "",
         "status": "done" if q.get("grade") else "idle",
         "meta": [{"k": "grade", "v": q.get("grade") or "—"}]},
        {"id": "lines", "title": "Count line", "status": "done" if lines else "blocked",
         "subtitle": "" if lines else "draw a line to count",
         "meta": [{"k": "lines", "v": len(lines)}]},
        {"id": "extract", "title": "Extract",
         "subtitle": model or "",
         "status": "running" if extracting else ("done" if n_tracks
                   else ("error" if job and job["status"] == "error" else "idle")),
         "progress": (job or {}).get("progress") or 0,
         "meta": [{"k": "tracks", "v": n_tracks}]},
        {"id": "count", "title": "Count",
         "status": "done" if counts else ("blocked" if n_tracks and not lines else "idle"),
         "meta": [{"k": "crossings", "v": counts["total"] if counts else "—"}]},
        {"id": "verify", "title": "Verify", "subtitle": "human sample",
         "status": "done" if reviewed else "idle",
         "meta": [{"k": "checked", "v": reviewed}]},
    ]
    # Axle class sits between counting and the report because it does not change the
    # vehicle total -- it moves trucks between the 2Axle / 3Axle / MAV columns, which the
    # detector cannot tell apart at all (17 of 17 called 2Axle on a road that is two
    # thirds 3-axle). Its own node, so a report built before it has run says so.
    ax = axle_pass.state(video_id)
    nodes.append({
        "id": "axles", "title": "Axle class", "subtitle": (
            f"model {ax['model']['id']} · {100*(ax['model']['accuracy'] or 0):.0f}%"
            if ax["model"] else "no promoted model"),
        # `total` counts this app's own checks; a clip whose trucks were all settled in
        # the Lab has none of those and was reading "idle" while being entirely done.
        "status": ("blocked" if not ax["model"] else
                   "error" if ax["pending"] else
                   "done" if (ax["total"] or ax["human"]) else "idle"),
        "meta": [{"k": "classified", "v": ax["auto"] + ax["human"]},
                 {"k": "to review", "v": ax["pending"]},
                 {"k": "too far", "v": ax["too_small"]}]})
    edges = [{"from": "source", "to": "quality"}, {"from": "source", "to": "lines"},
             {"from": "quality", "to": "extract"}, {"from": "lines", "to": "count"},
             {"from": "extract", "to": "count"}, {"from": "count", "to": "verify"},
             {"from": "count", "to": "axles"}, {"from": "axles", "to": "report"}]

    # the three APRDC sub-splits run in parallel off counting — the reason this is a graph
    for attr, parent, label in (("taxi", "Car_Jeep_Van", "Taxi"),
                                ("maxi", "3W_Auto", "7-seater"),
                                ("apsrtc", "Bus", "APSRTC")):
        st = attr_state(attr, parent)
        nodes.append({"id": f"attr_{attr}", "title": label, "subtitle": f"of {parent}",
                      "status": ("done" if st["pool"] and st["judged"] >= st["pool"]
                                 else "running" if st["judged"] else "idle"),
                      "progress": round(100 * st["judged"] / st["pool"], 1) if st["pool"] else 0,
                      "meta": [{"k": "judged", "v": f"{st['judged']}/{st['pool']}"}]})
        edges.append({"from": "count", "to": f"attr_{attr}"})
        edges.append({"from": f"attr_{attr}", "to": "report"})

    reports_dir = HERE / "reports_out"
    made = sorted(reports_dir.glob(f"*{v['name']}*.xlsx")) if reports_dir.exists() else []
    nodes.append({"id": "report", "title": "Report", "subtitle": "IRC / APRDC",
                  "status": "done" if made else ("idle" if counts else "blocked"),
                  "meta": [{"k": "files", "v": len(made)}]})
    edges.append({"from": "verify", "to": "report"})

    return {"video": {"id": v["id"], "name": v["name"], "site": site["name"] if site else None,
                      "model": model, "clock_ok": clock_ok},
            "graph": {"nodes": nodes, "edges": edges},
            "counts": {"total": counts["total"] if counts else None,
                       "per_class": {k: c["total"] for k, c in counts["per_class"].items()}
                       if counts else {}}}


@app.get("/api/dashboard")
def dashboard():
    """Everything the overview needs in one round trip.

    Counting is a query over stored trajectories, so this is cheap for the videos that
    have a line; videos without one are reported as such rather than shown as zero —
    a placeholder zero is a number people act on.
    """
    out = {"videos": [], "totals": {}, "per_class": {}, "grades": [], "sites": []}
    per_class = {}
    # Hourly profiles are kept PER SITE. Vehicles counted on different roads describe
    # different traffic streams; adding them into one curve would be a meaningless number.
    by_site = {}
    site_rows = {s["id"]: s for s in db.rows("SELECT * FROM sites")}
    for v in db.rows("SELECT * FROM videos ORDER BY id DESC"):
        q = db.jload(v["quality"], {})
        n_tracks = (db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=?", v["id"]) or {})["n"]
        # Ask sites.lines_for, never the scenes table: a station default line is
        # inherited by every video of that camera and has no scenes row. Reading
        # `scenes` directly hid a whole station from this page.
        lines, line_source = sites.lines_for(v["id"])
        model = (db.one("SELECT model_id FROM tracks WHERE video_id=? LIMIT 1", v["id"]) or {}).get("model_id")
        # engine.probe() falls back to this literal when it cannot read a clock from the
        # filename. Those videos have no real wall-clock, so they must not be plotted on
        # an hourly chart — a fabricated 00:00 hour is worse than a missing one.
        clock_ok = bool(v["start_clock"]) and v["start_clock"] != engine.FALLBACK_CLOCK
        site = site_rows.get(v.get("site_id"))
        row = {"id": v["id"], "name": v["name"], "grade": q.get("grade"),
               "condition": q.get("condition"), "tracks": n_tracks,
               "has_line": bool(lines), "line_source": line_source,
               "model": model, "start_clock": v["start_clock"],
               "clock_ok": clock_ok, "clock_source": v.get("clock_source"),
               "site_id": v.get("site_id"), "site_name": site["name"] if site else None,
               "suggested_site": None if v.get("site_id") else sites.suggest_site(v["name"]),
               "excluded": bool(v.get("excluded")),
               "excluded_reason": v.get("excluded_reason"),
               "minutes": round((v["frames"] or 0) / max(v["fps"] or 1, 1) / 60, 1),
               "counted": None}
        if lines and n_tracks:
            try:
                res = counting.count_video(v["id"], lines)
                row["counted"] = res["total"]
                if not row["excluded"]:
                    for cls, c in res["per_class"].items():
                        per_class[cls] = per_class.get(cls, 0) + c["total"]
                if clock_ok and site and not row["excluded"]:
                    b = by_site.setdefault(site["id"], {"hourly": {}, "classes": {},
                                                        "counted": 0, "videos": 0})
                    b["counted"] += res["total"]
                    b["videos"] += 1
                    for e in res["events"]:
                        hr = e["clock"][:2] + ":00"
                        b["hourly"][hr] = b["hourly"].get(hr, 0) + 1
                    for cls, c in res["per_class"].items():
                        b["classes"][cls] = b["classes"].get(cls, 0) + c["total"]
            except Exception as exc:                      # one bad video must not blank the page
                row["error"] = str(exc)[:120]
        out["videos"].append(row)
        if q.get("grade"):
            out["grades"].append({"name": v["name"], "grade": q["grade"],
                                  "short": q.get("condition", "")})
    counted = [v for v in out["videos"] if v["counted"] is not None and not v["excluded"]]
    reviewed = (db.one("SELECT COUNT(*) n FROM box_reviews") or {})["n"]
    for sid, b in by_site.items():
        s = sites.enrich(dict(site_rows[sid]))
        peak = max(b["hourly"].items(), key=lambda kv: kv[1]) if b["hourly"] else None
        out["sites"].append({
            "id": sid, "code": s["code"], "name": s["name"],
            "road_name": s["road_name"], "chainage": s["chainage"],
            "lat": s.get("lat"), "lon": s.get("lon"), "bearing": s.get("bearing"),
            "compass": s.get("compass"), "glare": s.get("glare"),
            "daylight": s.get("daylight"), "map_url": s.get("map_url"),
            "videos": b["videos"], "counted": b["counted"],
            "peak_hour": peak[0] if peak else None,
            "peak_count": peak[1] if peak else 0,
            "hourly": [{"label": h, "value": n} for h, n in sorted(b["hourly"].items())],
            "classes": b["classes"],
        })
    out["sites"].sort(key=lambda s: -s["counted"])
    out["all_sites"] = [{"id": s["id"], "code": s["code"], "name": s["name"],
                         "camera_id": s["camera_id"]} for s in site_rows.values()]
    out["totals"] = {
        "videos": len(out["videos"]),
        "processed": len([v for v in out["videos"] if v["tracks"]]),
        "awaiting_line": len([v for v in out["videos"] if v["tracks"] and not v["has_line"]]),
        "vehicles": sum(v["counted"] for v in counted),
        "sites": len(site_rows),
        "reviewed": reviewed,
        "clock_unknown": len([v for v in out["videos"] if not v["clock_ok"]]),
        "unassigned": len([v for v in out["videos"] if not v["site_id"]]),
    }
    out["per_class"] = per_class
    return out


@app.get("/api/video-file/{video_id}")
def video_file(video_id: int):
    v = db.one("SELECT path FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404)
    return FileResponse(v["path"], media_type="video/mp4")


@app.get("/api/scenes/{video_id}")
def get_scene(video_id: int):
    s = db.one("SELECT * FROM scenes WHERE video_id=?", video_id)
    return {"lines": db.jload(s["lines"], []) if s else []}


@app.post("/api/scenes/{video_id}")
def save_scene(video_id: int, inp: SceneIn):
    db.run("INSERT OR REPLACE INTO scenes VALUES (?,?,?)",
           video_id, db.jdump(inp.lines), time.time())
    return {"ok": True, "n_lines": len(inp.lines)}


class ExtractIn(BaseModel):
    model_id: str | None = None


@app.get("/api/models")
def get_models():
    models_registry.discover()
    return {"models": models_registry.listing(), "default": models_registry.default_id()}


@app.post("/api/models/default")
def set_default_model(m: ExtractIn):
    try:
        return {"default": models_registry.set_default(m.model_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/extract/{video_id}")
def start_extract(video_id: int, body: ExtractIn | None = None):
    if not db.one("SELECT id FROM videos WHERE id=?", video_id):
        raise HTTPException(404)
    running = db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1")
    if running:
        raise HTTPException(409, "another job is running - one GPU, one job")
    model_id = (body.model_id if body else None) or models_registry.default_id()
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) "
                    "VALUES (?,?,?,?,?)", video_id, "extract", "queued", 0,
                    f"queued on {model_id}" if model_id else "")
    threading.Thread(target=engine.extract, args=(video_id, job_id),
                     kwargs={"model_id": model_id}, daemon=True).start()
    return {"job_id": job_id, "model_id": model_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=?", job_id)
    if not j:
        raise HTTPException(404)
    return j


@app.get("/api/progress/{job_id}")
def progress_stream(job_id: int):
    def gen():
        import json
        while True:
            j = db.one("SELECT * FROM jobs WHERE id=?", job_id)
            yield f"data: {json.dumps(j)}\n\n"
            if j and j["status"] in ("done", "error"):
                break
            time.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/counts/{video_id}")
def counts(video_id: int):
    lines = sites.lines_for(video_id)[0]
    if not lines:
        raise HTTPException(400, "draw at least one count line first")
    res = counting.count_video(video_id, lines)
    res["events"] = res["events"][-200:]  # UI shows the tail; full set goes to reports
    return res


class VerdictIn(BaseModel):
    track_id: int
    frame: int
    verdict: str
    new_class: int | None = None


@app.post("/api/render/{video_id}")
def start_render(video_id: int):
    running = db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1")
    if running:
        raise HTTPException(409, "another job is running")
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) VALUES (?,?,?,?,?)",
                    video_id, "render", "queued", 0, "")
    threading.Thread(target=render_mod.render, args=(video_id, job_id), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/annotated/{video_id}")
def annotated(video_id: int):
    # Renders live in the station's folder now, not a flat one. This looked only in the
    # flat folder, so every clip the Lab organised returned "not rendered yet" while the
    # file sat on disk — the same shape of bug as the count line the app could not see.
    p = _render_file(video_id)
    if not p:
        raise HTTPException(404, "not rendered yet")
    return FileResponse(p, media_type="video/mp4", filename=p.name)


def _render_file(video_id):
    """Wherever this video's annotated render actually is."""
    v = db.one("SELECT name, site_id FROM videos WHERE id=?", video_id)
    if not v:
        return None
    fname = f"annotated_{v['name']}.mp4"
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "lab"))
        import organise
        q = organise.render_path(video_id)
        if q and Path(q).exists():
            return Path(q)
    except Exception:
        pass
    for cand in [Path(__file__).parent / "annotated" / fname,
                 *(Path(__file__).parent.parent / "stations").glob(f"*/renders/{fname}")]:
        if cand.exists():
            return cand
    return None


@app.get("/api/frame/{video_id}/{frame_idx}")
def frame_jpg(video_id: int, frame_idx: int):
    from fastapi.responses import Response
    data = review_api.get_frame_jpg(video_id, frame_idx)
    if data is None:
        raise HTTPException(404)
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/review-sample/{video_id}")
def review_sample(video_id: int, n: int = 80):
    return {"items": review_api.sample_tracks(video_id, n=n), "classes": engine.CLASSES}


@app.post("/api/review-verdict/{video_id}")
def review_verdict(video_id: int, inp: VerdictIn):
    review_api.save_verdict(video_id, inp.track_id, inp.frame, inp.verdict, inp.new_class)
    return {"ok": True}


@app.get("/api/review-stats/{video_id}")
def review_stats(video_id: int):
    return review_api.stats(video_id)


@app.post("/api/judge/{video_id}")
def start_judge(video_id: int, n: int = 80):
    running = db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1")
    if running:
        raise HTTPException(409, "another job is running")
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) VALUES (?,?,?,?,?)",
                    video_id, "judge", "queued", 0, "")
    threading.Thread(target=review_api.judge_sample, args=(video_id, job_id, n), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/dedup/{video_id}")
def run_dedup(video_id: int):
    import dedup as dedup_mod
    return dedup_mod.dedup(video_id)


class AttrVerdictIn(BaseModel):
    track_id: int
    value: str


@app.get("/api/attr-sample/{video_id}")
def attr_sample(video_id: int, attr: str = "taxi"):
    import attr_api
    if attr not in attr_api.ATTRS:
        raise HTTPException(400, "unknown attr")
    return {"items": attr_api.crossing_tracks(video_id, attr),
            "spec": {k: v for k, v in attr_api.ATTRS[attr].items() if k != "question"}}


@app.post("/api/attr-verdict/{video_id}/{attr}")
def attr_verdict(video_id: int, attr: str, inp: AttrVerdictIn):
    import attr_api
    attr_api.save_attr(video_id, inp.track_id, attr, inp.value, "human")
    return {"ok": True}


@app.post("/api/attr-judge/{video_id}/{attr}")
def attr_judge(video_id: int, attr: str):
    import attr_api
    running = db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1")
    if running:
        raise HTTPException(409, "another job is running")
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) VALUES (?,?,?,?,?)",
                    video_id, "attr-judge", "queued", 0, "")
    threading.Thread(target=attr_api.judge_attr, args=(video_id, attr, job_id), daemon=True).start()
    return {"job_id": job_id}


# ─────────────────── axle class ───────────────────
# The Lab trains these models; the app only ever runs them. `axle_pass.current_model`
# reads the Lab's promotion table, so weights that failed their gate cannot be used here.
@app.post("/api/axles/{video_id}")
def axles_run(video_id: int):
    running = db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1")
    if running:
        raise HTTPException(409, "another job is running")
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) "
                    "VALUES (?,?,?,?,?)", video_id, "axles", "queued", 0, "")

    def work():
        db.run("UPDATE jobs SET status='running' WHERE id=?", job_id)
        try:
            r = axle_pass.run(video_id, job_id=job_id)
            db.run("UPDATE jobs SET status='done', progress=100, message=? WHERE id=?",
                   json.dumps(r)[:400], job_id)
        except Exception as e:
            db.run("UPDATE jobs SET status='error', message=? WHERE id=?", str(e)[:400], job_id)
    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/axles/{video_id}")
def axles_state(video_id: int):
    return {**axle_pass.state(video_id), "queue": axle_pass.queue(video_id),
            "answers": axle_pass.ANSWERS}


@app.get("/api/axles/{video_id}/{track_id}/{kind}.jpg")
def axles_image(video_id: int, track_id: int, kind: str):
    r = db.one("SELECT crop_path, ctx_path FROM axle_checks WHERE video_id=? AND track_id=?",
               video_id, track_id)
    p = (r or {}).get("ctx_path" if kind == "ctx" else "crop_path")
    if not p or not Path(p).is_file():
        raise HTTPException(404, "no image")
    return FileResponse(p, media_type="image/jpeg")


class AxleVerdictIn(BaseModel):
    track_id: int
    value: str


@app.post("/api/axle-verdict/{video_id}")
def axle_verdict(video_id: int, inp: AxleVerdictIn):
    try:
        return axle_pass.verdict(video_id, inp.track_id, inp.value)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/review-remaining")
def review_remaining(site_id: int, attr: str = ""):
    """Which clips of this station still have work, in clock order.

    Review is a station-level job, not a per-file one: a survey is 8 clips and asking
    somebody to edit a URL 8 times is how 29 of 43 buses get missed while everyone
    believes the pass is finished.
    """
    import attr_api
    import axle_pass
    out = []
    for v in db.rows("SELECT id, name, start_clock FROM videos WHERE site_id=? "
                     "ORDER BY start_clock", site_id):
        n = (len([x for x in attr_api.crossing_tracks(v["id"], attr) if not x.get("judged")])
             if attr else len(axle_pass.queue(v["id"])))
        if n:
            out.append({"video_id": v["id"], "clock": v["start_clock"], "pending": n})
    return {"site_id": site_id, "attr": attr or "axles",
            "videos": out, "total": sum(v["pending"] for v in out)}


@app.get("/api/report/{video_id}")
def report(video_id: int, taxonomy: str = ""):
    lines = sites.lines_for(video_id)[0]
    if not lines:
        raise HTTPException(400, "draw a count line first")
    out = reports.export(video_id, lines, taxonomy or None)
    return FileResponse(out, filename=Path(out).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


(HERE / "annotated").mkdir(exist_ok=True)
# self-heal: jobs are threads of this process - a restart orphans them
db.run("UPDATE jobs SET status='error', message='orphaned by app restart' "
       "WHERE status IN ('running','queued')")

app.mount("/annotated", StaticFiles(directory=HERE / "annotated"), name="annotated")
# shared design system, served by both apps from one place so they cannot drift
app.mount("/shared", StaticFiles(directory=HERE.parent / "shared"), name="shared")
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8799)
