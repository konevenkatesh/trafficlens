"""Human random-sample verification + optional single-VLM judge (Gemini via OpenRouter).

Sampling: one representative box per track (the track's LARGEST box = most judgeable
moment), restricted to judgeable sizes (height >= 28px), random order. A human
verdict on a track sets tracks.class_override -> every count/report updates instantly.
"""
import base64
import io
import json
import random
import re
import time
import urllib.request
from pathlib import Path

import cv2

import db
from engine import CLASSES

JUDGE_MODEL = "google/gemini-2.5-flash-lite"


def sample_tracks(video_id, n=80, min_h=28):
    tracks = db.rows("SELECT * FROM tracks WHERE video_id=? AND n_points>=3", video_id)
    reviewed = {r["track_id"] for r in db.rows(
        "SELECT track_id FROM box_reviews WHERE video_id=?", video_id)}
    out = []
    for t in tracks:
        if t["track_id"] in reviewed:
            continue
        p = db.one(
            "SELECT frame, x1, y1, x2, y2 FROM track_points WHERE video_id=? AND track_id=? "
            "ORDER BY (y2-y1) DESC LIMIT 1", video_id, t["track_id"])
        if not p or (p["y2"] - p["y1"]) < min_h:
            continue
        cls = t["class_override"] if t["class_override"] is not None else t["cls"]
        out.append({"track_id": t["track_id"], "frame": p["frame"], "cls": int(cls),
                    "cls_name": CLASSES[cls],
                    "box": [p["x1"], p["y1"], p["x2"], p["y2"]]})
    random.shuffle(out)
    return out[:n]


def get_frame_jpg(video_id, frame_idx):
    v = db.one("SELECT path FROM videos WHERE id=?", video_id)
    cap = cv2.VideoCapture(v["path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def save_verdict(video_id, track_id, frame, verdict, new_class):
    db.run("INSERT OR REPLACE INTO box_reviews VALUES (?,?,?,?,?,?)",
           video_id, track_id, frame, verdict, new_class, time.time())
    if verdict == "reclass" and new_class is not None:
        db.run("UPDATE tracks SET class_override=? WHERE video_id=? AND track_id=?",
               new_class, video_id, track_id)
    elif verdict == "not_vehicle":
        db.run("UPDATE tracks SET class_override=-1 WHERE video_id=? AND track_id=?",
               video_id, track_id)  # -1 = excluded from counting
    elif verdict == "correct":
        db.run("UPDATE tracks SET class_override=NULL WHERE video_id=? AND track_id=?",
               video_id, track_id)


def stats(video_id):
    revs = db.rows("SELECT * FROM box_reviews WHERE video_id=?", video_id)
    n = len(revs)
    correct = sum(1 for r in revs if r["verdict"] == "correct")
    judged = db.rows("SELECT j.*, t.cls, t.class_override FROM judgments j "
                     "JOIN tracks t ON t.video_id=j.video_id AND t.track_id=j.track_id "
                     "WHERE j.video_id=?", video_id)
    j_agree = sum(1 for j in judged
                  if j["judged"] == CLASSES[j["class_override"] if j["class_override"] not in (None, -1) else j["cls"]])
    return {"human_reviewed": n,
            "human_agree_pct": round(100 * correct / n, 1) if n else None,
            "reclassed": sum(1 for r in revs if r["verdict"] == "reclass"),
            "not_vehicle": sum(1 for r in revs if r["verdict"] == "not_vehicle"),
            "judge_n": len(judged),
            "judge_agree_pct": round(100 * j_agree / len(judged), 1) if judged else None}


SYS = ("You classify vehicles for an Indian traffic survey. The vehicle is outlined in RED. "
       "Answer with ONE of: " + ", ".join(CLASSES) + ", not_vehicle, unclear. "
       'Reply ONLY JSON: {"class":"<answer>"}')


def judge_sample(video_id, job_id, n=80):
    """Gemini flash-lite judges the same style of sample; results stored, never auto-applied."""
    db.run("UPDATE jobs SET status='running', started=? WHERE id=?", time.time(), job_id)
    try:
        key = (Path.home() / ".openrouter" / "key").read_text().strip()
        v = db.one("SELECT path FROM videos WHERE id=?", video_id)
        done = {r["track_id"] for r in db.rows(
            "SELECT track_id FROM judgments WHERE video_id=? AND model=?", video_id, JUDGE_MODEL)}
        items = [s for s in sample_tracks(video_id, n=n * 2, min_h=28) if s["track_id"] not in done][:n]
        cap = cv2.VideoCapture(v["path"])
        ok_n = 0
        for k, it in enumerate(items):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(it["frame"]))
            ok, frame = cap.read()
            if not ok:
                continue
            x1, y1, x2, y2 = [int(x) for x in it["box"]]
            m = int(max(x2 - x1, y2 - y1) * 0.3)
            H, W = frame.shape[:2]
            crop = frame[max(0, y1 - m):min(H, y2 + m), max(0, x1 - m):min(W, x2 + m)].copy()
            cv2.rectangle(crop, (min(m, x1), min(m, y1)),
                          (min(m, x1) + (x2 - x1), min(m, y1) + (y2 - y1)), (0, 0, 255), 3)
            if max(crop.shape[:2]) > 320:
                sc = 320 / max(crop.shape[:2])
                crop = cv2.resize(crop, (int(crop.shape[1] * sc), int(crop.shape[0] * sc)))
            okj, jb = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            b64 = base64.b64encode(jb.tobytes()).decode()
            body = {"model": JUDGE_MODEL, "max_tokens": 25, "temperature": 0,
                    "messages": [{"role": "system", "content": SYS},
                                 {"role": "user", "content": [
                                     {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                                     {"type": "text", "text": "Classify the vehicle in the red box."}]}]}
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                             json.dumps(body).encode(),
                                             {"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key,
                                              "User-Agent": "trafficlens/1.0"})
                resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
                txt = resp["choices"][0]["message"]["content"]
                mm = re.search(r'"class"\s*:\s*"([^"]+)"', txt)
                judged = mm.group(1) if mm else "parse_error"
                db.run("INSERT OR REPLACE INTO judgments VALUES (?,?,?,?)",
                       video_id, it["track_id"], JUDGE_MODEL, judged)
                ok_n += 1
            except Exception:
                pass
            if k % 10 == 0:
                db.run("UPDATE jobs SET progress=?, message=? WHERE id=?",
                       round(100 * k / max(len(items), 1), 1), f"{ok_n} judged", job_id)
        cap.release()
        db.run("UPDATE jobs SET status='done', progress=100, finished=?, message=? WHERE id=?",
               time.time(), f"judge done: {ok_n} boxes", job_id)
    except Exception as e:
        db.run("UPDATE jobs SET status='error', message=?, finished=? WHERE id=?",
               str(e)[:300], time.time(), job_id)
