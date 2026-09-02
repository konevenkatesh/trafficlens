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
import os
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

# Where the bundled files live. Frozen, everything is unpacked under sys._MEIPASS and
# `__file__` points inside it, so `Path(__file__).parent / "static"` resolves to
# _internal/static -- which does not exist, because the spec places these at
# survey/static. Laying both cases out explicitly is the only way this stays true in a
# build nobody can run locally.
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
STATIC = BUNDLE / "survey" / "static"
SHARED = BUNDLE / "shared"

# Written at run time, so never inside the bundle: that is a temp directory in a frozen
# build, deleted on exit, and read-only under Program Files.
OUT = Path(os.environ.get("TRAFFICLENS_DATA") or ROOT) / "reports"


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    # /shared/ too, not just /static/. ui.css lives there, so a browser that had cached
    # it kept rendering an old app after an upgrade -- which looks exactly like the new
    # build never installed.
    if request.url.path.startswith(("/api/", "/static/", "/shared/")):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/version")
def version():
    """Which build this actually is. The first question after any "I don't see the
    changes", and it used to be unanswerable."""
    try:
        import buildinfo
        return {"build": buildinfo.BUILD, "commit": buildinfo.COMMIT,
                "built": buildinfo.BUILT}
    except Exception:
        return {"build": "unknown", "commit": "unknown", "built": ""}


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
            # The line comes AFTER detection, because that is when there is a frame to
            # draw it on and some idea of where the traffic runs. Counting, review and
            # the report all need it; detection does not.
            "steps": [
                {"key": "folder", "label": "Footage attached", "done": len(ids) > 0},
                {"key": "extract", "label": "Vehicles detected", "done": extracted > 0},
                {"key": "line", "label": "Count line drawn", "done": has_line},
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
    # Whole files, counted once each -- see work.work_seconds. Summing seconds_here told
    # a surveyor with a 3-hour recording that one hour of it would take a third of the
    # time it really takes.
    todo_s = work.work_seconds(site_id)

    # An extraction that failed leaves the hour looking exactly like one never started:
    # no tracks, tile says "detect". The surveyor presses it again, it fails again, and
    # nothing on screen ever says why. Surface it.
    fails = db.rows("""SELECT j.video_id, j.message, j.finished, v.name
                       FROM jobs j JOIN videos v ON v.id=j.video_id
                       WHERE v.site_id=? AND j.kind='extract' AND j.status='error'
                         AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.video_id=j.video_id)
                       ORDER BY j.id DESC LIMIT 10""", site_id)
    # How many recordings sit on a guessed time. The screen has always had code to warn
    # about this and it never once fired, because nothing ever sent the field -- so
    # footage filmed at night and filed at its copy time looked entirely normal, and the
    # first sign of trouble was an hourly report for hours that were never filmed.
    guessed = work.clock_report(site_id)["guessed"]
    return {"guessed": guessed, "station": {"id": s["id"], "code": s["code"], "name": s["name"]},
            "progress": _progress(site_id),
            "hours": hrs,
            "line": db.jload(s["default_line"], []),
            "device": work.device_note(),
            "remaining_estimate_s": round(work.estimate_s(todo_s)),
            "failures": [{"video_id": f["video_id"], "name": f["name"],
                          "message": (f["message"] or "")[:200]} for f in fails],
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
    try:
        vids = sum(1 for c in p.iterdir()
                   if c.is_file() and c.suffix.lower() in work.VIDEO_EXT)
    except (PermissionError, OSError):
        vids = 0
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "dirs": [{"name": c.name, "path": str(c)} for c in kids],
            "drives": _drives(),
            "videos_here": vids}


def _drives():
    """Every place a folder could live, including removable media.

    On Windows the picker starts in C:\\Users\\<name> and the parent of C:\\ is C:\\,
    so walking up never leaves the system drive -- and footage arrives on a USB disk or an
    external drive, which is to say never on C:. Enumerating the roots is the only way out.
    """
    out = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:\\")
            if d.exists():
                out.append({"name": f"{letter}:", "path": str(d)})
    else:
        out.append({"name": "/", "path": "/"})
        vol = Path("/Volumes")           # macOS mounts removable media here
        if vol.is_dir():
            try:
                for c in sorted(vol.iterdir()):
                    if c.is_dir():
                        out.append({"name": c.name, "path": str(c)})
            except OSError:
                pass
    home = Path.home()
    out.append({"name": "Home", "path": str(home)})
    return out


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
    # Deliberately does NOT require a count line. Detection finds vehicles; the line only
    # decides which of them crossed, and that is applied at counting time. Demanding the
    # line first meant a surveyor had to draw one over footage they had not looked at
    # yet, before the app could even show them a frame worth drawing on. Extract first,
    # draw the line when you can see what you are drawing on -- which is the order people
    # actually work in.
    r = work.enqueue_hour(site_id, hour, (body.model_id if body else None))
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {**r, "queue": work.queue_state()}


@app.get("/api/queue")
def queue():
    q = work.queue_state()
    # Several clips can be detecting at once now, so every one of them needs its own
    # progress. Matched on job KIND as well as video: hardcoding 'extract' meant a render
    # read the progress of some earlier extraction and showed 100% from the first second.
    for cur in q.get("running_all") or []:
        j = db.one("""SELECT progress,message,started FROM jobs WHERE video_id=? AND kind=?
                      ORDER BY id DESC LIMIT 1""",
                   cur["video_id"], cur.get("kind") or "extract")
        if not j:
            continue
        cur.update({"progress": j["progress"] or 0, "message": j["message"]})
        pct = (j["progress"] or 0) / 100.0
        el = time.time() - (j["started"] or time.time())
        cur["eta_s"] = round(el / pct - el) if pct > 0.02 else None
    ra = q.get("running_all") or []
    q["running"] = ra[0] if ra else None      # older single-job callers still work
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
def review(site_id: int, mode: str = "critical", cls: str = "", limit: int = 300):
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
    mix = {}
    for vid in _site_videos(site_id):
        q = verify.queue(vid, only_class=cls or None,
                         mandatory_only=(mode == "critical"),
                         answered=False if mode != "done" else True, limit=limit)
        totals["total"] += q.get("total", 0)
        totals["mandatory"] += q.get("mandatory", 0)
        totals["answered"] += q.get("answered", 0)
        # The class list comes from the UNFILTERED counts, or picking "Bus" would leave
        # the dropdown showing only Bus and no way back to the others.
        for c, n in (q.get("classes") or []):
            mix[c] = mix.get(c, 0) + n
        for it in q.get("items", []):
            items.append({**it, "video_id": vid, "clip": q.get("video", {}).get("name")})
        if len(items) >= limit:
            break
    # Biggest first: a vehicle 1000px wide is one a person can settle in a second, and
    # front-loading those means an interrupted session still got through the easy wins.
    items.sort(key=lambda x: (not x.get("mandatory"), -(x.get("box_w") or 0)))
    return {"items": items[:limit], "mode": mode, "cls": cls, **totals,
            "classes": sorted(mix.items(), key=lambda kv: -kv[1]),
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


# ───────────────────────────── cloud GPU ─────────────────────────────
class CloudIn(BaseModel):
    key: str | None = None
    gpu: str | None = None
    limit_usd: float | None = None
    enabled: bool | None = None


@app.get("/api/cloud")
def cloud_status():
    """Connection, price, spend and anything running — one call, safe with no key."""
    import cloud
    return cloud.status()


@app.post("/api/cloud/settings")
def cloud_save(body: CloudIn):
    import cloud
    cloud.save_config(key=body.key, gpu=body.gpu,
                      limit_usd=body.limit_usd, enabled=body.enabled)
    return cloud.status()


@app.post("/api/cloud/stop")
def cloud_stop():
    """Kill every pod, now. The button for when somebody is not sure."""
    import cloud
    return {**cloud.stop_all(), "status": cloud.status()}


@app.get("/api/cloud/runs")
def cloud_runs(limit: int = 30):
    """The spending ledger, newest first — what ran, for how long, what it cost."""
    import cloud
    cloud.init()
    return {"runs": db.rows("""SELECT pod_id,gpu,cost_per_hr,started,stopped,seconds,
                                      usd,status,clips,note
                               FROM cloud_runs ORDER BY id DESC LIMIT ?""", limit),
            "spend": cloud.spend()}


# ───────────────────────────── the clock ─────────────────────────────
class ClockIn(BaseModel):
    video_id: int
    clock: str
    shift_others: bool = True


@app.get("/api/stations/{site_id}/clock")
def clock_get(site_id: int):
    """Every recording, its start time, and whether that time is trustworthy."""
    return work.clock_report(site_id)


@app.post("/api/stations/{site_id}/clock")
def clock_set(site_id: int, body: ClockIn):
    """Correct a start time. Everything downstream is rebuilt from it on the next read."""
    try:
        r = work.set_clock(body.video_id, body.clock, body.shift_others)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**r, **work.clock_report(site_id), "hours": work.hours(site_id)}


# ───────────────────────────── speed ─────────────────────────────
class TrapIn(BaseModel):
    a: dict | None = None
    b: dict | None = None
    metres: float | None = None
    expected_kmh: float | None = None


@app.get("/api/stations/{site_id}/speed")
def speed_get(site_id: int):
    """The trap, and every reading it has produced so far."""
    import speed
    trap = speed.trap_for(site_id)
    if not trap:
        return {"trap": None, "summary": {"n": 0}}
    rows, fps = [], None
    for v in db.rows("""SELECT id, fps FROM videos WHERE site_id=?
                        AND COALESCE(excluded,0)=0""", site_id):
        rows.extend(speed.speeds_for(v["id"], trap))
        fps = fps or v["fps"]
    return {"trap": trap, "summary": speed.summary(rows, trap),
            "accuracy": speed.accuracy_note(trap, fps or 12)}


@app.post("/api/stations/{site_id}/speed")
def speed_set(site_id: int, body: TrapIn):
    import speed
    try:
        speed.save_trap(site_id, body.a, body.b, body.metres, body.expected_kmh)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return speed_get(site_id)


# ───────────────────────────── annotated video ─────────────────────────────
def _render_path(video_id):
    """Where this recording's annotated video actually is.

    Two places, because render.py writes to whichever it can: if the Lab's `organise`
    module is importable it files the render inside the station's own folder, otherwise
    it falls back to a flat directory. Checking only the flat one reported "not made yet"
    for a video sitting finished on disk -- which is exactly what happened the first time
    this was tested.
    """
    import render
    v = db.one("SELECT name FROM videos WHERE id=?", video_id)
    if not v:
        return None
    try:
        import organise
        q = organise.render_path(video_id)
        if q and Path(q).is_file():
            return Path(q)
    except Exception:
        pass
    p = render.OUT_DIR / f"annotated_{v['name']}.mp4"
    return p if p.is_file() else None


def _render_state(video_id):
    """Whether a render exists and whether it still tells the truth.

    A render draws the class the vehicle is currently recorded as, and the count that
    follows from it — so corrections DO reach the video, but only the next time it is
    made. A render finished before a reviewer reclassified twenty vehicles is now a
    picture of an older answer, and offering it as "Watch" makes the app lie in the most
    convincing possible format. So the file's own mtime is compared against the newest
    verdict for that recording, and an older file is offered as a remake instead.
    """
    p = _render_path(video_id)
    if not p:
        return {"ready": False, "stale": False}
    made = p.stat().st_mtime
    try:
        last = (db.one("SELECT MAX(created) c FROM clip_verdicts WHERE video_id=?",
                       video_id) or {}).get("c")
    except Exception:      # no verdicts table yet — nothing has been reviewed
        last = None
    return {"ready": True, "made": made, "stale": bool(last and last > made)}


@app.post("/api/clips/{video_id}/annotate")
def annotate(video_id: int):
    """Draw the boxes, tracks and the count line onto the footage.

    This is how a surveyor checks the count is real rather than plausible: watching a
    lorry cross the line and the number tick is worth more than any accuracy figure. It
    is also what gets sent to a client who does not believe the total.
    """
    import sites
    if not db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=?", video_id)["n"]:
        raise HTTPException(400, "detect this recording first — there is nothing to draw")
    if not sites.lines_for(video_id)[0]:
        raise HTTPException(400, "draw the count line first — the video shows crossings")
    r = work.enqueue_render(video_id)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {**r, "queue": work.queue_state()}


@app.get("/api/clips/{video_id}/render_state")
def render_state(video_id: int):
    """One answer to "where is my video". Readiness, staleness and the running job in a
    single call, so the button can report progress instead of saying "Queued…" for six
    minutes and then nothing."""
    st = _render_state(video_id)
    q = work.queue_state()
    if any(j["video_id"] == video_id and j.get("kind") == "render"
           for j in (q.get("waiting") or [])):
        return {**st, "job": "waiting"}
    if any(j["video_id"] == video_id and j.get("kind") == "render"
           for j in (q.get("running_all") or [])):
        j = db.one("""SELECT progress,message FROM jobs WHERE video_id=? AND kind='render'
                      ORDER BY id DESC LIMIT 1""", video_id) or {}
        return {**st, "job": "running", "progress": j.get("progress") or 0,
                "message": j.get("message")}
    j = db.one("""SELECT status,message FROM jobs WHERE video_id=? AND kind='render'
                  ORDER BY id DESC LIMIT 1""", video_id) or {}
    if j.get("status") == "error" and not st["ready"]:
        return {**st, "job": "error", "message": j.get("message")}
    return {**st, "job": None}


@app.get("/api/clips/{video_id}/annotated.mp4")
def annotated(video_id: int):
    p = _render_path(video_id)
    if not p:
        raise HTTPException(404, "not made yet")
    return FileResponse(str(p), media_type="video/mp4", filename=p.name)


# ───────────────────────────── the report ─────────────────────────────
def _overlaps(video_ids):
    """Recordings whose clock times cover each other — i.e. traffic counted twice.

    This is not hypothetical. Attaching a folder that still holds the original 80-minute
    recordings alongside the 15-minute clips cut from them puts the same vehicles on the
    timeline under two names, and the total looks entirely plausible: it is simply
    double. Nothing else in the pipeline notices, because every clip is individually
    correct. So the report says so, loudly, rather than quietly adding them up.
    """
    from datetime import datetime, timedelta
    spans = []
    for v in db.rows(f"""SELECT id,name,start_clock,frames,fps FROM videos
                         WHERE id IN ({','.join('?' * len(video_ids))})
                         AND start_clock IS NOT NULL""", *video_ids):
        try:
            a = datetime.strptime(v["start_clock"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        d = (v["frames"] or 0) / (v["fps"] or 1)
        if d > 0:
            spans.append((a, a + timedelta(seconds=d), v))
    spans.sort()
    out = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a0, a1, va = spans[i]
            b0, b1, vb = spans[j]
            if b0 >= a1:
                break                       # sorted: nothing later can overlap either
            secs = (min(a1, b1) - b0).total_seconds()
            # A second or two of rounding at a clip boundary is not a double count.
            if secs > 30:
                out.append({"a": va["name"], "b": vb["name"],
                            "a_id": va["id"], "b_id": vb["id"],
                            "minutes": round(secs / 60, 1),
                            "from": b0.strftime("%Y-%m-%d %H:%M")})
    return out


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
                      **{"annotated": (_st := _render_state(vid))["ready"],
                         "annotated_stale": _st["stale"]},
                      "checks": [x for x in c.get("checks", []) if x.get("level") != "ok"]})
    bins.sort(key=lambda b: b["t"])
    overlaps = _overlaps(vids)
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
        "overlaps": overlaps,
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
    out = OUT / f"{s['code']}_count.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    # build() collects, write() renders. Two calls, not one -- passing the path to build()
    # would silently be read as its `meta` argument and produce a workbook nowhere.
    aprdc_workbook.write(aprdc_workbook.build(vids), str(out))
    return FileResponse(str(out), filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# ───────────────────────────── shell ─────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/shared", StaticFiles(directory=str(SHARED)), name="shared")


@app.get("/")
def index():
    """The page, with its assets stamped so a browser cannot serve yesterday's copy.

    no-store on the responses only helps a browser that asks. One that already has app.js
    cached from a previous version may not ask at all, and then a freshly installed build
    renders the old screens -- which is indistinguishable from the update having failed,
    and was reported as exactly that. A changing query string is a different URL, so there
    is nothing to reuse.
    """
    html = (STATIC / "index.html").read_text()
    return HTMLResponse(html.replace("__BUILD__", version()["build"]),
                        headers={"Cache-Control": "no-store, must-revalidate"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
