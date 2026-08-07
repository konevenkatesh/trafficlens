"""Lab datastore. Shares trafficlens.db with the counting app so lab runs can
reference existing videos/tracks, but owns every lab_* table."""
import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "app" / "trafficlens.db"
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_settings (
  key TEXT PRIMARY KEY, value TEXT, updated REAL);

CREATE TABLE IF NOT EXISTS lab_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, source_path TEXT, status TEXT DEFAULT 'draft',
  config TEXT, created REAL, finished REAL, note TEXT);

CREATE TABLE IF NOT EXISTS lab_stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, stage TEXT, status TEXT DEFAULT 'pending',
  progress REAL DEFAULT 0, message TEXT, started REAL, finished REAL,
  cost_usd REAL DEFAULT 0, meta TEXT);

CREATE TABLE IF NOT EXISTS lab_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, idx INTEGER, name TEXT, path TEXT,
  start_s REAL, dur_s REAL, size_mb REAL, frames INTEGER, fps REAL,
  width INTEGER, height INTEGER, grade TEXT, quality TEXT,
  video_id INTEGER, status TEXT DEFAULT 'ready', compressed_path TEXT,
  compressed_mb REAL);

CREATE TABLE IF NOT EXISTS lab_crops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, segment_id INTEGER, video_id INTEGER, track_id INTEGER,
  frame INTEGER, x1 REAL, y1 REAL, x2 REAL, y2 REAL,
  det_class INTEGER, det_conf REAL, crop_path TEXT, ctx_path TEXT,
  final_class INTEGER, state TEXT DEFAULT 'new', human_class INTEGER,
  agree_n INTEGER DEFAULT 0, created REAL);

CREATE TABLE IF NOT EXISTS lab_judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  crop_id INTEGER, run_id INTEGER, model TEXT, verdict INTEGER,
  verdict_name TEXT, confidence REAL, raw TEXT,
  in_tokens INTEGER, out_tokens INTEGER, cost_usd REAL,
  latency_ms INTEGER, error TEXT, created REAL);

CREATE TABLE IF NOT EXISTS lab_costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, provider TEXT, stage TEXT, item TEXT,
  qty REAL, unit TEXT, usd REAL, ts REAL, meta TEXT);

CREATE TABLE IF NOT EXISTS lab_pods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, pod_id TEXT, name TEXT, gpu TEXT, hourly REAL,
  status TEXT, created REAL, terminated REAL, cost_usd REAL DEFAULT 0,
  ssh TEXT, telemetry TEXT, purpose TEXT);

CREATE TABLE IF NOT EXISTS lab_evals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, kind TEXT, model TEXT, n INTEGER,
  accuracy REAL, cost_usd REAL, usd_per_1k REAL, latency_ms REAL,
  detail TEXT, created REAL);

CREATE TABLE IF NOT EXISTS lab_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, verb TEXT, object TEXT, detail TEXT, ts REAL);

CREATE INDEX IF NOT EXISTS ix_stage_run ON lab_stages(run_id);
CREATE INDEX IF NOT EXISTS ix_seg_run ON lab_segments(run_id);
CREATE INDEX IF NOT EXISTS ix_crop_run ON lab_crops(run_id, state);
CREATE INDEX IF NOT EXISTS ix_judge_crop ON lab_judgments(crop_id);
CREATE INDEX IF NOT EXISTS ix_cost_run ON lab_costs(run_id);
CREATE INDEX IF NOT EXISTS ix_event_run ON lab_events(run_id, ts);
"""


_wal_set = False


def conn():
    """Thread-local connection. journal_mode is set once per process: re-running
    that pragma on every new connection can stall on a large WAL file."""
    global _wal_set
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(str(DB), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        if not _wal_set:
            c.execute("PRAGMA journal_mode=WAL")
            _wal_set = True
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def run(sql, *a):
    c = conn()
    cur = c.execute(sql, a)
    c.commit()
    return cur.lastrowid


def runmany(sql, rows):
    c = conn()
    c.executemany(sql, rows)
    c.commit()


def rows(sql, *a):
    return [dict(r) for r in conn().execute(sql, a).fetchall()]


def one(sql, *a):
    r = conn().execute(sql, a).fetchone()
    return dict(r) if r else None


def jdump(o):
    return json.dumps(o, default=str)


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


# ── settings (API keys live here; file-based keys are imported on first boot) ──
def get_setting(key, default=None):
    r = one("SELECT value FROM lab_settings WHERE key=?", key)
    return r["value"] if r else default


def set_setting(key, value):
    run("INSERT INTO lab_settings (key,value,updated) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
        key, value, time.time())


def seed_keys_from_disk():
    """First boot: adopt the keys already on disk so the Lab works immediately."""
    for key, path in (("openrouter_key", Path.home() / ".openrouter" / "key"),
                      ("runpod_key", Path.home() / ".runpod" / "key")):
        if not get_setting(key) and path.exists():
            v = path.read_text().strip()
            if v:
                set_setting(key, v)


def log(run_id, verb, obj, detail=""):
    run("INSERT INTO lab_events (run_id,verb,object,detail,ts) VALUES (?,?,?,?,?)",
        run_id, verb, obj, detail, time.time())


def charge(run_id, provider, stage, item, qty, unit, usd, meta=None):
    """Every cent the Lab spends passes through here."""
    run("INSERT INTO lab_costs (run_id,provider,stage,item,qty,unit,usd,ts,meta) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        run_id, provider, stage, item, qty, unit, usd, time.time(), jdump(meta or {}))
    if stage:
        run("UPDATE lab_stages SET cost_usd=COALESCE(cost_usd,0)+? "
            "WHERE run_id=? AND stage=?", usd, run_id, stage)
