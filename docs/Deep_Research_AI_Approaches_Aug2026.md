# Deep Research: AI Video-Based Classified Traffic Counting — State of the Art (Aug 2026)

Consolidated from 4 research threads: (1) SOTA detection/tracking/counting, (2) Indian-specific work & datasets, (3) SAM3 evaluation, (4) cloud GPU costs. For the Traffic_Count project.

---

## 1. Recommended architecture (converged across all threads)

**Two-stage: foundation model for labeling ONCE → small fast specialist for bulk counting.**

```
Sample 2–5k frames from own CCTV (day/night/rain)
   → SAM3 / Grounding DINO auto-label with text prompts ("auto rickshaw", "tractor with trolley"...)  [~$10 one-time]
   → human verify/correct in CVAT/Roboflow  [10–20 hrs one-time]
   → fine-tune small detector (RF-DETR-S/M or YOLO26s)  [$2–10 GPU, one-time]
   → bulk inference: detector (TensorRT FP16, 640px, every 2nd frame, batched)
      + ByteTrack (motion-only) + LineZone crossing (bottom-center anchor, threshold 2)
      + majority-vote class per track  → 15-min binned direction-wise counts → IRC tables
```

## 2. Detector choice (Aug 2026 landscape)

| Model | mAP (COCO) | T4 latency | License | Notes |
|---|---|---|---|---|
| **RF-DETR-Medium** | 54.7 | 4.4 ms | **Apache 2.0** | Best fine-tuning transfer (RF100-VL SOTA); NMS-free = better in dense 2W swarms |
| RF-DETR-Small | 53.0 | 3.5 ms | Apache 2.0 | Sweet spot for speed |
| YOLO26s / m | 48.6 / 53.1 | 2.5 / 4.7 ms | AGPL-3.0 (viral!) | Fastest path (Ultralytics ObjectCounter one-liner); STAL helps small objects |
| D-FINE-L / DEIMv2-S | 54.0 / 50.9 | ~8 / small | Apache/MIT | Strong alternatives, DINOv3 backbone few-shot strength |
| YOLO-NAS | — | — | restricted | **Avoid** — abandoned after NVIDIA/Deci acquisition |

- DETR-family (NMS-free) degrades more gracefully with overlapping motorbikes than NMS-based YOLO.
- Licensing matters if this becomes a product: RF-DETR/D-FINE/RTMDet are permissive; Ultralytics is AGPL or paid.

## 3. Tracker & counting logic

- **ByteTrack (motion-only)** = production default; association <1 ms/frame CPU; ID switches barely affect line-crossing counts.
- **OC-SORT / Hybrid-SORT** better for weaving motorbikes (non-linear motion, +10 HOTA on non-linear benchmarks). BoT-SORT: disable GMC (fixed camera), skip ReID (5–20 ms/frame cost for marginal gain).
- Detector quality >> tracker choice.
- Counting best practices (Roboflow Supervision `sv.LineZone`): bottom-center anchor, `minimum_crossing_threshold≥2`, line placed mid-frame where boxes are big, one line per direction, majority-vote class over track lifetime (fixes truck/bus flicker), trajectory-based counting not per-frame.
- Frameworks: Supervision (MIT, ~30k stars) for DIY; Ultralytics Solutions (AGPL) for fastest prototype; NVIDIA DeepStream 8 if ever scaling to many cameras (35+ streams/T4).

## 4. SAM3 verdict

- SAM3 (Nov 2025; SAM 3.1 Mar 2026): promptable concept segmentation, 848M params, ~32 FPS on H100 (3.1 multiplexing), SAM License (commercial OK with restrictions).
- **Per-frame SAM3 on 24h footage: ~$50 (self-host H100) to ~$675 (fal.ai API) per camera-day. Fine-tuned small detector: $1–7 per camera-day → 20–400× cheaper.**
- Independent benchmark (TDS, May 2026): fine-tuned YOLOv11 beats SAM3 by 17–47% on fixed-class domain tasks, 30× faster.
- Segmentation masks add nothing to line-crossing counts; boxes suffice.
- **Use SAM3 only for**: auto-labeling odd classes (animal cart, tractor-trolley, cycle-rickshaw), active-learning re-labeling of low-confidence frames, offline QA audits.
- Hosted labeling: fal.ai $0.002/image; Roboflow Label Assist + one-click SAM3 fine-tune; CVAT SAM3 plugin.

## 5. Indian-specific findings

**Academic reality check:**
- No published system does the full 15-class MoRTH taxonomy at audit accuracy. Academic work: 7–10 merged classes, 90–97% daytime count accuracy, degraded night/monsoon.
- Manual counting itself: ~7.5% MAPE (Moratuwa study) — the realistic bar to beat.
- Stock YOLO on heterogeneous traffic: ~25% MAPE; site-fine-tuned: ~21% (older YOLOv4 study) → fine-tuning on own camera views is non-negotiable.
- Motorcycle-dominated junction counting via trajectory methods: >90% per-class (IJITSR 2024).
- Axle-count subclasses (2-axle vs 3-axle vs MAV) from single video angle = weakest link; toll AVC systems use separate axle sensors. Strategy: merge or use best-effort visual axle count, validate.

**Datasets to bootstrap fine-tuning:**
| Dataset | Size | Why |
|---|---|---|
| ITD (IIT Roorkee) | 9.2k imgs, 280k objects, 8 Indo-HCM classes | Closest to MoRTH classes; CC BY-NC |
| DriveIndia (TiHAN IIT-H, 2025) | 67k imgs, 24 classes, fog/rain/night | Weather diversity; best baseline 78.7 mAP50 |
| DATS_2022 | 10k imgs, 45 classes | Rickshaws, carts, tractors (rare classes) |
| IDD / IDD-X (IIIT-H) | 46k+ imgs | Auto-rickshaw; ego-view though |
| IITM-HeTra | 1.4k frames | True CCTV viewpoint |
| Roboflow Universe "Tansam" | 2k imgs, 36 classes | Only public set with 2/3/4/5+-axle truck labels |

**Official acceptance:** IRC:SP:19-2020 explicitly allows ATCC/videography for classified counts; ≥95% per-class accuracy vs manual verification is the cited acceptance threshold. IHMCL runs 5,100+ site ATCC survey program (7-day, twice yearly). NHAI toll-audit norms (vendor-cited): >99.5% count, >98% classification.

**Vendor pricing benchmarks to beat:**
- Indian ATCC survey vendors: ~₹50–60k per 7-day site (~₹7–8.5k/camera-day)
- GoodVision: ~€4/video-hour (~₹10k/camera-day); DataFromSky/Miovision similar, quote-based
- Vehant/Intozi/TRAZER/VaaaN: hardware/toll products, quote-based; claims 98–99.5%
- MetroCount (pneumatic tube, non-video): 96.5% on Indian roads

## 6. Cloud GPU plan

**Costs (mid-2026, verified):**
| Option | Price | Best for |
|---|---|---|
| **Kaggle** | FREE 30 GPU-hr/week (T4×2/P100) | Fine-tuning experiments |
| **Modal serverless** | T4 $0.59/hr, **$30/mo free credit** (~40 T4-hrs), 1 TiB volume free | First production batches — likely free for months |
| **RunPod community 4090** | **$0.34/hr** | Cheapest bulk inference (~$1/camera-day) |
| **Vast.ai 4090** | $0.35 OD / $0.14 spot | Same, spot risk |
| Jarvislabs L4 (India) | $0.44/hr (~₹38) | INR billing, data in India |
| E2E Networks L4 (India) | ~₹50/hr | Same |
| AWS Mumbai g4dn spot | $0.186/hr | If AWS credits |
| IndiaAI Mission pool | H100 ~₹92/hr subsidized | Apply if productizing |

**Per-job estimates (YOLO-s + ByteTrack, 24h of 1080p footage = 2.16M frames):**
- T4/L4: 5–12 GPU-hrs → $2.50–7 | RTX 4090: 2–4 GPU-hrs → $0.70–1.50
- Fine-tune (5–10k imgs, 100–300 epochs): 3–12 T4-hrs → $2–10 (fits in Kaggle free tier)
- Storage/transfer: Cloudflare R2 ($0.015/GB-mo, $0 egress); India upload ~57 Mbps avg → 1GB ≈ 2.5–4.5 min

**Mac mini M4 16GB reality:** YOLO-n/s via CoreML/ANE ≈ 25–60 FPS (1–2× realtime) — fine for dev + spot checks on clips; medium models ~0.5–1× realtime; MPS training buggy → all fine-tuning in cloud.

## 7. Known failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Dense 2W swarm occlusion | NMS-free detector, higher camera angle, trajectory counting, OC-SORT |
| LCV vs 2-axle truck, minibus vs bus confusion | Majority-vote over track, merged reporting classes where needed, targeted fine-tune data |
| Axle subclasses from video | Merge to "truck" + best-effort; flag for manual sample verification |
| Night/headlight glare | Night frames in fine-tune set; consider separate night model/threshold; IR illumination for permanent sites |
| Class flicker frame-to-frame | Track-level majority vote (single biggest classing fix) |
| Double counts from jittery boxes | LineZone crossing threshold ≥2 frames |

## 8. Proposed benchmark plan (sample clips)

1. Ground truth: manually annotate counts for 10–15 min segments of each site (day + night) — direction-wise, 15-class.
2. Test matrix: {YOLO26s, RF-DETR-S/M, D-FINE} × {stock COCO-classes-mapped, fine-tuned} × {ByteTrack, OC-SORT} × {every frame, every 2nd, every 3rd}.
3. Metrics: per-class count MAPE vs ground truth (target ≤5% per IRC SP:19), end-to-end FPS, GPU cost per hour of footage.
4. Sites: Srisailam junction (dense urban — hardest), Bhalki (roundabout geometry), TDP-ATP (easy highway + night glare test).

## Key links

RF-DETR: github.com/roboflow/rf-detr · Supervision: github.com/roboflow/supervision · BoxMOT: github.com/mikel-brostrom/boxmot · Autodistill: github.com/autodistill/autodistill · SAM3: github.com/facebookresearch/sam3 · ITD: github.com/teg-iitr/ITD-Indian-traffic-dataset · DriveIndia: arxiv.org/abs/2507.19912 · IHMCL: ihmcl.co.in/traffic-survey · Modal: modal.com/pricing · RunPod: runpod.io/pricing
