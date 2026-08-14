"""Detect on a rented GPU instead of this machine.

A surveyor's laptop does roughly 3 fps on the processor. A 24-hour station day is a
million frames, which is four days of waiting. The same work on a rented RTX 4090 is
about three and a half hours. That is the whole reason this module exists.

**Pods, not serverless.** Serverless would scale to zero on its own, but it needs a
container image of about six gigabytes built and pushed to a registry every time the
detector changes -- a second build pipeline to maintain. A pod needs none of that: create
it, send the code and the weights, run, delete. The one thing serverless gives for free is
that you cannot forget a running worker, so that guarantee is built here instead, three
ways over (see `_watchdog`).

**Money is the dangerous part, so it is the part with the most machinery.** A GPU left
running overnight costs more than the survey earns. Every pod this module starts is
recorded before it exists and reconciled after it dies; spending is checked against a
limit before anything is created; and a watchdog kills pods that nobody is using even if
the app crashes and forgets them.

Nothing here is required. With no key configured the app behaves exactly as it does now,
detecting locally, and every screen still works.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

RP_REST = "https://rest.runpod.io/v1"
RP_GQL = "https://api.runpod.io/graphql"

# RunPod sits behind Cloudflare, which rejects urllib's default User-Agent outright --
# it comes back 403, which reads exactly like a bad API key and sent me looking at the
# key first. The Lab hit this once already and left a note; this is that note obeyed.
UA = "TrafficLens/1.0 (+survey)"

# A pod with no work is pure loss. Five minutes is long enough to ride out the gap between
# one clip finishing and the next starting, short enough that a forgotten pod costs pennies
# rather than a night's rent.
IDLE_SECONDS = 300

# The least a pod can usefully cost: image pull, boot, and one clip. A pod that cannot
# pay for this out of what is left of the monthly limit is refused rather than started
# and then killed a minute later, having spent money and produced nothing.
MIN_RUN_SECONDS = 900

SCHEMA = """
-- One row per pod, written BEFORE the pod is created and updated as it lives and dies.
-- Written first on purpose: if the create call succeeds and the app dies before recording
-- it, the pod exists and nothing knows to stop it. A row for a pod that never started is
-- harmless; a pod with no row bills until somebody notices.
CREATE TABLE IF NOT EXISTS cloud_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pod_id TEXT, gpu TEXT, cost_per_hr REAL,
  started REAL, stopped REAL, seconds REAL, usd REAL,
  status TEXT,                 -- starting | running | stopped | failed
  clips INTEGER DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS ix_cloud_started ON cloud_runs(started);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ───────────────────────────── settings ─────────────────────────────
# Global settings live in lab_site_settings with site_id NULL, and that combination is a
# trap: the table's PRIMARY KEY is (site_id, key), but SQLite treats every NULL as
# distinct, so ON CONFLICT never fires on a global row. Each save INSERTed a new row and
# each read returned the oldest one -- seven rows for one setting, and a surveyor raising
# their spend limit was silently ignored. Delete-then-insert is the only correct upsert
# here, and reads take the newest as a belt-and-braces measure.
def _setting(key, default=None):
    init()
    r = db.one("""SELECT value FROM lab_site_settings
                  WHERE site_id IS NULL AND key=? ORDER BY updated DESC LIMIT 1""", key)
    return r["value"] if r else default


def _set(key, value):
    init()
    db.run("DELETE FROM lab_site_settings WHERE site_id IS NULL AND key=?", key)
    db.run("""INSERT INTO lab_site_settings (site_id,key,value,updated)
              VALUES (NULL,?,?,?)""", key, str(value), time.time())


def config():
    """What the Settings screen shows. The key itself is never returned."""
    key = _setting("runpod_key", "") or ""
    return {
        "configured": bool(key),
        "key_hint": (key[:7] + "…" + key[-4:]) if len(key) > 14 else ("set" if key else ""),
        "gpu": _setting("runpod_gpu", "NVIDIA GeForce RTX 4090"),
        "monthly_limit_usd": float(_setting("runpod_limit_usd", "25") or 25),
        "enabled": (_setting("runpod_enabled", "0") == "1") and bool(key),
        "idle_seconds": IDLE_SECONDS,
    }


def save_config(key=None, gpu=None, limit_usd=None, enabled=None):
    if key is not None:
        _set("runpod_key", key.strip())
    if gpu:
        _set("runpod_gpu", gpu)
    if limit_usd is not None:
        _set("runpod_limit_usd", float(limit_usd))
    if enabled is not None:
        _set("runpod_enabled", "1" if enabled else "0")
    return config()


# ───────────────────────────── the API ─────────────────────────────
def _key():
    return _setting("runpod_key", "") or ""


def _gql(query, variables=None, timeout=45):
    k = _key()
    if not k:
        return None, "no RunPod key saved"
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        RP_GQL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Authorization": f"Bearer {k}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return None, f"RunPod returned {e.code}"
    except Exception as e:
        return None, f"cannot reach RunPod: {type(e).__name__}"
    if d.get("errors"):
        return None, str(d["errors"][0].get("message", "unknown error"))[:200]
    return d.get("data"), None


def status():
    """Is the key good, what does it cost, and is anything running right now.

    One call the Settings screen can poll. Everything a person needs to trust the
    connection -- that the key works, what the balance is, whether a pod is burning money
    at this second -- rather than three separate things to check.
    """
    cfg = config()
    out = {**cfg, "ok": False, "balance_usd": None, "gpu_price": None,
           "running": [], "error": None}
    # `spend` is filled in on every path, including the failure ones. A caller that has
    # to check whether a key exists before it can read a field will eventually forget.
    out["spend"] = spend()
    if not cfg["configured"]:
        out["error"] = "no API key saved"
        return out

    d, err = _gql("query { myself { clientBalance } }")
    if err:
        out["error"] = err
        return out
    out["ok"] = True
    out["balance_usd"] = round((d.get("myself") or {}).get("clientBalance") or 0, 2)

    d, _ = _gql("""query { gpuTypes { displayName
                     lowestPrice(input:{gpuCount:1}) { uninterruptablePrice } } }""")
    for g in ((d or {}).get("gpuTypes") or []):
        if g.get("displayName") and g["displayName"] in cfg["gpu"]:
            out["gpu_price"] = (g.get("lowestPrice") or {}).get("uninterruptablePrice")
            break

    out["running"] = live_pods()
    out["spend"] = spend()          # recomputed now that live pods are known
    return out


def live_pods():
    """Pods that exist on RunPod right now, from RunPod's own view.

    Asked of the provider, never of the local table. The table records what this app
    believes it started; the provider knows what is actually billing. When those disagree
    -- app crashed mid-run, someone started a pod by hand -- the provider is right, and
    the difference is exactly the money nobody is watching.
    """
    d, err = _gql("""query { myself { pods { id name desiredStatus costPerHr
                                              runtime { uptimeInSeconds } } } }""")
    if err:
        return []
    out = []
    for p in ((d or {}).get("myself") or {}).get("pods") or []:
        rt = p.get("runtime") or {}
        up = rt.get("uptimeInSeconds") or 0
        out.append({"id": p["id"], "name": p.get("name"),
                    "status": p.get("desiredStatus"),
                    "cost_per_hr": p.get("costPerHr") or 0,
                    "uptime_s": up,
                    "spent_so_far": round((p.get("costPerHr") or 0) * up / 3600, 3)})
    return out


def terminate(pod_id):
    _, err = _gql("mutation ($i:String!) { podTerminate(input:{podId:$i}) }",
                  {"i": pod_id})
    db.run("""UPDATE cloud_runs SET status='stopped', stopped=?,
                seconds=? - started, usd=cost_per_hr * (? - started)/3600.0
              WHERE pod_id=? AND status!='stopped'""",
           time.time(), time.time(), time.time(), pod_id)
    return {"ok": err is None, "error": err}


def stop_all():
    """Kill everything. The button a person reaches for when they are not sure."""
    killed = []
    for p in live_pods():
        terminate(p["id"])
        killed.append(p["id"])
    return {"stopped": killed}


# ───────────────────────────── spending ─────────────────────────────
def spend():
    """What this has cost, from the local ledger plus anything still running.

    The month is calendar, matching how a budget is actually set. Pods still alive are
    counted at their cost so far, because a limit that only counts finished work lets one
    long run blow straight through it.
    """
    init()
    pods = live_pods()
    _close_stale({p["id"] for p in pods})
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()
    done = db.one("""SELECT COALESCE(SUM(usd),0) usd, COUNT(*) n FROM cloud_runs
                     WHERE started >= ? AND status='stopped'""", month_start) or {}
    live = sum(p["spent_so_far"] for p in pods)
    total = round((done.get("usd") or 0) + live, 3)
    limit = float(_setting("runpod_limit_usd", "25") or 25)
    return {"month_usd": total, "live_usd": round(live, 3),
            "runs_this_month": done.get("n") or 0,
            "limit_usd": limit, "remaining_usd": round(max(0.0, limit - total), 3),
            "over_limit": total >= limit}


def _close_stale(live_ids):
    """Close ledger rows whose pod no longer exists.

    Only rows marked 'stopped' count towards the month, so a run that ended without this
    process seeing it -- the app was killed, the pod was terminated from the RunPod
    dashboard, a create half-succeeded -- stayed open and contributed exactly nothing to
    the total. Money genuinely spent, invisible to the limit that is supposed to cap it.

    The end time is unknown, so it is taken as now. That over-states the cost of a pod
    that died hours ago, which is the right direction to be wrong in for a budget: it
    stops sooner rather than later, and the RunPod dashboard remains the authority on
    what was actually billed.
    """
    open_rows = db.rows("""SELECT id, pod_id, cost_per_hr, started FROM cloud_runs
                           WHERE status != 'stopped'""")
    now = time.time()
    for r in open_rows:
        if r["pod_id"] in live_ids:
            continue
        ran = max(0.0, now - (r["started"] or now))
        db.run("""UPDATE cloud_runs SET status='stopped', stopped=?, seconds=?, usd=?,
                    note = COALESCE(note,'') || ' · closed late; cost is an upper bound'
                  WHERE id=?""",
               now, round(ran, 1), round((r["cost_per_hr"] or 0) * ran / 3600.0, 4), r["id"])


def may_start():
    """Whether a new pod is allowed, and if not, exactly why.

    Checked before every create. A monthly limit that is only displayed is a limit that
    gets exceeded; this one refuses.
    """
    cfg = config()
    if not cfg["configured"]:
        return False, "no RunPod API key saved — add one in Settings"
    if not cfg["enabled"]:
        return False, "cloud detection is switched off in Settings"
    s = spend()
    if s["over_limit"]:
        return False, (f"this month's limit of ${s['limit_usd']:.2f} is used up "
                       f"(${s['month_usd']:.2f} spent). Raise it in Settings to continue.")
    st = status()
    if not st.get("ok"):
        return False, st.get("error") or "cannot reach RunPod"
    if (st.get("balance_usd") or 0) < 0.5:
        return False, f"RunPod balance is ${st['balance_usd']:.2f} — top it up to continue"

    # Enough headroom to be worth starting. "Not yet over the limit" is not the same as
    # "can afford this": with $0.01 left and a $0.34/hr card, the old check said yes and
    # the pod blew straight through the limit on its first minute. A pod that cannot pay
    # for its own boot should never be created.
    price = st.get("gpu_price") or 0
    if price:
        need = price * (MIN_RUN_SECONDS / 3600.0)
        if s["remaining_usd"] < need:
            return False, (f"only ${s['remaining_usd']:.2f} left of this month's "
                           f"${s['limit_usd']:.2f} limit — a {cfg['gpu']} costs "
                           f"${price:.2f}/hr and needs at least ${need:.2f} to be worth "
                           f"starting. Raise the limit in Settings.")
    return True, None


# ───────────────────────────── the watchdog ─────────────────────────────
# The single most expensive way this can fail is a pod nobody is using. Serverless would
# make that impossible; with pods it has to be enforced. Three independent guards, because
# any one of them can be defeated by the app dying at the wrong moment:
#
#   1. the queue drains  -> the pod is deleted immediately (the normal path)
#   2. this watchdog     -> deletes anything idle longer than IDLE_SECONDS
#   3. startup reconcile -> on launch, kills pods left over from a previous session
#
_LAST_WORK = [0.0]
_WATCH = None


def note_work():
    """Called whenever a clip starts or finishes on the cloud pod."""
    _LAST_WORK[0] = time.time()


def _watchdog():
    while True:
        time.sleep(30)
        try:
            pods = live_pods()
            if not pods:
                continue
            # Two reasons to kill, and the second one matters more. Idle is waste; over
            # the limit is the surveyor's stated ceiling being breached. The limit was
            # only ever checked BEFORE a pod started, so a long survey could sail past it
            # for hours -- the one thing the limit exists to prevent.
            over = spend()["over_limit"]
            idle = time.time() - (_LAST_WORK[0] or time.time())
            if not over and idle < IDLE_SECONDS:
                continue
            why = ("stopped: this month's spending limit was reached" if over
                   else "stopped by the idle watchdog")
            for p in pods:
                terminate(p["id"])
                db.run("""INSERT INTO cloud_runs (pod_id,gpu,cost_per_hr,started,stopped,
                            seconds,usd,status,note)
                          SELECT ?,?,?,?,?,?,?,'stopped',?
                          WHERE NOT EXISTS (SELECT 1 FROM cloud_runs WHERE pod_id=?)""",
                       p["id"], p.get("name"), p["cost_per_hr"],
                       time.time() - p["uptime_s"], time.time(), p["uptime_s"],
                       p["spent_so_far"], why, p["id"])
        except Exception:
            continue          # a watchdog that dies on a transient error is not a watchdog


def start_watchdog():
    global _WATCH
    if _WATCH and _WATCH.is_alive():
        return
    _WATCH = threading.Thread(target=_watchdog, daemon=True)
    _WATCH.start()


def reconcile_on_start():
    """Anything left running from a previous session is orphaned. Kill it and say so.

    The app closing does not stop a pod. Without this, quitting mid-run leaves a GPU
    billing until somebody opens the RunPod dashboard.
    """
    if not _key():
        return {"orphans": []}
    orphans = live_pods()
    for p in orphans:
        terminate(p["id"])
    return {"orphans": [p["id"] for p in orphans],
            "recovered_usd": round(sum(p["spent_so_far"] for p in orphans), 3)}
