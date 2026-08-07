"""Locations.

Counts belong to a road, not to a hard drive. Two videos from different sites can never
be added together — a combined "traffic by hour" across locations is a meaningless number —
so every video is assigned to exactly one site and all aggregation is per-site.

Assignment is automatic where the footage says so (DVR files carry a camera id) and manual
otherwise. The same applies to the clock: read it from the filename when it is there, let
the user type it when it is not, and never invent one.
"""
import re
import time
from datetime import datetime

import db
import imagery
import solar

# Wall-clock patterns seen in this project's footage. Order matters: the most specific
# (camera + timestamp) is tried first.
CLOCK_PATTERNS = [
    ("dvr", re.compile(r"(?P<cam>ch\d{1,2})[_-](?P<ts>\d{14})(?!\d)"), "%Y%m%d%H%M%S"),
    ("date_time", re.compile(r"(?<!\d)(?P<ts>\d{8}[_-]\d{6})(?!\d)"), "%Y%m%d_%H%M%S"),
    ("compact", re.compile(r"(?<!\d)(?P<ts>\d{14})(?!\d)"), "%Y%m%d%H%M%S"),
]
MIN_YEAR, MAX_YEAR = 2000, datetime.now().year + 1


def parse_clock(name):
    """Read a start time out of a filename. Returns {clock, source, camera_id}.

    `clock` is None when nothing parses — the caller must then ask the user rather than
    substitute a placeholder, because a wrong start time silently corrupts every time bin
    in the report.
    """
    for label, rx, fmt in CLOCK_PATTERNS:
        m = rx.search(name)
        if not m:
            continue
        raw = m.group("ts").replace("-", "_")
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if not (MIN_YEAR <= dt.year <= MAX_YEAR):
            continue                      # a plausible-looking number that isn't a date
        return {"clock": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "source": f"filename:{label}",
                "camera_id": m.groupdict().get("cam")}
    return {"clock": None, "source": None, "camera_id": None}


def enrich(s):
    """Add what the coordinates and camera bearing imply: which way it faces, when the
    sun will be in the lens, and how long the daylight lasts."""
    s["compass"] = solar.compass(s.get("bearing"))
    z = s.get("imagery_zoom")
    if z and s.get("lat") is not None:
        s["imagery_zoom"] = z
        s["imagery_mpp"] = round(imagery.metres_per_pixel(s["lat"], z), 2)
    s["glare"] = solar.glare_windows(s.get("lat"), s.get("lon"), s.get("bearing"))
    s["daylight"] = solar.daylight(s.get("lat"), s.get("lon"))
    if s.get("lat") is not None and s.get("lon") is not None:
        s["map_url"] = f"https://www.openstreetmap.org/?mlat={s['lat']}&mlon={s['lon']}#map=17/{s['lat']}/{s['lon']}"
    return s


def list_sites():
    out = db.rows("SELECT * FROM sites ORDER BY name")
    for s in out:
        s["videos"] = (db.one("SELECT COUNT(*) n FROM videos WHERE site_id=?", s["id"]) or {})["n"]
        enrich(s)
    return out


GEO_FIELDS = ("lat", "lon", "bearing", "geo_source", "imagery_zoom")


def refresh_imagery_zoom(site_id):
    """Probe how sharp the satellite imagery gets here and remember it.

    Runs once when a station gains coordinates. Cheap (a handful of tile fetches) and
    worth it: it is the difference between a station rendering at 0.28 m/px and the same
    station rendering a blurred 2.3 m/px tile because a global cap was set for the worst
    site on the list.
    """
    s = db.one("SELECT lat, lon FROM sites WHERE id=?", site_id)
    if not s or s["lat"] is None:
        return None
    z = imagery.deepest_zoom(s["lat"], s["lon"])
    if z:
        db.run("UPDATE sites SET imagery_zoom=? WHERE id=?", z, site_id)
    return z


def create(**kw):
    if not (kw.get("name") or "").strip():
        raise ValueError("a station needs a name")
    code = (kw.get("code") or "").strip() or _auto_code(kw["name"])
    if db.one("SELECT id FROM sites WHERE code=?", code):
        raise ValueError(f"station code {code} already exists")
    now = time.time()
    return db.run(
        """INSERT INTO sites (code,name,road_name,road_ref,chainage,district,state,
                              camera_id,carriageway,notes,lat,lon,bearing,geo_source,
                              created,updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        code, kw["name"].strip(), kw.get("road_name"), kw.get("road_ref"),
        kw.get("chainage"), kw.get("district"), kw.get("state"),
        (kw.get("camera_id") or "").strip() or None, kw.get("carriageway"),
        kw.get("notes"), kw.get("lat"), kw.get("lon"), kw.get("bearing"),
        kw.get("geo_source"), now, now)


def _auto_code(name):
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    stem = (letters[:3] or "SITE")
    n = 1
    while db.one("SELECT id FROM sites WHERE code=?", f"{stem}-{n:02d}"):
        n += 1
    return f"{stem}-{n:02d}"


def update(site_id, **kw):
    fields = [k for k in ("code", "name", "road_name", "road_ref", "chainage", "district",
                          "state", "camera_id", "carriageway", "notes") + GEO_FIELDS
              if k in kw]
    if not fields:
        return
    db.run(f"UPDATE sites SET {','.join(f'{f}=?' for f in fields)}, updated=? WHERE id=?",
           *[kw[f] for f in fields], time.time(), site_id)


def suggest_site(video_name):
    """A DVR file names its camera, and a site names its camera, so most footage can
    assign itself. Returns a site id or None — never a guess based on anything weaker."""
    cam = parse_clock(video_name).get("camera_id")
    if not cam:
        return None
    hit = db.one("SELECT id FROM sites WHERE camera_id=?", cam)
    return hit["id"] if hit else None


def assign(video_id, site_id):
    db.run("UPDATE videos SET site_id=? WHERE id=?", site_id, video_id)


def set_clock(video_id, clock, source="manual"):
    """Accepts 'YYYY-MM-DD HH:MM:SS' or the 'YYYY-MM-DDTHH:MM' an <input type=datetime-local>
    produces."""
    s = (clock or "").strip().replace("T", " ")
    if len(s) == 16:
        s += ":00"
    datetime.strptime(s, "%Y-%m-%d %H:%M:%S")        # raises if malformed
    db.run("UPDATE videos SET start_clock=?, clock_source=? WHERE id=?", s, source, video_id)
    return s


def lines_for(video_id):
    """The count line for a video: its own override, else its station's default.

    Both apps must answer this the same way or they disagree about the count. The Lab
    introduced station default lines -- one line per camera, inherited by all 168 files of
    a 7-day survey rather than drawn 168 times -- and the survey app kept reading only the
    per-video `scenes` row. A video on a station line therefore reported zero vehicles
    here while counting correctly in the Lab, which reads as a broken pipeline rather than
    a missing lookup.

    Returns (lines, source) where source is 'video', 'station' or 'none', because
    "which line produced this number" is a question a report has to be able to answer.
    """
    sc = db.one("SELECT lines FROM scenes WHERE video_id=?", video_id)
    if sc and sc["lines"]:
        ln = db.jload(sc["lines"], [])
        if ln:
            return ln, "video"
    v = db.one("SELECT site_id FROM videos WHERE id=?", video_id)
    if v and v["site_id"]:
        r = db.one("SELECT default_line FROM sites WHERE id=?", v["site_id"])
        if r and r["default_line"]:
            ln = db.jload(r["default_line"], [])
            if ln:
                return ln, "station"
    return [], "none"
