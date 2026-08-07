# Indian Classified Traffic Volume Count — Official Standards & Formats

Reference compiled 2026-08-01 for the Traffic_Count project (video-based vehicle counting).

## 1. Governing codes

| Code | What it governs |
|---|---|
| IRC:9-1972 (2nd rev: IRC:9-2022) | Traffic census on non-urban roads — count station selection, 7-day x 24-hr counts, direction-wise, tally-mark field sheets, ADT computation. [Full text 1972](https://law.resource.org/pub/in/bis/irc/irc.gov.in.009.1972.pdf) |
| IRC:SP:19-2001 | Manual for Survey, Investigation & Preparation of Road Projects — the DPR bible; Section 6 covers all traffic surveys, Proforma 1 = classified count form. [Full text](https://archive.org/stream/govlawircy2001sp19/govlawircy2001sp19_djvu.txt) |
| IRC:64-1990 | Capacity of rural roads — PCU factors + Design Service Volumes. [Full text](https://law.resource.org/pub/in/bis/irc/translate/irc.gov.in.064.1990.html) |
| IRC:106-1990 | Capacity of urban roads — urban PCU factors + capacities. [Full text](https://archive.org/stream/gov.in.irc.106.1990/irc.gov.in.106.1990_djvu.txt) |
| IRC:102-1988 | O-D / cordon surveys for bypass planning |
| IRC:108-1996 | Traffic prediction on rural highways (PCU set commonly cited in DPRs) |
| IRC:SP:73-2015 / SP:84 | 2-lane / 4-lane NH manuals — current Design Service Volume tables |
| Indo-HCM 2017 (CSIR-CRRI) | Newest capacity manual, dynamic PCUs (supersedes 64/106 methodology) |
| NH Fee Rules 2008 | Toll vehicle classes (for toll/revenue studies) |
| MoRTH circular RW/NH-33044/28/2015/S&R(R), 29-06-2015 | 4-laning trigger at 15,000 PCU/day on 2-lane NHs |

## 2. Standard vehicle classification (MoRTH/NHAI DPR proforma)

Per IRC:SP:19-2001 + IRC:9-1972 (verified from a live NHAI DPR annexure):

**Fast / motorised (12 classes):**
1. 2-Wheeler
2. 3-Wheeler (Auto Rickshaw)
3. Car / Jeep / Van / Taxi
4. LCV (Light Commercial Vehicle)
5. Mini Bus
6. Govt. Bus
7. Private Bus
8. Tractor
9. Tractor with Trolley/Trailer
10. 2-Axle Truck
11. 3-Axle Truck
12. Multi-Axle Vehicle (MAV, 4+ axles)

**Slow / non-motorised (3 classes):**
13. Cycle
14. Cycle Rickshaw
15. Animal Drawn Vehicle (bullock cart)

(+ optional memo column: pedestrians/others)

**ATCC 17-class scheme** (NHAI ATCC surveys) further splits: LCV into passenger/goods, MAV into 4 / 5 / 6 / 7+ axle, tractor into 1/2 trailers. 

**Toll classes (NH Fee Rules 2008):** Car/Jeep/Van · LCV/Mini Bus · Bus/2-axle Truck · 3-axle · 4-6 axle HCM/EME/MAV · 7+ axle oversized.

## 3. PCU factors

**IRC:64-1990 (rural/non-urban):**

| Vehicle | PCU |
|---|---|
| Motorcycle / Scooter | 0.5 |
| Car / Pick-up / Auto-rickshaw | 1.0 |
| Tractor / LCV | 1.5 |
| Truck or Bus | 3.0 |
| Truck-trailer / Tractor-trailer | 4.5 |
| Cycle | 0.5 |
| Cycle rickshaw | 2.0 |
| Hand cart | 3.0 |
| Horse-drawn | 4.0 |
| Bullock cart | 8.0 (small: 6.0) |

**IRC:106-1990 (urban)** — depends on composition share (@5% / @10%+ of stream):
2W 0.5/0.75 · Car 1.0/1.0 · Auto 1.2/2.0 · LCV 1.4/2.0 · Truck/Bus 2.2/3.7 · Tractor-trailer 4.0/5.0 · Cycle 0.4/0.5 · Cycle rickshaw 1.5/2.0 · Tonga 1.5/2.0 · Hand cart 2.0/3.0

**Common DPR practice (IRC:108-1996):** 2W 0.5 · Car/3W 1.0 · Minibus/LCV/Tractor 1.5 · Bus/2-axle/3-axle 3.0 · MAV-articulated 4.5 · Cycle 0.5 · Cycle-rickshaw 2.0 · Animal-drawn 8.0

## 4. Methodology requirements

- **Duration:** 7 consecutive days x 24 hours for classified volume count (IRC 9 census: twice yearly — peak + lean season; DPR: at least one 7-day count per homogeneous section). NHAI review typically rejects <7-day counts for AADT. Toll/concession studies often 14 days.
- **Recording interval:** field practice = 15-minute classified tallies, aggregated to hourly (IRC 9 prescribes hourly columns).
- **Direction-wise:** mandatory — separate count per direction (IRC 9 cl. 5.2).
- **ADT:** average of the 7 daily 24-hr totals.
- **AADT = ADT x Seasonal Correction Factor (SCF)** — SCF from a continuous count station in the region, or from monthly fuel-sales data at corridor fuel stations.
- **Peak hour:** highest hourly volume, identified separately for fast and slow traffic; typically 8-10% of daily volume.
- **Design Service Volume (LOS B), IRC:SP:73-2015:** 2-lane plain 15,000 PCU/day (18,000 with paved shoulders); rolling 11,000/13,000; mountainous 7,000/9,000. 4-laning trigger: 15,000 PCU/day (MoRTH 2015 circular).

## 5. Standard deliverables/report format

1. Field data sheets (station, date, direction, 15-min x class tallies) — IRC 9 Plate I
2. Daily traffic summary: hour x class matrix per direction, fast/slow subtotals, peak hour highlighted — Plate II
3. Weekly summary → ADT (day-wise vehicles + PCU, 7-day average) — Plate III
4. Location-wise classified ADT table (each class in "No. of Vehicles" and "PCU" columns)
5. PCU factor table with IRC citation
6. Traffic composition pie chart + hourly variation curve, peak/lean hours
7. Daily variation factors, SCF derivation, class-wise AADT table
8. Growth projections (20-yr horizon, class-wise rates per SP:19 App-2) vs Design Service Volume → lane recommendation
9. Companion surveys (if in scope): O-D, turning movement, axle load (VDF/MSA), speed-delay

## Notes
- IRC:9-2022 and Indo-HCM 2017 are purchase-only; details above from public 1972-2001 editions + verified DPR annexures. For a formal submission, cross-check against purchased current editions.
- Key sources: law.resource.org, archive.org (IRC full texts), environmentclearance.nic.in DPR annexures, ihmcl.co.in (ATCC program), morth.nic.in circular compendium.
