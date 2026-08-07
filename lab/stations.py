"""Stations: the unit everything else hangs off.

A survey delivers seven days of round-the-clock footage from ONE count station, and the
job is to be accurate *at that station*. So a station -- not a video, not a run -- owns
the footage, the runs, the datasets and the models built from it.

The `sites` table is shared with the survey app rather than duplicated here. The two
applications must agree on what "Bhalki Junction" means; a second station list would
drift within a week and quietly split a station's data in half.

Coverage is treated as a first-class fact. Seven days is 168 hours and DVR files land in
ragged 1-2 hour chunks that straddle hour boundaries, so "which hours do we actually
have?" is a real question with a non-obvious answer, and sampling from footage you have
not checked the coverage of is how a survey ends up with a missing afternoon.
"""
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import db
from pipeline import clock_from_name, ffprobe

# Where survey footage arrives. The external drive first -- that is where the real
# 7-day deliveries land; the local folders are the working copies.
ROOT = Path(__file__).parent.parent
FOOTAGE_ROOTS = [Path("/Volumes/RK/Traffic"), ROOT / "video", ROOT / "app_videos"]

# DVR chunks abut within a second or two of each other; anything under this is a seam,
# not an overlap, and treating it as one produced sixteen phantom warnings.
SEAM = timedelta(seconds=90)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_footage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER, path TEXT UNIQUE, name TEXT, camera TEXT,
  start_clock TEXT, clock_source TEXT, dur_s REAL, size_mb REAL,
  fps REAL, width INTEGER, height INTEGER,
  day TEXT, hour INTEGER, discovered REAL, note TEXT, dup_of INTEGER,
  session TEXT, site_confirmed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_footage_site ON lab_footage(site_id, day);
"""


def init():
    db.conn().executescript(SCHEMA)
    # Runs, datasets and trainings all become station-scoped. Added as ALTERs because
    # these tables already carry live data.
    for tbl, col in (("lab_runs", "site_id INTEGER"),
                     ("lab_datasets", "site_id INTEGER"),
                     ("lab_trainings", "site_id INTEGER"),
                     ("lab_trainings", "scope TEXT")):
        try:
            db.conn().execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except Exception:
            pass                        # already there
    db.conn().commit()


# ─────────────────────────────── stations ───────────────────────────────
def stations():
    init()
    out = db.rows("SELECT * FROM sites ORDER BY code")
    for s in out:
        # One audit per station, shared with progress() below. The list used to count
        # `dup_of IS NULL` rows while the station page counted recordings in the folder,
        # so the same station read 9.3 h in the list and 4.3 h when you opened it.
        a = audit(s["id"])
        t = a["totals"]
        s["footage"] = {"files": t["recordings"], "hours": t["hours"], "days": t["days"],
                        "first_day": (t["span"] or {}).get("from", "")[:10] or None,
                        "last_day": (t["span"] or {}).get("to", "")[:10] or None,
                        "outside": len(a["outside"]), "missing": len(a["missing"])}
        s["runs"] = (db.one("SELECT COUNT(*) n FROM lab_runs WHERE site_id=?", s["id"]) or {}).get("n", 0)
        s["datasets"] = (db.one("SELECT COUNT(*) n FROM lab_datasets WHERE site_id=?", s["id"]) or {}).get("n", 0)
        s["models"] = db.rows(
            "SELECT id,tag,scope,map50,recall,status FROM lab_trainings WHERE site_id=? "
            "ORDER BY id DESC", s["id"])
        s["progress"] = progress(s["id"], a)
    return out


# The journey a station actually goes through. Shown as progress rather than a row of
# counts, because the useful question on a list of projects is "where is this one and
# what does it need next", and a table of zeros answers neither.
STAGES = [
    ("footage",  "Footage attached"),
    ("line",     "Count line drawn"),
    ("extract",  "Detections extracted"),
    ("labels",   "Labels confirmed"),
    ("dataset",  "Dataset built"),
    ("model",    "Station model trained"),
]


def progress(site_id, a=None):
    """Where this station has got to, and the one thing to do next.

    Footage is scoped to the station's folder and counted by RECORDING -- a distinct
    stretch of clock time -- so the rail, the tiles and the coverage grid cannot
    disagree. `a` lets a caller pass an audit it already has; stations() lists five
    stations and would otherwise compute each one twice.
    """
    a = a or audit(site_id)
    f = {"n": a["totals"]["recordings"], "hours": a["totals"]["hours"]}
    paths = [r["path"] for r in db.rows(
        "SELECT path FROM lab_footage WHERE site_id=? AND dup_of IS NULL", site_id)]
    # Excluded clips are kept for comparison but must not drive a station's progress:
    # one benchmark segment left SRI-01 claiming a drawn line and extracted detections
    # while it reported no footage at all.
    vids = db.rows("SELECT id FROM videos WHERE site_id=? AND COALESCE(excluded,0)=0", site_id)
    vid_ids = [v["id"] for v in vids]
    lined = extracted = 0
    # A station default counts as a line for every one of its videos — the whole point of
    # having one. Counting only per-video `scenes` rows left this step showing as pending
    # forever once the station default was in use.
    has_default = bool(default_line(site_id))
    if vid_ids:
        q = ",".join("?" * len(vid_ids))
        own = db.one(f"SELECT COUNT(*) n FROM scenes WHERE video_id IN ({q})", *vid_ids)["n"]
        lined = len(vid_ids) if has_default else own
        extracted = db.one(
            f"SELECT COUNT(DISTINCT video_id) n FROM tracks WHERE video_id IN ({q})",
            *vid_ids)["n"]
    runs = db.rows("SELECT id FROM lab_runs WHERE site_id=?", site_id)
    labels = 0
    if runs:
        q = ",".join("?" * len(runs))
        labels = db.one(
            f"""SELECT COUNT(*) n FROM lab_crops WHERE run_id IN ({q})
                AND state IN ('agreed','reclass','human')""",
            *[r["id"] for r in runs])["n"]
    # Verification on the Clips tab produces labels too, and this step could not see them:
    # it counted only lab_crops, the older judge-the-crops path. So a station with 193
    # verdicts read "nothing judged or reviewed" on the very page that displayed the 193.
    if vid_ids:
        q = ",".join("?" * len(vid_ids))
        labels += db.one(f"""SELECT COUNT(*) n FROM clip_verdicts
                             WHERE video_id IN ({q}) AND kind IN ('class','attribute','reject')""",
                         *vid_ids)["n"]
    datasets = (db.one("SELECT COUNT(*) n FROM lab_datasets WHERE site_id=?", site_id) or {}).get("n", 0)
    models = db.rows("SELECT id,tag,map50,recall,status FROM lab_trainings WHERE site_id=?", site_id)

    done = {
        "footage": (f.get("n") or 0) > 0,
        "line": lined > 0 or has_default,
        "extract": extracted > 0,
        "labels": labels > 0,
        "dataset": datasets > 0,
        "model": len(models) > 0,
    }
    detail = {
        "footage": f"{f.get('hours') or 0} h across {f.get('n') or 0} recording(s)" if done["footage"] else "no footage yet",
        "line": (f"station default, used by {lined} video(s)" if has_default
                 else f"{lined} video(s) with their own line") if done["line"]
                else "nothing can be counted yet",
        "extract": f"{extracted} video(s) extracted" if done["extract"] else "detector has not run",
        "labels": f"{labels} confirmed label(s)" if done["labels"] else "nothing judged or reviewed",
        "dataset": f"{datasets} dataset(s)" if done["dataset"] else "no training data built",
        "model": (f"{len(models)} model(s), best mAP50 "
                  f"{max((m['map50'] or 0) for m in models):.3f}") if models else "using the global model",
    }
    nxt = next((k for k, _ in STAGES if not done[k]), None)
    ACTION = {
        "footage": ("Attach footage", "station"),
        "line": ("Draw a count line", "counts"),
        "extract": ("Run the pipeline", "station"),
        "labels": ("Judge and review crops", "station"),
        "dataset": ("Build a dataset", "station"),
        "model": ("Train a station model", "training"),
    }
    return {
        "stages": [{"key": k, "label": lbl, "done": done[k], "detail": detail[k]}
                   for k, lbl in STAGES],
        "done_count": sum(done.values()), "total": len(STAGES),
        "next": nxt,
        "next_label": ACTION.get(nxt, ("Ready — nothing outstanding", None))[0],
        "next_route": ACTION.get(nxt, (None, None))[1],
    }


def thumbnail(site_id):
    """A frame from this station's own footage — a station should be recognisable."""
    import cv2
    out = ROOT / "lab_gold" / "_thumbs"
    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"site{site_id}.jpg"
    if dst.exists():
        return str(dst)
    row = db.one("""SELECT path FROM lab_footage WHERE site_id=? AND dup_of IS NULL
                    ORDER BY start_clock LIMIT 1""", site_id)
    if not row:
        return None
    cap = cv2.VideoCapture(row["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, 300)
    ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        return None
    h, w = img.shape[:2]
    k = 480 / w
    cv2.imwrite(str(dst), cv2.resize(img, (480, int(h * k))), [cv2.IMWRITE_JPEG_QUALITY, 80])
    return str(dst)


def station(site_id):
    init()
    s = db.one("SELECT * FROM sites WHERE id=?", site_id)
    if not s:
        return None
    s["footage"] = db.rows(
        "SELECT * FROM lab_footage WHERE site_id=? "
        "ORDER BY dup_of IS NOT NULL, start_clock", site_id)
    s["coverage"] = coverage(site_id)
    s["runs"] = db.rows("SELECT id,name,status,created FROM lab_runs WHERE site_id=? "
                        "ORDER BY id DESC", site_id)
    s["datasets"] = db.rows("SELECT id,name,n_train,n_val,fingerprint FROM lab_datasets "
                            "WHERE site_id=? ORDER BY id DESC", site_id)
    s["models"] = db.rows("SELECT * FROM lab_trainings WHERE site_id=? ORDER BY id DESC",
                          site_id)
    s["progress"] = progress(site_id)
    s["default_line"] = default_line(site_id)
    s["videos"] = _videos(site_id)
    s["gold"] = _gold(site_id)
    return s


def _videos(site_id):
    """Extracted videos for this station, with their line and count state."""
    import json as _json
    out = db.rows("""SELECT id, name, frames, fps, start_clock FROM videos
                     WHERE site_id=? AND COALESCE(excluded,0)=0 ORDER BY id DESC""", site_id)
    for v in out:
        lines, src = lines_for(v["id"])
        v["n_lines"] = len(lines)
        v["line_source"] = src
        v["tracks"] = db.one("SELECT COUNT(*) n FROM tracks WHERE video_id=? AND dup_of IS NULL",
                             v["id"])["n"]
        v["counted"] = None
        if lines and v.get("start_clock"):
            try:
                import counting
                v["counted"] = counting.count_video(v["id"], lines)["total"]
            except Exception:
                pass
    return out


def _gold(site_id):
    """Both gold sets, because there are two and they are not interchangeable.

    `goldset` is the original 60 hand-drawn frames. Clip verification is the other, and
    it is the one that has grown -- but the Labels tab badge read `done` from this dict,
    which only ever counted the frame set, so a station with 193 verdicts showed
    "Labels 0" above the panel listing all 193. `verdicts` is what the tab actually
    displays, so that is what the badge counts.
    """
    out = {}
    try:
        import goldset
        out = {**goldset.stats(site_id), **goldset.validation(site_id)}
    except Exception:
        pass
    try:
        import clips
        out["verdicts"] = clips.gold_from_verdicts(site_id).get("verdicts", 0)
    except Exception:
        out.setdefault("verdicts", 0)
    return out


# ─────────────────────────── the count line ───────────────────────────
def default_line(site_id):
    r = db.one("SELECT default_line FROM sites WHERE id=?", site_id)
    return db.jload(r["default_line"], []) if r and r["default_line"] else []


def line_check(site_id, lines=None):
    """Judge a count line against the vehicles this station has actually tracked.

    Where the line sits decides every number in the report, and the mistakes are invisible
    while you draw: a line parallel to the traffic, or one too short for the carriageway,
    looks perfectly reasonable on a still frame. Measured on KDP's first clip, a line in
    the top-right corner counted 1 vehicle out of 196 tracks; moved across the carriageway
    the same clip counted 60. Nothing about the drawing said which was which.

    So this replays the stored trajectories against the proposed line and reports what
    would happen. It is a query over track_points -- no GPU, no re-extraction -- so it can
    run every time the line moves.
    """
    # An empty list means "check what is saved" -- there is nothing to check about
    # no line, and the caller passes [] when it simply has not drawn yet.
    lines = lines or default_line(site_id)
    if not lines:
        return {"ok": False, "why": "no line drawn"}
    v = db.one("""SELECT v.id, v.name, COUNT(p.rowid) n FROM videos v
                  JOIN track_points p ON p.video_id = v.id
                  WHERE v.site_id=? AND COALESCE(v.excluded,0)=0
                  GROUP BY v.id ORDER BY n DESC LIMIT 1""", site_id)
    if not v:
        return {"ok": False, "why": "nothing extracted at this station yet — "
                                    "extract one clip and the line can be checked against it"}
    rows = db.rows("""SELECT track_id, frame, (x1+x2)/2 cx, y2 by, (y2-y1) h
                      FROM track_points WHERE video_id=? ORDER BY track_id, frame""", v["id"])
    paths = {}
    for r in rows:
        paths.setdefault(r["track_id"], []).append(r)

    out = []
    for ln in lines:
        (lx1, ly1), (lx2, ly2) = ln["start"], ln["end"]
        dx, dy = lx2 - lx1, ly2 - ly1
        L2 = dx * dx + dy * dy or 1
        side = lambda x, y: 1 if dx * (y - ly1) - dy * (x - lx1) > 0 else -1
        inside = before = after = never = 0
        crossers = recrossers = 0
        heights, headings = [], []
        for pts in paths.values():
            if len(pts) < 12:
                continue
            prev, hit, hits = None, False, 0
            for r in pts:
                s = side(r["cx"], r["by"])
                if prev is not None and s != prev[0]:
                    hit = True; hits += 1
                    d0 = dx * (prev[2] - ly1) - dy * (prev[1] - lx1)
                    d1 = dx * (r["by"] - ly1) - dy * (r["cx"] - lx1)
                    k = d0 / (d0 - d1) if d0 != d1 else 0
                    ix = prev[1] + (r["cx"] - prev[1]) * k
                    iy = prev[2] + (r["by"] - prev[2]) * k
                    t = ((ix - lx1) * dx + (iy - ly1) * dy) / L2
                    if t < 0:
                        before += 1
                    elif t > 1:
                        after += 1
                    else:
                        inside += 1
                        heights.append(r["h"])
                        headings.append((r["cx"] - prev[1], r["by"] - prev[2]))
                prev = (s, r["cx"], r["by"])
            if not hit:
                never += 1
            else:
                crossers += 1
                if hits > 1:
                    recrossers += 1

        # A line lying ALONG the traffic is the failure that looks fine on a still frame.
        # Measured PER CROSSING and folded to 0-90 before averaging: traffic runs both
        # ways across a count line, so averaging the raw heading vectors cancels the two
        # directions against each other and reports a good line as nearly parallel.
        angle = None
        if headings:
            import math
            angs = []
            for hx, hy in headings:
                if not (hx or hy):
                    continue
                a = abs(math.degrees(math.atan2(dx * hy - dy * hx, dx * hx + dy * hy)))
                angs.append(min(a, 180 - a))           # 90 = square to the traffic
            if angs:
                angs.sort()
                angle = round(angs[len(angs) // 2], 1)
        med_h = sorted(heights)[len(heights) // 2] if heights else None

        findings = []
        if not inside:
            findings.append(("bad", "No tracked vehicle crosses this line — it is not on "
                                    "the traffic's path."))
        if before or after:
            findings.append(("bad", f"{before + after} crossing(s) fall past the ends — the "
                                    f"line is too short for the carriageway."))
        # NOT judged on the image-space angle. A camera looking down a road sees vehicles
        # travelling almost horizontally, so a line correctly drawn across the carriageway
        # is also almost horizontal -- measured on KDP: line at -5deg, traffic at -6deg and
        # +15deg, and it counts 59 vehicles cleanly. Flagging that as "nearly parallel"
        # was a false alarm. The real symptom of a badly angled or jittery line is a
        # vehicle crossing it more than once, so that is what gets measured.
        if inside and recrossers:
            pct = 100.0 * recrossers / max(crossers, 1)
            if pct > 15:
                findings.append(("bad", f"{recrossers} vehicle(s) cross this line more than "
                                        f"once ({pct:.0f}%) — it is catching jitter rather "
                                        f"than a clean pass. Move it where vehicles are "
                                        f"travelling squarely across."))
            elif pct > 5:
                findings.append(("warn", f"{recrossers} vehicle(s) cross more than once "
                                         f"({pct:.0f}%). The debounce cancels these, but a "
                                         f"steadier spot would count more directly."))
        if med_h is not None and med_h < 30:
            findings.append(("warn", f"Vehicles are only {med_h:.0f} px tall where they "
                                     f"cross — too small to classify reliably. Move the "
                                     f"line nearer the camera."))
        if not findings:
            findings.append(("ok", f"{inside} vehicle(s) cross this line, all within the "
                                   f"drawn span."))
        out.append({"name": ln.get("name"), "crossings": inside,
                    "vehicles": crossers, "recrossed": recrossers,
                    "past_ends": before + after, "never_crossed": never,
                    "angle_to_traffic": angle, "median_height_px": med_h,
                    "findings": [{"level": l, "text": t} for l, t in findings]})
    return {"ok": True, "checked_on": {"video_id": v["id"], "name": v["name"]}, "lines": out}


def set_default_line(site_id, lines):
    """One line per STATION, inherited by every video from it.

    The camera does not move between files, so the same line is correct for all of them.
    Storing it per video would mean drawing it 168 times for a single 7-day survey, and
    168 chances to place it slightly differently -- which would show up as drifting counts
    that look like traffic.
    """
    db.run("UPDATE sites SET default_line=?, line_set=? WHERE id=?",
           db.jdump(lines), time.time(), site_id)
    db.log(None, "line", f"station {site_id}", f"{len(lines)} default line(s)")
    return {"ok": True, "lines": len(lines)}


def lines_for(video_id):
    """The line a video should be counted with, and where it came from.

    A per-video line always wins -- it is an explicit override for the case where one
    file genuinely differs (camera nudged, roadworks). Otherwise the station's default
    applies, which is the normal case.
    """
    sc = db.one("SELECT lines FROM scenes WHERE video_id=?", video_id)
    if sc:
        return db.jload(sc["lines"], []), "video"
    v = db.one("SELECT site_id FROM videos WHERE id=?", video_id)
    if v and v["site_id"]:
        ln = default_line(v["site_id"])
        if ln:
            return ln, "station"
    return [], "none"


# ────────────────────────── discovery and ingest ──────────────────────────
def camera_of(name):
    """`ch01_20250916143338.mp4` -> 'ch01'.

    No trailing \\b: the character after the camera id is an underscore, which is itself
    a word character, so a word boundary never occurs there and the pattern matched
    nothing at all.
    """
    m = re.search(r"(?<![a-z0-9])(ch\d{1,2})(?![0-9])", name, re.I)
    return m.group(1).lower() if m else None


def scan(roots=None, probe=True):
    """Find footage on disk and work out where and when each file belongs.

    Deliberately does NOT assign a station. `ch01` is a DVR channel number, not an
    identity -- every recorder has a ch01 -- so matching on it merges unrelated surveys.
    That is not hypothetical: nine files here carry the watermark
    "trichy to karikudi tk1" (Tamil Nadu) and were filed under Bhalki Junction
    (Karnataka), recorded ten months apart, purely because both cameras are "ch01".

    Files are grouped into SESSIONS instead -- one camera, one run of contiguous days --
    and a session is attached to a station by an explicit decision.
    """
    init()
    seen = {r["path"] for r in db.rows("SELECT path FROM lab_footage")}
    found, added = [], 0
    for root in (roots or FOOTAGE_ROOTS):
        root = Path(root)
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.mp4")):
            rec = {"path": str(p), "name": p.name, "camera": camera_of(p.name),
                   "size_mb": round(p.stat().st_size / 1e6, 1),
                   "known": str(p) in seen}
            clk = clock_from_name(p.name)
            rec["start_clock"] = clk.isoformat(" ") if clk else None
            rec["clock_source"] = "filename" if clk else "unknown"
            rec["site_id"] = None
            found.append(rec)
            if rec["known"]:
                continue
            if probe:
                info = ffprobe(str(p)) or {}
                rec.update({"dur_s": info.get("duration_s"), "fps": info.get("fps"),
                            "width": info.get("width"), "height": info.get("height")})
            db.run("""INSERT OR IGNORE INTO lab_footage
                      (site_id,path,name,camera,start_clock,clock_source,dur_s,size_mb,
                       fps,width,height,day,hour,discovered)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   rec["site_id"], rec["path"], rec["name"], rec["camera"],
                   rec["start_clock"], rec["clock_source"], rec.get("dur_s"),
                   rec["size_mb"], rec.get("fps"), rec.get("width"), rec.get("height"),
                   clk.strftime("%Y-%m-%d") if clk else None,
                   clk.hour if clk else None, time.time())
            added += 1
    dups = dedupe()
    n_sessions = group_sessions()
    return {"found": len(found), "added": added, "duplicates": dups,
            "sessions": n_sessions, "files": found}


def group_sessions(gap_days=2):
    """One camera plus one run of contiguous days = one survey session.

    A gap longer than `gap_days` starts a new session: a DVR channel reused ten months
    later is a different survey at a different place, and nothing in the filename says so.
    """
    from datetime import date
    init()
    n = 0
    for c in db.rows("SELECT DISTINCT camera FROM lab_footage WHERE camera IS NOT NULL"):
        rows = db.rows("""SELECT id, day FROM lab_footage
                          WHERE camera=? AND day IS NOT NULL ORDER BY day, id""",
                       c["camera"])
        cur, prev = None, None
        for r in rows:
            d = date.fromisoformat(r["day"])
            if prev is None or (d - prev).days > gap_days:
                cur = f"{c['camera']}:{r['day']}"
                n += 1
            prev = d
            db.run("UPDATE lab_footage SET session=? WHERE id=?", cur, r["id"])
    return n


def watermark(session, force=False):
    """Save the burned-in caption strip from a session's first file.

    DVR footage carries the survey name across the bottom of the frame -- "bhalki jn",
    "atp to tdp", "trichy to karikudi tk1". That caption is the only reliable statement of
    where the footage came from: filenames carry a channel number that every recorder
    reuses, which is how a Tamil Nadu survey ended up filed under a Karnataka junction.
    Showing the strip turns assigning a session from a guess into a reading.
    """
    import cv2
    init()
    row = db.one("""SELECT path FROM lab_footage WHERE session=? AND dup_of IS NULL
                    ORDER BY start_clock LIMIT 1""", session)
    if not row:
        return None
    out = ROOT / "lab_gold" / "_watermarks"
    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"{session.replace(':', '_')}.jpg"
    if dst.exists() and not force:
        return str(dst)
    cap = cv2.VideoCapture(row["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, 300)
    ok, img = cap.read()
    cap.release()
    if not ok or img is None:
        return None
    h, w = img.shape[:2]
    cv2.imwrite(str(dst), img[int(h * 0.85):, int(w * 0.45):],
                [cv2.IMWRITE_JPEG_QUALITY, 88])
    return str(dst)


def sessions():
    """Every survey session, with whatever station it has been confirmed to."""
    init()
    rows = db.rows("""SELECT session, camera, COUNT(*) files,
                             MIN(day) first_day, MAX(day) last_day,
                             ROUND(SUM(dur_s)/3600,1) hours,
                             MAX(site_id) site_id, MAX(site_confirmed) confirmed
                      FROM lab_footage
                      WHERE session IS NOT NULL AND dup_of IS NULL
                      GROUP BY session ORDER BY first_day DESC""")
    sites = {s["id"]: s for s in db.rows("SELECT id, code, name FROM sites")}
    for r in rows:
        st = sites.get(r["site_id"])
        r["station"] = f"{st['code']} · {st['name']}" if st else None
        r["watermark"] = bool(watermark(r["session"]))
    return rows


def assign_session(session, site_id):
    """Attach a whole session to a station -- the decision scan() deliberately will not make."""
    init()
    prev = db.one("SELECT MAX(site_id) s FROM lab_footage WHERE session=?", session)
    db.run("UPDATE lab_footage SET site_id=?, site_confirmed=1 WHERE session=?",
           site_id, session)
    n = db.one("SELECT COUNT(*) n FROM lab_footage WHERE session=?", session)["n"]
    # Logged because a mis-assigned session silently merges two surveys, and the only
    # reason the last one was caught was a gold-set frame that looked wrong.
    db.log(None, "assigned", f"session {session}",
           f"{n} file(s) -> site {site_id}" +
           (f" (was site {prev['s']})" if prev and prev["s"] and prev["s"] != site_id else ""))
    return {"session": session, "site_id": site_id, "files": n, "was": prev["s"] if prev else None}


def dedupe():
    """Mark same-camera, same-start-clock files as copies of one recording.

    The archive on the external drive and the local working folder hold the same footage
    under the same names, so a plain scan counts those hours twice. Coverage then reports
    107 minutes inside a 60-minute hour and -- far worse -- the same vehicles are counted
    twice in a report. That exact failure has happened here before (Bhalki read 558 from
    what was really one video), so it is handled during the scan rather than left to be
    noticed later.

    The lowest id wins and FOOTAGE_ROOTS puts the delivery drive first, so the archive
    copy stays canonical.
    """
    init()
    marked = 0
    rows = db.rows("""SELECT id, camera, path, start_clock, dur_s FROM lab_footage
                      WHERE start_clock IS NOT NULL AND camera IS NOT NULL
                      ORDER BY camera, start_clock, dur_s DESC""")
    spans = []
    for r in rows:
        try:
            t0 = datetime.fromisoformat(r["start_clock"])
        except (TypeError, ValueError):
            continue
        spans.append((r["id"], r["camera"], t0, t0 + timedelta(seconds=float(r["dur_s"] or 0))))

    for i, (rid, cam, a0, a1) in enumerate(spans):
        for pid, pcam, p0, p1 in spans:
            if pid == rid or pcam != cam:
                continue
            # Same recording twice, or a shorter clip cut out of a longer one. The second
            # case is the one that bites: the working excerpts here are named `_atp15` and
            # `_bhalki15` and are literally 15 minutes lifted out of the hour-long file, so
            # counting both reports the same vehicles twice.
            contained = p0 <= a0 + SEAM and a1 <= p1 + SEAM
            longer = (p1 - p0) > (a1 - a0) or (p1 - p0) == (a1 - a0) and pid < rid
            if contained and longer:
                db.run("UPDATE lab_footage SET dup_of=? WHERE id=?", pid, rid)
                marked += 1
                break
    return marked


def assign(paths, site_id):
    """Point files at a station by hand, for cameras whose id is not in the filename."""
    init()
    for p in paths:
        db.run("UPDATE lab_footage SET site_id=? WHERE path=?", site_id, p)
    return {"assigned": len(paths), "site_id": site_id}


def set_clock(path, iso):
    """Correct a start time by hand.

    Never guessed from mtime: a wrong start clock silently moves every vehicle into the
    wrong 15-minute block, and the report looks perfectly well-formed while being wrong.
    """
    clk = datetime.fromisoformat(iso)
    db.run("""UPDATE lab_footage SET start_clock=?, clock_source='manual', day=?, hour=?
              WHERE path=?""", clk.isoformat(" "), clk.strftime("%Y-%m-%d"), clk.hour, path)
    return {"ok": True, "start_clock": clk.isoformat(" ")}


# ─────────────────────────────── coverage ───────────────────────────────
def coverage(site_id):
    """Minutes of footage per day per hour -- the 7x24 grid a survey is judged on.

    Files are spread across the hours they actually span rather than counted at their
    start hour, because DVR chunks are 1-2 hours long and routinely cross a boundary.
    Counting a 17:50 file as "hour 17" would show a covered evening that is really a
    covered ten minutes.
    """
    # Counted per RECORDING, not per row. Two copies of the same hour on two drives is
    # one hour of road; and an hour still counts while any copy of it survives, so
    # deleting a working copy must not blank a grid cell the archive can still fill.
    # (The old query filtered `dup_of IS NULL`, which made both mistakes at once:
    # whichever copy scan() happened to index first decided whether the hour existed.)
    raw = db.rows("SELECT * FROM lab_footage WHERE site_id=?", site_id)
    folder = _folder_of(site_id)
    for r in raw:
        p = Path(r["path"])
        r["in_folder"] = bool(folder) and str(p.parent) == folder
        r["on_disk"] = p.exists()
    # Scoped to the station's folder, exactly as audit() is: the grid and the totals
    # beside it have to describe the same footage.
    if folder:
        raw = [r for r in raw if r["in_folder"]]
    # Same hold-out as audit(): a part-copied file has a part-length, and a grid drawn
    # from part-lengths is worse than one with a visible hole.
    raw = [r for r in raw if (r["dur_s"] or 0) or not r["on_disk"]]
    recs, _ = _recordings(raw)
    grid = {}
    for r in [x for x in recs if x["present"]]:
        try:
            t0 = datetime.fromisoformat(r["start"])
        except (TypeError, ValueError):
            continue
        left = float(r["minutes"] or 0) * 60
        while left > 0:
            day, hour = t0.strftime("%Y-%m-%d"), t0.hour
            to_hour_end = 3600 - (t0.minute * 60 + t0.second)
            used = min(left, to_hour_end)
            grid.setdefault(day, {})
            grid[day][hour] = round(grid[day].get(hour, 0) + used / 60, 1)
            left -= used
            t0 += timedelta(seconds=used)
    days = sorted(grid)
    return {
        "days": days,
        "grid": grid,
        "hours_covered": sum(1 for d in grid for h in grid[d] if grid[d][h] >= 55),
        "hours_partial": sum(1 for d in grid for h in grid[d] if 0 < grid[d][h] < 55),
        "total_hours": round(sum(v for d in grid for v in grid[d].values()) / 60, 1),
    }


def _folder_of(site_id):
    """The station's footage folder, or None if it has not really got one.

    Only an absolute path counts. ATP-01 has `footage_dir = "."` stored from an early
    attach, and a relative path means whatever directory the server happened to start
    in -- scoping a station to that would empty it, and scanning it once walked the
    whole project tree and filed 92 renders and segments as survey footage.
    """
    f = ((db.one("SELECT footage_dir FROM sites WHERE id=?", site_id) or {})
         .get("footage_dir") or "").rstrip("/")
    return f if f.startswith("/") else None


def _still_growing(path, wait=0.35):
    """True if the file is being written right now.

    Two stats a third of a second apart. A 1 GB copy onto an SSD takes seconds, so a
    Process pressed mid-copy would otherwise probe a partial file and record a partial
    duration as fact. Cheap enough to run per file, and only ever delays attaching a
    recording until the copy finishes.
    """
    try:
        a = path.stat().st_size
        if a == 0:
            return True
        time.sleep(wait)
        return path.stat().st_size != a
    except OSError:
        return False


def _span(r):
    """(start, end) of a recording, or None when its clock is unreadable."""
    try:
        t0 = datetime.fromisoformat(r["start_clock"])
    except (TypeError, ValueError):
        return None
    return t0, t0 + timedelta(seconds=float(r["dur_s"] or 0))


def reconcile(site_id, probe=True):
    """Bring the station back in step with its folder, then report what changed.

    The folder is the truth and it is not static: files arrive after the first attach,
    get deleted to free a drive, or are replaced by a re-export. So this is a diff, run
    as often as you like, never an initialisation:

      * in the folder, unknown to the DB   -> attached and probed
      * known, but the file is gone        -> flagged missing, never deleted, because
                                              clips and verdicts still reference it
      * size changed on disk               -> re-probed
      * a copy in the folder marked as a
        duplicate of one somewhere else    -> the folder copy becomes the primary

    That last rule is the one that was inverted. `dedupe()` keeps the lowest id and
    FOOTAGE_ROOTS indexes the archive drive first, so an explicit choice of station
    folder was always demoted to "duplicate" -- and duplicates are excluded from the
    file list and from this very reconcile, which is why deleting a file from the
    chosen folder changed nothing anywhere on screen.
    """
    folder = _folder_of(site_id)
    added, missing, changed, promoted, copying = [], [], [], [], []

    if folder and Path(folder).is_dir():
        known = {r["path"] for r in db.rows("SELECT path FROM lab_footage")}
        for p in sorted(Path(folder).iterdir()):
            if p.suffix.lower() not in (".mp4", ".avi", ".mkv") or str(p) in known:
                continue
            if _still_growing(p):
                copying.append(p.name)       # attach it on the next Process, finished
                continue
            info = (ffprobe(str(p)) or {}) if probe else {}
            clk = clock_from_name(p.name)
            db.run("""INSERT OR IGNORE INTO lab_footage
                      (site_id,path,name,camera,start_clock,clock_source,dur_s,size_mb,
                       fps,width,height,day,hour,discovered,site_confirmed)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                   site_id, str(p), p.name, camera_of(p.name),
                   clk.isoformat(" ") if clk else None,
                   "filename" if clk else "unknown",
                   info.get("duration_s"), round(p.stat().st_size / 1e6, 1),
                   info.get("fps"), info.get("width"), info.get("height"),
                   clk.strftime("%Y-%m-%d") if clk else None, clk.hour if clk else None,
                   time.time())
            added.append(p.name)

    # Presence and size, for every row this station owns -- wherever it lives.
    for r in db.rows("SELECT * FROM lab_footage WHERE site_id=?", site_id):
        f = Path(r["path"])
        if not f.exists():
            db.run("UPDATE lab_footage SET missing=1 WHERE id=?", r["id"])
            missing.append({"name": r["name"], "dir": str(f.parent)})
            continue
        db.run("UPDATE lab_footage SET missing=0 WHERE id=?", r["id"])
        size_mb = round(f.stat().st_size / 1e6, 1)
        if _still_growing(f):
            # Record the size so the next Process sees it settle, but do not probe:
            # a duration read now describes however much of the file has landed.
            db.run("UPDATE lab_footage SET size_mb=?, dur_s=NULL, fps=NULL WHERE id=?",
                   size_mb, r["id"])
            copying.append(r["name"])
            continue
        if probe and (abs((r["size_mb"] or 0) - size_mb) > 0.5
                      or not r["fps"] or not r["dur_s"]):
            info = ffprobe(r["path"]) or {}
            if info:
                db.run("""UPDATE lab_footage SET fps=?, dur_s=?, width=?, height=?,
                          size_mb=? WHERE id=?""",
                       info.get("fps"), info.get("duration_s"), info.get("width"),
                       info.get("height"), size_mb, r["id"])
                changed.append(r["name"])

    # Point every duplicate group at the copy in the station's own folder.
    rows = db.rows("SELECT * FROM lab_footage WHERE site_id=?", site_id)
    for r in rows:
        p = Path(r["path"])
        r["in_folder"] = bool(folder) and str(p.parent) == folder
        r["on_disk"] = p.exists()
    for rec in _recordings(rows)[0]:
        head = rec["primary"]
        if head["dup_of"] is not None:
            db.run("UPDATE lab_footage SET dup_of=NULL WHERE id=?", head["id"])
            promoted.append(head["name"])
        for spare in rec["spares"]:
            if spare["dup_of"] != head["id"]:
                db.run("UPDATE lab_footage SET dup_of=? WHERE id=?", head["id"], spare["id"])

    a = audit(site_id)
    a.update({"added": added, "missing_now": missing, "changed": changed,
              "promoted": promoted, "copying": copying})
    if added or missing or changed or promoted or copying:
        db.log(None, "reconciled", f"station {site_id}",
               f"{len(added)} added · {len(missing)} missing · {len(changed)} re-probed"
               + (f" · {len(promoted)} promoted" if promoted else "")
               + (f" · {len(copying)} still copying" if copying else ""))
    return a


def _recordings(rows):
    """Collapse footage rows into RECORDINGS: one per stretch of time, however many
    copies of it exist on however many drives.

    Identity is the interval covered, not the filename or the drive. The same hour
    copied onto a working SSD is the same hour of road, and counting it twice inflates
    the station total and its coverage grid alike. Returns (recordings, undated).
    """
    groups, undated = {}, []
    for r in rows:
        sp = _span(r)
        if not sp:
            undated.append(r)
            continue
        groups.setdefault((sp[0].isoformat(), round((r["dur_s"] or 0) / 60)), []).append(r)
    # Absorb any group whose span sits inside another's. A 15-minute excerpt cut out of
    # an hour-long recording is not a second recording -- counting it as one added its
    # minutes to the station total a second time. Longest first, so the container is
    # always established before anything that might belong to it.
    ordered = sorted(groups.items(), key=lambda kv: -kv[1][0]["dur_s"] if kv[1][0]["dur_s"] else 0)
    kept = []
    for key, copies in ordered:
        a0, a1 = _span(copies[0])
        for k_key, k_copies in kept:
            p0, p1 = _span(k_copies[0])
            if p0 <= a0 + SEAM and a1 <= p1 + SEAM:
                k_copies.extend(copies)          # an excerpt of that recording
                break
        else:
            kept.append((key, copies))

    out = []
    for (start, minutes), copies in sorted(kept):
        # The station's own folder holds the primary copy; everything else is a spare.
        # That is the opposite of what dedupe() decided by scan order, and deliberately
        # so: an explicit choice of folder must beat whichever drive was indexed first.
        copies.sort(key=lambda r: (not r.get("in_folder"), not r.get("on_disk"), r["id"]))
        head = copies[0]
        out.append({
            "name": head["name"], "start": start, "minutes": minutes,
            "end": _span(head)[1].isoformat(sep=" ", timespec="seconds"),
            "camera": head["camera"], "primary": head, "copies": copies,
            "n_copies": len(copies), "spares": copies[1:],
            # Present if ANY copy is on disk: losing the SSD working copy does not
            # lose the hour when the archive still has it.
            "present": any(c.get("on_disk") for c in copies),
            "in_folder": any(c.get("in_folder") for c in copies),
        })
    return out, undated


def audit(site_id):
    """What this station actually holds, where it is, and what is wrong with it.

    Written because the Overview page was assembling one sentence out of two unrelated
    facts -- "6 file(s) attached from /Volumes/MySSD/Station169" -- where the count came
    from the station's non-duplicate rows and the folder came from `sites.footage_dir`.
    The six files were on a different drive entirely, so deleting one from the named
    folder changed nothing on screen and there was no way to find that out.

    Everything here is derived from the rows plus the disk, and every number the page
    shows comes from this one function so the parts cannot disagree again.
    """
    s = db.one("SELECT camera_id FROM sites WHERE id=?", site_id) or {}
    folder = _folder_of(site_id)
    allrows = db.rows("SELECT * FROM lab_footage WHERE site_id=? ORDER BY start_clock, id",
                      site_id)
    for r in allrows:
        p = Path(r["path"])
        r["dir"] = str(p.parent)
        r["in_folder"] = bool(folder) and str(p.parent) == folder
        r["on_disk"] = p.exists()
        r["missing"] = 1 if not r["on_disk"] else 0
        r["minutes"] = round((r["dur_s"] or 0) / 60)

    # THE FOLDER IS THE STATION'S FOOTAGE. Rows attached to this station from anywhere
    # else are listed as something to resolve, never merged in and never counted -- an
    # earlier version treated a second location as "spare copies" of the same hours and
    # folded them into the totals, so the headline kept reporting an old archive's 9.3 h
    # while the folder actually held 4.3 h. A station with no folder set yet falls back
    # to everything attached, because otherwise it would read as empty.
    rows = [r for r in allrows if r["in_folder"]] if folder else allrows
    outside = [{"id": r["id"], "name": r["name"], "dir": r["dir"], "minutes": r["minutes"],
                "on_disk": r["on_disk"], "start": r["start_clock"]}
               for r in allrows if folder and not r["in_folder"]]

    # A file still being copied into the folder is NOT footage yet. Probing a half-written
    # 1 GB file returns whatever duration has landed so far -- one recording read 37 min
    # instead of 81 -- and that wrong length flows straight into coverage, the gap list
    # and the 15-minute bins. So anything with no readable duration, or nothing on disk
    # yet, is held out of every total until a later Process finds it finished.
    incomplete = [{"name": r["name"], "size_mb": r["size_mb"],
                   "why": "zero bytes — copy has not started or failed"
                          if not (r["size_mb"] or 0) else
                          "no readable duration — still being written, or not a video"}
                  for r in rows if r["on_disk"] and not (r["dur_s"] or 0)]
    _bad = {i["name"] for i in incomplete}
    rows = [r for r in rows if r["name"] not in _bad or not r["on_disk"]]

    recordings, undated = _recordings(rows)

    # ── gaps and overlaps across the survey ───────────────────────────────────────
    gaps, overlaps = [], []
    for a, b in zip(recordings, recordings[1:]):
        a_end, b_start = datetime.fromisoformat(a["end"]), datetime.fromisoformat(b["start"])
        if b_start - a_end > SEAM:
            gaps.append({"from": a["end"], "to": b["start"],
                         "minutes": round((b_start - a_end).total_seconds() / 60)})
        elif a_end - b_start > SEAM:
            overlaps.append({"a": a["name"], "b": b["name"],
                             "minutes": round((a_end - b_start).total_seconds() / 60)})

    # ── footage that may not belong to this station ───────────────────────────────
    # The DVR channel is the only cheap signal, and it is weak on its own -- every
    # recorder has a ch01 -- so a camera mismatch is reported as a question, never
    # acted on. A recording days away from the rest of the survey is the other tell.
    cams = [r["camera"] for r in recordings if r["camera"]]
    main_cam = max(set(cams), key=cams.count) if cams else None
    days = sorted({r["start"][:10] for r in recordings})
    main_days = set(days)
    if len(days) > 1:
        # days more than 2 apart from every other day are their own survey
        main_days = {d for d in days
                     if any(d != o and abs((datetime.fromisoformat(d)
                                            - datetime.fromisoformat(o)).days) <= 2
                            for o in days)} or set(days)
    foreign = [{"name": r["name"], "camera": r["camera"], "start": r["start"],
                "why": ("camera %s, this station is %s" % (r["camera"], main_cam))
                       if main_cam and r["camera"] != main_cam
                       else "recorded %s, away from the rest of the survey" % r["start"][:10]}
               for r in recordings
               if (main_cam and r["camera"] and r["camera"] != main_cam)
               or r["start"][:10] not in main_days]

    # A folder can hold files nobody has attached yet -- that is the "I added footage"
    # case, and it has to be visible without pressing anything.
    unattached = []
    if folder and Path(folder).is_dir():
        known = {r["path"] for r in db.rows("SELECT path FROM lab_footage")}
        for p in sorted(Path(folder).glob("*.mp4")) + sorted(Path(folder).glob("*.avi")) \
                + sorted(Path(folder).glob("*.mkv")):
            if str(p) not in known:
                unattached.append({"name": p.name, "path": str(p),
                                   "size_mb": round(p.stat().st_size / 1e6, 1)})

    # ── a station with footage but no folder recorded ─────────────────────────────
    # Most stations predate the folder concept: they were attached by scan + session
    # assignment, so `footage_dir` is empty while footage plainly exists, and the card
    # asks you to complete "step 1" underneath a list of attached files.
    #
    # The folder is usually inferrable -- all the files sit in one directory -- but
    # offering that blindly is dangerous. /Volumes/RK/Traffic holds BHK's footage, ATP's
    # footage AND an unassigned Tamil Nadu survey, and adopting it as a station folder
    # would make the next reconcile attach every one of them to that station. So the hint
    # is only offered when the directory belongs to this station alone.
    hint = None
    if not folder and allrows:
        dirs = {}
        for r in allrows:
            dirs.setdefault(r["dir"], []).append(r)
        if len(dirs) == 1:
            d = next(iter(dirs))
            others = db.rows("""SELECT DISTINCT s.code FROM lab_footage f
                                JOIN sites s ON s.id = f.site_id
                                WHERE f.site_id <> ? AND f.path LIKE ?""",
                             site_id, d + "/%")
            strays = db.one("""SELECT COUNT(*) n FROM lab_footage
                               WHERE site_id IS NULL AND path LIKE ?""", d + "/%")["n"]
            hint = {"dir": d, "files": len(dirs[d]),
                    "shared_with": [o["code"] for o in others], "unassigned": strays,
                    "exclusive": not others and not strays}
        else:
            hint = {"dir": None, "dirs": sorted(dirs), "exclusive": False,
                    "shared_with": [], "unassigned": 0, "files": len(allrows)}

    present = [r for r in recordings if r["present"]]
    total_min = sum(r["minutes"] for r in present)
    span = None
    if present:
        span = {"from": present[0]["start"], "to": present[-1]["end"]}
    return {
        "folder": folder or None,
        "folder_exists": bool(folder) and Path(folder).is_dir(),
        # Included so the page needs exactly one request: a grid fetched separately is
        # a grid that can disagree with the totals beside it.
        "coverage": coverage(site_id),
        "folder_hint": hint,
        "recordings": recordings,
        "incomplete": incomplete,
        "outside": outside,
        "unattached": unattached,
        "gaps": gaps,
        "overlaps": overlaps,
        "duplicates": [{"name": r["name"], "start": r["start"], "minutes": r["minutes"],
                        "where": [c["dir"] for c in r["copies"]],
                        "in_folder": r["in_folder"]}
                       for r in recordings if r["n_copies"] > 1],
        "foreign": foreign,
        "undated": [{"name": r["name"], "path": r["path"]} for r in undated],
        "missing": [{"name": r["name"], "path": r["path"], "dir": r["dir"]}
                    for r in rows if not r["on_disk"]],
        "totals": {
            # De-duplicated: recordings, not rows. Six rows that are three recordings
            # copied twice is three recordings.
            "recordings": len(recordings), "present": len(present),
            "files": len(rows), "hours": round(total_min / 60, 1),
            "days": len(days), "span": span, "camera": main_cam,
        },
    }


def suggest_sample(site_id, n_windows=6):
    """Pick sample windows that span the day rather than clustering in one hour.

    Traffic composition changes completely between the morning peak, midday, the evening
    peak and night, so a sample drawn from a single window trains a model that has never
    seen headlights. Spreading across time-of-day bands costs nothing and is the whole
    difference between a station model that works at 03:00 and one that does not.
    """
    rows = db.rows("""SELECT path, name, start_clock, dur_s FROM lab_footage
                      WHERE site_id=? AND start_clock IS NOT NULL AND dur_s > 0
                        AND dup_of IS NULL
                      ORDER BY start_clock""", site_id)
    bands = [("night", 0, 6), ("morning", 6, 11), ("midday", 11, 16),
             ("evening", 16, 20), ("late", 20, 24)]
    picked, by_band = [], {}
    for r in rows:
        try:
            h = datetime.fromisoformat(r["start_clock"]).hour
        except (TypeError, ValueError):
            continue
        band = next((b[0] for b in bands if b[1] <= h < b[2]), "night")
        by_band.setdefault(band, []).append(r)
    # Round-robin across bands so a band with many files cannot crowd out the others.
    order = [b[0] for b in bands]
    i = 0
    while len(picked) < n_windows and any(by_band.get(b) for b in order):
        b = order[i % len(order)]
        if by_band.get(b):
            r = by_band[b].pop(0)
            picked.append({**r, "band": b})
        i += 1
    return picked
