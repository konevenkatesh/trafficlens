"""Run extraction on a rented GPU instead of this machine.

`cloud.py` owns the account and the money. This owns the work: bring a pod up, put the
model and the video on it, run, bring the trajectories back, and write them into the same
tables a local extraction writes. Everything downstream — counting, review, the report —
cannot tell which machine produced a track.

**It is the same code, not the same number.** Same weights, same tracker, same stride,
same vote-per-track logic: running the agent and `engine.extract` on one machine gives
byte-identical output — 86 tracks and 5117 points, every field equal, when this was
checked. Across GPU backends it does not. The same clip gave 86 tracks on an Apple GPU
and 83 on a rented RTX 4090: about 3.5% of tracks, at the margins where a detection sits
either side of the confidence floor. That is floating-point arithmetic differing between
Metal and CUDA, not a defect here, and it is the same difference the surveyor would see
moving between any two machines. Worth knowing before a clip is re-run somewhere else and
the total shifts slightly.

**One pod, reused.** Bringing a pod up costs 2-4 minutes of image pull and boot, billed.
Doing that per clip on a 24-hour station would spend more on booting than on detecting.
The pod is created on the first clip, kept while work keeps arriving, and killed by the
idle watchdog in `cloud.py` when it stops.

**The pod is disposable and holds nothing.** Results are written into the local database
as each clip finishes. If the pod dies mid-clip the clip re-runs; there is no state on it
worth recovering, and making it resumable would cost more in complexity than it saves.

**Everything is authenticated.** The RunPod proxy address is derived from the pod id and
is reachable by anyone, so a per-pod token guards every endpoint but the health probe.
"""
import base64
import gzip
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cloud
import db

# The container. Pinned to the same ultralytics the app pins locally: a different version
# tracks differently, and "the cloud gave me another number" is not a defect anyone can
# debug. Its CUDA torch is already inside, so the pod installs nothing at boot.
IMAGE = "ultralytics/ultralytics:8.4.114"
AGENT_PORT = 8000
BOOT_TIMEOUT = 600            # image pull on a cold machine is minutes, not seconds
LOCK = threading.Lock()
_POD = {}                     # the pod this process is using, if any


def _agent_source():
    """agent.py as text.

    Read as a file rather than imported, because it runs on the pod and must never be
    imported here. Inside a frozen build __file__ points into the PyInstaller archive
    rather than at anything on disk, so the bundled copy is looked up first -- without
    this the .exe raises FileNotFoundError on the first cloud clip, and only there.
    """
    import sys
    for c in (Path(getattr(sys, "_MEIPASS", "")) / "agent.py",
              Path(__file__).resolve().parent / "agent.py"):
        if c.is_file():
            return c.read_text()
    raise FileNotFoundError("agent.py is missing from this build")


def _docker_args(token):
    """The pod's start command, carrying the agent inside it.

    base64 rather than a clone or a registry push: the agent is one file, it changes with
    the app, and anything fetched at boot is a network dependency on a machine nobody can
    log into to diagnose.
    """
    b64 = base64.b64encode(_agent_source().encode()).decode()
    # The agent lives in /work, NOT in /. Python puts the script's own directory at the
    # front of sys.path, and this image keeps its source checkout at /ultralytics -- so an
    # agent at /agent.py made "/" the import root, where the directory /ultralytics
    # shadowed the installed package. The pod came up healthy, took the weights and the
    # video, and only then failed with "cannot import name 'YOLO' from 'ultralytics'
    # (unknown location)". /work has nothing in it to collide with.
    return ("bash -c '"
            f"export TL_TOKEN={token} TL_PORT={AGENT_PORT}; "
            "mkdir -p /work; "
            f"echo {b64} | base64 -d > /work/agent.py; "
            "cd /work && python3 /work/agent.py"
            "'")


def _url(pod_id):
    return f"https://{pod_id}-{AGENT_PORT}.proxy.runpod.net"


def _call(pod, path, data=None, method=None, timeout=120, raw=False):
    req = urllib.request.Request(
        _url(pod["id"]) + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"X-Token": pod["token"], "User-Agent": cloud.UA,
                 "Content-Type": "application/octet-stream" if raw else "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body if raw else json.loads(body or b"{}")


# ───────────────────────────── the pod ─────────────────────────────
def _create(gpu, token):
    """Ask RunPod for a machine. Recorded in `cloud_runs` BEFORE it can exist.

    The row goes in first on purpose: if the create succeeds and this process dies before
    writing it, a GPU is billing and nothing knows to stop it. A row for a pod that never
    started is harmless noise; the reverse costs real money.
    """
    q = """mutation ($in: PodFindAndDeployOnDemandInput) {
             podFindAndDeployOnDemand(input: $in) { id name costPerHr } }"""
    base = {
        "gpuCount": 1, "gpuTypeId": gpu,
        "name": "trafficlens", "imageName": IMAGE,
        "dockerArgs": _docker_args(token),
        "ports": f"{AGENT_PORT}/http",
        # No network volume: the pod keeps nothing between runs, and a volume is billed
        # after the pod is gone -- the one charge that survives "stop everything".
        "volumeInGb": 0, "containerDiskInGb": 30,
        "startSsh": False,
    }
    # CPU and RAM are deliberately NOT specified. Asking for 8 vCPU and 24GB looked
    # harmless and was rejected outright with "this machine does not have the resources"
    # -- RunPod's 4090 hosts offer 5 vCPU, so a card sitting at High stock was refused
    # over a requirement this workload never had. The host's own pairing for the GPU is
    # right by definition; naming numbers only narrows what can be found.
    #
    # Community first because it is cheaper, then secure. Capacity moves hour to hour and
    # a survey should not stop because one pool happened to be empty.
    err = None
    got = None
    for kind in ("COMMUNITY", "SECURE"):
        d, err = _gql_retry(q, {"in": {**base, "cloudType": kind}}, tries=2)
        pod = ((d or {}).get("podFindAndDeployOnDemand") or {}) if d else {}
        if pod.get("id"):
            got = kind
            break
    else:
        pod = {}
    if not pod.get("id"):
        return None, (err or f"no {gpu} was free just now — try another card in Settings")
    # The real price, not the advertised one. Settings quotes RunPod's lowest listed
    # price for the card; the machine actually allocated can cost more than double that
    # -- the first 4090 this rented billed $0.74/hr against a $0.34 quote. Recording what
    # was really charged is the difference between a ledger and a guess.
    rate = pod.get("costPerHr") or 0
    db.run("""INSERT INTO cloud_runs (pod_id,gpu,cost_per_hr,started,status,note)
              VALUES (?,?,?,?,'starting',?)""",
           pod["id"], gpu, rate, time.time(),
           f"detection · {got.lower()} cloud · ${rate:.2f}/hr")
    return {"id": pod["id"], "token": token,
            "cost_per_hr": pod.get("costPerHr") or 0}, None


def _gql_retry(q, args, tries=3):
    """Community-cloud capacity comes and goes; one refusal is not an answer."""
    err = None
    for n in range(tries):
        d, err = cloud._gql(q, args, timeout=90)
        if not err:
            return d, None
        time.sleep(2 + 3 * n)
    return None, err


def _wait_ready(pod, on_note=None):
    """Block until the agent answers, or give up with a reason a person can act on."""
    t0 = time.time()
    last = None
    while time.time() - t0 < BOOT_TIMEOUT:
        try:
            req = urllib.request.Request(_url(pod["id"]) + "/health",
                                         headers={"User-Agent": cloud.UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                h = json.loads(r.read() or b"{}")
            if h.get("ok"):
                if not h.get("cuda"):
                    return False, "the rented machine came up with no usable GPU"
                return True, h.get("gpu")
        except Exception as e:
            last = type(e).__name__
        if on_note:
            on_note(f"starting the GPU… {int(time.time() - t0)}s")
        time.sleep(6)
    return False, (f"the rented machine did not come up within "
                   f"{BOOT_TIMEOUT // 60} minutes ({last})")


def ensure_pod(on_note=None):
    """The pod for this session, creating one if there is not a live one already.

    Refuses through `cloud.may_start()` first — key, switch, monthly limit and balance —
    so the money checks happen before anything can be created rather than after.
    """
    with LOCK:
        if _POD.get("id"):
            live = {p["id"] for p in cloud.live_pods()}
            if _POD["id"] in live:
                return _POD, None
            _POD.clear()          # it died or somebody killed it; start again

        ok, why = cloud.may_start()
        if not ok:
            return None, why

        gpu = cloud.config()["gpu"]
        if on_note:
            on_note(f"renting a {gpu}…")
        pod, err = _create(gpu, secrets.token_urlsafe(24))
        if err:
            return None, err
        cloud.note_work()
        ready, detail = _wait_ready(pod, on_note)
        if not ready:
            # Anything that cannot be used must not stay billing. This is the failure
            # path most likely to leak money, so it terminates before it reports.
            cloud.terminate(pod["id"])
            return None, detail
        pod["gpu_name"] = detail
        _POD.clear()
        _POD.update(pod)
        db.run("UPDATE cloud_runs SET status='running' WHERE pod_id=?", pod["id"])
        return _POD, None


def _put(pod, rel, path, on_note=None):
    """Upload one file, skipping it if the pod already has it byte-for-byte.

    The weights are the same 50MB on every clip of a survey. Sending them once per pod
    rather than once per clip is the difference between a minute of overhead and an hour
    of it across a station day.
    """
    seen = pod.setdefault("_sent", {})
    size = Path(path).stat().st_size
    if seen.get(rel) == size:
        return
    if on_note:
        on_note(f"sending {Path(path).name} ({size / 1e6:.0f} MB)…")
    with open(path, "rb") as f:
        req = urllib.request.Request(
            _url(pod["id"]) + "/" + rel, data=f, method="PUT",
            headers={"X-Token": pod["token"], "User-Agent": cloud.UA,
                     "Content-Type": "application/octet-stream",
                     "Content-Length": str(size)})
        with urllib.request.urlopen(req, timeout=3600) as r:
            json.loads(r.read() or b"{}")
    seen[rel] = size


# ───────────────────────────── running a clip ─────────────────────────────
def extract(video_id, job_id, imgsz=960, conf=0.12, model_id=None):
    """The cloud twin of engine.extract(). Same arguments, same tables, same result.

    Signature-compatible on purpose: `work._drain` chooses between the two and nothing
    else in the app has to know which ran.
    """
    import engine

    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    db.run("UPDATE jobs SET status='running', started=? WHERE id=?", time.time(), job_id)

    def note(msg, pct=None):
        db.run("UPDATE jobs SET message=?" + (", progress=?" if pct is not None else "")
               + " WHERE id=?", *( [msg, pct, job_id] if pct is not None else [msg, job_id]))

    try:
        use_id = model_id or engine.MODEL_ID
        weights = engine.ROOT / "models" / f"{use_id}.pt"
        if not weights.exists():
            raise FileNotFoundError(f"model weights not found: {weights.name}")

        pod, err = ensure_pod(on_note=note)
        if err:
            raise RuntimeError(err)
        cloud.note_work()

        _put(pod, f"models/{weights.name}", weights, note)
        _put(pod, "tracker.yaml", engine.TRACKER, note)
        _put(pod, f"video/{Path(v['path']).name}", v["path"], note)

        stride = engine.stride_for(v["fps"])
        _call(pod, "/run", json.dumps({
            "video": Path(v["path"]).name, "weights": weights.name,
            "imgsz": imgsz, "conf": conf, "stride": stride,
            "frames": v["frames"]}).encode())

        last_beat = time.time()
        while True:
            time.sleep(4)
            cloud.note_work()          # the watchdog must not kill a pod mid-clip
            p = _call(pod, "/progress")
            if p.get("phase") == "error":
                raise RuntimeError(p.get("error") or "the GPU reported a failure")
            if p.get("phase") == "done":
                break
            note(f"cloud: {p.get('message') or p.get('phase')}", p.get("pct"))
            if time.time() - last_beat > 3600:
                raise TimeoutError("the clip did not finish within an hour on the GPU")

        note("bringing the results back")
        blob = _call(pod, "/result", raw=True, timeout=1800)
        res = json.loads(gzip.decompress(blob))
        _ingest(video_id, use_id, res)
        cloud.note_work()

        import dedup as dedup_mod
        d = dedup_mod.dedup(video_id)
        db.run("""UPDATE jobs SET status='done', progress=100, finished=?, message=?
                  WHERE id=?""", time.time(),
               f"{len(res['tracks'])} tracks stored, {d['suppressed']} duplicates "
               f"suppressed (on {pod.get('gpu_name') or 'a rented GPU'})", job_id)
        db.run("UPDATE cloud_runs SET clips = COALESCE(clips,0) + 1 WHERE pod_id=?",
               pod["id"])
    except Exception as e:
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)


def _ingest(video_id, model_id, res):
    """Write the pod's answer into the tables a local extraction writes.

    Deleting first, exactly as the local path does: re-running a clip must replace its
    trajectories rather than double them. That is not hypothetical here — a cloud clip
    that fails after ingest gets retried.
    """
    db.run("DELETE FROM track_points WHERE video_id=?", video_id)
    db.run("DELETE FROM tracks WHERE video_id=?", video_id)
    db.runmany("INSERT INTO track_points VALUES (?,?,?,?,?,?,?,?)",
               [(video_id, *p) for p in res["points"]])
    db.runmany(
        "INSERT INTO tracks (video_id,track_id,cls,cls_votes,class_override,"
        "t_start,t_end,n_points,model_id) VALUES (?,?,?,?,NULL,?,?,?,?)",
        [(video_id, t["track_id"], t["cls"], db.jdump(t["votes"]),
          t["t_start"], t["t_end"], t["n_points"], model_id) for t in res["tracks"]])


def in_use():
    """Whether cloud detection should be used for the next clip. Cheap: no network."""
    if os.environ.get("TRAFFICLENS_NO_CLOUD"):
        return False
    c = cloud.config()
    return bool(c["enabled"] and c["configured"])
