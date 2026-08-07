"""TrafficLens Survey — the surveyor's app.

Seven steps, in order, and nothing else on screen:

    name the station -> point at the footage folder -> draw the count line once
    -> extract an hour -> review what the model is unsure of -> read the report

The Lab is where models are made. This app only *uses* one: a global detector plus the
universal heads that ship with it. There is no training here, no dataset builder, no
judges, no spend. Those are not simplifications of this product -- they are a different
product, and putting them in front of a surveyor is how a survey gets ruined by somebody
helpfully retraining something.

What it does share is the engine, because that is the part that must not fork: the same
`engine.extract`, the same `counting.count_video`, the same `verify`, the same
`report_card`. A second implementation of counting would be a second set of numbers.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.append(str(ROOT / "lab"))

from fastapi import FastAPI, HTTPException                       # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles                      # noqa: E402
from pydantic import BaseModel                                   # noqa: E402

import db          # noqa: E402
import work        # noqa: E402

app = FastAPI(title="TrafficLens Survey")
STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith(("/api/", "/static/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/health")
def health():
    """Opening a connection is what applies the schema and its migrations, so this
    doubles as the boot check: if it answers, the datastore is usable."""
    db.conn()
    return {"ok": True, "device": work.device_note()}


# ───────────────────────────── stations ─────────────────────────────
class StationIn(BaseModel):
    name: str
    code: str = ""


def _progress(site_id):
    """Where this station is in the seven steps, as plain counts.

    Everything is derived, never stored. A stored "step 3 of 7" goes stale the moment a
    file is deleted or a line is redrawn, and then the app confidently tells the surveyor
    they are finished when they are not.
    """
    vids = db.rows("SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id)
    ids = [v["id"] for v in vids]
    site = db.one("SELECT footage_dir, default_line FROM sites WHERE id=?", site_id) or {}
    extracted = counted = verified = 0
    if ids:
        q = ",".join("?" * len(ids))
        extracted = db.one(f"SELECT COUNT(DISTINCT video_id) n FROM tracks "
                           f"WHERE video_id IN ({q})", *ids)["n"]
        counted = db.one(f"SELECT COUNT(*) n FROM tracks WHERE video_id IN ({q})", *ids)["n"]
        verified = db.one(f"SELECT COUNT(*) n FROM clip_verdicts "
                          f"WHERE video_id IN ({q})", *ids)["n"]
    has_line = bool(db.jload((site or {}).get("default_line"), []))
    return {"folder": (site or {}).get("footage_dir"), "files": len(ids),
            "extracted": extracted, "tracks": counted, "verified": verified,
            "line": has_line,
            "steps": [
                {"key": "folder", "label": "Footage attached", "done": len(ids) > 0},
                {"key": "line", "label": "Count line drawn", "done": has_line},
                {"key": "extract", "label": "Vehicles detected", "done": extracted > 0},
                {"key": "review", "label": "Reviewed", "done": verified > 0},
            ]}


@app.get("/api/stations")
def stations():
    out = []
    for s in db.rows("SELECT id,code,name,footage_dir FROM sites ORDER BY id DESC"):
        out.append({**s, **_progress(s["id"])})
    return {"stations": out, "device": work.device_note()}


@app.post("/api/stations")
def new_station(body: StationIn):
    """A name is enough. Everything else is optional and can wait."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "give the station a name")
    code = (body.code or "").strip() or name.upper().replace(" ", "-")[:12]
    if db.one("SELECT id FROM sites WHERE code=?", code):
        raise HTTPException(409, f"a station called {code} already exists")
    sid = db.run("INSERT INTO sites (code,name,created) VALUES (?,?,?)",
                 code, name, time.time())
    return {"id": sid, "code": code, "name": name}


@app.get("/api/stations/{site_id}")
def station(site_id: int):
    s = db.one("SELECT id,code,name,footage_dir,default_line FROM sites WHERE id=?", site_id)
    if not s:
        raise HTTPException(404, "no such station")
    hrs = work.hours(site_id)
    todo_s = sum(sum(f["seconds_here"] for f in h["files"] if not f["tracks"]) for h in hrs)
    return {"station": {"id": s["id"], "code": s["code"], "name": s["name"]},
            "progress": _progress(site_id),
            "hours": hrs,
            "line": db.jload(s["default_line"], []),
            "device": work.device_note(),
            "remaining_estimate_s": round(work.estimate_s(todo_s)),
            "queue": work.queue_state()}


@app.delete("/api/stations/{site_id}")
def delete_station(site_id: int):
    """Only ever an empty one. Deleting a station with footage would orphan its counts
    and verdicts, and there is no undo in this app -- so the app does not offer it."""
    n = db.one("SELECT COUNT(*) n FROM videos WHERE site_id=?", site_id)["n"]
    if n:
        raise HTTPException(409, f"this station has {n} file(s) attached — detach first")
    db.run("DELETE FROM sites WHERE id=?", site_id)
    return {"ok": True}


# ───────────────────────────── the folder ─────────────────────────────
class FolderIn(BaseModel):
    folder: str


@app.get("/api/browse")
def browse(path: str = ""):
    """A folder picker the app can drive itself.

    A browser cannot hand a web app a real filesystem path, and typing one by hand is
    exactly the step a surveyor gets wrong. So the server lists directories and the UI
    walks them. Directories only -- the target of this screen is a folder.
    """
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
        if not p.is_dir():
            p = p.parent
        kids = sorted((c for c in p.iterdir()
                       if c.is_dir() and not c.name.startswith(".")),
                      key=lambda c: c.name.lower())
    except (PermissionError, OSError) as e:
        raise HTTPException(400, f"cannot read {p}: {e}")
    vids = sum(1 for c in p.iterdir()
               if c.is_file() and c.suffix.lower() in work.VIDEO_EXT)
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "dirs": [{"name": c.name, "path": str(c)} for c in kids],
            "videos_here": vids}


@app.post("/api/stations/{site_id}/folder")
def set_folder(site_id: int, body: FolderIn):
    r = work.attach(site_id, body.folder)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {**r, "progress": _progress(site_id), "hours": work.hours(site_id)}


@app.get("/api/stations/{site_id}/rescan")
def rescan(site_id: int):
    """What is in the folder right now, against what the app knows about."""
    s = db.one("SELECT footage_dir FROM sites WHERE id=?", site_id)
    if not s or not s["footage_dir"]:
        raise HTTPException(400, "no folder attached yet")
    return work.attach(site_id, s["footage_dir"])


# ───────────────────────────── the count line ─────────────────────────────
class LineIn(BaseModel):
    lines: list


@app.get("/api/stations/{site_id}/frame")
def station_frame(site_id: int, at: float = 0.25):
    """A full-resolution frame to draw the line on, from this station's own footage.

    `at` is a fraction through the first recording rather than a frame number: the very
    first frames of a DVR file are often a grey buffer, and a line drawn on grey is a line
    drawn in the wrong place.
    """
    v = db.one("""SELECT id,path,frames FROM videos WHERE site_id=?
                  AND COALESCE(excluded,0)=0 ORDER BY start_clock LIMIT 1""", site_id)
    if not v:
        raise HTTPException(404, "no footage attached yet")
    import cv2
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, int((v["frames"] or 100) * max(0.0, min(at, 0.95))))
    ok, img = cap.read()
    cap.release()
    if not ok:
        raise HTTPException(500, "could not read a frame from the footage")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/jpeg")


@app.get("/api/stations/{site_id}/line")
def get_line(site_id: int):
    s = db.one("SELECT default_line FROM sites WHERE id=?", site_id)
    if not s:
        raise HTTPException(404, "no such station")
    return {"lines": db.jload(s["default_line"], [])}


@app.post("/api/stations/{site_id}/line")
def set_line(site_id: int, body: LineIn):
    """One line, drawn once, used by every recording at this station.

    Saved explicitly, never on mouse-move. An autosaving editor turned a stray click into
    a stored line here once, and the count that came out of it looked entirely plausible.
    """
    db.run("UPDATE sites SET default_line=?, line_set=? WHERE id=?",
           db.jdump(body.lines or []), time.time(), site_id)
    return {"ok": True, "lines": body.lines or []}


# ───────────────────────────── extracting ─────────────────────────────
class HourIn(BaseModel):
    model_id: str | None = None


@app.post("/api/stations/{site_id}/hours/{hour}/extract")
def extract_hour(site_id: int, hour: str, body: HourIn | None = None):
    if not db.jload((db.one("SELECT default_line FROM sites WHERE id=?", site_id)
                     or {}).get("default_line"), []):
        raise HTTPException(400, "draw the count line first — nothing can be counted without it")
    r = work.enqueue_hour(site_id, hour, (body.model_id if body else None))
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {**r, "queue": work.queue_state()}


@app.get("/api/queue")
def queue():
    q = work.queue_state()
    cur = q.get("running")
    if cur:
        j = db.one("""SELECT progress,message,started FROM jobs WHERE video_id=?
                      AND kind='extract' ORDER BY id DESC LIMIT 1""", cur["video_id"])
        if j:
            cur.update({"progress": j["progress"] or 0, "message": j["message"]})
            pct = (j["progress"] or 0) / 100.0
            el = time.time() - (j["started"] or time.time())
            cur["eta_s"] = round(el / pct - el) if pct > 0.02 else None
    return q


@app.post("/api/queue/cancel")
def queue_cancel():
    return work.cancel()


@app.get("/api/models")
def models():
    """The detectors this build can use. One is the default; the rest are for when a
    better one ships. Named, with their measured scores, so the choice is not blind."""
    import models_registry
    models_registry.init()
    models_registry.discover()
    return {"models": models_registry.listing(), "default": models_registry.default_id()}


# ───────────────────────────── reviewing ─────────────────────────────
def _site_videos(site_id):
    return [v["id"] for v in db.rows(
        """SELECT v.id FROM videos v WHERE v.site_id=? AND COALESCE(v.excluded,0)=0
           AND EXISTS (SELECT 1 FROM tracks t WHERE t.video_id=v.id)
           ORDER BY v.start_clock""", site_id)]


@app.get("/api/stations/{site_id}/review")
def review(site_id: int, mode: str = "critical", limit: int = 300):
    """One review queue for the whole station, hardest-first.

    Per-clip queues are the Lab's model and they are wrong here: a surveyor does not care
    which 15-minute file a lorry was in, and making them finish clip 1 before seeing the
    worst call in clip 9 wastes the only expensive resource in this process, which is
    their attention.

    `critical` is the default and means what the model is least able to settle on its own:
    heavy vehicles, where one wrong call moves the report by 3-4.5 PCU, plus anything the
    confidence gate refused. `all` is every counted vehicle, for a full audit.
    """
    import verify
    items, totals = [], {"total": 0, "mandatory": 0, "answered": 0}
    for vid in _site_videos(site_id):
        q = verify.queue(vid, mandatory_only=(mode == "critical"),
                         answered=False if mode != "done" else True, limit=limit)
        totals["total"] += q.get("total", 0)
        totals["mandatory"] += q.get("mandatory", 0)
        totals["answered"] += q.get("answered", 0)
        for it in q.get("items", []):
            items.append({**it, "video_id": vid, "clip": q.get("video", {}).get("name")})
        if len(items) >= limit:
            break
    # Biggest first: a vehicle 1000px wide is one a person can settle in a second, and
    # front-loading those means an interrupted session still got through the easy wins.
    items.sort(key=lambda x: (not x.get("mandatory"), -(x.get("box_w") or 0)))
    return {"items": items[:limit], "mode": mode, **totals,
            "answers": verify.answers() if hasattr(verify, "answers") else None}


class VerdictIn(BaseModel):
    video_id: int
    track_id: int
    answer: str


@app.post("/api/review")
def verdict(body: VerdictIn):
    import verify
    try:
        return verify.verdict(body.video_id, body.track_id, body.answer)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/review/{video_id}/{track_id}/{kind}.jpg")
def review_image(video_id: int, track_id: int, kind: str):
    import verify
    cp, xp = verify.crop(video_id, track_id)
    p = xp if kind == "ctx" else cp
    if not p or not Path(p).is_file():
        raise HTTPException(404, "no image")
    return FileResponse(p, media_type="image/jpeg")


# ───────────────────────────── the report ─────────────────────────────
@app.get("/api/stations/{site_id}/report")
def report(site_id: int):
    """The station's numbers, rolled up from every extracted clip.

    Built by asking report_card for each clip and summing, rather than by a second
    implementation of counting. The clip-level card is what the Lab validates against, so
    a station total computed any other way would be a number nobody has checked.
    """
    import report_card
    s = db.one("SELECT code,name FROM sites WHERE id=?", site_id)
    if not s:
        raise HTTPException(404, "no such station")
    vids = _site_videos(site_id)
    if not vids:
        return {"station": dict(s), "empty": True,
                "note": "nothing extracted yet — run an hour first"}

    per_class, pcu_by_class, bins, clips = {}, {}, [], []
    total = pcu_total = 0
    attrs = {}
    for vid in vids:
        c = report_card.build(vid)
        if c.get("error"):
            clips.append({"video_id": vid, "error": c["error"]})
            continue
        for k, v in c["per_class"].items():
            per_class[k] = per_class.get(k, 0) + v
        for k, v in c["pcu_by_class"].items():
            pcu_by_class[k] = round(pcu_by_class.get(k, 0) + v, 1)
        for a in c.get("attributes", []):
            t = attrs.setdefault(a["attr"], {**a, "yes": 0, "no": 0,
                                              "unreviewed": 0, "pool": 0})
            for f in ("yes", "no", "unreviewed", "pool"):
                t[f] += a[f]
        total += c["total"]
        pcu_total = round(pcu_total + c["pcu_total"], 1)
        bins.extend(c["bins_15min"])
        clips.append({"video_id": vid, "name": c["video"]["name"],
                      "start": c["video"]["start_clock"], "total": c["total"],
                      "pcu": c["pcu_total"],
                      "checks": [x for x in c.get("checks", []) if x.get("level") != "ok"]})
    bins.sort(key=lambda b: b["t"])
    hourly = {}
    for b in bins:
        h = b["t"][:13] + ":00"
        hourly[h] = hourly.get(h, 0) + b["n"]
    return {
        "station": dict(s), "total": total, "pcu_total": pcu_total,
        "per_class": per_class, "pcu_by_class": pcu_by_class,
        "composition": sorted(
            [{"class": k, "n": v, "share": round(100 * v / total, 1) if total else 0}
             for k, v in per_class.items()], key=lambda x: -x["n"]),
        "bins_15min": bins,
        "hourly": [{"hour": k, "n": v} for k, v in sorted(hourly.items())],
        "attributes": list(attrs.values()),
        "clips": clips,
        "reviewed": db.one(
            f"""SELECT COUNT(*) n FROM clip_verdicts WHERE video_id IN
                ({','.join('?' * len(vids))})""", *vids)["n"],
    }


@app.get("/api/stations/{site_id}/report.xlsx")
def report_xlsx(site_id: int):
    """The deliverable, in the format the client receives."""
    import aprdc_workbook
    vids = _site_videos(site_id)
    if not vids:
        raise HTTPException(400, "nothing extracted yet")
    s = db.one("SELECT code FROM sites WHERE id=?", site_id)
    out = ROOT / "survey_out" / f"{s['code']}_count.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    # build() collects, write() renders. Two calls, not one -- passing the path to build()
    # would silently be read as its `meta` argument and produce a workbook nowhere.
    aprdc_workbook.write(aprdc_workbook.build(vids), str(out))
    return FileResponse(str(out), filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# ───────────────────────────── shell ─────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/shared", StaticFiles(directory=str(ROOT / "shared")), name="shared")


@app.get("/")
def index():
    return HTMLResponse((STATIC / "index.html").read_text())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
