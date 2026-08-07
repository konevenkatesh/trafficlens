"""Tier-1 footage quality metrics (cheap, at ingest): blur, exposure, glare, day/night."""
import cv2
import numpy as np


def assess(path, samples=6):
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    metrics = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        lap = cv2.Laplacian(gray, cv2.CV_64F).var()
        mean_y = float(gray.mean())
        under = float((gray < 30).mean())
        over = float((gray > 245).mean())
        contrast = float(gray.std())
        metrics.append((lap, mean_y, under, over, contrast))
    cap.release()
    if not metrics:
        return {"error": "unreadable"}
    m = np.array(metrics)
    mean_y = float(m[:, 1].mean())
    q = {
        "blur_laplacian": round(float(m[:, 0].mean()), 1),
        "mean_luma": round(mean_y, 1),
        "under_frac": round(float(m[:, 2].mean()), 3),
        "glare_frac": round(float(m[:, 3].mean()), 4),
        "contrast": round(float(m[:, 4].mean()), 1),
        "condition": "night" if mean_y < 60 else ("dusk" if mean_y < 95 else "day"),
    }
    warnings = []
    if q["blur_laplacian"] < 60:
        warnings.append("footage soft/blurry - classification accuracy reduced")
    if q["condition"] == "night":
        warnings.append("night footage - counts OK, class labels less reliable")
    if q["glare_frac"] > 0.01:
        warnings.append("glare/headlight bloom present")
    if q["under_frac"] > 0.4:
        warnings.append("heavily underexposed")
    q["warnings"] = warnings
    grade = "A"
    if warnings:
        grade = "B" if len(warnings) == 1 else ("C" if len(warnings) == 2 else "D")
    q["grade"] = grade
    return q
