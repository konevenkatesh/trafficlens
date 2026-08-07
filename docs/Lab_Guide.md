# TrafficLens Lab

The workbench that takes raw footage to a fine-tuned model, with every stage, judgment
and cent visible. Runs at **http://localhost:8800** alongside the counting app (8799);
both share `app/trafficlens.db`.

```bash
.venv/bin/python lab/api.py
```

## What it does

| Stage | What happens | Notes |
|---|---|---|
| **probe** | ffprobe + Tier-1 quality grade | resolution, fps, duration, A–D grade |
| **segment** | cuts fixed-length parts (default 15 min) | stream copy — no re-encode, no quality loss |
| **compress** | storage/upload copy at ≤50% source bitrate | keeps the original if re-encoding wouldn't shrink it |
| **extract** | YOLO v3 + ByteTrack → trajectory store | runs in its own process; segments register as videos, so the counting app can draw lines on them |
| **sample** | one crop per track at its largest moment | round-robin across classes so rare ones survive |
| **judge** | 3 vision models vote on every crop | ~$0.0004 per crop for all three |
| **review** | human decides the crops judges disagreed on | crop + full-frame context + each judge's vote |
| **dataset** | YOLO dataset from confirmed labels | 7:1 train/val split, `data.yaml` written |
| **train** | GPU pod lifecycle, telemetry, verified stop | idle guard auto-stops a pod below 5% GPU |
| **eval** | bake-off: accuracy and cost per model | scored on hand-graded gold crops |

## The judging rule, and why

Judges: `qwen3-vl-32b-instruct` (Alibaba), `gemini-2.5-flash-lite` (Google),
`llama-4-scout` (Meta) — three separate model families so their mistakes stay
uncorrelated.

Measured on 30 hand-graded crops of the hard, rare classes:

| Judges agreeing | Share of crops | Accuracy |
|---|---|---|
| All three | 60% | **72%** |
| Only two of three | 37% | **18%** |
| Three-way split | 3% | 0% |

A three-judge majority scored **51.7%** overall — *worse* than the best single judge
(53.3%) at 3.7× the cost. So the ensemble is not used to out-guess one model; it is used
to sort crops into "safe to auto-label" and "a human must look". **Only unanimous
verdicts are auto-accepted.** That auto-labels ~60% of crops and routes the rest to review.

Two rules follow from published evidence rather than this measurement:

- **Judges never count axles.** Axle counting is the weakest measured VLM skill (10–47%
  correct across cheap models), so 2-axle / 3-axle / multi-axle collapse into one
  `Heavy_Truck` answer and the detector's own axle split is kept.
- **Excluded models.** The Gemma family (structured output is broken for it on
  OpenRouter), `qwen3.7-flash` (#23 of 24 on vision evals despite being cheapest),
  `qwen3-vl-235b` (4.6× the output price, worse on reasoning), and
  `mistral-small-3.2` — dropped after testing here, because OpenRouter routes it to
  providers that answer image requests with "Image content is not supported by this model".

## Guard rails

Set on the Settings page, enforced in code:

- **Judge budget per run** — the judge loop stops when a run's spend hits the cap.
- **Idle pod auto-stop** — a pod below 5% GPU for N minutes is terminated automatically,
  because an idle pod is the expensive failure mode.
- **Termination is verified** — after every stop, the pod list is re-read and the UI says
  plainly if the pod is still alive.

## Costs

Every charge is booked the moment it happens, to `lab_costs`, with the run, stage,
provider and quantity. Nothing is estimated after the fact. The Costs page is the ledger;
the two pills in the top bar are the live OpenRouter and RunPod balances.
