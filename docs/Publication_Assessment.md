# Can This Project Become a Research Paper?
## Honest Publication Assessment — TrafficLens / Indian ATCC Pipeline (Aug 2026)

---

## 1. What we actually have (inventory of claims we could defend)

1. **End-to-end system**: DVR footage → open-vocabulary auto-labeling → automated + human label verification → fine-tuned 15-class MoRTH detector → trajectory counting → IRC/APRDC-format outputs. Working, measured, cheap (~₹1,500 total cloud spend).
2. **Two-stage auto-labeling** (Grounding-DINO boxes + SigLIP2 crop classification with per-class prompt ensembles), with documented failure modes on Indian classes (tractor phrase over-triggering: 1,874 false positives; rider-box 2W inflation: 7,719 duplicates; vendor-cart/animal-cart confusion) and their geometric/classifier remedies.
3. **A three-judge VLM verification study with real numbers** (the most distinctive piece):
   - Blind crop judging: Claude Haiku 4.5 = 42%, Qwen3-VL-32B = 55% agreement with human verdicts
   - +Full-frame context, Qwen3-VL-235B = 60% exact (~70% counting drop-equivalents)
   - Ensemble triage resolved 92% of 18,380 boxes automatically; human queue reduced to 1,540
   - Complete cost accounting: ~$12.6 of API spend vs ~15 hours of human review avoided
4. **A clean label-quality ablation**: v1→v2 trained with identical architecture/hyperparameters/split, differing ONLY in label cleanup (ensemble + 1,661 human verdicts). Overall mAP50 0.516→0.654; corrected classes jumped +19 to +36 points (Mini_Bus +36, 3W_Auto +23, LCV +19) while untouched strong classes stayed flat (2W 0.819→0.823). This is causal evidence of label-quality effect with per-class attribution.
5. **Empirical footage-adequacy curves**: per-class F1 vs bbox-pixel-height (2W needs ≥48px, 3W ≥64px), proposed as a replacement for human-viewing standards (DORI) that have no principled link to CNN accuracy. Our own literature search confirmed: *no published Indian standard ties pixel density to counting accuracy*.
6. **A candidate dataset**: 9,509 CCTV frames, ~74k boxes, MoRTH-aligned 15-class taxonomy, 4 sites, day/dusk/night, with a *layered verification provenance* (auto-label → 3 VLM judgments → human verdicts per box) that no public Indian dataset has.
7. **Regulatory-output automation**: taxonomy-profile system emitting IRC:9/SP:19 and APRDC proformas directly.

## 2. Honest gaps (what a reviewer kills us on today)

1. **No ground-truth counting validation.** We have never compared counts against a human manual count. Every accuracy claim is currently at the label/detection level, not the count level. For any transportation venue this is disqualifying until fixed.
2. **Judge evaluation n=132.** Suggestive, not statistically strong. Needs 500+ human-verdict boxes, stratified, ideally with a second human for inter-annotator agreement.
3. **Single-region, 4-site data.** Generalization untested (no leave-one-site-out).
4. **Validation labels are themselves semi-automatic.** The height-F1 curve and val mAP measure agreement with cleaned auto-labels, not gold truth (we saw the Car mid-band anomaly exposing this).
5. **Method novelty is integrative, not algorithmic.** Every component exists in literature; the contribution is the composition, the measurements, and the domain. Wrong venue = instant reject; right venue = the strength.

## 3. Publishable angles, ranked by fit

### Angle A — Applied systems paper (STRONGEST)
**"An affordable human-in-the-loop AI pipeline for IRC-compliant classified traffic volume counts"**
- Venues: IEEE ITSC (conference, very achievable), Transportation Research Record, IEEE T-ITS (competitive), Journal of Eastern Asia Society for Transportation Studies; India: Transportation Research Group of India (TRG) conference, IRC journals for practitioner reach.
- Story: manual counts cost ₹50–60k/site-week; commercial video services €4/hr; our pipeline ≈ ₹200–500/site-day all-in, meeting IRC ±5% (to be shown), with full auditability (every count → footage) — a cost reduction of 30–100× with a verification methodology regulators can inspect.
- **Required before submission**: cluster-sampled ground-truth validation on ≥2 sites (day+night bins) with per-class MAPE vs the IRC bar; full 24h processing demonstration; cost model table.

### Angle B — Data-centric AI / HITL workshop paper (STRONG, fastest)
**"VLM judges as annotation-QA triage: cost/accuracy trade-offs on a 15-class vehicle taxonomy"**
- Venues: CVPR/ICCV/NeurIPS workshops (Data-Centric AI, Human-in-the-Loop Learning, VLMs-in-practice), WACV applications track.
- Contributions: the 42/55/60% judge-vs-human measurements; crop-vs-context effect; ensemble triage design (92% auto-resolution); the v1→v2 causal label-quality ablation; bias analysis (judges' systematic adjacent-class confusions — LCV↔truck, Bus↔MiniBus — mirror human uncertainty; "unclear" vs "not_vehicle" equivalence).
- **Required**: expand human gold set to ~500 boxes; add 2 baselines (single-judge, CLIP-similarity auto-verify — we already have SigLIP scores); tighten stats (CIs via bootstrap).
- Workshop bar is reachable within weeks; also serves as the citable foundation for Angle A.

### Angle C — Dataset paper (GOOD, most infrastructure)
**"MoRTH-15: a provenance-layered CCTV dataset for Indian classified traffic counting"**
- Venues: Scientific Data, Data in Brief, ITSC/WACV dataset tracks.
- Differentiators vs ITD/DATS/DriveIndia/IDD: true fixed-CCTV viewpoint, MoRTH-aligned taxonomy, day/night pairs from same cameras, and per-box verification provenance (auto/judge×3/human) enabling label-noise research; plus the height-F1 curves as per-class metadata.
- **Required**: PII pipeline (plate/face blur) before release; licence decision; hosting; datasheet. DPDP Act compliance documented.

### Angle D — Pure CV method paper (DO NOT PURSUE)
No new architecture/loss/algorithm. Framing this as a method paper invites rejection.

## 4. Extensions that would materially raise the ceiling

1. **The GT validation study** (Angle A's gate; ~2-3 days of human counting via our review tool, already built).
2. **Judge-scaling curve**: add 2–3 more judges (Gemini Flash, GPT-class, open 8B) → accuracy-vs-cost frontier for annotation QA; single figure, high citation value.
3. **Leave-one-site-out generalization** + fine-tune-per-site delta — quantifies how much site-specific adaptation matters (practitioners' #1 question).
4. **Second region's footage** (even 2 cameras from a different state) transforms generality claims.
5. **Active-learning round 3**: use accuracy-queue deviations to retrain and report the improvement-per-human-hour curve — closes the "data engine" story arc.
6. **Comparison point vs one commercial service** on a shared clip (GoodVision trial) — a table reviewers love, with the caveat handled diplomatically.

## 5. Realistic verdict

- **Today, as-is**: a workshop paper (Angle B) is genuinely within reach after ~2–3 weeks of tightening (gold-set expansion + baselines + writing). The judge-economics measurements and the clean label-ablation are the sellable core; nobody has published tidy numbers on VLM-judge annotation QA for a regulatory vehicle taxonomy.
- **With the GT validation done** (the same work the product needs anyway): a solid applied paper (Angle A) for ITSC/TRR — the cost-vs-accuracy story against manual surveys and commercial services is compelling and society-relevant (5,100+ NHAI ATCC sites).
- **With dataset release**: Angle C compounds citations of both.
- The three angles share 80% of their material; the sensible sequence is **B (workshop, fast) → A (conference/journal, after GT) → C (dataset, when PII pipeline exists)** — each reuses the previous.

## 6. Practical notes

- **Authorship/collaboration**: an academic co-author from an Indian transportation group (IIT TEG-style) would materially help Angles A/C (venue norms, review credibility, student manpower for GT counting).
- **Ethics/compliance**: footage is self-collected; for publication figures and any dataset release, blur plates/faces (DPDP Act 2023); state data handling in the paper.
- **Disclosure**: document the AI-assisted engineering per venue policy; the *judged* labels' provenance is itself part of the method, so transparency is an asset here, not a risk.
- **Timing**: IEEE ITSC submissions typically early-year deadlines; TRB Annual Meeting deadline is Aug 1 (next cycle); workshops roll continuously with CVPR/NeurIPS cycles — B can target the next available workshop deadline.

## 7. One-line answer

Yes — not as a "new method" CV paper, but as (1) a fast workshop paper on VLM-judge annotation economics that is ready after modest tightening, and (2) a strong applied ITS paper once the ground-truth validation we already need for the product is done; the dataset release is the compounding third act.
