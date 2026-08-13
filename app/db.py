"""TrafficLens (personal edition) - SQLite store: videos, scenes, trajectories, jobs."""
import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "trafficlens.db"
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, fps REAL, frames INTEGER,
  width INTEGER, height INTEGER, start_clock TEXT, quality TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS scenes (
  video_id INTEGER PRIMARY KEY, lines TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS tracks (
  video_id INTEGER, track_id INTEGER, cls INTEGER, cls_votes TEXT,
  class_override INTEGER, t_start INTEGER, t_end INTEGER, n_points INTEGER,
  model_id TEXT, dup_of INTEGER, PRIMARY KEY (video_id, track_id)
);
CREATE TABLE IF NOT EXISTS track_points (
  video_id INTEGER, track_id INTEGER, frame INTEGER,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL, conf REAL
);
CREATE INDEX IF NOT EXISTS idx_points ON track_points (video_id, track_id, frame);
CREATE TABLE IF NOT EXISTS box_reviews (
  video_id INTEGER, track_id INTEGER, frame INTEGER, verdict TEXT, new_class INTEGER, ts REAL,
  PRIMARY KEY (video_id, track_id)
);
CREATE TABLE IF NOT EXISTS judgments (
  video_id INTEGER, track_id INTEGER, model TEXT, judged TEXT,
  PRIMARY KEY (video_id, track_id, model)
);
CREATE TABLE IF NOT EXISTS track_attrs (
  video_id INTEGER, track_id INTEGER, attr TEXT, value TEXT, source TEXT,
  PRIMARY KEY (video_id, track_id, attr)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, video_id INTEGER, kind TEXT, status TEXT,
  progress REAL, message TEXT, started REAL, finished REAL
);
-- A location. Counts from different roads are different surveys and must never be
-- summed together, so every video belongs to exactly one site.
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE,          -- short id used in reports, e.g. 'BHK-01'
  name TEXT,                 -- 'Bhalki Junction'
  road_name TEXT, road_ref TEXT, chainage TEXT,
  district TEXT, state TEXT,
  camera_id TEXT,            -- 'ch01' — lets DVR filenames self-assign
  carriageway TEXT, notes TEXT,
  created REAL, updated REAL
);
"""

# Geo layer, added later: coordinates plus the compass bearing the camera looks along.
# The bearing is what makes sun-glare prediction possible, and it is the thing a crew
# standing at the station can capture from a phone in one tap.
GEO_COLS = [("sites", "lat", "REAL"), ("sites", "lon", "REAL"),
            ("sites", "bearing", "REAL"), ("sites", "geo_source", "TEXT"),
            # deepest zoom with real imagery at this station — probed, because coverage
            # varies within a few km (z19 in Bidar city, z18 at Bhalki 40 km away)
            ("sites", "imagery_zoom", "INTEGER")]

# Columns added after the fact. CREATE TABLE IF NOT EXISTS will not add them, so they
# are applied here, idempotently, on first connection.
MIGRATIONS = [
    ("videos", "site_id", "INTEGER"),
    ("videos", "clock_source", "TEXT"),      # filename | manual | none
    # A video can be present but must not count: a duplicate of the same footage, a
    # corrupt file, a re-run kept for comparison. Excluding is reversible; deleting isn't.
    ("videos", "excluded", "INTEGER"),
    ("videos", "excluded_reason", "TEXT"),
    ("tracks", "dup_of", "INTEGER"),         # were previously ALTERed in by dedup.py
    ("tracks", "join_to", "INTEGER"),
    # The Lab added these to `sites` with a bare ALTER at runtime, so they exist on any
    # database the Lab has ever opened -- and on none that it has not. The survey app
    # needs all three (the footage folder, and the station's one count line), so on a
    # fresh install every attach and every line save failed with "no such column".
    # Declared here instead, where any app that opens the datastore gets them.
    ("sites", "footage_dir", "TEXT"),
    ("sites", "default_line", "TEXT"),
    ("sites", "line_set", "REAL"),
    # ── station-scoped attribute heads ──
    # A head is calibrated to one camera. "760px is too small to read axles" is true of
    # KDP-01's mounting height and lens, not of a station set further back where the same
    # truck is 500px; an LCV/Car boundary is that road's vehicle mix. Promoting a head
    # trained here to every station is how a local fix becomes a global regression.
    #
    # NULL means global, which is what every existing row becomes -- the axle head keeps
    # serving every station exactly as it does today, and only a station that trains its
    # own head overrides it, only for itself.
    ("lab_attr_models", "site_id", "INTEGER"),
    ("lab_attr_promotions", "site_id", "INTEGER"),
] + GEO_COLS


def _migrate(c):
    for table, col, decl in MIGRATIONS:
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        # PRAGMA on a table that does not exist returns nothing, which is indistinguishable
        # here from a table missing the column -- and ALTER would then fail on the missing
        # table. The lab_* tables are created lazily by the Lab's own init(), so on a fresh
        # database they are legitimately absent when the survey app first connects.
        if not have:
            continue
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    c.commit()


def conn():
    if not hasattr(_local, "c"):
        _local.c = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA journal_mode=WAL")
        # WAL lets readers run beside a writer, but two WRITERS still serialise -- and
        # without a busy timeout the loser raises "database is locked" immediately rather
        # than waiting. That is fine with one extraction at a time and fatal with several:
        # each one bulk-inserts thousands of track points, so they WILL collide.
        _local.c.execute("PRAGMA busy_timeout=30000")
        _local.c.executescript(SCHEMA)
        _migrate(_local.c)
    return _local.c


def rows(q, *a):
    return [dict(r) for r in conn().execute(q, a).fetchall()]


def one(q, *a):
    r = conn().execute(q, a).fetchone()
    return dict(r) if r else None


def run(q, *a):
    c = conn()
    cur = c.execute(q, a)
    c.commit()
    return cur.lastrowid


def runmany(q, seq):
    c = conn()
    c.executemany(q, seq)
    c.commit()


def jdump(x):
    return json.dumps(x)


def jload(s, default=None):
    try:
        return json.loads(s)
    except Exception:
        return default
