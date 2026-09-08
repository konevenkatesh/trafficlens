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
import hashlib
import hmac
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
import stash

# The container. Pinned to the same ultralytics the app pins locally: a different version
# tracks differently, and "the cloud gave me another number" is not a defect anyone can
# debug. Its CUDA torch is already inside, so the pod installs nothing at boot.
IMAGE = "ultralytics/ultralytics:8.4.114"
AGENT_PORT = 8000
# How long to wait for a machine, and how many to try. Ten minutes, raised from five. Five was tuned on the only two hosts that had worked at
# the time, both of which answered in about 115 seconds -- so it looked like a 2.6x margin
# and was really a sample of two. Hosts that then took longer were abandoned mid-download
# and the work thrown away. A pod pulling a 4.6GB image and a pod crash-looping on a dead
# driver are indistinguishable through the API (both RUNNING, both uptime 0), so there is
# no clever signal to wait for -- only the choice of how long to give it. At $0.34/hr the
# extra five minutes costs under three cents on a host that was never going to work, and
# saves a whole retry cycle on one that was.
BOOT_TIMEOUT = 600
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
# One PUT never carries more than this. RunPod's proxy is behind Cloudflare, which cuts a
# large request body off part way through: every one of a station's ~1GB recordings failed
# with "EOF occurred in violation of protocol (_ssl.c:2427)" while 36MB test clips went
# through fine. Parts also make a dropped connection cost one part instead of the file.
PART = 32 << 20
PART_TRIES = 4
# Per-PART, not per-file, and sized to the part: a 32MB part that has not finished in five
# minutes is stalled, not slow, and must fail so it can be retried on a fresh connection.
# The old value of two hours applied to each part, so a proxy that stopped forwarding a
# body mid-way -- which it does -- hung the surveyor for two hours per part.
UPLOAD_TIMEOUT = 300
LOCK = threading.Lock()
# Uploads are serialised. Two at once share the same link and finish no sooner, and the
# per-pod record of what has been sent would need locking anyway.
_UPLOCK = threading.Lock()
# How many clips ahead to send while the GPU works. One: a station recording is about a
# gigabyte and the container disk is 30GB, so the pod holds the clip it is detecting and
# the one arriving, never a growing pile.
PREFETCH = 1
_POD = {}                     # the pod this process is using, if any
_STAGED = {}                  # video path -> object key already in the bucket
_STAGELOCK = threading.Lock()


def _stage(path, on_note=None):
    """Put a recording in the bucket once, however many times it is asked for."""
    with _STAGELOCK:
        if path in _STAGED:
            return _STAGED[path]
    key = stash.upload(path, on_note)
    with _STAGELOCK:
        _STAGED[path] = key
    return key


def _unstage(path):
    with _STAGELOCK:
        key = _STAGED.pop(path, None)
    if key:
        stash.delete(key)

# A short history of what the rented GPU has actually been doing, so the surveyor can see
# it rather than infer it from a progress bar. Each entry is one phase with its duration;
# uploads also carry the measured rate, which is the number that decides whether the cloud
# is worth using at all and varies fourfold between hosts.
ACTIVITY = []
_ACTLOCK = threading.Lock()
ACTIVITY_MAX = 40


def note_phase(kind, detail="", seconds=None, mb=None, mbps=None):
    with _ACTLOCK:
        ACTIVITY.append({"t": time.time(), "kind": kind, "detail": detail,
                         "seconds": round(seconds, 1) if seconds is not None else None,
                         "mb": round(mb, 1) if mb is not None else None,
                         "mbps": round(mbps, 2) if mbps is not None else None})
        del ACTIVITY[:-ACTIVITY_MAX]


def activity():
    with _ACTLOCK:
        return list(ACTIVITY)


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


def _sign(pod, method, path, length):
    """The headers that prove a request came from the app that rented this pod.

    Signature over method, path, a timestamp and the body length -- see agent._authed
    for why it is a signature and not the token.
    """
    ts = f"{time.time():.3f}"
    msg = f"{method}|{path}|{ts}|{int(length)}"
    return {"X-Ts": ts, "X-Sig": hmac.new(pod["token"].encode(), msg.encode(),
                                          hashlib.sha256).hexdigest()}


def _base(pod):
    """Where to talk to this pod: the direct TCP mapping if it has one, else the proxy.

    Measured on the same pod at the same minute: /health over the direct port answered
    in 2.4s; over the proxy it did not answer at all for six minutes. The proxy is the
    fallback for hosts without a public IP, not the default.
    """
    tcp = pod.get("tcp")
    return f"http://{tcp[0]}:{tcp[1]}" if tcp else _url(pod["id"])


def _call(pod, path, data=None, method=None, timeout=120, raw=False):
    method = method or ("POST" if data else "GET")
    req = urllib.request.Request(
        _base(pod) + path, data=data, method=method,
        headers={**_sign(pod, method, path, len(data) if data else 0),
                 "User-Agent": cloud.UA,
                 "Content-Type": "application/octet-stream" if raw else "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body if raw else json.loads(body or b"{}")


def _direct_port(pod_id):
    """The pod's public IP and the port RunPod mapped to the agent, or None.

    This is the whole reason uploads now work: it is a raw TCP path to the pod that goes
    nowhere near Cloudflare. Measured on the same afternoon from the same machine, the
    proxied route ran at 0.1-0.2 MB/s and stalled for minutes at a time; this machine
    reaches Cloudflare's own edge at 7-13 MB/s and the host is rated at 5.5 Gbit/s. The
    proxy is for health checks and results, and that is all it is used for now.
    """
    d, err = cloud._gql("""query { myself { pods { id runtime { ports {
                             ip isIpPublic privatePort publicPort type } } } } }""")
    if err:
        return None
    for p in ((d or {}).get("myself") or {}).get("pods") or []:
        if p["id"] != pod_id:
            continue
        for port in ((p.get("runtime") or {}).get("ports") or []):
            if (port.get("type") == "tcp" and port.get("isIpPublic")
                    and int(port.get("privatePort") or 0) == AGENT_PORT):
                return port["ip"], int(port["publicPort"])
    return None


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
        # The agent's port as a raw TCP mapping ONLY. Asking for "8000/http,8000/tcp"
        # looked like belt and braces and was a bug: RunPod cannot map one port both
        # ways, so the proxy entry was silently re-pointed at a private port nothing
        # listened on (19123) and every proxied request answered 404. Everything --
        # health, progress, uploads, results -- goes over the TCP mapping; the proxy is
        # not used on a host that has a public IP, and supportPublicIp asks for one.
        "ports": f"{AGENT_PORT}/tcp",
        "supportPublicIp": True,
        # With RunPod network storage configured, the pod is created in the volume's
        # datacenter with the volume mounted at /workspace. The recording the app uploaded
        # is then already on the pod's disk. This pins the pod to one datacenter, which
        # narrows host choice -- the price of not moving a gigabyte twice.
        **({"networkVolumeId": stash.config()["bucket"],
            "volumeMountPath": "/workspace",
            "dataCenterId": stash.datacenter()} if stash.is_runpod() else {}),
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
    # Community first because it is cheaper, then secure -- unless a network volume is
    # attached: RunPod's docs say "Network volumes are only available for Pods in the
    # Secure Cloud", so the community attempt can only ever fail, and it failed with
    # "no longer any instances available", which reads as an empty datacenter.
    kinds = ("SECURE",) if stash.is_runpod() else ("COMMUNITY", "SECURE")
    for kind in kinds:
        # Five minutes for the create, not ninety seconds. With a public IP required,
        # RunPod was measured taking well over two minutes to answer -- and it creates
        # the pod first, so a client that gives up early leaves a billing orphan and
        # then makes another. A long wait here is the cheapest fix there is.
        d, err = _gql_retry(q, {"in": {**base, "cloudType": kind}}, tries=1, timeout=300)
        pod = ((d or {}).get("podFindAndDeployOnDemand") or {}) if d else {}
        if pod.get("id"):
            got = kind
            break
        # RunPod sometimes creates the pod and then takes longer to answer than any
        # sensible client timeout. The pod exists, is billing, and nothing knows about it
        # -- measured: one ran untracked for six minutes while the create call was
        # retried on top of it. So after a failed create, look for a pod that was made
        # for THIS attempt. Only this attempt's token can produce a valid signature, so a
        # signed probe is proof of ownership; anything else unclaimed is a stray and dies.
        adopted = _adopt_or_kill(token)
        if adopted:
            pod, got = adopted, kind
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


# The most a card may cost per hour before it is not worth renting for this. Detection is
# about 3 min per hour of footage on a 4090 ($0.74/hr measured); a card at twice the price
# would have to be twice as fast to break even, and none of the cheap ones are slower than
# half. Set high enough to reach the RTX PRO 4500 and 5090 tier, low enough to exclude
# datacenter cards (A100, L40S, RTX PRO 6000) that cost $1.59-$6.79 for no gain here.
MAX_GPU_PRICE = 1.25
MIN_GPU_GB = 16


def _no_stock(err):
    e = (err or "").lower()
    return ("no longer any instances" in e or "was free just now" in e
            or "no instances" in e or "not available" in e)


def _gpu_plan(preferred, on_note=None):
    """Which cards to try, in order, and what they cost right now.

    A network volume pins the pod to one datacenter, and a datacenter's stock changes by
    the hour: at one probe EU-RO-1 had 4090s at Medium stock and an hour earlier none. So
    the card is chosen at run time from what is actually there -- the surveyor's choice
    first if it is in stock, otherwise the cheapest card with enough memory, up to
    MAX_GPU_PRICE. Without a volume, the pod can go anywhere and the preference stands.

    Returns a list of (gpu_id, price_or_None). Empty means the datacenter has nothing
    usable at any acceptable price; `_stock_note` says what it does have.
    """
    if not stash.is_runpod():
        return [(preferred, None)]
    stock = _in_stock(stash.datacenter())
    if stock is None:
        return [(preferred, None)]          # could not ask; behave as before
    ok = [(g, price) for g, price, gb in stock if gb >= MIN_GPU_GB and price <= MAX_GPU_PRICE]
    ok.sort(key=lambda x: (x[0] != preferred, x[1]))
    if on_note and ok and ok[0][0] != preferred:
        short = lambda g: g.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
        on_note(f"{stash.datacenter()} has no {short(preferred)} free right now — "
                + "trying " + ", then ".join(f"{short(g)} (${p:.2f}/hr)" for g, p in ok[:3]))
    return ok


def _in_stock(dc):
    """(gpu_id, price, memory_gb) for every card RunPod will sell in `dc` right now.

    None if RunPod could not be asked. One query, ~0.6 s measured.
    """
    q = """query($dc:String!){ gpuTypes { id memoryInGb
             lowestPrice(input:{gpuCount:1, dataCenterId:$dc, secureCloud:true,
                                supportPublicIp:true})
               { stockStatus uninterruptablePrice } } }"""
    d, err = cloud._gql(q, {"dc": dc}, timeout=30)
    if err:
        return None
    out = []
    for g in (d or {}).get("gpuTypes") or []:
        lp = g.get("lowestPrice") or {}
        if lp.get("stockStatus") and lp.get("uninterruptablePrice"):
            out.append((g["id"], float(lp["uninterruptablePrice"]), int(g.get("memoryInGb") or 0)))
    return out


def _stock_note(dc):
    stock = _in_stock(dc) or []
    short = lambda g: g.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
    if not stock:
        return f"RunPod has no card of any kind free in {dc} right now."
    return (f"In stock in {dc} right now: "
            + ", ".join(f"{short(g)} ${p:.2f}/hr" for g, p, gb in sorted(stock, key=lambda x: x[1]))
            + f". The app uses cards with {MIN_GPU_GB} GB or more up to ${MAX_GPU_PRICE:.2f}/hr.")


def _adopt_or_kill(token):
    """After a create call failed to answer: claim the pod it made if it made one.

    The youngest unrecorded `trafficlens` pod rented in the last ten minutes is taken as
    this attempt's, booting or not -- it cannot prove itself yet, and killing it for
    that just makes another. Every other unrecorded one is a stray and is terminated.
    Ownership is settled by the first signed call after boot: a stranger's agent answers
    403 and the pod is dropped then.
    """
    from datetime import datetime, timezone
    known = {r["pod_id"] for r in db.rows("SELECT pod_id FROM cloud_runs WHERE pod_id IS NOT NULL")}
    d, err = cloud._gql("query { myself { pods { id name costPerHr lastStatusChange } } }")
    if err:
        return None
    cands = []
    for p in ((d or {}).get("myself") or {}).get("pods") or []:
        if p["id"] in known or (p.get("name") or "") != "trafficlens":
            continue
        age = None
        try:
            # "Rented by User: Tue Sep 08 2026 08:21:40 GMT+0000 (...)"
            stamp = (p.get("lastStatusChange") or "").split(": ", 1)[1].split(" GMT")[0]
            age = (datetime.now(timezone.utc) - datetime.strptime(
                stamp, "%a %b %d %Y %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds()
        except Exception:
            age = None
        cands.append((age if age is not None else 1e9, p))
    cands.sort(key=lambda x: x[0])
    claimed = None
    for age, p in cands:
        if claimed is None and age < 600:
            claimed = {"id": p["id"], "token": token, "cost_per_hr": p.get("costPerHr") or 0,
                       "adopted": True}
        else:
            cloud.terminate(p["id"])
    return claimed


def _gql_retry(q, args, tries=3, timeout=90):
    """Community-cloud capacity comes and goes; one refusal is not an answer."""
    err = None
    for n in range(tries):
        d, err = cloud._gql(q, args, timeout=timeout)
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
            if not pod.get("tcp"):
                pod["tcp"] = _direct_port(pod["id"])
        # The direct port first if RunPod has published one, the proxy otherwise. A pod
        # sat fully booted for six minutes answering /health over its TCP port in 2.4s
        # while the proxy returned nothing, and was about to be abandoned as dead.
        bases = ([f"http://{pod['tcp'][0]}:{pod['tcp'][1]}"] if pod.get("tcp") else []) \
                + [_url(pod["id"])]
        for base in bases:
            try:
                req = urllib.request.Request(base + "/health",
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
            # Say what is happening, not just that time is passing. Almost all of this
            # wait is one 4.6GB image download onto a machine that has never run it, which
            # is normal and unavoidable -- but "starting the GPU… 87s" with no explanation
            # looks identical to a hang, and the surveyor cannot tell a working machine
            # from a broken one. Measured: a good host answers in about 115 seconds.
            el = int(time.time() - t0)
            if el < 45:
                on_note(f"renting a machine… {el}s")
            elif el < 150:
                on_note(f"downloading the detector onto it, 4.6 GB — "
                        f"normal on a new machine ({el}s)")
            else:
                on_note(f"still starting after {el}s — slower than the usual 2 minutes. "
                        f"Will try another machine if it does not answer.")
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

        preferred = cloud.config()["gpu"]
        plan = _gpu_plan(preferred, on_note)
        if not plan:
            dc = stash.datacenter()
            return None, (f"{dc}, where the storage volume is, has no suitable graphics "
                          f"card free right now. {_stock_note(dc)} Capacity there changes "
                          f"by the hour — press Process again in a few minutes.")
        detail = None
        out_of_stock = set()
        for attempt in range(1, BOOT_TRIES + 1):
            pod = err = None
            for gpu, price in plan:
                if gpu in out_of_stock:
                    continue
                if on_note:
                    on_note(f"renting a {gpu}…"
                            + (f" (machine {attempt} of {BOOT_TRIES})" if attempt > 1 else ""))
                pod, err = _create(gpu, secrets.token_urlsafe(24))
                if pod:
                    break
                if _no_stock(err):
                    # Gone between the stock probe and the create; the next card in the
                    # plan is the answer, not the same request again.
                    out_of_stock.add(gpu)
                    continue
                return None, err
            if not pod:
                dc = stash.datacenter() or "RunPod"
                return None, (f"every suitable card in {dc} was taken before the app could "
                              f"rent it ({', '.join(sorted(out_of_stock)) or preferred}). "
                              f"{_stock_note(dc) if stash.is_runpod() else ''} Press Process "
                              f"again in a few minutes.")
            cloud.note_work()
            t0 = time.time()
            ready, detail = _wait_ready(pod, on_note)
            note_phase("boot", f"{gpu} — {'ready' if ready else 'failed'}",
                       seconds=time.time() - t0)
            # Anything that cannot be used must not stay billing. This is the failure path
            # most likely to leak money, so it terminates before it retries or reports.
            if not ready:
                cloud.terminate(pod["id"])
                db.run("""UPDATE cloud_runs SET note = COALESCE(note,'')
                            || ' · never came up' WHERE pod_id=?""", pod["id"])
                continue
            pod["gpu_name"] = detail
            try:
                _call(pod, "/progress", timeout=20)
            except Exception as e:
                # Health answered but our signature did not: this is not our pod (an
                # adopted stranger, or a token mix-up). It must not be uploaded to.
                cloud.terminate(pod["id"])
                return None, f"the rented machine did not accept this app's signature ({e})"
            pod["tcp"] = pod.get("tcp") or _direct_port(pod["id"])
            if on_note:
                on_note("direct upload port: " + (f"{pod['tcp'][0]}:{pod['tcp'][1]}"
                        if pod["tcp"] else "none on this host — uploads go via the proxy"))
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
    """Send one file as a sequence of byte-range parts.

    Each part is its own request with X-Offset and X-Total, so the agent writes it in
    place. A failed part is retried on a fresh connection; only that part is resent.
    """
    name = Path(path).name
    sent, t0, last = 0, time.time(), 0.0
    with open(path, "rb") as f:
        while sent < size:
            body = f.read(min(PART, size - sent))
            if not body:
                break
            _put_part(pod, rel, body, sent, size, name)
            sent += len(body)
            now = time.time()
            if on_note and (now - last > 3 or sent >= size):
                last = now
                rate = sent / max(now - t0, 1e-6) / 1e6
                left = (size - sent) / 1e6 / max(rate, 1e-6)
                on_note(f"sending {name} — {sent / 1e6:.0f} of {size / 1e6:.0f} MB "
                        f"at {rate:.1f} MB/s"
                        + (f", {left / 60:.0f} min left" if left > 90 else ""))
    note_phase("upload", name, seconds=time.time() - t0, mb=size / 1e6,
               mbps=(size / 1e6) / max(time.time() - t0, 1e-6))


def _put_part(pod, rel, body, offset, total, name):
    """One part, with retries. Raises with a readable reason if it cannot be delivered."""
    import http.client

    last = None
    for attempt in range(1, PART_TRIES + 1):
        # The socket timeout bounds each blocking call, not the part, so it is set per
        # 4MB send: 60s means a part is abandoned once it drops below ~0.07 MB/s, which
        # is "stalled" on any link that could ever finish a recording.
        tcp = pod.get("tcp")
        if tcp:
            conn = http.client.HTTPConnection(tcp[0], tcp[1], timeout=60, blocksize=CHUNK)
        else:
            conn = http.client.HTTPSConnection(_url(pod["id"]).replace("https://", ""),
                                               timeout=60, blocksize=CHUNK)
        try:
            conn.putrequest("PUT", "/" + rel, skip_accept_encoding=True)
            conn.putheader("Connection", "close")
            for k, v in _sign(pod, "PUT", "/" + rel, len(body)).items():
                conn.putheader(k, v)
            conn.putheader("User-Agent", cloud.UA)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("X-Offset", str(offset))
            conn.putheader("X-Total", str(total))
            conn.endheaders()
            view = memoryview(body)
            for i in range(0, len(body), CHUNK):
                conn.send(view[i:i + CHUNK])
            r = conn.getresponse()
            payload = r.read()
            if r.status == 200:
                return
            last = f"HTTP {r.status} {payload[:120].decode(errors='replace')}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        finally:
            conn.close()
        if attempt < PART_TRIES:
            # Recorded, not swallowed. A part quietly failing twice and succeeding on the
            # third try looks exactly like a slow network from the outside, and the two
            # need completely different fixes.
            note_phase("retry", f"{name} at {offset / 1e6:.0f} MB — {last}")
            time.sleep(2 * attempt)
    raise RuntimeError(
        f"could not send {name} to the GPU: part at {offset / 1e6:.0f} MB failed "
        f"{PART_TRIES} times ({last}). This is usually the network between this computer "
        f"and RunPod rather than the recording.")


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
            try:
                if stash.config()["configured"]:
                    _stage(path)             # into the bucket; needs no pod at all
                elif _POD.get("id"):
                    _put(pod, f"video/{Path(path).name}", path)
                else:
                    return
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
        # The recording goes to the bucket, not to the pod. The pod fetches it from there
        # over a datacenter link. Only when no bucket is configured does it go the old way,
        # straight at the pod, which works for a small clip and not for a station recording.
        video_url = video_path = None
        if stash.config()["configured"]:
            key = _stage(v["path"], note)
            if stash.is_runpod():
                video_path = stash.mount_path(key)      # already on the pod's disk
                note(f"{Path(v['path']).name} is on the GPU's storage")
            else:
                video_url = stash.url_for(key)
                note(f"{Path(v['path']).name} is in the bucket — the GPU is fetching it")
        else:
            _put(pod, f"video/{Path(v['path']).name}", v["path"], note)

        stride = engine.stride_for(v["fps"])
        _call(pod, "/run", json.dumps({
            "video": Path(v["path"]).name, "weights": weights.name,
            "video_url": video_url, "video_path": video_path,
            "imgsz": imgsz, "conf": conf, "stride": stride,
            "frames": v["frames"]}).encode())

        # The GPU is now busy for minutes. Use that time to send the next clip rather
        # than leaving the uplink idle and then making the surveyor wait for it.
        _prefetch(pod)

        t_detect = time.time()
        last_beat = time.time()
        while True:
            time.sleep(4)
            import engine as _e
            if _e.ABORT.is_set():
                raise RuntimeError("stopped by the surveyor")
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

        note_phase("detect", Path(v["path"]).name, seconds=time.time() - t_detect)
        note("bringing the results back")
        blob = _call(pod, "/result", raw=True, timeout=1800)
        res = json.loads(gzip.decompress(blob))
        _ingest(video_id, use_id, res)
        cloud.note_work()
        # Only once the trajectories are safely in the database. Deleting earlier would
        # mean a failed ingest could not be retried without sending the video again.
        _forget(pod, f"video/{Path(v['path']).name}")
        # Only after the results are in. A run that failed keeps its staged object so the
        # retry does not upload a gigabyte again; a crash's leftovers are swept at start.
        _unstage(v["path"])

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
