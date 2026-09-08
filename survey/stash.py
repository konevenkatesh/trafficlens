"""Staging storage: where a recording goes so a GPU pod can fetch it at datacenter speed.

This is the pattern every cloud GPU workflow uses and the one this app should have used
from the start: the surveyor's machine uploads to an object store in a nearby region, and
the pod -- wherever it is -- pulls from that store over a datacenter link. The machine
never pushes bulk data at the pod. It was tried, through RunPod's Cloudflare-fronted proxy
and then over a raw TCP port, and from India to a residential community-cloud host it ran
at 0.1-0.2 MB/s with minute-long stalls; the same machine reaches Cloudflare's edge at
7-13 MB/s. A gigabyte recording is two minutes to the bucket, seconds from the bucket to
the pod.

The store is RunPod's own network storage, because it needs nothing new: the same
account, the same console, one extra S3 key made under Settings. A network volume is the
"bucket" (its ID is the bucket name), it lives in one datacenter, and a pod created in
that datacenter MOUNTS it at /workspace -- so a recording uploaded as
s3://<volume>/trafficlens/x.mp4 is simply the file /workspace/trafficlens/x.mp4 on the
pod. No download step, no presigned URL (RunPod does not support them), no credentials
anywhere near the pod. Any other S3-compatible store also works, via a presigned URL the
pod fetches, for a site that already has one.

Objects are deleted once the results are in, and anything left over from a crash is swept
on the next start, because a bucket quietly holding a station day of footage is a bill.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db

PREFIX = "trafficlens/"
PART = 64 << 20            # S3 multipart parts; well over the 5MB minimum, under any cap
PARALLEL = 4               # parts in flight; the link is the limit, not the CPU
TRIES = 4
URL_TTL = 6 * 3600         # a presigned URL outlives any single detection
SWEEP_AFTER = 24 * 3600    # leftovers older than this are a crash's debris

_LOCK = threading.Lock()


# ───────────────────────────── settings ─────────────────────────────
def config():
    """What the Settings screen shows. The secret is never returned."""
    import cloud
    g = cloud._setting
    key = g("s3_key", "") or ""
    secret = g("s3_secret", "") or ""
    return {
        "endpoint": g("s3_endpoint", "") or "",
        "region": g("s3_region", "") or "",
        "bucket": g("s3_bucket", "") or "",
        "key_hint": (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("set" if key else ""),
        "configured": bool(key and secret and (g("s3_bucket", "") or "")),
    }


def normalise_endpoint(ep):
    """What a surveyor pastes, made into what boto3 needs: scheme added, path dropped."""
    ep = (ep or "").strip()
    if not ep:
        return ""
    if not re.match(r"^https?://", ep, re.I):
        ep = "https://" + ep
    m = re.match(r"^(https?://[^/\s]+)", ep, re.I)
    return (m.group(1) if m else ep).rstrip("/")


def volume_datacenter(volume_id):
    """Ask RunPod which datacenter a network volume lives in: (datacenter, reason).

    The bucket IS the volume ID, and the account's RunPod key is already saved for
    renting GPUs, so nobody should have to know or type the datacenter at all.
    """
    import cloud
    volume_id = (volume_id or "").strip()
    if not volume_id:
        return None, "the Bucket box is empty"
    if not cloud._key():
        return None, "no RunPod key is saved, so the app cannot ask which datacenter it is in"
    data, err = cloud._gql("query { myself { networkVolumes { id name dataCenterId } } }",
                           timeout=20)
    if err:
        return None, f"RunPod did not answer ({err})"
    vols = ((data or {}).get("myself") or {}).get("networkVolumes") or []
    for v in vols:
        if v.get("id") == volume_id and v.get("dataCenterId"):
            return v["dataCenterId"].upper(), ""
    have = ", ".join(f"{v.get('id')} ({v.get('name')}, {v.get('dataCenterId')})" for v in vols)
    return None, (f"RunPod has no network volume with ID {volume_id}"
                  + (f" — it has: {have}" if have else " — the account has no network volumes"))


def resolve():
    """Fill a blank endpoint/region from the volume, if RunPod will tell us.

    Returns "" when the endpoint and region are set (already, or now), else why not.
    """
    import cloud
    cfg = config()
    if cfg["endpoint"] and cfg["region"].lower() not in ("", "auto"):
        return ""
    dc, why = volume_datacenter(cfg["bucket"])
    if not dc:
        return why
    cloud._set("s3_endpoint", cfg["endpoint"] or f"https://s3api-{dc.lower()}.runpod.io")
    if cfg["region"].lower() in ("", "auto"):
        cloud._set("s3_region", dc)
    return ""


def save_config(endpoint=None, region=None, bucket=None, key=None, secret=None):
    import cloud
    if endpoint is not None:
        endpoint = normalise_endpoint(endpoint)
    ep = (endpoint if endpoint is not None else cloud._setting("s3_endpoint", "")) or ""
    if region is not None:
        region = region.strip()
        # A RunPod endpoint names its datacenter: https://s3api-eu-ro-1.runpod.io is the
        # region EU-RO-1. Derive it rather than ask, because the field defaulted to
        # "auto", RunPod rejects "auto", and a surveyor has no way to know either.
        m = re.search(r"s3api-([a-z0-9-]+)\.runpod\.io", ep.lower())
        if m and region.lower() in ("", "auto"):
            region = m.group(1).upper()
        cloud._set("s3_region", region or "auto")
    # ...and the other way round: a datacenter ID with no endpoint is still enough. The
    # Endpoint box shows the RunPod URL as a grey hint, and a hint reads as filled in.
    reg = (region if region is not None else cloud._setting("s3_region", "")) or ""
    if not ep and re.fullmatch(r"[A-Za-z]{2}-[A-Za-z]{2,3}-\d+", reg):
        endpoint = f"https://s3api-{reg.lower()}.runpod.io"
    if endpoint is not None:
        cloud._set("s3_endpoint", endpoint)
    if bucket is not None:
        cloud._set("s3_bucket", bucket.strip())
    if key is not None and key.strip():
        cloud._set("s3_key", key.strip())
    if secret is not None and secret.strip():
        cloud._set("s3_secret", secret.strip())
    resolve()
    return config()


def is_runpod():
    return "runpod.io" in (config()["endpoint"] or "")


def datacenter():
    """The datacenter the volume lives in -- the pod must be created there to mount it."""
    return (config()["region"] or "").upper() if is_runpod() else None


def mount_path(key):
    """Where an uploaded object appears inside a pod that has the volume mounted."""
    return f"/workspace/{key}"


def _client():
    import boto3
    from botocore.config import Config
    import cloud
    g = cloud._setting
    if not config()["configured"]:
        raise RuntimeError("storage is not set up — add the bucket in Settings")
    kw = {"aws_access_key_id": g("s3_key", ""), "aws_secret_access_key": g("s3_secret", ""),
          "region_name": g("s3_region", "auto") or "auto",
          "config": Config(retries={"max_attempts": 3, "mode": "standard"},
                           connect_timeout=20, read_timeout=120,
                           s3={"addressing_style": "path"})}
    ep = g("s3_endpoint", "")
    if ep:
        kw["endpoint_url"] = ep
    return boto3.client("s3", **kw)


def check():
    """Can this app reach the bucket with these credentials? Said in one sentence."""
    why = resolve()
    cfg = config()
    if not cfg["endpoint"]:
        return {"ok": False, "message": "the Endpoint URL is not set and could not be "
                f"worked out: {why}. Type it in (for a volume in EU-RO-1 it is "
                "https://s3api-eu-ro-1.runpod.io)"}
    if not cfg["bucket"]:
        return {"ok": False, "message": "the Bucket box is empty — paste the network volume ID"}
    if not cfg["configured"]:
        return {"ok": False, "message": "the access key or secret key is missing"}
    try:
        c = _client()
        b = cfg["bucket"]
        c.head_bucket(Bucket=b)
        # Round-trip a byte, because head_bucket alone can pass on read-only credentials.
        k = PREFIX + "_check"
        c.put_object(Bucket=b, Key=k, Body=b"ok")
        c.delete_object(Bucket=b, Key=k)
        return {"ok": True, "message": f"bucket {b} is reachable and writable"}
    except Exception as e:
        return {"ok": False, "message": _reason(e, cfg)}


def _reason(e, cfg=None):
    s = str(e)
    ep = (cfg or {}).get("endpoint") or ""
    for needle, plain in (("InvalidAccessKeyId", "the access key is not recognised"),
                          ("SignatureDoesNotMatch", "the secret key is wrong"),
                          ("NoSuchBucket", "there is no bucket by that name"),
                          ("AccessDenied", "these credentials cannot write to that bucket"),
                          # RunPod answers a wrong key, secret or volume ID with a bare 403.
                          ("(403)", "RunPod refused these credentials for that bucket — "
                                    "check the access key, the secret and the volume ID"),
                          ("Could not connect", f"nothing answers at {ep} — check the "
                                                "endpoint URL and the internet connection"),
                          ("Connect timeout", f"{ep} did not answer in time"),
                          ("Name or service not known", f"{ep} does not resolve"),
                          ("SSL", f"the secure connection to {ep} failed")):
        if needle in s:
            return plain
    return s[:160]


# ───────────────────────────── upload ─────────────────────────────
def upload(path, on_note=None):
    """Put one file in the bucket. Returns its object key.

    Multipart, parts in parallel, each part retried on its own: a dropped connection
    costs one 64MB part, and four parts in flight keep a fat link busy. Progress is
    reported with a rate, because a station recording is a gigabyte and a bar that says
    nothing for two minutes reads as a hang.
    """
    path = Path(path)
    size = path.stat().st_size
    c = _client()
    b = config()["bucket"]
    key = f"{PREFIX}{int(time.time())}-{path.name}"
    if size <= PART:
        with open(path, "rb") as f:
            c.put_object(Bucket=b, Key=key, Body=f)
        if on_note:
            on_note(f"sent {path.name} ({size / 1e6:.0f} MB)")
        return key

    mp = c.create_multipart_upload(Bucket=b, Key=key)
    uid = mp["UploadId"]
    n_parts = -(-size // PART)
    done = {"bytes": 0}
    t0 = time.time()
    last = [0.0]

    def send(i):
        off = i * PART
        n = min(PART, size - off)
        with open(path, "rb") as f:
            f.seek(off)
            body = f.read(n)
        err = None
        for attempt in range(1, TRIES + 1):
            try:
                r = c.upload_part(Bucket=b, Key=key, UploadId=uid, PartNumber=i + 1, Body=body)
                with _LOCK:
                    done["bytes"] += n
                    now = time.time()
                    if on_note and (now - last[0] > 3 or done["bytes"] >= size):
                        last[0] = now
                        rate = done["bytes"] / max(now - t0, 1e-6) / 1e6
                        left = (size - done["bytes"]) / 1e6 / max(rate, 1e-6)
                        on_note(f"sending {path.name} — {done['bytes'] / 1e6:.0f} of "
                                f"{size / 1e6:.0f} MB at {rate:.1f} MB/s"
                                + (f", {left / 60:.0f} min left" if left > 90 else ""))
                return {"PartNumber": i + 1, "ETag": r["ETag"]}
            except Exception as e:
                err = e
                time.sleep(2 * attempt)
        raise RuntimeError(f"part {i + 1} of {n_parts} failed {TRIES} times: {_reason(err)}")

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            futs = [ex.submit(send, i) for i in range(n_parts)]
            parts = [f.result() for f in as_completed(futs)]
        parts.sort(key=lambda p: p["PartNumber"])
        c.complete_multipart_upload(Bucket=b, Key=key, UploadId=uid,
                                    MultipartUpload={"Parts": parts})
    except Exception:
        # Never leave a half-finished multipart behind: its parts are stored and billed
        # until aborted, invisibly, forever.
        try:
            c.abort_multipart_upload(Bucket=b, Key=key, UploadId=uid)
        except Exception:
            pass
        raise
    return key


def url_for(key, ttl=URL_TTL):
    """A presigned GET: read one object, for a while, with no credentials attached."""
    c = _client()
    return c.generate_presigned_url("get_object",
                                    Params={"Bucket": config()["bucket"], "Key": key},
                                    ExpiresIn=ttl)


def delete(key):
    try:
        _client().delete_object(Bucket=config()["bucket"], Key=key)
        return True
    except Exception:
        return False


def sweep():
    """Remove leftovers from a crash: objects and unfinished multiparts older than a day."""
    if not config()["configured"]:
        return {"objects": 0, "multiparts": 0}
    c = _client()
    b = config()["bucket"]
    cutoff = time.time() - SWEEP_AFTER
    objs = mps = 0
    try:
        tok = None
        while True:
            kw = {"Bucket": b, "Prefix": PREFIX}
            if tok:
                kw["ContinuationToken"] = tok
            r = c.list_objects_v2(**kw)
            for o in r.get("Contents") or []:
                if o["LastModified"].timestamp() < cutoff:
                    c.delete_object(Bucket=b, Key=o["Key"])
                    objs += 1
            if not r.get("IsTruncated"):
                break
            tok = r.get("NextContinuationToken")
        r = c.list_multipart_uploads(Bucket=b, Prefix=PREFIX)
        for u in r.get("Uploads") or []:
            if u["Initiated"].timestamp() < cutoff:
                c.abort_multipart_upload(Bucket=b, Key=u["Key"], UploadId=u["UploadId"])
                mps += 1
    except Exception:
        pass
    return {"objects": objs, "multiparts": mps}
