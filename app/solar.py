"""Where the sun is, and whether it is shining into the camera.

A station records the compass bearing its camera looks along. Combined with the
station's coordinates that is enough to predict, for any date, the window when the sun
sits low and roughly in front of the lens — the condition that produces the glare which
wrecks detection. Knowing it in advance means a survey can be scheduled around it, and a
bad hour can be explained rather than argued about.

NOAA solar position equations; accurate to well under a degree, which is far finer than
the +/-35 degree cone this is used for. No dependencies, no network.
"""
import math
from datetime import datetime, timedelta

IST_OFFSET_H = 5.5          # India Standard Time; stations here are all in India


def position(dt, lat, lon, tz_h=IST_OFFSET_H):
    """Solar elevation and azimuth (degrees, azimuth clockwise from true north)."""
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)          # fractional year
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    time_offset = eqtime + 4 * lon - 60 * tz_h
    tst = hour * 60 + time_offset                                  # true solar time, minutes
    ha = math.radians(tst / 4 - 180)                               # hour angle
    latr = math.radians(lat)
    cos_zen = (math.sin(latr) * math.sin(decl)
               + math.cos(latr) * math.cos(decl) * math.cos(ha))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zen = math.acos(cos_zen)
    elev = 90 - math.degrees(zen)
    sin_zen = math.sin(zen)
    if abs(sin_zen) < 1e-6:
        return elev, 180.0
    cos_az = (math.sin(latr) * cos_zen - math.sin(decl)) / (math.cos(latr) * sin_zen)
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.degrees(math.acos(cos_az))
    if math.degrees(ha) > 0:
        az = 360 - az
    return elev, az % 360


def _delta(a, b):
    """Smallest angle between two bearings."""
    return abs((a - b + 180) % 360 - 180)


def glare_windows(lat, lon, bearing, date=None, *, max_elev=30.0, cone=35.0, step_min=10):
    """Periods on `date` when the sun is low and within `cone` degrees of where the
    camera looks. Returns [{start, end, worst_elev}] in local clock time."""
    if lat is None or lon is None or bearing is None:
        return []
    day = date or datetime.now().date()
    t = datetime(day.year, day.month, day.day, 0, 0)
    out, cur = [], None
    for _ in range(24 * 60 // step_min + 1):
        elev, az = position(t, lat, lon)
        hit = 0 < elev <= max_elev and _delta(az, bearing) <= cone
        if hit and cur is None:
            cur = {"start": t, "end": t, "worst_elev": elev}
        elif hit:
            cur["end"] = t
            cur["worst_elev"] = min(cur["worst_elev"], elev)
        elif cur:
            out.append(cur)
            cur = None
        t += timedelta(minutes=step_min)
    if cur:
        out.append(cur)
    return [{"start": w["start"].strftime("%H:%M"), "end": w["end"].strftime("%H:%M"),
             "worst_elev": round(w["worst_elev"], 1)} for w in out]


def daylight(lat, lon, date=None):
    """Sunrise/sunset by scanning elevation — good enough to plan a survey day."""
    if lat is None or lon is None:
        return {}
    day = date or datetime.now().date()
    t = datetime(day.year, day.month, day.day, 0, 0)
    rise = sett = None
    prev = None
    for _ in range(24 * 12 + 1):
        elev, _az = position(t, lat, lon)
        if prev is not None:
            if prev < 0 <= elev and rise is None:
                rise = t
            if prev >= 0 > elev:
                sett = t
        prev = elev
        t += timedelta(minutes=5)
    return {"sunrise": rise.strftime("%H:%M") if rise else None,
            "sunset": sett.strftime("%H:%M") if sett else None}


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing):
    if bearing is None:
        return None
    return COMPASS[int((bearing % 360) / 22.5 + 0.5) % 16]
