# Measuring vehicle speed from survey footage

**TrafficLens · concept note · August 2026**

This describes how speed is measured from the same footage already used for classified
counts, what accuracy it delivers, and what a site needs to look like for that accuracy to
be worth having. Every figure below is measured on real rural footage (station FID-33,
eight 15-minute clips, 4,751 tracked vehicles) or taken from published work — none is
estimated.

---

## 1. The idea

A vehicle is timed between two lines drawn across the carriageway a measured distance
apart.

> **speed = D ÷ (t₂ − t₁)**

That is all. The distance is measured once on the road with a wheel or tape. The times come
from the video, interpolated between frames rather than rounded to the nearest one.

International legal metrology has a name for this: OIML R 91-1:2025 calls it a
**fixed-distance speed meter** — *"a speed meter incorporating two or more detection points
at fixed distances and detecting the transit time of the vehicles between the detection
points."* Inductive loops and light barriers are the same category. We implement it with
detections instead of hardware.

**See:** `fig1-how-speed-is-measured.svg`

## 2. Why not the obvious approach

The intuitive method is to calibrate the camera, convert pixels to metres, and
differentiate each trajectory. We built that first. It failed twice, and both times the
output looked entirely reasonable — which is the dangerous kind of failure.

**The calibration was ambiguous.** We detected four lane dashes and were about to treat
them as consecutive, 9 m apart. A cross-ratio test — the projective invariant of four
collinear points — put their true spacing at gaps of 1, 2 and 2 dash-cycles (fits to 1.6%,
against 8.6% for "four in a row"). Believing the picture would have made every speed
roughly 40% low, with nothing in the result to suggest a problem.

**The model was wrong even when calibrated.** Mapping through a single road axis maps the
far lane short. The result: **motorcycles at 70 km/h and cars at 36 km/h** on the same road
in the same minute. No amount of tuning fixes a geometry error.

The trap has neither failure mode. On the same clips it produces the ordering a road
actually generates:

| Class | n | Median km/h |
|---|---|---|
| Car / Jeep / Van | 397 | 98 |
| LCV | 73 | 92 |
| Motorcycle | 434 | 86 |
| 3-Wheeler auto | 207 | 77 |
| 3-Axle truck | 58 | 75 |

*(Absolute values here use a provisional distance and are about 2× high; the ordering is
unaffected and is the point.)*

## 3. Where the error comes from

Measured on this footage: the tracked ground point jitters by **9.35 px** while a vehicle
moves about **8.3 px per frame**. That jitter, not the tape measure, dominates.

| Trap | at 40 km/h | at 60 km/h |
|---|---|---|
| 25 m, tape-measured | ±1.9 km/h | ±4.3 km/h |
| 25 m, Google Earth ruler | ±2.0 km/h | ±4.4 km/h |
| 9 m, tape-measured | ±5.2 km/h | ±11.8 km/h |

**Timing error scales as 1/D.** Doubling the trap halves the error. Distance error is
roughly 0.5% however carefully it is measured, and never dominates.

### Per-vehicle versus survey figures

These are per-vehicle errors, and they behave differently in aggregate. Random error
**averages out of a median** but **inflates the 85th percentile** — which is the figure
speed studies actually quote, and the one design speed and enforcement thresholds are set
from.

| Per-vehicle error | Median | 85th percentile |
|---|---|---|
| truth | 44.4 | 53.3 |
| ±7% | 44.2 | 53.7 (+0.4) |
| ±13% | 43.8 | 55.3 (**+1.9**) |
| ±20% | 43.5 | 56.9 (**+3.6**) |

A sloppy trap does not make the median wrong. It makes the 85th percentile too high, which
is worse, because that is the number that leaves the building.

## 4. Trap length: the real trade-off

A longer trap is more accurate but fewer vehicles are tracked across all of it. Measured:

| Trap | Vehicles | Yield | Per-vehicle error | Median error |
|---|---|---|---|---|
| 5 m | 173 | 23.4% | ±29.5% | ±2.24% |
| 9 m | 157 | 21.2% | ±16.4% | ±1.31% |
| 16 m | 145 | 19.6% | ±9.2% | ±0.77% |
| **20 m** | **135** | **18.3%** | **±7.4%** | **±0.63%** |

Going from 5 m to 20 m costs 22% of the sample and buys a 4× accuracy gain. **Longer wins,
clearly.** The binding constraint is not the tracker — it is that this camera only sees
**27 m of road**, so 20 m is the practical maximum.

### The sampling bias that matters more

| Class | Share of all traffic | Share of speed readings |
|---|---|---|
| Motorcycle | **50.2%** | **11.9%** |
| Car / Jeep / Van | 32.9% | **57.8%** |
| 3-Wheeler auto | 5.3% | 12.6% |

Motorcycles are half the traffic and an eighth of the measurements — they are small, they
weave, and they track in fragments. **An overall "median speed" for this station would be a
car speed wearing a mixed-traffic label.** Speed must be reported per class, or weighted by
the true class mix from the count.

## 5. What top accuracy requires

**See:** `fig2-accuracy-ladder.svg` and `fig3-best-accuracy-setup.svg`

Per-vehicle error at 50 km/h, each row adding to the one above:

| Setup | Error |
|---|---|
| Today — 9 m, 15 fps, 2 lines | ±8.2 km/h |
| 20 m trap | ±3.7 km/h |
| + 6-point crossing fit | ±2.14 km/h |
| + record at 30 fps | ±1.09 km/h |
| + 5 gates instead of 2 | ±0.59 km/h |
| + camera sited for 40 m of road | ±0.37 km/h |
| + steadier ground point | ±0.27 km/h |

For reference: the **OIML legal enforcement bar is ±3 km/h** up to 100 km/h, and published
monocular research on the BrnoCompSpeed benchmark (20,865 vehicles, LIDAR and GPS ground
truth) reports **1.2–2.8 km/h**.

The three cheapest wins are, in order: **a longer trap** (free), **30 fps capture** (costs
disk), and **more gates** (free — OIML requires at least three detection points anyway, and
four independent readings per vehicle allow outlier rejection).

## 6. What this does not give you

- **Not legal enforcement.** That requires type approval of the instrument, not merely
  accuracy. Two detection points would fail OIML on redundancy grounds alone.
- **Not validated accuracy — yet.** Everything above is a propagated error budget and
  internal consistency. Precision is demonstrated; accuracy is not, and cannot be until the
  system is checked against an independent reference. **Twenty passes of a GPS-logged
  vehicle at known speeds would settle it in an afternoon.** Until then, no figure here
  should be quoted as validated.
- **Not every vehicle** — see the sampling bias above.

## 7. What we would ask of a site

1. Camera **6–8 m up**, looking along the road, framing **40 m** of carriageway.
2. Record at **30 fps**.
3. **Five gate positions** marked with something visible in the video *and* measurable on
   the ground — painted marks or cones, not lane dashes.
4. The gate spacing **surveyed once** with a measuring wheel or GNSS.
5. **Twenty GPS-logged passes** at a range of speeds, to convert this from a precision
   claim into an accuracy claim.

Items 1–3 cost nothing but planning. Item 5 is what makes the numbers defensible.

---

### Sources

- OIML R 91-1:2025, *Speed measuring instruments for vehicles* —
  https://www.oiml.org/en/files/pdf_r/r091-1-e25.pdf
- Sochor et al., *BrnoCompSpeed: Review of Traffic Camera Calibration and Comprehensive
  Dataset for Monocular Speed Measurement* — https://arxiv.org/pdf/1702.06451
- *Efficient vision-based vehicle speed estimation*, J. Real-Time Image Processing, 2025 —
  https://link.springer.com/article/10.1007/s11554-025-01704-z
- *Vehicle Detection and Speed Tracking*, TechRxiv, December 2025 —
  https://www.techrxiv.org/doi/pdf/10.36227/techrxiv.176621094.40723308
