"""TrafficLens Lab — API + UI server (port 8800).

The Lab is the workbench: it takes raw footage all the way to a fine-tuned model
and shows every step, every judgment and every cent on the way.
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

import artifacts
import db
import errors as errors_mod
import goldset
import organise
import stations as stations_mod
import judge as judge_mod
import pipeline
import providers
import train
import trainings

ROOT = Path(__file__).parent.parent
STATIC = Path(__file__).parent / "static"
# The counting, reporting and render modules are the survey app's — imported, not copied,
# so the Lab's diagnosis and the client's deliverable can never disagree.
sys.path.insert(0, str(ROOT / "app"))
VIDEO_DIRS = [ROOT / "video", ROOT / "app_videos", ROOT / "benchmark" / "segments"]

app = FastAPI(title="TrafficLens Lab")
_bg = {}


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    # .css was missing here, so edits to the shared stylesheet did not reach the
    # browser -- the page loaded new markup against an old stylesheet and looked broken.
    if request.url.path.startswith("/api") or request.url.path.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.on_event("startup")
def boot():
    artifacts.init()
    stations_mod.init()
    goldset.init()
    db.init()
    db.seed_keys_from_disk()
    for s in db.rows("SELECT * FROM lab_stages WHERE status IN ('running','queued')"):
        db.run("UPDATE lab_stages SET status='error', message='interrupted by Lab restart' "
               "WHERE id=?", s["id"])
    db.run("UPDATE lab_runs SET status='idle' WHERE status='running'")
    train.resume_monitors()


# ────────────────────────────── dashboard ──────────────────────────────
@app.get("/api/state")
def state():
    runs = db.rows("SELECT * FROM lab_runs ORDER BY id DESC LIMIT 12")
    for r in runs:
        r["config"] = db.jload(r["config"], {})
        r["stages"] = db.rows("SELECT stage,status,progress,message,cost_usd "
                              "FROM lab_stages WHERE run_id=? ORDER BY id", r["id"])
    spend = db.rows("""SELECT provider, ROUND(SUM(usd),4) usd, COUNT(*) n
                       FROM lab_costs GROUP BY provider""")
    today = db.one("SELECT ROUND(COALESCE(SUM(usd),0),4) usd FROM lab_costs "
                   "WHERE ts > ?", time.time() - 86400)
    pods = db.rows("SELECT * FROM lab_pods WHERE terminated IS NULL")
    for p in pods:
        p["telemetry"] = db.jload(p["telemetry"], {})
    # The rate lives in settings, not in the page: a figure the reader cannot check or
    # change is a figure they cannot trust, and the rupee/dollar rate moves.
    return {
        "fx": {"rate": float(db.get_setting("inr_per_usd", "88") or 88), "base": "USD"},
        "openrouter": providers.or_balance(),
        "runpod": providers.rp_balance(),
        "runs": runs,
        "spend_by_provider": spend,
        "spend_24h": today["usd"] if today else 0,
        "spend_total": round(sum(s["usd"] or 0 for s in spend), 4),
        "live_pods": pods,
        "judges": judge_mod.judges_for(),
        "events": db.rows("SELECT * FROM lab_events ORDER BY id DESC LIMIT 20"),
    }


@app.get("/api/videos-on-disk")
def videos_on_disk():
    """Every video file in the scan roots, and which station owns it.

    The station is the point of this list. Work happens inside a station, so a file that
    belongs to one needs a way there, and a file that belongs to none is the actual
    finding — it is invisible to every station total until somebody attaches it.
    """
    owner = {r["path"]: {"site_id": r["site_id"], "code": r["code"], "name": r["name"]}
             for r in db.rows("""SELECT f.path, s.id site_id, s.code, s.name
                                 FROM lab_footage f JOIN sites s ON s.id = f.site_id""")}
    out = []
    for d in VIDEO_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.mp4")):
            st = p.stat()
            out.append({"path": str(p), "name": p.name, "dir": d.name,
                        "size_mb": round(st.st_size / 1e6, 1), "mtime": st.st_mtime,
                        "station": owner.get(str(p)),
                        "used": bool(db.one("SELECT id FROM lab_runs WHERE source_path=?", str(p)))})
    out.sort(key=lambda x: -x["mtime"])
    return out


# ──────────────────────────────── runs ────────────────────────────────
class RunIn(BaseModel):
    source_path: str
    name: str | None = None
    site_id: int | None = None
    config: dict = {}


@app.post("/api/runs")
def create_run(r: RunIn):
    src = Path(r.source_path)
    if not src.exists():
        raise HTTPException(400, f"file not found: {src}")
    cfg = {"segment_minutes": 15, "compress_crf": 30, "compress_width": 1280,
           "compress_target_ratio": 0.5, "extract_segments": [0],
           "imgsz": 960, "conf": 0.12, "sample_n": 300, **(r.config or {})}
    # A run started without an explicit station still gets one where the footage is
    # already matched to a camera -- otherwise the dataset it produces is unattributable.
    site_id = r.site_id
    if site_id is None:
        known = db.one("SELECT site_id FROM lab_footage WHERE path=?", str(src))
        site_id = known["site_id"] if known else None
    rid = db.run("INSERT INTO lab_runs (name,source_path,status,config,created,site_id) "
                 "VALUES (?,?,'draft',?,?,?)",
                 r.name or src.stem, str(src), db.jdump(cfg), time.time(), site_id)
    for s in pipeline.STAGES:
        db.run("INSERT INTO lab_stages (run_id,stage,status) VALUES (?,?,'pending')", rid, s)
    db.log(rid, "created", f"run {rid}", src.name)
    return {"id": rid}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    r = db.one("SELECT * FROM lab_runs WHERE id=?", run_id)
    if not r:
        raise HTTPException(404, "no such run")
    r["config"] = db.jload(r["config"], {})
    r["stages"] = db.rows("SELECT * FROM lab_stages WHERE run_id=? ORDER BY id", run_id)
    for s in r["stages"]:
        s["meta"] = db.jload(s["meta"], {})
    r["segments"] = db.rows("SELECT * FROM lab_segments WHERE run_id=? ORDER BY idx", run_id)
    r["crops"] = db.one("""SELECT COUNT(*) total,
          SUM(state='agreed') agreed, SUM(state='reclass') reclass,
          SUM(state='contested') contested, SUM(state='new') pending,
          SUM(human_class IS NOT NULL) human
        FROM lab_crops WHERE run_id=?""", run_id)
    r["costs"] = db.rows("""SELECT stage, provider, ROUND(SUM(usd),5) usd, COUNT(*) n
                            FROM lab_costs WHERE run_id=? GROUP BY stage,provider""", run_id)
    r["cost_total"] = round(sum(c["usd"] or 0 for c in r["costs"]), 5)
    r["pods"] = db.rows("SELECT * FROM lab_pods WHERE run_id=?", run_id)
    r["events"] = db.rows("SELECT * FROM lab_events WHERE run_id=? ORDER BY id DESC LIMIT 40",
                          run_id)
    r["class_dist"] = db.rows("""SELECT det_class, COUNT(*) n FROM lab_crops
                                 WHERE run_id=? GROUP BY det_class ORDER BY n DESC""", run_id)
    return r


class StartIn(BaseModel):
    stages: list[str]


@app.post("/api/runs/{run_id}/start")
def start_run(run_id: int, s: StartIn):
    if not db.one("SELECT id FROM lab_runs WHERE id=?", run_id):
        raise HTTPException(404, "no such run")
    ok, msg = pipeline.start(run_id, s.stages)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@app.get("/api/runs/{run_id}/stream")
def stream(run_id: int):
    """Live stage progress.

    This is a SYNCHRONOUS generator, so every open stream holds one of uvicorn's
    threadpool slots. Two rules keep that from wedging the whole app:

      1. End as soon as no stage is queued or running — judged on the STAGES, not on
         `lab_runs.status`. A run left in 'draft' never matched the old status test, so
         its stream looped forever and leaked a thread on every visit. Once the pool
         filled, every other request blocked and the UI looked frozen.
      2. Stop after MAX_STREAM_S regardless. The client reconnects if it still cares;
         an abandoned stream must not outlive the page that opened it.
    """
    MAX_STREAM_S = 300
    started = time.time()

    def gen():
        while True:
            st = db.rows("SELECT stage,status,progress,message,cost_usd "
                         "FROM lab_stages WHERE run_id=? ORDER BY id", run_id)
            run = db.one("SELECT status FROM lab_runs WHERE id=?", run_id)
            busy = any(s["status"] in ("running", "queued") for s in st)
            yield f"data: {json.dumps({'stages': st, 'status': run['status'] if run else '?'})}\n\n"
            if not busy or time.time() - started > MAX_STREAM_S:
                yield "data: {\"end\":1}\n\n"
                return
            time.sleep(1.5)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# ─────────────────────────────── judging ───────────────────────────────
@app.get("/api/models")
def models(vision_only: bool = True, limit: int = 60):
    ms = providers.or_models()
    if vision_only:
        ms = [m for m in ms if m["vision"]]
    return {"models": ms[:limit], "selected": judge_mod.judges_for(),
            "recommended": judge_mod.DEFAULT_JUDGES}


class JudgesIn(BaseModel):
    models: list[str]


@app.post("/api/judges")
def set_judges(j: JudgesIn):
    if not 1 <= len(j.models) <= 5:
        raise HTTPException(400, "pick between 1 and 5 judges")
    db.set_setting("judge_models", json.dumps(j.models))
    return {"ok": True, "models": j.models}


class JudgeIn(BaseModel):
    limit: int | None = None


@app.post("/api/runs/{run_id}/judge")
def start_judge(run_id: int, j: JudgeIn):
    if _bg.get(f"judge{run_id}") and _bg[f"judge{run_id}"].is_alive():
        raise HTTPException(409, "judging already running for this run")

    def work():
        db.run("UPDATE lab_runs SET status='running' WHERE id=?", run_id)
        try:
            judge_mod.run_judge(run_id, limit=j.limit)
            db.run("UPDATE lab_runs SET status='done' WHERE id=?", run_id)
        except Exception as e:
            pipeline.stage_fail(run_id, "judge", str(e))
            db.run("UPDATE lab_runs SET status='error' WHERE id=?", run_id)

    t = threading.Thread(target=work, daemon=True)
    _bg[f"judge{run_id}"] = t
    t.start()
    return {"ok": True}


class BakeIn(BaseModel):
    models: list[str]
    n: int = 40


@app.post("/api/bakeoff")
def start_bakeoff(b: BakeIn):
    if _bg.get("bakeoff") and _bg["bakeoff"].is_alive():
        raise HTTPException(409, "a bake-off is already running")
    db.set_setting("bakeoff_status", "running")

    def work():
        try:
            res = judge_mod.bakeoff(b.models, n=b.n)
            db.set_setting("bakeoff_status", "done")
            db.set_setting("bakeoff_result", json.dumps(res))
        except Exception as e:
            db.set_setting("bakeoff_status", f"error: {e}")

    t = threading.Thread(target=work, daemon=True)
    _bg["bakeoff"] = t
    t.start()
    return {"ok": True}


@app.get("/api/bakeoff")
def get_bakeoff():
    return {"status": db.get_setting("bakeoff_status", "idle"),
            "result": db.jload(db.get_setting("bakeoff_result"), None),
            "evals": db.rows("SELECT * FROM lab_evals WHERE kind='judge_bakeoff' "
                             "ORDER BY id DESC LIMIT 24")}


# ─────────────────────────────── review ───────────────────────────────
@app.get("/api/review/{run_id}")
def review_next(run_id: int, state: str = "contested"):
    where = "state=?" if state != "any" else "1=1"
    args = ([state] if state != "any" else [])
    c = db.one(f"""SELECT * FROM lab_crops WHERE run_id=? AND {where}
                   AND human_class IS NULL ORDER BY id LIMIT 1""", run_id, *args)
    if not c:
        return {"done": True,
                "remaining": 0,
                "reviewed": db.one("SELECT COUNT(*) n FROM lab_crops WHERE run_id=? "
                                   "AND human_class IS NOT NULL", run_id)["n"]}
    c["judgments"] = db.rows("SELECT model,verdict,verdict_name,confidence,error "
                             "FROM lab_judgments WHERE crop_id=?", c["id"])
    c["det_name"] = pipeline.CLASSES[c["det_class"]] if c["det_class"] is not None else "?"
    rem = db.one(f"SELECT COUNT(*) n FROM lab_crops WHERE run_id=? AND {where} "
                 f"AND human_class IS NULL", run_id, *args)["n"]
    return {"done": False, "crop": c, "remaining": rem, "classes": pipeline.CLASSES}


class VerdictIn(BaseModel):
    crop_id: int
    class_id: int          # -1 = not a vehicle


@app.post("/api/review/verdict")
def review_verdict(v: VerdictIn):
    db.run("UPDATE lab_crops SET human_class=?, state='human' WHERE id=?",
           v.class_id, v.crop_id)
    return {"ok": True}


@app.get("/api/crop/{crop_id}")
def crop_img(crop_id: int, kind: str = "crop"):
    c = db.one("SELECT crop_path, ctx_path FROM lab_crops WHERE id=?", crop_id)
    if not c:
        raise HTTPException(404, "no such crop")
    p = c["ctx_path"] if kind == "ctx" else c["crop_path"]
    if not p or not Path(p).exists():
        raise HTTPException(404, "image missing")
    return FileResponse(p)


# ──────────────────────────────── pods ────────────────────────────────
@app.get("/api/pods")
def pods():
    live = providers.rp_pods()
    tracked = db.rows("SELECT * FROM lab_pods ORDER BY id DESC LIMIT 20")
    for t in tracked:
        t["telemetry"] = db.jload(t["telemetry"], {})
    return {"live": live, "tracked": tracked,
            "gpu_types": providers.rp_gpu_types()[:12],
            "balance": providers.rp_balance()}


class AdoptIn(BaseModel):
    pod_id: str
    run_id: int = 0


@app.post("/api/pods/adopt")
def adopt_pod(a: AdoptIn):
    rid, msg = train.adopt(a.run_id, a.pod_id)
    if rid is None:
        raise HTTPException(404, msg)
    return {"ok": True, "row": rid, "message": msg}


@app.post("/api/pods/{row_id}/stop")
def stop_pod(row_id: int):
    return train.stop(row_id)


# ────────────────────────── training reports ──────────────────────────
@app.get("/api/trainings")
def list_trainings():
    items = trainings.listing()
    for t in items:                       # refresh live rows from the mirrored csv
        if t["status"] == "running":
            wip = ROOT / "models" / "round4_wip"
            if (wip / "results.csv").exists() and t["tag"].startswith("round4"):
                trainings.ingest(t["id"], wip / "results.csv", wip / "args.yaml")
                if t["pod_id"]:
                    trainings.attach_cost(t["id"], t["pod_id"])
    return {"trainings": trainings.listing(), "classes": trainings.CLASSES}


@app.get("/api/trainings/{tid}")
def get_training(tid: int):
    t = trainings.get(tid)
    if not t:
        raise HTTPException(404, "no such training")
    return t


# ──────────────────────────── costs & settings ────────────────────────
@app.get("/api/costs")
def costs(run_id: int | None = None, limit: int = 200):
    q = "SELECT * FROM lab_costs"
    args = []
    if run_id:
        q += " WHERE run_id=?"
        args.append(run_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    items = db.rows(q, *args)
    by_stage = db.rows("SELECT stage, provider, ROUND(SUM(usd),5) usd, COUNT(*) n "
                       "FROM lab_costs GROUP BY stage,provider ORDER BY usd DESC")
    return {"items": items, "by_stage": by_stage,
            "total": round(sum(i["usd"] or 0 for i in items), 5)}


class SettingIn(BaseModel):
    key: str
    value: str


@app.get("/api/settings")
def get_settings():
    keys = ["judge_budget_usd", "idle_stop_minutes", "judge_models", "inr_per_usd"]
    out = {k: db.get_setting(k) for k in keys}
    ork, rpk = providers.or_key(), providers.rp_key()
    out["openrouter_key_set"] = bool(ork)
    out["runpod_key_set"] = bool(rpk)
    out["openrouter_key_tail"] = ork[-6:] if ork else ""
    out["runpod_key_tail"] = rpk[-6:] if rpk else ""
    return out


@app.post("/api/settings")
def set_setting(s: SettingIn):
    allowed = {"openrouter_key", "runpod_key", "judge_budget_usd", "inr_per_usd",
               "idle_stop_minutes", "judge_models"}
    if s.key not in allowed:
        raise HTTPException(400, f"unknown setting {s.key}")
    db.set_setting(s.key, s.value.strip())
    return {"ok": True}


# ══════════════════ pipeline graph, datasets, weight archive ══════════════════
class GraphIn(BaseModel):
    graph: dict


@app.get("/api/runs/{run_id}/output/{node}")
def node_output(run_id: int, node: str, limit: int = 60):
    """What a single node actually produced -- the point of clicking one.

    Each node answers in its own terms (segments list their parts, sample lists
    crops) because "show me the output" means something different at each step.
    """
    r = db.one("SELECT * FROM lab_runs WHERE id=?", run_id)
    if not r:
        raise HTTPException(404, "no such run")
    st = db.one("SELECT * FROM lab_stages WHERE run_id=? AND stage=?", run_id, node) or {}
    if st:
        st["meta"] = db.jload(st.get("meta"), {})
    out = {"node": node, "stage": st, "kind": "none", "rows": [], "files": []}

    if node in ("probe", "segment", "compress"):
        out["kind"] = "segments"
        out["rows"] = db.rows(
            "SELECT idx,name,dur_s,size_mb,compressed_mb,grade,quality,frames,status,video_id "
            "FROM lab_segments WHERE run_id=? ORDER BY idx", run_id)
    elif node == "extract":
        out["kind"] = "extract"
        out["rows"] = db.rows(
            "SELECT idx,name,frames,fps,width,height,video_id FROM lab_segments "
            "WHERE run_id=? AND video_id IS NOT NULL ORDER BY idx", run_id)
    elif node == "sample":
        out["kind"] = "crops"
        out["rows"] = db.rows(
            "SELECT id,det_class,det_conf,track_id,frame,state FROM lab_crops "
            "WHERE run_id=? ORDER BY id DESC LIMIT ?", run_id, limit)
        out["summary"] = db.one(
            "SELECT COUNT(*) total, SUM(state='new') pending FROM lab_crops WHERE run_id=?", run_id)
    elif node.startswith("judge") or node == "consensus":
        out["kind"] = "judgments"
        out["rows"] = db.rows(
            """SELECT model, COUNT(*) n, SUM(error IS NOT NULL) errors,
                      ROUND(SUM(cost_usd),5) usd, ROUND(AVG(latency_ms)) ms
               FROM lab_judgments WHERE run_id=? GROUP BY model""", run_id)
        out["summary"] = db.one(
            """SELECT COUNT(*) total, SUM(state='agreed') agreed, SUM(state='reclass') reclass,
                      SUM(state='contested') contested FROM lab_crops WHERE run_id=?""", run_id)
    elif node == "dataset":
        out["kind"] = "dataset"
        out["rows"] = [d for d in artifacts.datasets() if d["run_id"] == run_id]
    out["files"] = [{"name": p.name, "mb": round(p.stat().st_size / 1e6, 2)}
                    for p in artifacts.run_files(run_id, _NODE_DIR.get(node, "")) [:limit]] \
        if _NODE_DIR.get(node) else []
    return out


_NODE_DIR = {"segment": "segments", "compress": "compressed", "extract": "frames",
             "sample": "crops", "dataset": "dataset"}


@app.get("/api/model-registry")
def model_registry():
    """Detector versions available to the extract node, best first."""
    try:
        rows = db.rows("SELECT id,label,file,map50,recall,is_default,size_mb FROM models "
                       "ORDER BY is_default DESC, map50 DESC")
    except Exception:
        rows = []
    return {"models": [{**m, "map50": round(m["map50"] or 0, 3)} for m in rows]}


# ════════════════════════════════ stations ════════════════════════════════
class AssignIn(BaseModel):
    paths: list[str]
    site_id: int


class ClockIn(BaseModel):
    path: str
    start_clock: str


class LinesIn(BaseModel):
    lines: list[dict]


class StationIn(BaseModel):
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
    bearing: float | None = None       # compass degrees the camera looks along
    geo_source: str | None = None      # gps | manual | map — how the location was fixed


@app.post("/api/stations")
def create_station(s: StationIn):
    """Create a count station. `sites` is shared with the survey app, so one made here
    is immediately the same station there — that is the point of sharing the table."""
    import sites
    try:
        sid = sites.create(**s.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log(None, "created", "station " + s.name, f"site {sid}")
    return {"id": sid}


@app.patch("/api/stations/{site_id}")
def update_station(site_id: int, s: StationIn):
    import sites
    if not db.one("SELECT id FROM sites WHERE id=?", site_id):
        raise HTTPException(404, "no such station")
    sites.update(site_id, **{k: v for k, v in s.model_dump().items() if v is not None})
    return {"ok": True}


@app.get("/api/stations")
def list_stations():
    return {"stations": stations_mod.stations()}


@app.get("/api/stations/{site_id}/line")
def get_station_line(site_id: int):
    """The station's default line, plus a video to draw it on."""
    v = db.one("""SELECT id, name, width, height FROM videos
                  WHERE site_id=? AND COALESCE(excluded,0)=0 ORDER BY id LIMIT 1""", site_id)
    users = db.rows("""SELECT v.id, v.name,
                         (SELECT COUNT(*) FROM scenes s WHERE s.video_id=v.id) own
                       FROM videos v WHERE v.site_id=? AND COALESCE(v.excluded,0)=0""", site_id)
    return {"lines": stations_mod.default_line(site_id),
            "draw_on": v,
            "videos": [{**u, "source": "video" if u["own"] else "station"} for u in users]}


@app.put("/api/stations/{site_id}/line")
def put_station_line(site_id: int, inp: LinesIn):
    if not db.one("SELECT id FROM sites WHERE id=?", site_id):
        raise HTTPException(404, "no such station")
    return stations_mod.set_default_line(site_id, inp.lines)


@app.get("/api/stations/{site_id}/frame")
def station_frame(site_id: int, at: int = 300, footage_id: int = 0):
    """A FULL-RESOLUTION frame from this station's own footage, to draw the count line on.

    Not the thumbnail: that is downscaled to 480px, and a line drawn on it would store
    coordinates in the wrong space. The line has to be placeable before anything is
    segmented or extracted — a station arrives as footage and nothing else, and until
    now there was literally no frame to draw on until a clip existed.

    A clip is a stream copy of its footage, so the two share dimensions and a line drawn
    here means the same thing on every clip cut from it.
    """
    from fastapi.responses import Response
    import cv2
    q = "SELECT path, name FROM lab_footage WHERE site_id=? AND dup_of IS NULL"
    args = [site_id]
    if footage_id:
        q += " AND id=?"
        args.append(footage_id)
    q += " AND (missing IS NULL OR missing=0) ORDER BY start_clock LIMIT 1"
    row = db.one(q, *args)
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "no footage on disk for this station")
    cap = cv2.VideoCapture(row["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, at))
    ok, img = cap.read()
    if not ok:                              # past the end, or a stubborn codec
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        raise HTTPException(404, f"could not decode a frame from {row['name']}")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Response(content=buf.tobytes(), media_type="image/jpeg",
                    headers={"X-Frame-Source": row["name"]})


@app.post("/api/stations/{site_id}/line-check")
def station_line_check(site_id: int, inp: LinesIn | None = None):
    """Replay this station's stored trajectories against a line and say what it would do.

    Where the line sits decides every number in the report, and the mistake is invisible
    on a still frame — on KDP a line in the corner counted 1 vehicle out of 196 tracks,
    and the same clip counted 60 once the line crossed the carriageway. This is a query
    over track_points, so it can run every time the line moves.
    """
    return stations_mod.line_check(site_id, inp.lines if inp else None)


@app.get("/api/stations/{site_id}/thumb")
def station_thumb(site_id: int):
    p = stations_mod.thumbnail(site_id)
    if not p or not Path(p).exists():
        raise HTTPException(404, "no footage to take a thumbnail from")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/stations/{site_id}")
def get_station(site_id: int):
    s = stations_mod.station(site_id)
    if not s:
        raise HTTPException(404, "no such station")
    # `suggested` (the old "Where to extract next" panel) is gone from the Overview: it
    # reshuffled whole files rather than proposing windows, so it never answered the
    # question it asked. stations.suggest_sample() is left in place for whatever
    # replaces it; nothing reads it today.
    return s


@app.get("/api/stations/{site_id}/clips")
def station_clips(site_id: int):
    """The station as its clips — the unit people actually work in.

    Runs come back too, but as provenance. Leading with them was the confusion: seven
    runs with test names, one of them dead, and no way to see which quarter-hour of road
    each represented.
    """
    import clips
    return {"summary": clips.summary(site_id), "clips": clips.clips(site_id),
            "runs": clips.runs(site_id)}


# ─────────────────── clip-level verification ───────────────────
@app.get("/api/stations/{site_id}/gold")
def station_gold(site_id: int):
    """The gold set built from clip verification — the labels a person actually made."""
    import clips
    return clips.gold_from_verdicts(site_id)


@app.get("/api/annotated/{video_id}")
def lab_annotated(video_id: int, dl: int = 0):
    """Serve the annotated preview — inline for the player, as a file when downloading."""
    p = organise.render_path(video_id)
    if not p or not Path(p).exists():
        raise HTTPException(404, "not rendered yet")
    return FileResponse(p, media_type="video/mp4",
                        filename=Path(p).name if dl else None)


@app.get("/api/verify/{video_id}")
def verify_queue(video_id: int, cls: str = "", mandatory: int = 0, answered: str = ""):
    """answered: '' every vehicle · '1' only what you have ruled on · '0' only what is left."""
    import verify
    return verify.queue(video_id, only_class=cls or None,
                        mandatory_only=bool(mandatory),
                        answered=None if answered == "" else answered == "1")


@app.get("/api/clip/{video_id}")
def clip_detail(video_id: int):
    """Everything one clip is and everything you can do to it, in one request.

    The clips grid could only ever show a card per clip with five buttons on it, three
    of which dead-ended. A clip is a place you work in, so it needs the state that
    decides which action is next: does it have a line, has a detector run and which one,
    how much has a person confirmed.
    """
    import clips as clips_mod
    v = db.one("""SELECT v.*, s.code station_code, s.name station_name
                  FROM videos v LEFT JOIN sites s ON s.id=v.site_id WHERE v.id=?""", video_id)
    if not v:
        raise HTTPException(404, "no such clip")
    v["quality"] = db.jload(v.get("quality"), {})
    lines, line_source = stations_mod.lines_for(video_id)
    seg = db.one("SELECT run_id, idx, path, start_s FROM lab_segments WHERE path=?", v["path"])
    source = None
    if seg:
        r = db.one("SELECT source_path FROM lab_runs WHERE id=?", seg["run_id"])
        if r:
            source = {"path": r["source_path"], "name": Path(r["source_path"]).name,
                      "part": seg["idx"], "offset_s": seg["start_s"]}
    tracks = db.one("""SELECT COUNT(*) n, COUNT(DISTINCT model_id) models,
                              MIN(model_id) model FROM tracks WHERE video_id=?""", video_id)
    # `started` comes back so the UI can show TIME LEFT. "3.76x realtime" is a fact about
    # the machine, not an answer to "when is this done" — the reader has to know the clip
    # length and do the arithmetic to get anything useful out of it.
    job = db.one("""SELECT id,kind,status,progress,message,started,finished FROM jobs
                    WHERE video_id=? ORDER BY id DESC LIMIT 1""", video_id)
    if job and job["status"] in ("running", "queued") and (job["progress"] or 0) > 1:
        elapsed = time.time() - (job["started"] or time.time())
        job["eta_s"] = max(0, round(elapsed * (100 - job["progress"]) / job["progress"]))
    elif job:
        job["eta_s"] = None
    verified = db.one("""SELECT COUNT(*) n, SUM(kind='class' AND was<>answer) changed,
                                SUM(kind='reject') rejected, SUM(kind='attribute') attrs
                         FROM clip_verdicts WHERE video_id=?""", video_id) or {}
    counted = None
    if lines and (tracks or {}).get("n"):
        try:
            import counting
            counted = counting.count_video(video_id, lines)["total"]
        except Exception:
            counted = None
    dur_min = round((v["frames"] or 0) / max(v["fps"] or 1, 1) / 60, 1)
    return {
        "clip": {**{k: v[k] for k in ("id", "name", "path", "fps", "frames", "width",
                                      "height", "start_clock", "site_id", "station_code",
                                      "station_name")},
                 "minutes": dur_min, "quality": v["quality"]},
        "source": source,
        "line": {"drawn": bool(lines), "n": len(lines), "source": line_source},
        "extract": {"tracks": (tracks or {}).get("n") or 0,
                    "model": (tracks or {}).get("model"),
                    "mixed_models": ((tracks or {}).get("models") or 0) > 1},
        "counted": counted,
        "verified": {"n": verified.get("n") or 0, "changed": verified.get("changed") or 0,
                     "rejected": verified.get("rejected") or 0,
                     "attrs": verified.get("attrs") or 0},
        "job": job,
        "models": model_registry()["models"],
        "next": ("draw the count line" if not lines else
                 "extract detections" if not (tracks or {}).get("n") else
                 "verify the counted vehicles" if not (verified.get("n") or 0) else
                 "review or report"),
    }


@app.get("/api/verify/{video_id}/{track_id}/{kind}.jpg")
def verify_image(video_id: int, track_id: int, kind: str):
    import verify
    cp, xp = verify.crop(video_id, track_id)
    p = xp if kind == "ctx" else cp
    if not p or not Path(p).is_file():
        raise HTTPException(404, "no image")
    return FileResponse(p, media_type="image/jpeg")


class VerifyIn(BaseModel):
    track_id: int
    answer: str


@app.post("/api/verify/{video_id}")
def verify_verdict(video_id: int, body: VerifyIn):
    import verify
    try:
        return verify.verdict(video_id, body.track_id, body.answer)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/geocode")
def lab_geocode(q: str = ""):
    """Find a place by name so a station is located by typing, as in the survey app."""
    import geocode
    return {"results": geocode.search(q, country="in")}


@app.get("/api/browse")
def browse_dirs(path: str = ""):
    """Walk the local filesystem one level at a time, for the folder picker.

    A browser cannot hand the server a native folder path — the OS dialog's paths are
    hidden from pages for security — so picking happens server-side: list, descend, pick.
    Only directories are shown, with a count of the videos each holds, because that count
    is how you recognise the right folder at a glance.
    """
    d = Path(path).expanduser() if path else Path("/Volumes")
    if not d.is_dir():
        raise HTTPException(400, f"not a folder: {d}")
    subs = []
    for x in sorted(d.iterdir()):
        if x.name.startswith(".") or not x.is_dir():
            continue
        try:
            n = len(list(x.glob("*.mp4"))) + len(list(x.glob("*.avi"))) + len(list(x.glob("*.mkv")))
        except PermissionError:
            n = -1
        subs.append({"name": x.name, "path": str(x), "videos": n})
    here = len(list(d.glob("*.mp4"))) + len(list(d.glob("*.avi"))) + len(list(d.glob("*.mkv")))
    return {"path": str(d), "parent": str(d.parent) if d != d.parent else None,
            "videos_here": here, "dirs": subs}


class FolderIn(BaseModel):
    path: str


@app.post("/api/stations/{site_id}/folder-preview")
def folder_preview(site_id: int, body: FolderIn):
    """List what a folder holds BEFORE anything is written — the 'Fetch folder' panel.

    Attaching a survey to the wrong directory should fail at a glance, not after a scan
    has filed a hundred files under the wrong station.
    """
    d = Path(body.path).expanduser()
    if not d.is_dir():
        raise HTTPException(400, f"not a folder: {d}")
    vids = sorted(d.glob("*.mp4")) + sorted(d.glob("*.avi")) + sorted(d.glob("*.mkv"))
    known = {r["path"] for r in db.rows("SELECT path FROM lab_footage")}
    return {"path": str(d), "files": [
        {"name": v.name, "size_mb": round(v.stat().st_size / 1e6, 1),
         "clock_guess": str(pipeline.clock_from_name(v.name) or ""),
         "already_known": str(v) in known}
        for v in vids]}


@app.post("/api/stations/{site_id}/folder")
def attach_folder(site_id: int, body: FolderIn):
    """Scan the folder (probe timings, camera, duplicates) and file it under this station."""
    d = Path(body.path).expanduser()
    if not d.is_dir():
        raise HTTPException(400, f"not a folder: {d}")
    r = stations_mod.scan(roots=[str(d)])
    n = db.one("""SELECT COUNT(*) c FROM lab_footage
                  WHERE path LIKE ? AND site_id IS NULL""", str(d) + "%")["c"]
    db.run("UPDATE lab_footage SET site_id=?, site_confirmed=1 "
           "WHERE path LIKE ? AND site_id IS NULL", site_id, str(d) + "%")
    try:
        db.run("ALTER TABLE sites ADD COLUMN footage_dir TEXT")
    except Exception:
        pass
    db.run("UPDATE sites SET footage_dir=? WHERE id=?", str(d), site_id)
    db.log(None, "folder", f"station {site_id}", f"{n} file(s) from {d}")
    return {"attached": n, "scan": r}


@app.post("/api/stations/{site_id}/process")
def process_station(site_id: int):
    """Reconcile the station with its folder and report every difference found."""
    if not db.one("SELECT id FROM sites WHERE id=?", site_id):
        raise HTTPException(404, "no such station")
    a = stations_mod.reconcile(site_id)
    if not a["totals"]["files"] and not a["unattached"]:
        raise HTTPException(400, "no footage attached yet — pick the station's folder first")
    try:
        stations_mod.thumbnail(site_id)
    except Exception:
        pass
    return _with_next(site_id, a)


@app.post("/api/footage/{footage_id}/detach")
def detach_footage(footage_id: int):
    """Take a file off a station without deleting anything.

    The other half of attaching, and it was missing: a file filed under the wrong
    station could only ever be re-filed under a different one, never simply removed.
    The row and the file both stay — only the station link goes — so a mistaken detach
    costs one click to undo from the unattached list.
    """
    f = db.one("SELECT name, site_id FROM lab_footage WHERE id=?", footage_id)
    if not f:
        raise HTTPException(404, "no such footage")
    if f["site_id"] is None:
        return {"ok": True, "already": True}
    db.run("UPDATE lab_footage SET site_id=NULL, site_confirmed=0 WHERE id=?", footage_id)
    db.log(None, "detached", f["name"], f"from station {f['site_id']}")
    return {"ok": True, "name": f["name"]}


@app.get("/api/stations/{site_id}/audit")
def station_audit(site_id: int):
    """What the station holds right now, read-only — the Overview page's one source.

    Deliberately separate from /process: looking must never write. The page can call
    this as often as it likes, and pressing Process is the only thing that edits a row.
    """
    if not db.one("SELECT id FROM sites WHERE id=?", site_id):
        raise HTTPException(404, "no such station")
    return _with_next(site_id, stations_mod.audit(site_id))


def _with_next(site_id, a):
    """Attach the single next action, computed from what the audit actually found.

    Ordered by what blocks what: footage you cannot find beats a clock you cannot read,
    which beats a line you have not drawn — each one makes the next meaningless.
    """
    line = bool((db.one("SELECT default_line FROM sites WHERE id=?", site_id) or {})
                .get("default_line"))
    clips = db.one("SELECT COUNT(*) n FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0",
                   site_id)["n"]
    a["line_drawn"] = line
    a["clips"] = clips
    # What the Lab has produced from this station, as opposed to what the drive holds.
    # The distinction is the whole point of the folder card: source is read in place,
    # everything below is ours and can be rebuilt.
    # Excluded clips are kept for comparison and must not count as work done —
    # the same filter clips.py and stations.py use.
    vids = [v["id"] for v in db.rows(
        "SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id)]
    built = {"clips": clips, "extracted": 0, "verified": 0, "tracks": 0}
    if vids:
        q = ",".join("?" * len(vids))
        built["extracted"] = db.one(
            f"SELECT COUNT(DISTINCT video_id) n FROM tracks WHERE video_id IN ({q})", *vids)["n"]
        built["tracks"] = db.one(
            f"SELECT COUNT(*) n FROM tracks WHERE video_id IN ({q})", *vids)["n"]
        built["verified"] = db.one(
            f"SELECT COUNT(*) n FROM clip_verdicts WHERE video_id IN ({q})", *vids)["n"]
    a["built"] = built
    a["next"] = (
        "pick the folder this station's footage lives in" if not a["folder"] else
        f"wait for {len(a['incomplete'])} file(s) to finish copying, then Process again"
        if a.get("incomplete") else
        f"restore or re-attach {len(a['missing'])} missing file(s)" if a["missing"] else
        f"attach {len(a['unattached'])} new file(s) sitting in the folder" if a["unattached"] else
        f"set the clock on {len(a['undated'])} file(s) — a guessed clock puts vehicles "
        f"in the wrong 15-minute bin" if a["undated"] else
        f"check {len(a['foreign'])} file(s) that may belong to another station" if a["foreign"] else
        # Segment, then EXTRACT ONE CLIP, then draw the line. Extraction does not depend
        # on the line -- the detector and tracker store trajectories for the whole frame,
        # and counting is a query over them: measured on KDP, 17-20 ms to recount after
        # moving the line, against ~45 minutes to extract. So extracting first costs
        # nothing extra and buys the evidence the line needs: with tracks stored, the
        # editor can say "59 vehicles would cross this" instead of leaving you to judge
        # a still frame. Drawing first means guessing, then discovering it was wrong.
        "segment footage into clips" if not clips else
        "extract one clip — then the line can be placed against real vehicle paths"
        if not built["extracted"] else
        "draw the count line — the editor will show what it would count" if not line else
        "verify the counted vehicles")
    return a


class FootagePatch(BaseModel):
    start_clock: str | None = None
    camera: str | None = None


@app.patch("/api/footage/{footage_id}")
def edit_footage(footage_id: int, body: FootagePatch):
    """Correct what probing got wrong. A wrong clock corrupts every bin downstream."""
    if not db.one("SELECT id FROM lab_footage WHERE id=?", footage_id):
        raise HTTPException(404, "no such footage")
    if body.start_clock:
        try:
            time.strptime(body.start_clock, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(400, "use YYYY-MM-DD HH:MM:SS")
        db.run("UPDATE lab_footage SET start_clock=?, clock_source='manual' WHERE id=?",
               body.start_clock, footage_id)
    if body.camera:
        db.run("UPDATE lab_footage SET camera=? WHERE id=?", body.camera, footage_id)
    return {"ok": True}


class ExtractIn(BaseModel):
    force: bool = False        # re-extract a clip that has already been verified
    model_id: str | None = None   # which detector; None = the registry's default


@app.post("/api/clips/{video_id}/extract")
def extract_clip(video_id: int, body: ExtractIn | None = None):
    """Extract ONE clip — the per-clip expensive step, chosen per clip on purpose.

    Two things this must not do, both of which it used to:

      * **Extract anything else.** `extract_segments` accumulated, so asking for clip 4
        re-ran clips 1-3 as well. Extraction is ~3.4x real time, so extracting eight
        clips one at a time cost thirty-six passes instead of eight.
      * **Silently discard human work.** `engine.extract` deletes this video's tracks
        before re-detecting, which drops every `class_override` outright and leaves each
        `clip_verdicts` row pointing at a track id the tracker has since reused for a
        different vehicle — while the verification panel keeps scoring against them and
        reporting a model accuracy that no longer means anything.

    So a clip that has been verified refuses to re-extract unless the caller says so,
    and saying so clears the verdicts rather than leaving them to lie about a vehicle
    that is no longer there.
    """
    v = db.one("SELECT path FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such clip")
    seg = db.one("SELECT run_id, idx FROM lab_segments WHERE path=?", v["path"])
    if not seg:
        raise HTTPException(400, "this clip has no segment record")

    force = bool(body and body.force)
    n_verdicts = db.one("SELECT COUNT(*) n FROM clip_verdicts WHERE video_id=?",
                        video_id)["n"]
    n_attrs = db.one("SELECT COUNT(*) n FROM track_attrs WHERE video_id=?",
                     video_id)["n"]
    if (n_verdicts or n_attrs) and not force:
        raise HTTPException(409,
            f"This clip already carries {n_verdicts} verdict(s) and {n_attrs} "
            f"attribute(s). Re-extracting renumbers every track, so they would describe "
            f"different vehicles. Extract again with force to discard that verification.")
    if force and (n_verdicts or n_attrs):
        db.run("DELETE FROM clip_verdicts WHERE video_id=?", video_id)
        db.run("DELETE FROM track_attrs WHERE video_id=?", video_id)
        db.log(None, "discarded", f"verification for clip {video_id}",
               f"{n_verdicts} verdict(s) and {n_attrs} attribute(s) — re-extraction "
               f"renumbers tracks, so keeping them would corrupt the accuracy figure")

    cfgrow = db.one("SELECT config FROM lab_runs WHERE id=?", seg["run_id"])
    cfg = db.jload(cfgrow["config"], {})
    # Exactly the clip that was asked for. Which clips a run has produced is already
    # recorded on `lab_segments` (video_id + status), so nothing needs this to accumulate.
    cfg["extract_segments"] = [seg["idx"]]
    if body and body.model_id:
        cfg["model_id"] = body.model_id
    db.run("UPDATE lab_runs SET config=? WHERE id=?", db.jdump(cfg), seg["run_id"])
    ok, msg = pipeline.start(seg["run_id"], ["extract"])
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "run_id": seg["run_id"], "part": seg["idx"],
            "discarded_verdicts": n_verdicts if force else 0}


@app.get("/api/stations/{site_id}/footage-tree")
def station_footage_tree(site_id: int):
    """Footage files, the clips cut from each, and what has been labelled on every clip."""
    import clips
    return {"summary": clips.summary(site_id), "footage": clips.footage_tree(site_id),
            "raw_dataset": clips.raw_dataset(site_id), "runs": clips.runs(site_id),
            "class_mix": clips.class_mix(site_id), "spend": clips.spend(site_id),
            "active": clips.active(site_id)}


class SegmentIn(BaseModel):
    minutes: int = 15


@app.post("/api/stations/{site_id}/footage/{footage_id}/segment")
def segment_footage(site_id: int, footage_id: int, body: SegmentIn):
    """Cut one footage file into clips. Cheap: a stream copy, no re-encode, no GPU.

    Deliberately separate from extraction. Segmenting a 50-minute file takes seconds and
    tells you what you have; extracting it costs ~5 minutes of GPU per clip. Making them
    one action means paying for detections on footage nobody has looked at yet.
    """
    f = db.one("SELECT path, name FROM lab_footage WHERE id=? AND site_id=?",
               footage_id, site_id)
    if not f:
        raise HTTPException(404, "no such footage at this station")
    # "Already segmented" must mean clips exist, not that a run row exists: a draft
    # created by the old UI and never run would otherwise block this file forever.
    prior = db.one("""SELECT r.id, COUNT(s.id) segs FROM lab_runs r
                      LEFT JOIN lab_segments s ON s.run_id=r.id
                      WHERE r.source_path=? AND r.site_id=? GROUP BY r.id
                      ORDER BY segs DESC LIMIT 1""", f["path"], site_id)
    if prior and prior["segs"]:
        raise HTTPException(409, "this file has already been segmented")
    if prior:
        rid = prior["id"]                      # reuse the empty draft
    else:
        rid = db.run("""INSERT INTO lab_runs (name,source_path,status,config,created,site_id)
                        VALUES (?,?,'draft',?,?,?)""",
                     Path(f["name"]).stem, f["path"],
                     db.jdump({"segment_minutes": body.minutes, "extract_segments": [],
                               "imgsz": 960, "conf": 0.12, "sample_n": 0}),
                     time.time(), site_id)
        for st in pipeline.STAGES:
            db.run("INSERT INTO lab_stages (run_id,stage,status) VALUES (?,?,'pending')", rid, st)
    n = pipeline.segment(rid)
    # Register each cut as a clip NOW, not at extraction. The clips tree reads `videos`,
    # and the entire point of segment-first is seeing what a file contains before paying
    # for extraction — clips that only exist after extracting defeat that.
    base_clock = pipeline.clock_from_name(f["name"])
    made = []
    for seg in db.rows("SELECT * FROM lab_segments WHERE run_id=? ORDER BY idx", rid):
        made.append(pipeline._register_video(dict(seg), base_clock))
    return {"run_id": rid, "clips": n, "video_ids": made}


@app.delete("/api/runs/{run_id}")
def remove_run(run_id: int):
    import clips
    r = clips.delete_run(run_id)
    if not r["deleted"]:
        raise HTTPException(409, r["why"])
    return r


@app.post("/api/stations/scan")
def scan_footage():
    """Walk the footage roots. Safe to re-run -- known files are skipped."""
    return stations_mod.scan()


@app.get("/api/sessions")
def list_sessions():
    """Footage grouped by camera + contiguous days.

    No longer shown as its own table -- but kept, because this grouping is what stops a
    DVR channel number merging two unrelated surveys, and the Stations page still counts
    unattached files from it.
    """
    return {"sessions": stations_mod.sessions()}


class SessionIn(BaseModel):
    session: str
    site_id: int


@app.post("/api/sessions/assign")
def assign_session(s: SessionIn):
    return stations_mod.assign_session(s.session, s.site_id)


@app.get("/api/sessions/{session}/watermark")
def session_watermark(session: str):
    """The burned-in caption from the footage — the evidence for the assignment."""
    p = stations_mod.watermark(session)
    if not p or not Path(p).exists():
        raise HTTPException(404, "no watermark strip for this session")
    return FileResponse(p)


@app.post("/api/stations/assign")
def assign_footage(a: AssignIn):
    return stations_mod.assign(a.paths, a.site_id)


@app.post("/api/stations/clock")
def fix_clock(c: ClockIn):
    return stations_mod.set_clock(c.path, c.start_clock)


# ════════════════════════════════ gold set ════════════════════════════════
class GoldBuildIn(BaseModel):
    site_id: int
    n_frames: int = 60


class GoldSaveIn(BaseModel):
    frame_id: int
    boxes: list[dict]
    seconds: float | None = None
    revealed: bool = False


@app.get("/api/gold/{site_id}")
def gold_stats(site_id: int):
    return {"site_id": site_id, **goldset.stats(site_id)}


@app.get("/api/gold/{site_id}/next")
def gold_next(site_id: int):
    return goldset.next_frame(site_id)


@app.post("/api/gold/build")
def gold_build(g: GoldBuildIn):
    return goldset.build(g.site_id, g.n_frames)


@app.post("/api/gold/save")
def gold_save(g: GoldSaveIn):
    return goldset.save_frame(g.frame_id, g.boxes, g.seconds, g.revealed)


@app.get("/api/gold/{site_id}/score")
def gold_score(site_id: int, model_id: str | None = None):
    return goldset.score(site_id, model_id)


@app.get("/api/gold/frame/{frame_id}/image")
def gold_image(frame_id: int):
    f = db.one("SELECT image_path FROM lab_gold_frames WHERE id=?", frame_id)
    if not f or not Path(f["image_path"]).exists():
        raise HTTPException(404, "frame image missing")
    return FileResponse(f["image_path"])


# ═══════════════ counting layer: line, count, report card ═══════════════
# The counting logic is imported from the survey app rather than reimplemented. One
# implementation means the Lab's diagnosis and the deliverable can never disagree --
# and the Lab had no way to close its own loop without it: no line, no count, no report.
@app.get("/api/scene/{video_id}")
def lab_get_scene(video_id: int):
    v = db.one("SELECT id,name,width,height,fps,frames,start_clock,site_id FROM videos WHERE id=?",
               video_id)
    if not v:
        raise HTTPException(404, "no such video")
    lines, source = stations_mod.lines_for(video_id)
    return {"video": v, "lines": lines, "line_source": source}


@app.post("/api/scene/{video_id}")
def lab_save_scene(video_id: int, inp: LinesIn):
    db.run("INSERT OR REPLACE INTO scenes VALUES (?,?,?)",
           video_id, db.jdump(inp.lines), time.time())
    db.log(None, "line", f"video {video_id}", f"{len(inp.lines)} line(s) saved")
    return {"ok": True, "n_lines": len(inp.lines)}


@app.get("/api/frame/{video_id}/{frame_idx}")
def lab_frame(video_id: int, frame_idx: int):
    """A single decoded frame, for drawing a count line on."""
    from fastapi.responses import Response
    import cv2
    v = db.one("SELECT path FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such video")
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
    ok, img = cap.read()
    cap.release()
    if not ok:
        raise HTTPException(404, "could not decode that frame")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/count/{video_id}")
def lab_count(video_id: int):
    import counting
    lines = stations_mod.lines_for(video_id)[0]
    if not lines:
        raise HTTPException(400, "no count line for this video or its station")
    r = counting.count_video(video_id, lines)
    r["events"] = r["events"][-200:]
    return r


@app.get("/api/reportcard/{video_id}")
def lab_report_card(video_id: int):
    """Everything the report says, as data -- charts and values, not a spreadsheet.

    The xlsx is the deliverable for the client; this is the screen that tells you whether
    the deliverable is any good before you send it.
    """
    import report_card
    return report_card.build(video_id)


@app.get("/api/report-xlsx/{video_id}")
def lab_report_xlsx(video_id: int, taxonomy: str = ""):
    import reports
    lines = stations_mod.lines_for(video_id)[0]
    if not lines:
        raise HTTPException(400, "no count line for this video or its station")
    out = reports.export(video_id, lines, taxonomy or None)
    return FileResponse(out, filename=Path(out).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ─────────────────── fine-grained attributes ───────────────────
# Axle class, bus operator, auto size, car use: one mechanism, four questions. See
# app/attrspec.py for what they are and lab/attrlabel.py for how answers are collected.
@app.get("/api/attrs")
def lab_attrs():
    import attrlabel
    return {"attributes": attrlabel.stats()}


@app.get("/api/attrs/{attribute}/queue")
def lab_attr_queue(attribute: str, limit: int = 120, answered: int = 0,
                   video: int = 0):
    import attrlabel
    try:
        return {"items": attrlabel.queue(attribute, limit=limit,
                                         include_answered=bool(answered),
                                         video_id=video or None)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/attrs/sample/{sample_id}/{kind}.jpg")
def lab_attr_image(sample_id: int, kind: str):
    import attrlabel
    attrlabel.init()
    r = db.one("SELECT crop_path, ctx_path FROM lab_attr_samples WHERE id=?", sample_id)
    p = (r or {}).get("ctx_path" if kind == "ctx" else "crop_path")
    if not p or not Path(p).is_file():
        raise HTTPException(404, "no image for that sample")
    return FileResponse(p, media_type="image/jpeg")


class AttrAnswer(BaseModel):
    value: str


@app.post("/api/attrs/sample/{sample_id}")
def lab_attr_set(sample_id: int, body: AttrAnswer):
    import attrlabel
    try:
        return attrlabel.set_human(sample_id, body.value)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


# ─────────────────── axle audit ───────────────────
# The one classification the judge layer was built to skip. `judge.PROMPT` forbids
# counting axles and `to_class_id` resolves a Heavy_Truck verdict to the detector's own
# call, so every heavy label in the system is either an echo or the fallback constant.
# These endpoints back the pass that actually counts them.
@app.get("/api/axles/summary")
def lab_axles_summary(videos: str = ""):
    import axles
    vids = [int(x) for x in videos.split(",") if x.strip().isdigit()] or None
    return axles.matrix(vids)


@app.get("/api/axles/queue")
def lab_axles_queue():
    import axles
    return {"items": axles.queue()}


@app.get("/api/axles/resolved")
def lab_axles_resolved():
    import axles
    return {"items": axles.resolved()}


@app.get("/api/axles/{check_id}/{kind}.jpg")
def lab_axles_image(check_id: int, kind: str):
    import axles
    axles.init()
    r = db.one("SELECT crop_path, ctx_path FROM lab_axle_checks WHERE id=?", check_id)
    p = (r or {}).get("ctx_path" if kind == "ctx" else "crop_path")
    if not p or not Path(p).is_file():
        raise HTTPException(404, "no image for that check")
    return FileResponse(p, media_type="image/jpeg")


class AxleAnswer(BaseModel):
    answer: str


@app.post("/api/axles/{check_id}")
def lab_axles_set(check_id: int, body: AxleAnswer):
    import axles
    try:
        axles.set_human(check_id, body.answer)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ─────────────────── review the implied crossings ───────────────────
# A crossing nobody witnessed is the one kind of count that has to be defensible on
# request: the vehicle was first seen past the line, and the count rests on where its
# heading says it must have come from. These two endpoints turn that inference into
# something you can look at and overrule.
@app.get("/api/crossings/{video_id}/implied")
def lab_implied_crossings(video_id: int):
    import crossings
    return crossings.diff(video_id)


@app.get("/api/crossings/{video_id}/{track_id}/image")
def lab_crossing_image(video_id: int, track_id: int):
    import crossings
    p = crossings.review_image(video_id, track_id)
    if not p:
        raise HTTPException(404, "could not render that track's birth frame")
    return FileResponse(p, media_type="image/jpeg")


# ─────────────────── annotated preview ───────────────────
# Unlike counting, this one IS a job: it decodes every frame, draws the boxes, tracks and
# the count line, then re-encodes. 63-110s for a 15-minute clip, so it gets real progress.
ANNOTATED = ROOT / "app" / "annotated"


@app.get("/api/render/{video_id}")
def render_status(video_id: int):
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such video")
    p = organise.render_path(video_id) or (ANNOTATED / f"annotated_{v['name']}.mp4")
    job = db.one("""SELECT * FROM jobs WHERE video_id=? AND kind='render'
                    ORDER BY id DESC LIMIT 1""", video_id)
    sc = db.one("SELECT updated FROM scenes WHERE video_id=?", video_id)
    made = p.stat().st_mtime if p.exists() else None
    # The line is drawn INTO the video, so editing it leaves the render showing a line
    # that no longer exists. Saying so beats letting someone review a stale picture.
    stale = bool(made and sc and (sc["updated"] or 0) > made)
    return {
        "video_id": video_id,
        "exists": p.exists(),
        "size_mb": round(p.stat().st_size / 1e6, 1) if p.exists() else None,
        "made": made,
        "line_changed": sc["updated"] if sc else None,
        "stale": stale,
        "job": job,
        "has_line": bool(sc),
    }


@app.post("/api/render/{video_id}")
def start_render(video_id: int):
    import render as render_mod
    if not db.one("SELECT id FROM videos WHERE id=?", video_id):
        raise HTTPException(404, "no such video")
    busy = db.one("SELECT id, video_id FROM jobs WHERE kind='render' "
                  "AND status IN ('queued','running') LIMIT 1")
    if busy:
        raise HTTPException(409, f"a render is already running (video {busy['video_id']})")
    job_id = db.run("INSERT INTO jobs (video_id,kind,status,progress,message) "
                    "VALUES (?,'render','queued',0,'')", video_id)
    threading.Thread(target=render_mod.render, args=(video_id, job_id), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/annotated-usage")
def annotated_usage():
    """What the renders are costing on disk, and what is safe to remove.

    Renders are DERIVED: ~60-110 s of CPU regenerates any of them exactly, so they are
    the cheapest thing in the project to delete and the most expensive to keep. A
    re-render overwrites the same filename, so versions never pile up -- but a render
    for footage nobody looks at any more sits there forever unless someone is told.
    """
    ANNOTATED.mkdir(parents=True, exist_ok=True)
    known = {f"annotated_{v['name']}.mp4": v["id"]
             for v in db.rows("SELECT id, name FROM videos")}
    files, total = [], 0
    for p in sorted(organise.all_renders()):
        sz = p.stat().st_size
        total += sz
        files.append({"name": p.name, "mb": round(sz / 1e6, 1), "made": p.stat().st_mtime,
                      "video_id": known.get(p.name), "orphan": p.name not in known})
    temps = [{"name": p.name, "mb": round(p.stat().st_size / 1e6, 1)}
             for p in ANNOTATED.glob("*.tmp.mp4")]
    return {"files": files, "total_mb": round(total / 1e6, 1),
            "orphans": [f for f in files if f["orphan"]], "temps": temps}


@app.delete("/api/annotated/{video_id}")
def delete_annotated(video_id: int):
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such video")
    p = organise.render_path(video_id)
    if not p or not p.exists():
        return {"deleted": False, "reason": "nothing rendered"}
    mb = round(p.stat().st_size / 1e6, 1)
    p.unlink()
    db.log(None, "deleted", f"render for video {video_id}", f"{mb} MB freed")
    return {"deleted": True, "mb": mb}


@app.post("/api/annotated/cleanup")
def cleanup_annotated():
    """Remove interrupted temp files and renders whose video row is gone."""
    ANNOTATED.mkdir(parents=True, exist_ok=True)
    known = {f"annotated_{v['name']}.mp4" for v in db.rows("SELECT name FROM videos")}
    freed, removed = 0.0, []
    for p in list(ANNOTATED.glob("*.tmp.mp4")) + [
            q for q in ANNOTATED.glob("*.mp4") if q.name not in known
            and not q.name.endswith(".tmp.mp4")]:
        freed += p.stat().st_size / 1e6
        removed.append(p.name)
        p.unlink()
    if removed:
        db.log(None, "cleaned", f"{len(removed)} render(s)", f"{round(freed,1)} MB freed")
    return {"removed": removed, "freed_mb": round(freed, 1)}


@app.get("/api/annotated/{video_id}")
def annotated(video_id: int):
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "no such video")
    p = organise.render_path(video_id)
    if not p or not p.exists():
        raise HTTPException(404, "not rendered yet")
    return FileResponse(p, media_type="video/mp4", filename=p.name)


@app.get("/api/errors")
def error_decomposition(site_id: int | None = None, video_id: int | None = None,
                        model_id: str | None = None):
    """Where a station's count error comes from, and what it would cost to fix."""
    return errors_mod.decompose(site_id, video_id, model_id)


@app.get("/api/all-videos")
def all_videos():
    """Every extracted video with its line and count status — the Counts list."""
    rows = db.rows("""SELECT v.id, v.name, v.frames, v.start_clock, v.site_id,
                             s.code || ' · ' || s.name AS station,
                             (SELECT COUNT(*) FROM scenes sc WHERE sc.video_id=v.id) has_line
                      FROM videos v LEFT JOIN sites s ON s.id = v.site_id
                      WHERE COALESCE(v.excluded,0)=0 ORDER BY v.id DESC""")
    import counting
    for r in rows:
        lines, src = stations_mod.lines_for(r["id"])
        r["n_lines"] = len(lines)
        r["line_source"] = src
        r["counted"] = None
        if lines and r.get("start_clock"):
            try:
                r["counted"] = counting.count_video(r["id"], lines)["total"]
            except Exception:
                r["counted"] = None
    return {"videos": rows}


@app.get("/api/countable-videos")
def countable_videos(site_id: int | None = None):
    """Videos that have a count line drawn -- the ones the counting layer can diagnose."""
    rows = db.rows("""SELECT v.id, v.name, v.start_clock, v.site_id
                      FROM videos v JOIN scenes s ON s.video_id = v.id
                      WHERE COALESCE(v.excluded,0)=0 ORDER BY v.id DESC""")
    if site_id:
        rows = [r for r in rows if r["site_id"] in (site_id, None)]
    return {"videos": rows}


@app.get("/api/datasets")
def list_datasets():
    return {"datasets": artifacts.datasets()}


@app.get("/api/weights")
def list_weights():
    return {"weights": artifacts.weights(), "verify": artifacts.verify_weights()}


@app.post("/api/weights/verify")
def verify_weights():
    return artifacts.verify_weights()


app.mount("/shared", StaticFiles(directory=str(ROOT / "shared")), name="shared")
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    # 0.0.0.0 so the Lab is reachable from a phone on the same WiFi. There is no
    # login on it, so keep it to a trusted network -- LAB_HOST=127.0.0.1 locks it
    # back down to this machine.
    import os
    uvicorn.run(app, host=os.environ.get("LAB_HOST", "0.0.0.0"),
                port=int(os.environ.get("LAB_PORT", "8800")), log_level="warning")
