"""Per-track ATTRIBUTE pass (APRDC sub-splits) - taxi / maxi-cab judgment.

Design principle (user's): only line-CROSSING tracks matter. For each crossing
track of the parent class, pick its best view moments from stored trajectories,
let a human or a cheap VLM judge the attribute, cache the verdict per track.
Unjudged tracks stay in the parent class - the Taxi/Maxi columns only ever
contain positive judgments, so the base counts can never degrade.
"""
import base64
import json
import re
import time
import urllib.request
from pathlib import Path

import cv2

import db
from counting import count_video
from engine import CLASSES

ATTRS = {
    "taxi": {"parent": "Car_Jeep_Van",
             "question": "Is this car a COMMERCIAL TAXI (yellow number plate / yellow board, or clear taxi markings)? Indian rules: private cars have white plates, taxis have yellow plates.",
             "values": ["taxi", "private", "unclear"]},
    "maxi": {"parent": "3W_Auto",
             "question": "Is this three-wheeler a LARGE 7-SEATER auto / maxi-cab (longer body, bigger passenger cabin) rather than a normal 3-seater auto-rickshaw?",
             "values": ["maxi", "normal", "unclear"]},
    # APRDC column 7. It had no definition at all, so the column was structurally
    # incapable of holding a number and printed 0 on every workbook ever produced.
    "apsrtc": {"parent": "Bus",
               "question": "Is this a state transport (APSRTC) bus — government livery, route board, service markings — rather than a private coach, school or staff bus?",
               "values": ["apsrtc", "private", "unclear"],
               "labels": {"apsrtc": "APSRTC / state", "private": "Private / school"}},
}
JUDGE_MODEL = "google/gemini-2.5-flash-lite"


def crossing_tracks(video_id, attr):
    """Tracks of the attr's parent class that crossed any saved line."""
    parent = ATTRS[attr]["parent"]
    import sites
    lines = sites.lines_for(video_id)[0]
    if not lines:
        return []
    res = count_video(video_id, lines)
    tids = sorted({e["track_id"] for e in res["events"] if e["class"] == parent})
    done = {r["track_id"]: r for r in db.rows(
        "SELECT * FROM track_attrs WHERE video_id=? AND attr=?", video_id, attr)}
    out = []
    for tid in tids:
        best = db.rows(
            "SELECT frame, x1, y1, x2, y2 FROM track_points WHERE video_id=? AND track_id=? "
            "ORDER BY (y2-y1)*(x2-x1) DESC LIMIT 2", video_id, tid)
        if not best:
            continue
        out.append({"track_id": tid,
                    "views": [{"frame": b["frame"],
                               "box": [b["x1"], b["y1"], b["x2"], b["y2"]]} for b in best],
                    "judged": done.get(tid, {}).get("value"),
                    "source": done.get(tid, {}).get("source")})
    return out


def save_attr(video_id, track_id, attr, value, source):
    db.run("INSERT OR REPLACE INTO track_attrs VALUES (?,?,?,?,?)",
           video_id, track_id, attr, value, source)


def _crop_b64(video_path, cap, frame_idx, box):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
    ok, frame = cap.read()
    if not ok:
        return None
    x1, y1, x2, y2 = [int(v) for v in box]
    m = int(max(x2 - x1, y2 - y1) * 0.25)
    H, W = frame.shape[:2]
    crop = frame[max(0, y1 - m):min(H, y2 + m), max(0, x1 - m):min(W, x2 + m)]
    if crop.size == 0:
        return None
    if max(crop.shape[:2]) > 360:
        sc = 360 / max(crop.shape[:2])
        crop = cv2.resize(crop, (int(crop.shape[1] * sc), int(crop.shape[0] * sc)))
    ok, jb = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return base64.b64encode(jb.tobytes()).decode() if ok else None


def judge_attr(video_id, attr, job_id):
    """Gemini judges all unjudged crossing tracks for this attribute."""
    db.run("UPDATE jobs SET status='running', started=? WHERE id=?", time.time(), job_id)
    try:
        key = (Path.home() / ".openrouter" / "key").read_text().strip()
        spec = ATTRS[attr]
        v = db.one("SELECT path FROM videos WHERE id=?", video_id)
        items = [t for t in crossing_tracks(video_id, attr) if not t["judged"]]
        cap = cv2.VideoCapture(v["path"])
        okn = 0
        for k, it in enumerate(items):
            imgs = []
            for view in it["views"]:
                b64 = _crop_b64(v["path"], cap, view["frame"], view["box"])
                if b64:
                    imgs.append({"type": "image_url",
                                 "image_url": {"url": "data:image/jpeg;base64," + b64}})
            if not imgs:
                continue
            sysmsg = (spec["question"] + " Two views of the SAME vehicle may be shown. "
                      "Reply ONLY JSON: {\"answer\":\"" + '"|"'.join(spec["values"]) + "\"}")
            body = {"model": JUDGE_MODEL, "max_tokens": 25, "temperature": 0,
                    "messages": [{"role": "system", "content": sysmsg},
                                 {"role": "user", "content": imgs + [
                                     {"type": "text", "text": "Judge this vehicle."}]}]}
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                             json.dumps(body).encode(),
                                             {"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key,
                                              "User-Agent": "trafficlens/1.0"})
                resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
                txt = resp["choices"][0]["message"]["content"]
                m = re.search(r'"answer"\s*:\s*"([^"]+)"', txt)
                val = m.group(1) if m and m.group(1) in spec["values"] else "unclear"
                save_attr(video_id, it["track_id"], attr, val, "ai")
                okn += 1
            except Exception:
                pass
            db.run("UPDATE jobs SET progress=?, message=? WHERE id=?",
                   round(100 * (k + 1) / max(len(items), 1), 1), f"{okn} judged", job_id)
        cap.release()
        db.run("UPDATE jobs SET status='done', progress=100, finished=?, message=? WHERE id=?",
               time.time(), f"attr {attr}: {okn} AI-judged", job_id)
    except Exception as e:
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)


def attr_class_map(video_id):
    """Returns {track_id: replacement_class_name} for report-time splitting.
    Human verdicts outrank AI. Only positive judgments split out."""
    out = {}
    rows = db.rows("SELECT * FROM track_attrs WHERE video_id=?", video_id)
    rows.sort(key=lambda r: 0 if r["source"] == "ai" else 1)  # human applied last = wins
    for r in rows:
        if r["attr"] == "taxi" and r["value"] == "taxi":
            out[r["track_id"]] = "Taxi_attr"
        elif r["attr"] == "maxi" and r["value"] == "maxi":
            out[r["track_id"]] = "Maxi_attr"
        elif r["value"] in ("private", "normal"):
            out.pop(r["track_id"], None)
    return out
