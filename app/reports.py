"""Report export: reuse the proven IRC/APRDC generator via a temp events.csv + config."""
import csv
import subprocess
import tempfile
from pathlib import Path

import db
from counting import count_video

ROOT = Path(__file__).parent.parent
GEN = ROOT / "benchmark" / "generate_irc_report.py"
PY = ROOT / ".venv" / "bin" / "python"
OUT_DIR = ROOT / "app" / "reports_out"


def export(video_id, lines, taxonomy=None):
    v = db.one("SELECT * FROM videos WHERE id=?", video_id)
    res = count_video(video_id, lines)
    # attribute splits (taxi/maxi): rename judged tracks' event class for the report
    import attr_api
    amap = attr_api.attr_class_map(video_id)
    if amap:
        for e in res["events"]:
            if e["track_id"] in amap and e["class"] in ("Car_Jeep_Van", "3W_Auto"):
                e["class"] = amap[e["track_id"]]
    OUT_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        ev = Path(td) / "events.csv"
        with open(ev, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["video_time_s", "clock", "track_id",
                                              "class", "line", "direction"])
            w.writeheader()
            for e in res["events"]:
                w.writerow({"video_time_s": e["time_s"], "clock": e["clock"],
                            "track_id": e["track_id"], "class": e["class"],
                            "line": e["line"], "direction": e["direction"]})
        cfgp = Path(td) / "cfg.yaml"
        lines_yaml = "\n".join(
            f"  - name: {ln['name']}\n    start: [{ln['start'][0]}, {ln['start'][1]}]\n"
            f"    end: [{ln['end'][0]}, {ln['end'][1]}]" for ln in lines)
        cfgp.write_text(f"name: {v['name']}\nstart_clock: \"{v['start_clock']}\"\nlines:\n{lines_yaml}\n")
        tag = "APRDC" if taxonomy else "IRC"
        out = OUT_DIR / f"{tag}_{v['name']}.xlsx"
        cmd = [str(PY), str(GEN), str(ev), "--config", str(cfgp), "--out", str(out)]
        if taxonomy:
            cmd += ["--taxonomy", str(ROOT / "benchmark" / "taxonomies" / taxonomy)]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT / "benchmark"))
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-300:])
    return str(out)
