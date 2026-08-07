"""How deep the satellite imagery actually goes, per station.

Coverage is local, not global. Measured on this project's own stations: Bidar city has
real tiles at z19 (~0.28 m/px) while Bhalki, 40 km away, stops at z18 — and Anantapur
stops at z18 too. A single global `maxNativeZoom` is therefore wrong in both directions:
too shallow and every station is needlessly blurry, too deep and the ones with thinner
coverage get the "map data not yet available" placeholder.

So each station is probed once, when it gets coordinates, and the answer is stored.

The placeholder is the awkward part: it returns HTTP 200 with a valid PNG, so it cannot
be detected as a failure — only by size. It is a small, flat image (2,521 bytes at the
time of writing); real photography over a road is several times larger.
"""
import math
import urllib.error
import urllib.request

TILE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")
UA = {"User-Agent": "TrafficLens/1.0 (traffic survey app)"}
REAL_MIN_BYTES = 4000       # placeholder measured at 2,521; real road tiles run 13-30 KB
DEEPEST, SHALLOWEST = 20, 15
DEFAULT_ZOOM = 18           # what to assume when the probe cannot run (offline)


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
             / math.pi) / 2 * n)
    return x, y


def _tile_bytes(lat, lon, z, timeout=12):
    x, y = tile_xy(lat, lon, z)
    req = urllib.request.Request(TILE.format(z=z, x=x, y=y), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return len(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return 0


def deepest_zoom(lat, lon):
    """Deepest zoom with real photography at this point, or None if it can't be probed."""
    if lat is None or lon is None:
        return None
    probed_any = False
    for z in range(DEEPEST, SHALLOWEST - 1, -1):
        n = _tile_bytes(lat, lon, z)
        if n:
            probed_any = True
        if n >= REAL_MIN_BYTES:
            return z
    return None if not probed_any else SHALLOWEST


def metres_per_pixel(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)
