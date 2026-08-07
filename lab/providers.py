"""Provider clients: OpenRouter (VLM judges) and RunPod (training GPUs).

Every call that costs money returns its own price so the Lab can book it the
moment it happens -- no end-of-run reconciliation, no guessing.
"""
import base64
import json
import time
import urllib.error
import urllib.request

import db

OR_BASE = "https://openrouter.ai/api/v1"
RP_REST = "https://rest.runpod.io/v1"
RP_GQL = "https://api.runpod.io/graphql"
_cache = {"models": None, "models_ts": 0}


UA = "TrafficLensLab/1.0"


def _req(url, method="GET", headers=None, body=None, timeout=90):
    # RunPod sits behind Cloudflare, which rejects urllib's default User-Agent
    # with error 1010 -- always send our own.
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        "User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, str(e)[:300]


# ════════════════════════════════ OpenRouter ════════════════════════════════
def or_key():
    return db.get_setting("openrouter_key", "")


def or_headers():
    return {"Authorization": f"Bearer {or_key()}",
            "HTTP-Referer": "http://localhost:8800", "X-Title": "TrafficLens Lab"}


"""Balances are shown on every page, but they are network calls to other companies.
Fetching them per request put ~0.5s of someone else's latency in front of every
navigation, which reads as a frozen app. Cache briefly; money does not move that fast."""
_bal_cache = {}
BALANCE_TTL_S = 60


def _cached_list(key, fn, ttl):
    """Same idea as _cached but for list results, where "ok" is simply non-empty."""
    hit = _bal_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if val:
        _bal_cache[key] = (time.time(), val)
    return val


def _cached(key, fn):
    hit = _bal_cache.get(key)
    if hit and time.time() - hit[0] < BALANCE_TTL_S:
        return hit[1]
    val = fn()
    if val.get("ok"):                      # never cache a failure — retry those
        _bal_cache[key] = (time.time(), val)
    return val


def or_balance():
    return _cached("or", _or_balance_live)


def _or_balance_live():
    if not or_key():
        return {"ok": False, "error": "no key"}
    d, err = _req(f"{OR_BASE}/credits", headers=or_headers(), timeout=20)
    if err:
        return {"ok": False, "error": err}
    c = d.get("data", {})
    total, used = float(c.get("total_credits", 0)), float(c.get("total_usage", 0))
    return {"ok": True, "total": total, "used": used, "remaining": round(total - used, 4)}


def or_models(force=False):
    """Live catalog with pricing. Cached 10 min -- pricing does move."""
    if not force and _cache["models"] and time.time() - _cache["models_ts"] < 600:
        return _cache["models"]
    d, err = _req(f"{OR_BASE}/models", timeout=30)
    if err:
        return _cache["models"] or []
    out = []
    for m in d.get("data", []):
        arch = m.get("architecture", {}) or {}
        p = m.get("pricing", {}) or {}
        try:
            pin, pout = float(p.get("prompt") or 0), float(p.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "id": m["id"], "name": m.get("name", m["id"]),
            "vision": "image" in (arch.get("input_modalities") or []),
            "in_per_m": round(pin * 1e6, 4), "out_per_m": round(pout * 1e6, 4),
            "context": m.get("context_length"),
            "free": pin == 0 and pout == 0,
        })
    out.sort(key=lambda x: (x["in_per_m"], x["out_per_m"]))
    _cache["models"], _cache["models_ts"] = out, time.time()
    return out


def price_of(model_id):
    for m in or_models():
        if m["id"] == model_id:
            return m["in_per_m"], m["out_per_m"]
    return 0.0, 0.0


RETRYABLE = ("429", "rate-limit", "temporarily", "502", "503", "504", "timed out",
             "Image content is not supported")


def or_vision(model, prompt, image_paths, max_tokens=120, temperature=0.0,
              timeout=120, tries=3):
    """One judgment. Returns (text, usage_dict, cost_usd, latency_ms, error).

    Retries matter here: OpenRouter routes a model across several upstream
    providers and some of them answer a vision request with a text-only variant
    or a rate-limit. A retry usually lands on a different provider.
    """
    if not or_key():
        return None, {}, 0.0, 0, "no OpenRouter key configured"
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        try:
            b64 = base64.b64encode(open(p, "rb").read()).decode()
        except OSError as e:
            return None, {}, 0.0, 0, f"crop unreadable: {e}"
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "user", "content": content}]}
    t0, last = time.time(), None
    for attempt in range(tries):
        d, err = _req(f"{OR_BASE}/chat/completions", "POST", or_headers(), body,
                      timeout=timeout)
        if not err:
            try:
                txt = d["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                last = f"malformed response: {str(d)[:160]}"
            else:
                u = d.get("usage", {}) or {}
                pin, pout = price_of(model)
                cost = (u.get("prompt_tokens", 0) * pin
                        + u.get("completion_tokens", 0) * pout) / 1e6
                return txt, u, cost, int((time.time() - t0) * 1000), None
        else:
            last = err
        if attempt < tries - 1 and any(k.lower() in (last or "").lower() for k in RETRYABLE):
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    return None, {}, 0.0, int((time.time() - t0) * 1000), last


# ═════════════════════════════════ RunPod ═══════════════════════════════════
def rp_key():
    return db.get_setting("runpod_key", "")


def rp_headers():
    return {"Authorization": f"Bearer {rp_key()}"}


def rp_gql(query, variables=None):
    if not rp_key():
        return None, "no key"
    return _req(RP_GQL, "POST", rp_headers(),        # key in the header, not the URL
                {"query": query, "variables": variables or {}}, timeout=30)


def rp_balance():
    return _cached("rp", _rp_balance_live)


def _rp_balance_live():
    d, err = rp_gql("query { myself { clientBalance } }")
    if err or not d:
        return {"ok": False, "error": err or "no data"}
    try:
        return {"ok": True, "remaining": round(float(d["data"]["myself"]["clientBalance"]), 4)}
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "unexpected shape"}


def rp_pods():
    """Live pods with runtime telemetry.

    GraphQL first: the REST list answers faster but omits `runtime`, so GPU
    utilisation and uptime come back empty -- which is most of what a training
    monitor is for. REST is the fallback.
    """
    if not rp_key():
        return []
    d, err = rp_gql("""query { myself { pods {
        id name desiredStatus costPerHr machineId
        runtime { uptimeInSeconds gpus { id gpuUtilPercent memoryUtilPercent }
                  container { cpuPercent memoryPercent } }
        machine { gpuDisplayName } } } }""")
    if not err and d:
        try:
            return [_pod_norm(p) for p in d["data"]["myself"]["pods"]]
        except (KeyError, TypeError):
            pass
    d, err = _req(f"{RP_REST}/pods", headers=rp_headers(), timeout=25)
    if err:
        return []
    if isinstance(d, list):
        return [_pod_norm(p) for p in d]
    if isinstance(d, dict) and "data" in d:
        return [_pod_norm(p) for p in d["data"]]
    return []


def _pod_norm(p):
    rt = p.get("runtime") or {}
    gpus = rt.get("gpus") or []
    mach = p.get("machine") or {}
    return {
        "id": p.get("id"), "name": p.get("name"),
        "status": p.get("desiredStatus") or p.get("status"),
        "hourly": float(p.get("costPerHr") or 0),
        "gpu": mach.get("gpuDisplayName") or p.get("gpuTypeId") or "?",
        "uptime_s": rt.get("uptimeInSeconds") or 0,
        "gpu_util": gpus[0].get("gpuUtilPercent") if gpus else None,
        "mem_util": gpus[0].get("memoryUtilPercent") if gpus else None,
        "cpu": (rt.get("container") or {}).get("cpuPercent"),
    }


def rp_gpu_types():
    """GPU catalog with prices. Cached 10 min -- this is a price list, not telemetry,
    and it was adding 0.6s to every visit to the Training page."""
    return _cached_list("gpu_types", _rp_gpu_types_live, 600)


def _rp_gpu_types_live():
    d, err = rp_gql("""query { gpuTypes { id displayName memoryInGb
        lowestPrice(input:{gpuCount:1}) { uninterruptablePrice minimumBidPrice } } }""")
    if err or not d:
        return []
    out = []
    for g in (d.get("data", {}).get("gpuTypes") or []):
        lp = g.get("lowestPrice") or {}
        out.append({"id": g["id"], "name": g.get("displayName"),
                    "vram": g.get("memoryInGb"),
                    "on_demand": lp.get("uninterruptablePrice"),
                    "spot": lp.get("minimumBidPrice")})
    return [g for g in out if g["on_demand"]]


def rp_terminate(pod_id):
    """Terminate + verify. Never trust a single success response."""
    _req(f"{RP_REST}/pods/{pod_id}", "DELETE", rp_headers(), timeout=25)
    rp_gql("mutation($id:String!){ podTerminate(input:{podId:$id}) }", {"id": pod_id})
    time.sleep(3)
    alive = [p["id"] for p in rp_pods()]
    return {"terminated": pod_id not in alive, "alive": alive}
