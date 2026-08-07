"""Place search, proxied.

Nominatim (OpenStreetMap) needs no API key, but its usage policy requires a real
User-Agent identifying the application and at most one request per second. A browser
cannot set a User-Agent, so the request goes through here instead — which also lets us
hold the rate limit and cache repeats, both of which the policy asks for.

https://operations.osmfoundation.org/policies/nominatim/

If a keyed provider is preferred later, only `search()` changes; the endpoint and the
UI stay as they are.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

ENDPOINT = "https://nominatim.openstreetmap.org/search"
UA = "TrafficLens/1.0 (Indian traffic survey app; contact venkatesh@bimsaarthi.com)"
MIN_INTERVAL_S = 1.1          # the policy says 1 req/s; leave headroom
_lock = threading.Lock()
_last_call = 0.0
_cache = {}                   # query -> (fetched_at, results)
CACHE_TTL_S = 24 * 3600


def search(q, limit=6, country="in"):
    """Look up a place by name. Returns [{name, lat, lon, kind}], never raises."""
    q = (q or "").strip()
    if len(q) < 3:
        return []
    key = (q.lower(), country, limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]

    global _last_call
    with _lock:                                   # serialise and pace every outbound call
        wait = MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        params = {"q": q, "format": "jsonv2", "limit": str(limit), "addressdetails": "1"}
        if country:
            params["countrycodes"] = country      # survey work is in India; keep it relevant
        url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = json.loads(r.read().decode())
        except Exception:
            return []

    out = []
    for item in raw:
        try:
            out.append({
                "name": item.get("display_name", "")[:160],
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "kind": item.get("type") or item.get("category") or "",
            })
        except (KeyError, TypeError, ValueError):
            continue
    _cache[key] = (time.time(), out)
    return out
