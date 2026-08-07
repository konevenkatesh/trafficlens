"""GPU lifecycle for fine-tuning runs.

The meter is the point. Every pod this Lab starts is polled on a fixed interval,
its accrued cost written to the ledger, and it is never assumed dead: termination
is confirmed against the live pod list before the row is closed. Idle pods are the
expensive failure mode, so an idle-timeout guard can stop one automatically.
"""
import threading
import time

import db
import providers

POLL_S = 20
_monitors = {}


def pods_live():
    return providers.rp_pods()


# RunPod keeps a finished pod in the pod list with desiredStatus EXITED, so "still
# listed" is not "still burning". Billing on presence alone charged one pod for three
# hours after it stopped. The test is deliberately one-sided: bill unless the status
# positively says it is not running, because under-reporting a live pod is the more
# dangerous mistake of the two.
NOT_BILLING = {"EXITED", "TERMINATED", "STOPPED", "PAUSED", "DEAD", "FAILED"}


def _accrue(row, live):
    """Book the money burned since the last poll, at the pod's own hourly rate."""
    now = time.time()
    last = row.get("_last_poll") or row["created"]
    dt_h = max(0.0, (now - last) / 3600)
    hourly = live.get("hourly") or row["hourly"] or 0
    if str(live.get("status") or "").upper() in NOT_BILLING:
        return 0.0
    delta = dt_h * hourly
    if delta > 0:
        db.run("UPDATE lab_pods SET cost_usd=COALESCE(cost_usd,0)+? WHERE id=?",
               delta, row["id"])
        db.charge(row["run_id"], "runpod", "train", f"{row['gpu']} {row['pod_id']}",
                  round(dt_h, 5), "gpu-hour", delta, {"pod": row["pod_id"]})
    return delta


def monitor(pod_row_id):
    """One thread per pod: telemetry + cost + idle guard until it is gone."""
    def loop():
        last_poll = time.time()
        idle_since = None
        # Once we have decided this pod must die, we keep deciding it. An unconfirmed
        # terminate used to `return`, which killed the meter and the only thing still
        # trying to shut the pod down -- the one failure where staying alive matters most.
        stopping, tries = None, 0
        while True:
            row = db.one("SELECT * FROM lab_pods WHERE id=?", pod_row_id)
            if not row or row["status"] in ("terminated", "gone"):
                return
            live = next((p for p in pods_live() if p["id"] == row["pod_id"]), None)
            if not live:
                db.run("UPDATE lab_pods SET status='gone', terminated=? WHERE id=?",
                       time.time(), pod_row_id)
                db.log(row["run_id"], "stopped", f"pod {row['pod_id']}",
                       "no longer listed by RunPod")
                return
            row["_last_poll"] = last_poll
            _accrue(row, live)
            last_poll = time.time()
            db.run("UPDATE lab_pods SET status=?, telemetry=? WHERE id=?",
                   live.get("status") or "running", db.jdump(live), pod_row_id)

            # Already condemned: retry the terminate every poll until RunPod confirms it.
            if stopping:
                tries += 1
                if stop(pod_row_id, reason=f"{stopping} (attempt {tries})")["verified"]:
                    return
                db.log(row["run_id"], "failed", f"pod {row['pod_id']}",
                       f"still not terminated after {tries} attempt(s) — the meter is "
                       f"kept running so the cost stays visible")
                time.sleep(POLL_S)
                continue

            # Hard wall-clock deadline. The idle guard depends on telemetry being
            # readable and on the job actually going quiet; this one does not care
            # why -- past the limit, the pod dies.
            try:
                max_h = float(db.get_setting("max_pod_hours", "5") or 0)
            except ValueError:
                max_h = 5.0
            age_h = (time.time() - (row["created"] or time.time())) / 3600
            if max_h and age_h >= max_h:
                db.log(row["run_id"], "stopped", f"pod {row['pod_id']}",
                       f"hit the {max_h:g}h hard deadline at {age_h:.1f}h — auto-terminated")
                stopping, tries = f"{max_h:g}h deadline", 1
                if stop(pod_row_id, reason=stopping)["verified"]:
                    return
                time.sleep(POLL_S)
                continue

            util = live.get("gpu_util")
            cap = float(db.get_setting("idle_stop_minutes", "20") or 0)
            if cap and util is not None:
                idle_since = (idle_since or time.time()) if util < 5 else None
                if idle_since and (time.time() - idle_since) > cap * 60:
                    db.log(row["run_id"], "stopped", f"pod {row['pod_id']}",
                           f"idle >{cap:.0f} min at <5% GPU — auto-stopped to stop the meter")
                    stopping, tries = "idle guard", 1
                    if stop(pod_row_id, reason=stopping)["verified"]:
                        return
                    time.sleep(POLL_S)
                    continue
            time.sleep(POLL_S)

    # One meter per pod. `resume_monitors()` runs on every Lab boot and used to overwrite
    # the handle without stopping the old thread, so two threads accrued against the same
    # pod and the ledger double-booked it.
    running = _monitors.get(pod_row_id)
    if running and running.is_alive():
        return
    t = threading.Thread(target=loop, daemon=True)
    _monitors[pod_row_id] = t
    t.start()


def adopt(run_id, pod_id, purpose="training"):
    """Track a pod that already exists (created here or in the RunPod console)."""
    live = next((p for p in pods_live() if p["id"] == pod_id), None)
    if not live:
        return None, f"pod {pod_id} not found on this account"
    existing = db.one("SELECT * FROM lab_pods WHERE pod_id=? AND terminated IS NULL", pod_id)
    if existing:
        return existing["id"], "already tracked"
    rid = db.run("""INSERT INTO lab_pods
              (run_id,pod_id,name,gpu,hourly,status,created,telemetry,purpose)
              VALUES (?,?,?,?,?,?,?,?,?)""",
                 run_id, pod_id, live.get("name"), live.get("gpu"),
                 live.get("hourly", 0), live.get("status", "running"),
                 time.time(), db.jdump(live), purpose)
    db.log(run_id, "created", f"pod {pod_id}",
           f"{live.get('gpu')} at ${live.get('hourly', 0)}/hr")
    monitor(rid)
    return rid, "tracking"


def stop(pod_row_id, reason="user"):
    row = db.one("SELECT * FROM lab_pods WHERE id=?", pod_row_id)
    if not row:
        return {"ok": False, "error": "unknown pod row"}
    res = providers.rp_terminate(row["pod_id"])
    if res["terminated"]:
        db.run("UPDATE lab_pods SET status='terminated', terminated=? WHERE id=?",
               time.time(), pod_row_id)
        db.log(row["run_id"], "stopped", f"pod {row['pod_id']}", f"terminated ({reason})")
    else:
        db.log(row["run_id"], "failed", f"pod {row['pod_id']}",
               "termination NOT confirmed — still listed")
    return {"ok": res["terminated"], "verified": res["terminated"],
            "still_alive": res["alive"], "cost_usd": round(row["cost_usd"] or 0, 4)}


def resume_monitors():
    """After a Lab restart, pick the meter back up on anything still running."""
    for row in db.rows("SELECT * FROM lab_pods WHERE terminated IS NULL"):
        monitor(row["id"])
