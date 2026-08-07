# TrafficLens — critical review of UI/UX, workflow, APIs and wiring
**6 Aug 2026.** Both apps read live (Lab 8800, survey 8799), every claim below checked against running code or the live database — not inferred from source.

Scope: `lab/` (API + 3,141-line frontend), `app/` (survey app + 8 pages), `shared/` (design system), and how the two apps connect through one SQLite file.

---

## Verdict

The parts are better than the whole. Individual screens are thoughtful — the report card, the verify flow, the error-decomposition page, the staged station rail are all genuinely good, and the reasoning in the comments is better than most production code. **What is broken is the wiring**: routes with no doors, endpoints with no callers, two implementations of the same question that answer differently, and a pipeline that cannot finish.

Three themes account for almost every finding:

1. **Screens exist that nothing links to.** This bug was found and fixed once before (per your notes) and has fully regressed — `#counts` and `#axles` are complete working screens in the Lab with **zero** entry points, and `#pipeline` is a destination that was never built.
2. **One question, two implementations, different answers.** `lines_for` exists twice and disagrees on an empty scene. The survey dashboard asks the `scenes` table directly instead of asking `sites.lines_for()`, and consequently cannot see an entire station.
3. **Human work is destroyed silently.** Re-extraction deletes tracks and re-mints track ids; every verdict, class override and attribute keyed on `(video_id, track_id)` is orphaned or re-pointed at a different vehicle. There is no guard, no confirmation, and the accuracy figures keep reporting as if nothing happened.

---

## S1 — Actively wrong right now

### 1. The pipeline cannot complete — guaranteed `NameError`
`lab/pipeline.py:362,436` read `skipped_gold`; it is only ever assigned in `dataset()` (`:486`). Verified by AST: the name is loaded but never stored in either scope.

```
sample           (def line 280) reads skipped_gold at [362, 363] -> assigned in scope: False
complete_frames  (def line 371) reads skipped_gold at [436, 437] -> assigned in scope: False
dataset          (def line 454) reads skipped_gold at [522, 523] -> assigned in scope: True
```

Both stages do all their work, commit it, then raise on the last line → `stage_fail` → the chain stops before `existence`/`judge`, and `lab_stages.meta` is never written. Sampling looks like it failed when it actually succeeded. **Two-line fix**, but nothing downstream of `sample` can run automatically until it lands.

### 2. Extracting one clip re-extracts every clip already done, and destroys their verdicts
`lab/api.py:836-840` **accumulates** into `extract_segments`:
```python
parts = set(cfg.get("extract_segments") or [])
parts.add(seg["idx"]); cfg["extract_segments"] = sorted(parts)
pipeline.start(seg["run_id"], ["extract"])
```
`pipeline.extract` (`:246`) then runs *every* index in that list and never checks `lab_segments.status='extracted'` — a status it sets at `:275` and nothing reads. `app/engine.py:87-88` opens with:
```python
db.run("DELETE FROM track_points WHERE video_id=?", video_id)
db.run("DELETE FROM tracks   WHERE video_id=?", video_id)
```

So clicking **Extract** on clip 4 re-runs clips 1–3, and for each one:
- `tracks.class_override` — every human reclassification, axle verdict and Lab verdict — is **deleted with the row**;
- `clip_verdicts` (1,608 rows), `lab_crops`, `track_attrs`, `axle_checks`, `lab_axle_checks`, `lab_attr_samples`, `box_reviews` all survive but key on `(video_id, track_id)` and silently re-attach to whatever vehicle inherits that id;
- `verify.state()` keeps reporting *"1,608 verified, 93% model accuracy"* — measured against tracks that no longer exist.

Cost as well as correctness: extraction runs at ~3.4× real time, so extracting 8 clips one at a time costs 36 extraction passes instead of 8. Same deletion is reachable in the survey app from an unlabelled **Re-extract** button (`index.html:115`, `:341`) — and there is not a single `confirm()` anywhere in that frontend.

### 3. The survey dashboard cannot see station default lines — a whole station is invisible
`app/main.py:293` asks the `scenes` table directly instead of calling `sites.lines_for()`. Measured live:

| | dashboard says | truth via `sites.lines_for()` |
|---|---|---|
| videos with a line | **2** | **12** (10 inherit the station default) |
| site cards shown | **2** — SRI-01, ATP-01 | 4 stations have countable footage |
| "awaiting line" | **10** | 10 of those already have one |

**FID-33 — 8 clips, the real survey station — does not appear on the dashboard at all.** Downstream: the Counts button is disabled for 20 of 22 videos (`index.html:116`), and Verify / Attributes / Reports filter on `v.tracks && v.has_line` (`:488`) so they list only 2 videos. One line restores five screens.

### 4. `--cc-acc` is referenced but never defined — the live-job progress bar is invisible
Confirmed in the running browser:
```
--cc-acc: (UNDEFINED)      --cc-accent: #6C47FF
.runrow .bar i computed background-color: rgba(0, 0, 0, 0)   → invisible: true
```
`ui.css:674` fills the running-job bar with `var(--cc-acc)`; it resolves to nothing, so `background-color` falls back to transparent. The track renders, the fill never does — **a running job is pixel-identical to a stalled one.** Also hits `.geo-hit:hover` (`:718`). One-line fix in the alias block: `--cc-acc: var(--cc-accent);`.

### 5. The count-line editor silently records wrong geometry when its container narrows
`lineeditor.js:123` and `boxlabeler.js:114` return CSS pixels from `getBoundingClientRect()`, but every conversion divides by a `scale` derived from the **canvas attribute width** in `fit()`. `ui.css:456` sets `max-width:100%`, and `fit()` only re-runs on **window** resize. Demonstrated live by narrowing the container with no resize event:

```
before: attr 292, css 292      after: attr 292, css 184
click coordinates now scaled by 0.63
→ a click at the right edge lands ~710 SOURCE px off on a 1920-wide frame
```

Triggers are routine: expanding the sidebar rail, or a scrollbar appearing as lines are added. It fails silently — the line looks correct on screen because `draw()` uses the same wrong scale — and a misplaced count line changes every number in the deliverable. No `ResizeObserver` in either editor. Related: `devicePixelRatio` is handled **nowhere** in the project (zero matches), so on any 2× display the review frame is upsampled and soft — a direct accuracy cost on a tool built to judge distant motorcycles.

### 6. A RunPod pod that has already exited is still being billed — live, right now
`lab_pods` row 3: `pod_id=8djap8zyc71fen`, `status='EXITED'`, `terminated=NULL`, `cost_usd=$1.599`, 382 ledger rows. `train.py:44` only exits the monitor on `status in ("terminated","gone")`; RunPod keeps exited pods listed, so `_accrue` (`:22-34`) charges `dt_h * hourly` unconditionally until the 5h deadline. It never consults `live["status"]` or `gpu_util`.

Two more in the same file: `monitor()` returns after calling `stop()` **regardless of whether termination was confirmed** (`:70`, `:81`) — the expensive failure mode is the unguarded one; and `resume_monitors()` (`:125`) overwrites `_monitors[id]` without stopping the previous thread, so a second call double-books the ledger.

### 7. The judge budget cap is inert whenever the model catalog is cold
`providers.price_of` (`:119`) returns `(0.0, 0.0)` for any model missing from `or_models()`, and `or_models` returns `[]` on error (`:98`). So a failed or cold catalog fetch makes every call cost **$0.00**, `judge.py:241`'s cap never fires, and the run books zero while the real spend happens. OpenRouter returns actual cost in the response; it is discarded in favour of this estimate. Separately: re-running the existence gate re-pays for every crop it keeps (`existence.py:236,254` — kept crops stay `state='new'`), and there is no spend cap at all on `existence.run_gate`, `axles.run`, or any `bakeoff`.

### 8. Columns that exist only because someone typed them into the sqlite CLI
`sites.default_line`, `sites.line_set`, `lab_footage.missing`, `sites.footage_dir` are in the live DB but in **no** schema or migration list. Two are `ALTER`ed in from inside request handlers (`lab/api.py:729`, `:751`) — inside `attach_folder` and `process_station`.

On a fresh database (`trafficlens.db` at repo root is 0 bytes — this has already been attempted): no station can have a count line, and `GET /api/stations/{id}` throws until somebody happens to call `POST /api/stations/{id}/process` first. **The schema depends on endpoint call order.** The survey app also reads `default_line` (`app/sites.py:179`) — a column the Lab created — so it cannot stand up alone either.

---

## S2 — Missing links

### Routes with no door (verified live in the running Lab)
```
#counts  renders "Counts"      → 0 entry points
#axles   renders "Axle audit"  → 0 entry points
#review  (contested-crop review) → no [data-review] emitter exists anywhere
#pipeline/<id>  → NO SUCH ROUTE. Falls through to viewOverview().
nav items: Overview, Stations, Datasets, Training, Judges, Logs, Settings
```
`#counts` and `#axles` are complete, working screens reachable only by typing the hash. The axle audit is the screen that fixes your documented 3-axle-read-as-2-axle problem.

**`#pipeline` is worse than orphaned — it is a dead end that writes to the database.** Every "Start a run" button (`app.js:3022`, `:3032`) creates a draft run plus 10 stage rows, then navigates to `#pipeline/<id>`, which silently renders Overview. Live proof: `#pipeline/999` → `h1 = "Lab"`, crumb `"Overview"`. There is already one orphan (run 15, `ch01_20260704130000`, created 13:45 today) sitting in the runs table having produced nothing.

Because that route is gone, so is everything reachable only from it: `livePipeline()`, `live()`, `showNodeOutput()`, `closeNodeOutput()`, `nodeOutputHtml()`, `stageRow` are all dead code, and the SSE endpoint `/api/runs/{id}/stream` plus `/api/runs/{id}/output/{node}` have **no callers**. The handlers `data-start`, `data-stages`, `data-judge`, `data-newfrom` in `wire()` have no emitters.

### Endpoints with no caller (that matter)
- **`GET /api/model-registry`** — returns v5/v4/v3 with metrics and the default flag. Nothing in the Lab calls it. There is no model picker anywhere.
- **`PATCH /api/footage/{id}`** — *"Correct what probing got wrong. A wrong clock corrupts every bin downstream."* The Overview tab prints `— set clock`, and Process returns a `no_clock` list captioned *"edit these before reporting"* — **and there is no edit control anywhere in the UI.** Footage with an unreadable clock is a permanent dead end.
- **`POST /api/axles/{video_id}`** (survey app) — the endpoint that *runs* the axle classifier is called by nothing. Worse, for a video never classified, `axles.html` renders *"Nothing left to review — every truck the model was unsure about has been settled."* It reports completion for work that has never started. Live: 3 of 8 FID-33 clips have zero `axle_checks` rows and would ship with every truck in the 2-Axle column.
- `POST /api/render`, `POST /api/judge`, `POST /api/dedup`, `GET /api/jobs/{id}`, `GET /api/progress/{id}` (survey app) — reachable only from `index_old.html`, which nothing links to but is still served.
- **Broken call:** `shared/boxlabeler.js:53` fetches `/api/gold/frame/${id}/image` — **404 in both apps**; no 4-segment gold route exists.

### The Lab has no navigation at all below the breakpoint
Measured in the browser at 526px wide:
```
sidebar: position fixed, left -264px     visible nav openers: 0
reachable nav links: 0                   no #burger, no #scrim in lab/static/index.html
```
`ui.css:253` implements the drawer, and `shell.js:96` renders the burger — but **the Lab does not use `mountShell`**; it hand-rolls its shell, so `data-drawer` is never set. The nav is permanently off-screen with no way to open it. This matters because `api.py:1365` binds `0.0.0.0` specifically *"so the Lab is reachable from a phone on the same WiFi."* On that phone there is no navigation.

---

## S3 — The workflow gap

Your stated purpose for the Lab is the station-adaptation loop: sample → label → build two datasets → train a station model → compare it against the global model → process the rest. Mapped against what the UI can actually do:

| # | Step | State |
|---|---|---|
| 1 | Start from the global model | **No UI** — `/api/model-registry` has no caller; no model picker exists |
| 2 | Sample footage, extract boxes | Works — but see S1 #2 |
| 3 | Label: 3 judges + human | Clip Verify works. The contested-crop screen `#review` is **unreachable** |
| 4 | Build train + held-out datasets | **No door** — the dataset stage lived on the run page, i.e. `#pipeline` |
| 5 | Train the station model | **No endpoint at all.** No POST anywhere starts a training |
| 6 | Compare station vs global | Backend supports it (`goldset.score(site_id, model_id)`); the UI calls it **without `model_id`**, so the button labelled *"Score a model"* can only ever score the default |
| 7 | Station model processes the rest | No model selection at extract time |
| 8 | Dataset grows forever | Works — artifacts registry is solid |
| 9 | Promote a better global | `POST /api/models/default` exists **only in the survey app** |
| 10 | Datasets/models with IDs | Works |

**Everything up to "labels" is wired. Everything from "build a dataset" onward is not.** The Lab can produce the training data and cannot use it. That is the single biggest structural gap, and it is larger than any individual bug in this review.

Compounding it: the gold-set training holdout — the guard against evaluating on training data, which you already paid for once with the v4 numbers — is **structurally bypassed for the current station**. `goldset.frozen_video_frames()` covers only videos 4, 5, 8; all 12 gold frames under `/Volumes/RK/Traffic/` freeze nothing, because the join at `goldset.py:390` matches source DVR paths against segment paths. All 144 `lab_gold_frames` rows have `video_id = NULL` for the same reason.

---

## S4 — Data integrity found in the live database

**Your chosen folder loses to the archive, by design.** KDP-01's `footage_dir` is `/Volumes/MySSD/Station169` — the folder you attached. All three files in it are marked duplicates of copies on `/Volumes/RK/Traffic`:

```
/Volumes/MySSD/Station169/ch03_20260704140535.mp4  → dup_of → /Volumes/RK/Traffic/ch03_20260704140535.mp4
/Volumes/MySSD/Station169/ch03_20260704152639.mp4  → dup_of → /Volumes/RK/Traffic/ch03_20260704152639.mp4
/Volumes/MySSD/Station169/ch03_20260704164850.mp4  → dup_of → /Volumes/RK/Traffic/ch03_20260704164850.mp4
```

`stations.py:434` states the rule explicitly — *"the lowest id wins and FOOTAGE_ROOTS puts the delivery drive first"* — so an explicit attach is always demoted to a duplicate of whatever was scanned first. Then `process_station` reconciles `WHERE dup_of IS NULL`, so **the folder you chose is never reconciled**, and unplugging /Volumes/RK makes KDP report missing files that are sitting on the SSD.

**And the UI never says so.** `app.js:783` computes `const dups = (s.footage || []).filter(f => f.dup_of)` and **never renders it**. Duplicates are invisible. There is also no un-dedupe: nothing anywhere clears `dup_of`, and nothing sets `site_id = NULL` — no detach.

**The Lab ingests its own output as source footage.** `scan()` uses `root.rglob("*.mp4")` with no exclusions. Of 177 `lab_footage` rows: 20 are **render outputs**, 49 are **segment/clip files**, 23 are benchmark result videos. `organise.run()` symlinks footage *into* `stations/<slug>/footage/`, which the next scan re-ingests — a feedback loop. One render (`annotated_ch01_..._atp15.mp4`) is now sitting in the footage duplicate graph. 132 of 177 rows have relative paths alongside absolute duplicates of the same file, because `scan()` never calls `.resolve()`.

**ATP-01's `footage_dir` is `"."`.** Pressing *Process footage →* on that station runs `scan(roots=["."])`, which recursively walks the entire project tree and ffprobes every mp4 in it. This is almost certainly where the 92 junk rows came from, and it is still stored, so it will happen again.

**Overlapping footage double-counts.** `_covered_seconds()` (`aprdc_workbook.py:84`) *sums* window overlaps and `partial` is `0 < cov < 895`, so over-coverage reads as complete. At ATP-01 today, video 3 is the same footage as videos 13+14 segmented differently — a delivery would report 167% coverage for 13:00–13:15 flagged as complete, and roughly double the vehicles in the overlap.

**Mixed detectors in one station, undeclared.** Site 4 has 3 clips on v3 and 5 on v4; site 1 has one of each. `aprdc_workbook.collect()` records `line_source` per clip but never `tracks.model_id`, and the Coverage sheet has no column for it — so a workbook can be half one detector, half another with no note.

**78% of spend is unattributable.** `clips.spend` inner-joins `lab_runs`, but $1.49 is booked with `run_id=NULL` (`axles.run` hardcodes it) and ~$2.57 with `run_id=0`, which matches no run. Of $5.17 total, the station spend panel shows about a fifth.

---

## S5 — Two apps, one database

Both are running now, both bound to `*`, both writing `app/trafficlens.db` (36 MB, 4.3 MB WAL).

- **The Lab never runs the survey app's migrations.** `lab/api.py:34` inserts `app/` on `sys.path`, but `db` is already bound to `lab/db.py` from line 19 — so `app/db.py` is never imported in the Lab process and `_migrate()` never runs. Add a column to `app/db.MIGRATIONS` and the Lab will fail with `no such column` at request time, with nothing pointing at the cause. (`_extract_worker.py` *does* put `app/` first, so the subprocess migrates while the server does not.)
- **Restarting the survey app kills the Lab's running jobs.** `app/main.py:663` runs at import: `UPDATE jobs SET status='error' WHERE status IN ('running','queued')` — no `kind` filter, no owner column, and `jobs` is shared. The mirror image: the survey app's concurrency guard has no `kind` filter either, so any Lab job returns `409 "one GPU, one job"` for every survey action.
- **The survey app loses every lock race.** Lab: `timeout=30`, `busy_timeout=30000`, WAL set once per process. Survey app: default 5s timeout, no `busy_timeout`, and `journal_mode=WAL` + `executescript(SCHEMA)` + `_migrate()` on **every new thread connection** — the exact stall `lab/db.py:85` warns about, against a 4.3 MB WAL.
- **`lines_for` exists twice and disagrees.** `lab/stations.py:262` returns `("video", [])` whenever a `scenes` row exists, even if empty; `app/sites.py:172` checks the row is non-empty and falls through to the station default. `scene.html`'s Clear+Save writes `[]`. After that the two apps report different totals for the same clip — and `app/render.py:26,29` calls **both, on adjacent lines**. The docstring at `sites.py:163` says they must agree. They do not.

Three separate axle stores exist (`axle_checks` 61 rows, `lab_axle_checks` 128, `lab_attr_samples` 950). `axle_pass._already_human()` checks two of the three.

---

## S6 — Deliverable correctness

- **55 hand-labelled APSRTC buses reach no workbook.** `attr_api.attr_class_map()` maps `taxi` and `maxi` only; `reports.py:24` only rewrites `Car_Jeep_Van`/`3W_Auto`, so a Bus can never be relabelled; `aprdc_18.yaml:11` declares `APSRTC Bus … from: []`. The workbook prints 0 and footnotes *"Columns pending attribute pass"* — telling the client the pass was not run, when 55 buses were judged by hand.
- **Two PCU tables disagree on Mini Bus:** `aprdc_workbook.py:51` says 1.5, `aprdc_18.yaml:10` says 3.0. Every other factor matches, and neither cites a source.
- **The good workbook generator has no door.** `app/deliver.py` and the NS/coverage/double-count machinery in `aprdc_workbook.py` have no route, no import, no CLI. The only reachable path (`reports.py` → `generate_irc_report.py`) has no not-surveyed handling, no coverage or provenance sheet, no APSRTC column, and produces one file per video — so an 8-clip survey yields 8 disconnected workbooks with no hour assembled across boundaries.
- **`FALLBACK_CLOCK` is treated as real in the delivery path.** The dashboard guards it; `aprdc_workbook._coverage()`, `collect()` and `reports.export()` do not — a clip with no filename timestamp is silently placed at midnight on 2026-01-01 and counted.
- **`videos.excluded` is honoured on the dashboard and nowhere else** — not in `deliver.videos_for()`, `aprdc_workbook.collect()`, or `reports.export()`. Video 7 escapes today only because site 1 has no line.
- **The double-count adjustment is internally inconsistent** — `aprdc_workbook.py:389` subtracts from the grand total only, so "TOTAL (adjusted)" ≠ the sum of the column cells above it, and PCU uses the unadjusted figures.
- **`attr.html` shows a full uncropped frame with no box.** The `box` returned by `crossing_tracks()` is never used — reviewers are asked *"is this car a taxi?"* about an unspecified car in a 1920×1080 frame at 46% width. The 14 taxi and 5 maxi verdicts were collected this way.
- **Both review screens jump to station 4 when work runs out** — `const SITE = P.get("site") || "4"` in `attr.html:37` and `axles.html:64`, and no entry point supplies `&site=`. Finishing a clip silently moves the reviewer to a different station's footage.

---

## S7 — UI/UX

**Where it violates your own rules.** *Staged UI, one action per state, with a computed Next:* — the station rail and the `next` line from `process` do this well. The clip cards do not:

```
20:28–20:43 | part 0 · 15.1 min | no line
buttons: Extract, Verify, Report, Line, Preview
```
Five buttons side by side, and on an un-lined, un-extracted clip three of them dead-end — probed live: Verify → `"this clip has no count line"`, Report → `"no count line drawn"`, Preview → `exists: false`. Only Extract and Line do anything. The survey app's Footage rows are worse: five buttons plus a station select, clock input, Save and Exclude, with disabled buttons carrying no `title` explaining why.

**Numbers that contradict themselves on one screen** (KDP-01, read from the live DOM):
```
header: "9.3 h footage · 1 day(s) · 9 full hours"
tile:   "Footage | 1.83 h | 8 clips · 110 min"
tile:   "Waiting on you | 0 | 8 clip(s) need a line · 8 need extraction"
```
Two different numbers labelled *Footage* (attached vs segmented, neither distinguished), and a headline of **0** above a subtitle listing 16 blocked items.

**Error and loading states.** `makeRenderer` replaces the whole page with an error box and passes no retry — the only recovery is a browser reload, contradicting `shell.js:57` (*"a failed card must not blank the page around it"*). `review.html`, `axles.html`, `counts.html` and `scene.html` have no `try` around their loaders, so a 500 leaves them frozen on `loading…` forever. `attr.html` is the only page that does this properly. `api()` renders FastAPI 422 bodies as `[object Object]` (`shell.js:38` — `detail` is an array), and has no timeout, so a hung request leaves `navProgress` stuck at 70% with no error and no retry.

**Expensive work with no warning.** *AI judge taxis* fires one OpenRouter call per crossing car, unbounded, real money — the user gets `alert('AI taxi judge started')` with no count, no estimate, no progress, no completion signal. `reports.export()` writes to a fixed filename, so regenerating **overwrites the previous deliverable in place**. The `job_id` returned by the judge and axle endpoints is read by nothing; the only progress mechanism is a 4-second full-page re-render that re-runs `count_video()` for every counted video.

**Remaining lifecycle leaks** (this family has bitten you twice already):
- `boxlabeler.js:195` adds an anonymous `window resize` listener and `destroy()` (`:197`) removes only the keydown — every gold frame retains its whole labeller and decoded image forever. Third instance of the same bug; `lineeditor.js:237` gets it right.
- Switching a station sub-tab from **line** to **clips** replaces `#wsbody` without destroying `ST_LINE` — `route()` only tears it down on a route change, not a tab change.
- `createMiniMap` has no teardown, and the survey app re-mounts all of them every 4 seconds while a job runs — each is a full Leaflet map with its own window resize binding. Unbounded.
- `createMapPicker().destroy()` exists and is never called; the Lab discards the handle entirely (`app.js:248`).
- `mappicker.loadLeaflet()` caches its rejection forever — one transient failure disables every map in the tab until reload.
- `nodegraph.destroy()` does `innerHTML=''` but the listeners are on the element itself; remounting stacks them and compounds the zoom.

**Accessibility.** No modal anywhere has `role="dialog"`, a focus trap, focus restoration or an Escape handler. Canvas editors have no `tabindex`, no keyboard path to their primary action, and `boxlabeler` has **no way to delete a box without a keyboard** while its hint text advertises `Del` to a touch device. The skip link (`shell.js:78`, `href="#main"`) is parsed by the router as a route and throws keyboard users onto the Dashboard. No route-change announcement, no scroll reset. `--cc-fg-off` fails AA in both themes (2.4:1 light, 3.0:1 dark) and is used for 9px and 11px informational text.

**Other real defects.** `listview.js:111` emits two `class` attributes on sortable right-aligned headers, so `lv-sortable` is dropped — numeric columns are sortable with no affordance saying so. `rankedBars` (`shell.js:253`) renders a negative value as a **full** bar (invalid width → `auto` → 100%). `nodegraph` calls `preventDefault()` on every wheel event and sets `touch-action:none`, so on mobile the page cannot be scrolled past the graph. `index.html:427` puts `JSON.stringify(station)` in a **single-quoted** attribute while `esc()` doesn't escape `'` — a station named `St Mary's Junction` breaks the Edit button, and `notes` is an injection vector. `.btn.danger` is defined twice (`ui.css:124`, `:693`); the later wins, so every destructive button is a theme-blind `#8a3030` instead of `--cc-bad`.

**Path traversal:** `main.py:652` takes `taxonomy` from the query string and `reports.py:46` joins it to a directory with no validation before passing it to a subprocess whose stderr is returned in the 500 body.

**Both apps bind `0.0.0.0` with no authentication.** Verified from the LAN: `http://192.168.0.3:8800/api/browse?path=/Users` returns a directory listing. Combined with `POST /api/settings` (writes API keys), `POST /api/videos` (arbitrary path) and `GET /api/video-file/{id}` (streams it back), anyone on the WiFi can walk the filesystem and read any file the process can. The code comments the no-login tradeoff but not this reach.

---

## Fix order

**First — cheap, and each unblocks something big**
1. `lab/pipeline.py:362,436` — initialise `skipped_gold`. Two lines; the pipeline cannot complete without it.
2. `app/main.py:293` — call `sites.lines_for()`. One line; restores five screens and un-hides FID-33.
3. `ui.css` alias block — `--cc-acc: var(--cc-accent);`. One line; makes running jobs visible.
4. `lab/api.py:836` — stop accumulating `extract_segments`, and refuse extraction when `clip_verdicts` exist unless forced (and on force, tombstone them so the accuracy figures stop lying).
5. `train.py:29` — don't bill a pod whose `live["status"]` isn't running; don't `return` after an unconfirmed `stop()`.

**Second — the missing links**
6. Give `#counts`, `#axles` and `#review` entry points; build `#pipeline` or stop navigating to it (right now it writes a DB row and silently shows Overview).
7. Wire `PATCH /api/footage/{id}` to an edit control — the warning telling you to fix the clock has no control to fix it with.
8. Add a model picker: `/api/model-registry` for extraction, and pass `model_id` to `goldset.score`. Without it the Lab's stated purpose — compare station vs global — cannot be performed.
9. Make the Lab use `mountShell`, or add the burger/scrim wiring.
10. Give `POST /api/axles/{id}` a button, and make `axles.html` distinguish *never run* from *nothing left*.

**Third — structural**
11. Collapse `lines_for` to one implementation and delete the other.
12. Put every hand-added column into a real idempotent migration, and give the Lab one migrator that also owns the shared tables.
13. Make an explicit attach beat first-seen in `dedupe()`, exclude `renders/`, `lab_work/` and `annotated_*` from `scan()`, `.resolve()` every path, and clear ATP-01's `footage_dir="."`.
14. Fix the canvas `pos()` scaling plus a `ResizeObserver`, and add devicePixelRatio handling while in there.
15. Decide the deliverable path: wire `deliver.py`/`aprdc_workbook.py` to a route, or delete them — the best workbook code in the project cannot be run.

---

*Not changed: nothing. This review is read-only — no files edited, no rows written. The one DOM experiment (narrowing the line-editor container) was reverted in the same call.*
