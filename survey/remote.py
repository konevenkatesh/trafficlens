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
# How long to wait for a machine before writing it off, and how many to try. Measured:
# a good host pulls the image and answers in about 115 seconds. One that has not answered
# in five minutes is not slow, it is stuck -- and waiting ten minutes on it, as this first
# did, costs twice as much and still fails. Cutting the wait and moving to another machine
# turns a total loss into a retry that usually works.
BOOT_TIMEOUT = 300
# Three machines, not two. RunPod hosts fail often enough that this is not pessimism:
# across testing only one pod in five came up first try, one answered with no usable GPU,
# and one sat in a container-start crash loop with a dead NVIDIA driver on the host
# ("nvidia-container-cli: detection error: nvml error"). None of those are anything the
# app can fix or the surveyor can influence -- the only remedy is another machine, and a
# failed attempt costs about two cents.
BOOT_TRIES = 3
# 4MB writes rather than the 8KB urllib defaults to. This was changed on the theory that
# the small chunks were throttling upload; measured on one pod, both ways, same file, it
# makes no difference at all — 2.68 MB/s against 2.64 MB/s. Kept because streaming by hand
# is what makes progress reporting and a real error message possible, not for speed.
CHUNK = 4 << 20
UPLOAD_TIMEOUT = 7200         # a 1GB station recording on a bad line
LOCK = threading.Lock()
# Uploads are serialised. Two at once share the same link and finish no sooner, and the
# per-pod record of what has been sent would need locking anyway.
_UPLOCK = threading.Lock()
# How many clips ahead to send while the GPU works. One: a station recording is about a
# gigabyte and the container disk is 30GB, so the pod holds the clip it is detecting and
# the one arriving, never a growing pile.
PREFETCH = 1
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


def _pod_state(pod_id):
    """RunPod's own view of the pod: status, and whether the container ever started."""
    d, err = cloud._gql("""query { myself { pods { id desiredStatus
                                     runtime { uptimeInSeconds } } } }""")
    if err:
        return None, None
    for p in ((d or {}).get("myself") or {}).get("pods") or []:
        if p["id"] == pod_id:
            return p.get("desiredStatus"), (p.get("runtime") or {}).get("uptimeInSeconds")
    return "GONE", None


def _wait_ready(pod, on_note=None):
    """Block until the agent answers, or give up with a reason a person can act on.

    Watches RunPod's status as well as the agent, because the most common failure does not
    look like slowness. A host with a broken NVIDIA driver accepts the rental, reports
    RUNNING, and then loops forever on "error starting container ... nvml error" -- the
    container never runs, so /health never answers and the full timeout is spent waiting
    for something that cannot happen. A pod that has stopped or vanished is hopeless
    immediately, and saying so early is both cheaper and clearer than a timeout.
    """
    t0 = time.time()
    last = None
    checked = 0.0
    while time.time() - t0 < BOOT_TIMEOUT:
        if time.time() - checked > 30:
            checked = time.time()
            state, _up = _pod_state(pod["id"])
            if state in ("EXITED", "TERMINATED", "DEAD", "GONE"):
                return False, (f"the rented machine stopped before it could start "
                               f"(RunPod reported {state.lower()}). This is a fault on "
                               f"their host, not with your key or this app.")
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
    return False, (f"the rented machine did not answer within "
                   f"{BOOT_TIMEOUT // 60} minutes ({last}). Usually a bad host — "
                   f"a broken GPU driver there will loop on 'error starting container' "
                   f"where nothing this app does can help.")


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
        detail = None
        for attempt in range(1, BOOT_TRIES + 1):
            if on_note:
                on_note(f"renting a {gpu}…"
                        + (f" (machine {attempt} of {BOOT_TRIES})" if attempt > 1 else ""))
            pod, err = _create(gpu, secrets.token_urlsafe(24))
            if err:
                return None, err
            cloud.note_work()
            t0 = time.time()
            ready, detail = _wait_ready(pod, on_note)
            # Anything that cannot be used must not stay billing. This is the failure path
            # most likely to leak money, so it terminates before it retries or reports.
            if not ready:
                cloud.terminate(pod["id"])
                db.run("""UPDATE cloud_runs SET note = COALESCE(note,'')
                            || ' · never came up' WHERE pod_id=?""", pod["id"])
                continue
            pod["gpu_name"] = detail
            _POD.clear()
            _POD.update(pod)
            db.run("""UPDATE cloud_runs SET status='running',
                        note = COALESCE(note,'') || ' · ready in ' || ? || 's'
                      WHERE pod_id=?""", int(time.time() - t0), pod["id"])
            return _POD, None
        return None, ((detail or "no machine came up")
                      + f" Tried {BOOT_TRIES} machines. RunPod capacity and host health "
                        f"vary hour to hour; waiting a few minutes and starting again "
                        f"usually lands on a working one.")


def _put(pod, rel, path, on_note=None):
    """Upload one file, skipping it if the pod already has it byte-for-byte.

    The weights are the same 20MB on every clip of a survey. Sending them once per pod
    rather than once per clip is the difference between a minute of overhead and an hour
    of it across a station day.

    Written against http.client rather than urllib so the upload can be streamed by hand:
    the caller gets a running rate and an honest error. A station recording is around 1GB,
    and an upload that reports nothing for ten minutes reads as a hang.

    NOT for throughput, though that is why it was first written. urllib sends a file object
    in 8192-byte reads and one pod measured 0.7 MB/s, so the small writes looked like the
    cause. Measured properly — same file, same pod, both ways — 4MB chunks gave 2.68 MB/s
    and 8KB gave 2.64 MB/s. The chunk size is irrelevant; what varies is the machine.
    Across pods the same upload ran between 0.7 and 2.7 MB/s, roughly 4x, which is 4 to 16
    minutes per hour of footage at ~676MB an hour. On a good pod that is about level with
    detection (~3 min per hour of footage on a 4090); on a bad one, upload is the whole
    cost. The fix worth making next is overlapping the two, not making the bytes faster.
    """
    import http.client

    size = Path(path).stat().st_size
    # One upload at a time, and the have-we-sent-it check happens inside the lock. Since
    # the next clip is now sent while the current one detects, two threads can want this
    # at once -- and the common case is the prefetcher already sending the very file the
    # worker has just reached, where a second copy would be pure waste.
    with _UPLOCK:
        seen = pod.setdefault("_sent", {})
        if seen.get(rel) == size:
            return
        _upload(pod, rel, path, size, on_note)
        seen[rel] = size


def _upload(pod, rel, path, size, on_note):
    import http.client

    name = Path(path).name
    host = _url(pod["id"]).replace("https://", "")
    conn = http.client.HTTPSConnection(host, timeout=UPLOAD_TIMEOUT, blocksize=CHUNK)
    try:
        conn.putrequest("PUT", "/" + rel, skip_accept_encoding=True)
        conn.putheader("X-Token", pod["token"])
        conn.putheader("User-Agent", cloud.UA)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()

        sent, t0, last = 0, time.time(), 0.0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                now = time.time()
                if on_note and (now - last > 3 or sent == size):
                    last = now
                    rate = sent / max(now - t0, 1e-6) / 1e6
                    left = (size - sent) / 1e6 / max(rate, 1e-6)
                    on_note(f"sending {name} — {sent / 1e6:.0f} of {size / 1e6:.0f} MB "
                            f"at {rate:.1f} MB/s"
                            + (f", {left / 60:.0f} min left" if left > 90 else ""))
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError(f"upload of {name} failed: HTTP {r.status} "
                               f"{body[:120].decode(errors='replace')}")
    finally:
        conn.close()


def _forget(pod, rel):
    """Delete a file from the pod and stop believing it is there.

    Both halves matter. Deleting without forgetting makes the next upload of the same name
    a no-op against a pod that no longer has it, and the run fails looking for a video
    that was removed.
    """
    try:
        _call(pod, "/" + rel, method="DELETE", timeout=60)
    except Exception:
        # Not fatal: the agent also drops each clip as it finishes with it, so this is
        # the second of two chances. Counted rather than ignored, because if both keep
        # failing the container disk fills part-way through a survey and the real cause
        # would be invisible. `free_gb` from /progress is what actually notices.
        pod["_undeleted"] = pod.get("_undeleted", 0) + 1
    with _UPLOCK:
        pod.setdefault("_sent", {}).pop(rel, None)


def _prefetch(pod):
    """Send the next queued clips while the GPU is busy with this one.

    Upload and detection each cost roughly three to sixteen minutes per hour of footage,
    and doing them in turn meant a station day paid for both end to end. They use nothing
    in common -- one is this laptop's uplink, the other is a GPU on another continent --
    so the only reason they were serial is that the code asked for them in order.

    Fire and forget. A failure here costs nothing: the clip is simply uploaded the normal
    way when its turn comes, which is what used to happen every time.
    """
    def run():
        for path in _upcoming(PREFETCH):
            if not _POD.get("id"):
                return                     # pod went away; nothing to send to
            try:
                _put(pod, f"video/{Path(path).name}", path)
            except Exception:
                return
    threading.Thread(target=run, daemon=True).start()


def _upcoming(limit):
    """Paths of the next few queued extractions, newest queue state each time.

    Read from the live queue rather than passed in, because the surveyor can add or cancel
    an hour while this one runs -- a list captured earlier would send files nobody wants.
    """
    try:
        import work
        with work._QLOCK:
            ids = [j["video_id"] for j in work._Q
                   if j.get("kind", "extract") == "extract"][:limit]
    except Exception:
        return []
    out = []
    for vid in ids:
        v = db.one("SELECT path FROM videos WHERE id=?", vid)
        if v and v["path"] and Path(v["path"]).is_file():
            out.append(v["path"])
    return out


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

        # The GPU is now busy for minutes. Use that time to send the next clip rather
        # than leaving the uplink idle and then making the surveyor wait for it.
        _prefetch(pod)

        last_beat = time.time()
        while True:
            time.sleep(4)
            cloud.note_work()          # the watchdog must not kill a pod mid-clip
            p = _call(pod, "/progress")
            if p.get("phase") == "error":
                raise RuntimeError(p.get("error") or "the GPU reported a failure")
            if p.get("phase") == "done":
                break
            free = p.get("free_gb")
            if free is not None and free < 3:
                raise RuntimeError(
                    f"the rented machine is nearly out of disk ({free} GB free) — "
                    f"stop and restart cloud detection to get a clean one")
            note(f"cloud: {p.get('message') or p.get('phase')}", p.get("pct"))
            if time.time() - last_beat > 3600:
                raise TimeoutError("the clip did not finish within an hour on the GPU")

        note("bringing the results back")
        blob = _call(pod, "/result", raw=True, timeout=1800)
        res = json.loads(gzip.decompress(blob))
        _ingest(video_id, use_id, res)
        cloud.note_work()
        # Only once the trajectories are safely in the database. Deleting earlier would
        # mean a failed ingest could not be retried without sending the video again.
        _forget(pod, f"video/{Path(v['path']).name}")

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
