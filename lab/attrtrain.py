"""Train a small crop classifier for one fine-grained attribute.

The measured case for this file: cheap VLMs top out at 61% on axle class, and premium ones
did worse (58% for a 235B reasoning model, 56% for GPT-5.1) at 33x the price. The ceiling
is not the model's intelligence, it is that a VLM squeezes a 400px truck into a handful of
vision tokens and the rear-bogie gap -- the entire evidence -- does not survive. A small
CNN reading the crop at native resolution has the pixels the question needs.

Three things here exist because the alternative silently produces a good-looking number
that is worth nothing.

**Splits are grouped by clip, never by crop.** The dataset holds consecutive frames of the
same vehicle. Split those at random and the same truck appears in train and val, so the
model is scored on pictures it memorised: a validation accuracy that reads 95% and predicts
nothing about a new station. Whole clips go to one side or the other.

**Recall is reported per class, and accuracy is not the headline.** With 3-axle at 61
examples and MAV at 13, a model that answers "3_axle" for everything scores well on
accuracy while being useless for exactly the classes the proforma needs. Macro-F1 and the
per-class table are the honest summary.

**Training below the label threshold is refused, not warned about.** A four-way classifier
fitted to 13 examples of a class will report a confident number, and a confident number
with nothing behind it is the failure this whole subsystem was built to undo. `force=True`
exists for smoke-testing the code path and permanently marks the run `reliable=0`.
"""
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import db

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
import attrspec  # noqa: E402

WEIGHTS_DIR = ROOT / "models" / "attrs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_attr_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attribute TEXT, arch TEXT, classes TEXT,
  n_train INTEGER, n_val INTEGER, groups_train INTEGER, groups_val INTEGER,
  epochs INTEGER, accuracy REAL, macro_f1 REAL, per_class TEXT, confusion TEXT,
  baseline_note TEXT, vlm_accuracy REAL,
  path TEXT, sha256 TEXT, size_mb REAL, device TEXT, seconds REAL,
  reliable INTEGER DEFAULT 1, note TEXT, created REAL,
  -- NULL = serves every station. Set = calibrated to one camera and used only there.
  -- A head learns a particular mounting height, lens and vehicle mix; promoting one
  -- trained at KDP-01 to all stations is how a local fix becomes a global regression.
  site_id INTEGER);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ───────────────────────────── leakage-safe grouping ─────────────────────────────
def group_of(row):
    """Which clip a crop came from, so no vehicle can straddle the split.

    Video-sourced crops are one per track, so the track is the group. Dataset crops are
    frames sampled out of clips -- `atp_130000_0003` and `atp_130000_0004` are consecutive
    frames, near-certainly the same vehicle -- so the whole clip is the group. Grouping by
    clip rather than by a frame-number window is the conservative choice: it costs a little
    data efficiency and cannot leak.
    """
    if row["source"] == "dataset" and row["source_ref"]:
        stem = row["source_ref"].split(":")[1].split("/")[-1].rsplit(".", 1)[0]
        g = re.sub(r"_f\d+.*$", "", stem)      # lab_run1_v4_f9319_r2 -> lab_run1_v4
        return re.sub(r"_\d+$", "", g)         # atp_130000_0003      -> atp_130000
    return f"video{row['video_id']}_t{row['track_id']}"


def labelled(attribute):
    """Every human-answered crop this attribute's model should learn from.

    Abstentions and `train_exclude` answers are dropped here rather than downstream, so
    the split, the class weights and the metrics all see the same population.
    """
    keep = set(attrspec.trainable_values(attribute))
    # The frozen eval clip is held out of EVERY training set, attribute heads included —
    # a head that has seen its vehicles poisons any pipeline-level score on that clip.
    from eval_clip import EVAL_VIDEO_ID
    rows = db.rows("""SELECT * FROM lab_attr_samples
                      WHERE attribute=? AND human IS NOT NULL
                        AND (video_id IS NULL OR video_id != ?)""", attribute, EVAL_VIDEO_ID)
    return [r for r in rows if r["human"] in keep
            and r["crop_path"] and Path(r["crop_path"]).is_file()]


def make_split(attribute, val_frac=0.25, seed=0):
    """Assign whole clips to train or val, keeping the val class mix as even as possible.

    Groups are taken rarest-class-first: with MAV at a handful of examples, a purely random
    group draw regularly puts zero of them in val, and a per-class recall computed from
    zero examples is not a measurement.
    """
    import random
    rows = labelled(attribute)
    by_group = defaultdict(list)
    for r in rows:
        by_group[group_of(r)].append(r)

    counts = Counter(r["human"] for r in rows)
    groups = sorted(by_group)
    random.Random(seed).shuffle(groups)

    # Per-class quota rather than a global one. Taking rare-class groups first put *every*
    # `not_a_truck` example in val and none in train -- a class the model could not learn
    # and was then scored on. A quota per class keeps val representative while guaranteeing
    # train never runs out of a class; the 1.4 slack lets whole clips move without being
    # rejected for overshooting by one crop.
    quota = {c: max(1, round(n * val_frac * 1.4)) for c, n in counts.items()}
    floor = {c: max(2, round(n * 0.5)) for c, n in counts.items()}   # keep for training

    target = max(1, int(len(rows) * val_frac))
    val, n = set(), 0
    val_by_cls, train_left = Counter(), Counter(counts)
    for g in groups:
        if n >= target:
            break
        gc = Counter(r["human"] for r in by_group[g])
        if any(val_by_cls[c] + k > quota[c] for c, k in gc.items()):
            continue                       # would over-sample this class into val
        if any(train_left[c] - k < floor[c] for c, k in gc.items()):
            continue                       # would starve training of this class
        val.add(g)
        val_by_cls.update(gc)
        train_left.subtract(gc)
        n += len(by_group[g])

    for g, rs in by_group.items():
        for r in rs:
            db.run("UPDATE lab_attr_samples SET split=? WHERE id=?",
                   "val" if g in val else "train", r["id"])
    return {"groups": len(by_group), "groups_val": len(val),
            "n_train": len(rows) - n, "n_val": n,
            "val_classes": dict(Counter(r["human"] for g in val for r in by_group[g])),
            "train_classes": dict(Counter(r["human"] for g in by_group
                                          if g not in val for r in by_group[g]))}


# ───────────────────────────── training ─────────────────────────────
def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class _Crops:
    """Crops and their answers. Kept tiny on purpose -- a few hundred JPEGs fit in RAM."""

    def __init__(self, rows, classes, train, size=224):
        from torchvision import transforms as T
        self.rows, self.classes, self.size = rows, classes, size
        norm = T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        # Horizontal flip is safe here: an axle count, a livery and a plate colour are all
        # unchanged by mirroring, while the camera does see traffic from both directions.
        self.tf = T.Compose(
            ([T.RandomHorizontalFlip(), T.ColorJitter(0.25, 0.25, 0.2, 0.02),
              T.RandomAffine(5, translate=(0.03, 0.03), scale=(0.92, 1.08))] if train else [])
            + [T.Resize((size, size)), T.ToTensor(), norm])

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        from PIL import Image
        r = self.rows[i]
        img = Image.open(r["crop_path"]).convert("RGB")
        return self.tf(img), self.classes.index(r["human"])


def train(attribute, epochs=14, arch="resnet18", val_frac=0.25, seed=0,
          force=False, lr=3e-4, batch=32, img_size=320):
    """`img_size` is not a tuning knob for this task, it is the evidence budget.

    The distinction between a 3-axle and a 4-axle truck is a gap of a few tens of pixels
    between rear wheel groups in a ~500px crop. Resize that to 224 and the gap is a few
    pixels wide -- which is precisely the criticism levelled at the VLMs for squeezing the
    crop into vision tokens, repeated one layer down.

    Measured on 459 labels, rather than assumed:

        224px   73% accuracy   macro-F1 0.696    52s
        320px   75%            macro-F1 0.726    91s   <- default
        448px   70%            macro-F1 0.666   178s

    More pixels stop helping well before the crop runs out of them. At 448 the backbone
    is both further from the 224 its ImageNet weights were pretrained at and fitting more
    parameters to ~340 training images, and it overfits. 320 is the measured optimum for
    this dataset size; it is worth re-measuring once the label count roughly doubles,
    because the turning point moves with the data.
    """
    import torch
    import torch.nn as nn
    import torchvision
    from torch.utils.data import DataLoader

    init()
    spec = attrspec.spec(attribute)
    if spec.get("mode") == "human" and not force:
        return {"refused": True, "attribute": attribute, "mode": "human",
                "why": "This attribute is answered by a person, by decision, not by a "
                       "model. Its interesting class is rare enough that a classifier "
                       "would learn to always predict the common one and score well while "
                       "finding none of them. Change `mode` in attrspec.py to override."}
    rows = labelled(attribute)
    if len(rows) < spec["min_labels"] and not force:
        return {"refused": True, "attribute": attribute, "have": len(rows),
                "need": spec["min_labels"], "short": spec["min_labels"] - len(rows),
                "why": "A classifier fitted to this few examples reports a confident "
                       "number with nothing behind it. Label more, or pass force=True to "
                       "smoke-test the code path (the run is marked unreliable)."}

    sp = make_split(attribute, val_frac, seed)
    rows = labelled(attribute)                       # re-read: split is now assigned
    classes = sorted({r["human"] for r in rows})
    tr = [r for r in rows if r["split"] == "train"]
    va = [r for r in rows if r["split"] == "val"]
    if not tr or not va:
        return {"refused": True, "why": "split produced an empty side", **sp}

    dev = _device()
    t0 = time.time()
    model = getattr(torchvision.models, arch)(weights="IMAGENET1K_V1")
    model.fc = nn.Linear(model.fc.in_features, len(classes))
    model = model.to(dev)

    # Class weights: without them the loss is dominated by the common class and the rare
    # ones -- the whole reason this model exists -- are never learned.
    freq = Counter(r["human"] for r in tr)
    w = torch.tensor([len(tr) / (len(classes) * max(freq.get(c, 0), 1)) for c in classes],
                     dtype=torch.float32, device=dev)
    crit = nn.CrossEntropyLoss(weight=w, label_smoothing=0.05)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    dl_tr = DataLoader(_Crops(tr, classes, True, img_size), batch_size=batch, shuffle=True)
    dl_va = DataLoader(_Crops(va, classes, False, img_size), batch_size=batch)

    best, best_state, hist = -1.0, None, []
    for ep in range(epochs):
        model.train()
        for x, y in dl_tr:
            x, y = x.to(dev), y.to(dev)
            opt.zero_grad()
            crit(model(x), y).backward()
            opt.step()
        sched.step()
        m = _evaluate(model, dl_va, classes, dev)
        hist.append({"epoch": ep + 1, "accuracy": m["accuracy"], "macro_f1": m["macro_f1"]})
        # Selected on macro-F1, not accuracy: accuracy would pick the epoch that gave up
        # on the rare classes.
        if m["macro_f1"] > best:
            best, best_state = m["macro_f1"], {k: v.cpu().clone()
                                               for k, v in model.state_dict().items()}
    if best_state:
        model.load_state_dict(best_state)
    final = _evaluate(model, dl_va, classes, dev)
    secs = time.time() - t0

    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    blob = {"arch": arch, "classes": classes, "attribute": attribute,
            "state_dict": {k: v.cpu() for k, v in model.state_dict().items()}}
    tmp = WEIGHTS_DIR / f"{attribute}_{arch}.tmp.pt"
    torch.save(blob, tmp)
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    out = WEIGHTS_DIR / f"{attribute}_{arch}_{sha[:8]}.pt"
    tmp.rename(out)

    vlm = _vlm_baseline(attribute)
    mid = db.run("""INSERT INTO lab_attr_models
        (attribute,arch,classes,n_train,n_val,groups_train,groups_val,epochs,accuracy,
         macro_f1,per_class,confusion,baseline_note,vlm_accuracy,path,sha256,size_mb,
         device,seconds,reliable,note,created)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        attribute, arch, db.jdump(classes), len(tr), len(va),
        sp["groups"] - sp["groups_val"], sp["groups_val"], epochs,
        final["accuracy"], final["macro_f1"], db.jdump(final["per_class"]),
        db.jdump(final["confusion"]), vlm["note"], vlm["accuracy"], str(out), sha,
        round(out.stat().st_size / 1e6, 2), dev, round(secs, 1),
        0 if force and len(rows) < spec["min_labels"] else 1,
        "forced below label threshold" if force else "", time.time())

    return {"model_id": mid, "attribute": attribute, "arch": arch, "device": dev,
            "seconds": round(secs, 1), "classes": classes, "split": sp,
            "reliable": not (force and len(rows) < spec["min_labels"]), "img_size": img_size,
            "weights": str(out), "history": hist, "vlm_baseline": vlm, **final}


def _evaluate(model, loader, classes, dev):
    import torch
    model.eval()
    cm = Counter()
    with torch.no_grad():
        for x, y in loader:
            pred = model(x.to(dev)).argmax(1).cpu()
            for t, p in zip(y.tolist(), pred.tolist()):
                cm[(classes[t], classes[p])] += 1
    n = sum(cm.values()) or 1
    right = sum(v for (t, p), v in cm.items() if t == p)
    per, f1s = {}, []
    for c in classes:
        tp = cm.get((c, c), 0)
        sup = sum(v for (t, _), v in cm.items() if t == c)
        pp = sum(v for (_, p), v in cm.items() if p == c)
        rec = tp / sup if sup else None
        prec = tp / pp if pp else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else 0.0
        f1s.append(f1)
        per[c] = {"support": sup, "recall": round(rec, 3) if rec is not None else None,
                  "precision": round(prec, 3) if prec is not None else None,
                  "f1": round(f1, 3)}
    return {"accuracy": round(right / n, 3),
            "macro_f1": round(sum(f1s) / len(f1s), 3) if f1s else 0.0,
            "per_class": per,
            "confusion": [{"truth": t, "pred": p, "n": v} for (t, p), v in
                          sorted(cm.items(), key=lambda kv: -kv[1])]}


def _vlm_baseline(attribute):
    """What the judges managed on the same question, so 'better' is a measured claim.

    Only the axle question has a VLM number, because only it was ever run through the
    judges. For the rest this returns None rather than a placeholder -- an invented
    baseline would make any classifier look good by construction.
    """
    if attribute != "axles":
        return {"accuracy": None, "note": "no VLM baseline was measured for this attribute"}
    return {"accuracy": 0.61,
            "note": "best single VLM (gemini-2.5-flash-lite, prompt v3) on 107 human "
                    "labels; premium models scored 47-58% at up to 33x the cost"}


def models(attribute=None):
    init()
    q = "SELECT * FROM lab_attr_models"
    args = []
    if attribute:
        q += " WHERE attribute=?"
        args = [attribute]
    out = db.rows(q + " ORDER BY id DESC", *args)
    for m in out:
        m["classes"] = db.jload(m["classes"], [])
        m["per_class"] = db.jload(m["per_class"], {})
        m["confusion"] = db.jload(m["confusion"], [])
        m["exists"] = Path(m["path"]).is_file() if m["path"] else False
        m["beats_vlm"] = (m["vlm_accuracy"] is not None
                          and m["accuracy"] is not None
                          and m["accuracy"] > m["vlm_accuracy"])
    return out
