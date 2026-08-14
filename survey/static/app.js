/* TrafficLens Survey — the whole front end.

   One file, no build step, hash routing. The app has four screens and they are the four
   things a surveyor does, in order. Anything that would need a fifth screen probably
   belongs in the Lab instead.

   The rule this UI is built around: never show a number without saying where it came
   from, and never offer an action that cannot be taken yet. A greyed-out "Extract" with
   the reason next to it teaches the workflow; an enabled one that errors does not. */

import { mountLineEditor } from '/shared/lineeditor.js';
import { createVoice, TRAINABLE, loadAliases, saveAlias, forgetAliases }
  from '/static/voice.js';

const $ = s => document.querySelector(s);
const app = $('#app');
let POLL = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = n => (n == null ? '—' : Number(n).toLocaleString('en-IN'));

function mins(s) {
  if (s == null) return '—';
  if (s < 90) return `${Math.round(s)} sec`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

async function api(path, body, method) {
  const r = await fetch(path, body === undefined ? {} : {
    method: method || 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

let TT;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(TT); TT = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3000);
}

/* ─────────────────────────── stations ─────────────────────────── */
async function viewStations() {
  const d = await api('/api/stations');
  const dev = d.device;
  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:20px"><div>
      <h1>Traffic counts</h1>
      <p>Point the app at a folder of camera footage and it produces the count.</p></div></div>

    ${dev.cloud ? `<div class="card" style="margin-bottom:16px;border-color:var(--cc-acc)">
      <div class="card-body" style="display:flex;align-items:center;gap:12px">
        <div style="flex:1"><b>Detection runs on a rented GPU.</b>
          Much faster than this computer, and it costs money while it runs.
          <span class="muted-sm">${esc(dev.name)}</span></div>
        <a class="btn ghost sm" href="#settings">Settings</a></div></div>` : ''}

    ${dev.device === 'cpu' ? `<div class="card" style="margin-bottom:16px;border-color:var(--cc-warn)">
      <div class="card-body"><b>No graphics card found.</b> Detection will run on the
      processor, which is about nine times slower — roughly 35 minutes per 15 minutes of
      footage. It still works; leave it running.</div></div>` : ''}

    <div class="card" style="margin-bottom:18px"><div class="card-body">
      <label class="lbl">Add a station</label>
      <div style="display:flex;gap:8px;margin-top:6px">
        <input class="field" id="nm" placeholder="e.g. Kadapa bypass km 12" style="flex:1">
        <button class="btn primary" id="add">Create</button>
      </div>
      <p class="muted-sm" style="margin:8px 0 0">A name is all it needs.</p>
    </div></div>

    ${d.stations.length ? d.stations.map(s => {
      const done = s.steps.filter(x => x.done).length;
      return `<a class="card" href="#station/${s.id}" style="display:block;margin-bottom:10px;
                 text-decoration:none;color:inherit">
        <div class="card-body" style="display:flex;align-items:center;gap:16px">
          <div style="flex:1">
            <div style="font-size:16px;font-weight:640">${esc(s.name)}</div>
            ${/* Files can exist without a folder on record — stations attached before
                  this app existed. Count what is there rather than what is configured,
                  or a station with 4,751 vehicles reads "no footage yet". */''}
            <div class="muted-sm">${esc(s.code)}${s.files
              ? ` · ${s.files} recording(s)` : ' · no footage yet'}</div>
          </div>
          <div style="text-align:right">
            <div class="big">${num(s.tracks)}</div>
            <div class="muted-sm">vehicles detected</div>
          </div>
          <div style="text-align:right;min-width:96px">
            <div style="font-weight:600">${done} of 4</div>
            <div class="muted-sm">steps done</div>
          </div>
        </div></a>`;
    }).join('') : `<div class="card"><div class="card-body" style="text-align:center;
        padding:40px;color:var(--cc-fg-3)">No stations yet. Create one above to start.</div></div>`}

    <p class="muted-sm" style="margin-top:20px">Running on ${esc(dev.name)} ·
      <a href="#settings">Settings</a> · <span id="ver">…</span></p>
  </div>`;

  const go = async () => {
    const name = $('#nm').value.trim();
    if (!name) return toast('Give the station a name', true);
    try {
      const s = await api('/api/stations', { name });
      location.hash = `#station/${s.id}`;
    } catch (e) { toast(e.message, true); }
  };
  $('#add').onclick = go;
  $('#nm').onkeydown = e => { if (e.key === 'Enter') go(); };
  // Printed on the first screen, not hidden behind a menu. "I installed it and none of
  // the changes are there" is unanswerable without it, and the answer turned out to be
  // three different possible faults.
  api('/api/version', undefined, 'GET').then(v => {
    const el = $('#ver');
    if (el) el.textContent = `build ${v.build}` + (v.commit && v.commit !== 'source'
      ? ` (${String(v.commit).slice(0, 7)})` : '');
  }).catch(() => {});
}

/* ─────────────────────────── one station ─────────────────────────── */
async function viewStation(id) {
  const d = await api(`/api/stations/${id}`);
  if (viewStation._for !== id) { PICKED.clear(); viewStation._for = id; }
  const p = d.progress, hrs = d.hours;
  const nextIdx = p.steps.findIndex(s => !s.done);
  const q = d.queue || {};

  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:18px">
      <div><h1>${esc(d.station.name)}</h1>
        <p>${esc(d.station.code)}${p.folder ? ` · ${esc(p.folder)}` : ''}</p></div>
      <a class="btn ghost" href="#stations">All stations</a>
    </div>

    <div class="steps">${p.steps.map((s, i) => `
      <div class="s ${s.done ? 'done' : i === nextIdx ? 'now' : ''}">
        <div class="n">${s.done ? '✓' : i + 1}</div><div>${esc(s.label)}</div>
      </div>`).join('')}</div>

    ${(d.failures || []).length ? `<div class="card"
      style="margin-bottom:14px;border-color:var(--cc-bad)"><div class="card-body">
      <b>${d.failures.length} recording(s) could not be processed</b>
      <p class="muted-sm" style="margin:6px 0 0">These hours will stay incomplete until
        the cause is fixed. Pressing detect again will hit the same error.</p>
      <ul class="muted-sm" style="margin:8px 0 0;padding-left:18px">
        ${d.failures.map(f => `<li><b>${esc(f.name)}</b> — ${esc(f.message) || 'no reason recorded'}</li>`).join('')}
      </ul></div></div>` : ''}

    <div id="stepFolder"></div>
    ${/* Hours appear as soon as there is footage. They used to be gated behind the count
          line, which meant a surveyor was asked to draw a line over footage the app had
          not yet shown them — and if nothing attached, the screen simply ended there
          with no explanation. */''}
    ${p.files ? `<div id="stepHours"></div>` : ''}
    ${p.files ? `<div id="stepLine"></div>` : ''}
    ${p.files ? `<div id="stepSpeed"></div>` : ''}
    ${p.tracks ? `<div id="stepAfter"></div>` : ''}
  </div>`;

  paintFolder(id, d);
  if (p.files) paintHours(id, d);
  if (p.files) paintLine(id, d);
  if (p.files) paintSpeed(id);
  if (p.tracks) paintAfter(id, d);

  clearInterval(POLL);
  if (q.running || (q.waiting || []).length) POLL = setInterval(() => tick(id), 3000);
}

/* ── step 1: the folder ── */
function paintFolder(id, d) {
  const el = $('#stepFolder');
  const p = d.progress;
  if (p.folder) {
    el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body"
      style="display:flex;align-items:center;gap:14px">
      <div style="flex:1"><b>${p.files} recording(s)</b>
        <div class="muted-sm" style="font-family:ui-monospace,monospace">${esc(p.folder)}</div>
        ${d.guessed ? `<div class="muted-sm" style="color:var(--cc-warn-fg)">
          ${d.guessed} of them had no date in the filename — the file's own timestamp was
          used, which is often the copy time. Check the hours below look right.</div>` : ''}</div>
      <button class="btn ghost" id="reFolder">Check for new files</button>
      <button class="btn ghost" id="chFolder">Change folder</button>
    </div></div>`;
    $('#reFolder').onclick = async e => {
      e.target.disabled = true; e.target.textContent = 'Checking…';
      try {
        const r = await api(`/api/stations/${id}/rescan`, undefined, 'GET');
        toast(r.added.length ? `${r.added.length} new file(s) added` : 'No new files');
        viewStation(id);
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    };
    $('#chFolder').onclick = () => openPicker(id);
    return;
  }
  el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body">
    <h2 style="margin:0 0 4px;font-size:17px">Attach the footage folder</h2>
    <p class="muted-sm" style="margin:0 0 12px">The folder you copied off the camera.
      Files are placed on the timeline using the date and time in their filenames.</p>
    <button class="btn primary" id="pick">Choose folder…</button>
  </div></div>`;
  $('#pick').onclick = () => openPicker(id);
}

/* What the folder scan actually did, file by file.
   "0 recordings attached" is useless on its own: the surveyor cannot tell a wrong folder
   from unreadable files from names the app could not date. Every file gets a line. */
function showScan(r, folder) {
  const rows = r.report || [];
  const badge = { added: 'ok', already: '', duplicate: 'warn',
                  unreadable: 'warn', 'no-date': 'warn' };
  modal('Nothing was attached from that folder', `
    <p class="muted-sm" style="margin:0 0 10px;font-family:ui-monospace,monospace">
      ${esc(folder)}</p>
    ${rows.length ? `<div class="lv-table" style="max-height:46vh;overflow:auto">
      <table><thead><tr><th>File</th><th>What happened</th></tr></thead><tbody>
      ${rows.map(x => `<tr><td class="mono">${esc(x.name)}</td>
        <td><span class="status ${badge[x.status] || ''}">${esc(x.status)}</span>
          ${x.note ? `<div class="muted-sm">${esc(x.note)}</div>` : ''}</td></tr>`).join('')}
      </tbody></table></div>`
      : `<p>No video files at all in this folder. The app looks for
         ${esc([...'mp4 avi mkv mov m4v ts dav'.split(' ')].join(', '))} files, and does
         not look inside sub-folders — pick the folder that holds the recordings
         themselves.</p>`}`,
    [{ label: 'Pick a different folder', primary: true,
       act: () => { closeModal(); openPicker(null, folder); } },
     { label: 'Close', act: closeModal }]);
}

async function openPicker(id, start) {
  let cur = start || '';
  const draw = async () => {
    let b;
    try { b = await api(`/api/browse?path=${encodeURIComponent(cur)}`, undefined, 'GET'); }
    catch (e) { return toast(e.message, true); }
    cur = b.path;
    modal(`Choose the footage folder`, `
      ${/* Typing or pasting a path is often the fastest way there, and on Windows it is
            sometimes the ONLY way: a mapped network share or a drive letter that is not
            in the list. Clicking through is still there for anyone who prefers it. */''}
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input class="field" id="pkPath" placeholder="or paste a path, e.g. D:\\Footage\\KDP-01"
               value="${esc(b.path)}" style="flex:1;font-family:ui-monospace,monospace">
        <button class="btn secondary" id="pkGo">Go</button>
      </div>
      ${/* Footage arrives on a USB stick or an external drive — practically never on the
            system drive — and walking up from C:\Users never reaches one. */''}
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
        ${(b.drives || []).map(x => `<button class="btn sm ghost"
          data-go="${esc(x.path)}">💾 ${esc(x.name)}</button>`).join('')}
      </div>
      <div class="picker">
        <div class="here">${esc(b.path)}</div>
        ${b.parent ? `<button data-go="${esc(b.parent)}">↰ &nbsp;up one level</button>` : ''}
        ${b.dirs.map(x => `<button data-go="${esc(x.path)}">📁 &nbsp;${esc(x.name)}</button>`).join('')
          || '<div style="padding:14px;color:var(--cc-fg-3)">No sub-folders here.</div>'}
      </div>
      <p class="muted-sm" style="margin:10px 0 0">
        ${b.videos_here ? `<b>${b.videos_here} video file(s)</b> in this folder.`
                        : 'No video files directly in this folder.'}</p>`,
      [{ label: `Use this folder`, primary: true, disabled: !b.videos_here,
         act: async () => {
           try {
             const r = await api(`/api/stations/${id}/folder`, { folder: cur });
             if (!r.added.length) {
               // Never close on a no-op. "0 attached" with the dialog gone looks exactly
               // like the app ignoring the click, which is what happened before.
               return showScan(r, cur);
             }
             toast(`${r.added.length} recording(s) attached`
                   + (r.guessed_clock.length
                      ? ` — ${r.guessed_clock.length} had no date in the filename` : ''));
             closeModal(); viewStation(id);
           } catch (e) { toast(e.message, true); }
         } }]);
    document.querySelectorAll('[data-go]').forEach(b2 => {
      b2.onclick = () => { cur = b2.dataset.go; draw(); };
    });
    const box = $('#pkPath'), go = $('#pkGo');
    const jump = () => { const v = box.value.trim(); if (v) { cur = v; draw(); } };
    if (go) go.onclick = jump;
    if (box) box.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); jump(); } };
  };
  draw();
}

/* ── step 2: the count line ── */
function paintLine(id, d) {
  const el = $('#stepLine');
  const has = (d.line || []).length;
  el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body"
    style="display:flex;align-items:center;gap:14px">
    <div style="flex:1">
      <b>${has ? 'Count line drawn' : 'Draw the count line'}</b>
      <div class="muted-sm">${has
        ? 'One line, used by every recording at this station.'
        : d.progress.extracted
          ? 'Vehicles are counted when they cross this line. Draw it once — detection is '
            + 'already done, so this is the last thing before the report.'
          : 'Vehicles are counted when they cross this line. You can draw it now, or '
            + 'detect an hour first and draw it once you have seen the road.'}</div>
    </div>
    <button class="btn ${has ? 'ghost' : 'primary'}" id="drawLine">
      ${has ? 'Redraw' : 'Draw the line'}</button>
  </div></div>`;
  $('#drawLine').onclick = () => openLine(id, d.line || []);
}

/* ── speed: two lines and a tape measure ── */
async function paintSpeed(id) {
  const el = $('#stepSpeed');
  if (!el) return;
  let d;
  try { d = await api(`/api/stations/${id}/speed`, undefined, 'GET'); } catch { return; }
  const t = d.trap, s = d.summary || {};
  el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body">
    <div style="display:flex;align-items:center;gap:14px">
      <div style="flex:1"><b>Speed${t ? '' : ' (optional)'}</b>
        <div class="muted-sm">${t
          ? `Two lines ${t.metres} m apart. ${s.n
              ? `${num(s.n)} vehicles measured.` : 'No vehicle has crossed both yet.'}`
          : 'Draw two lines across the road and say how far apart they are on the ground. '
            + 'Speed is then the time between them — no camera calibration, and the only '
            + 'number you have to get right is the distance.'}</div></div>
      <button class="btn ${t ? 'ghost' : 'secondary'}" id="setTrap">${
        t ? 'Change' : 'Set up speed'}</button>
    </div>

    ${s.n ? `<div class="grid g4" style="margin-top:16px">
      ${[['Median', s.median], ['85th percentile', s.p85], ['15th', s.p15],
         ['Vehicles', s.n]].map(([k, v]) => `<div>
        <div class="big" style="font-size:22px">${v}${k === 'Vehicles' ? '' : ''}</div>
        <div class="muted-sm">${k}${k === 'Vehicles' ? '' : ' km/h'}</div></div>`).join('')}
    </div>
    ${/* The 85th percentile is the figure a design or enforcement decision is made from,
          so it gets the same weight as the median rather than hiding in a table. */''}
    <table style="margin-top:14px"><thead><tr><th>Class</th><th class="right">Vehicles</th>
      <th class="right">Median km/h</th></tr></thead><tbody>
      ${Object.entries(s.by_class || {}).map(([c, v]) => `<tr><td>${esc(c)}</td>
        <td class="right num">${num(v.n)}</td>
        <td class="right num">${v.median}</td></tr>`).join('')}
    </tbody></table>
    ${(s.warnings || []).map(w => `<div class="card" style="margin-top:12px;
        border-color:var(--cc-warn)"><div class="card-body muted-sm">${esc(w)}</div></div>`).join('')}
    ${d.accuracy ? `<p class="muted-sm" style="margin:12px 0 0">${esc(d.accuracy.note)}</p>` : ''}
    ` : ''}
  </div></div>`;
  $('#setTrap').onclick = () => openTrap(id, t);
}

function openTrap(id, trap) {
  const pre = trap ? [{ name: 'A', ...trap.a }, { name: 'B', ...trap.b }] : [];
  modal('Set up speed measurement', `
    <p class="muted-sm" style="margin:0 0 10px">Draw <b>two</b> lines across the road, one
      after the other along the direction of travel. Draw each one <b>square across the
      carriageway</b> — line them up with something real, like a lane marking or the road
      edge. Two lines that merely look parallel on screen are not parallel on the ground,
      and one direction of traffic then reads far faster than the other.</p>
    <p class="muted-sm" style="margin:0 0 10px">Then measure the distance between them on
      the road and type it below. That measurement is the whole calibration: everything
      else is timing, which the video already knows.</p>
    <div id="trapHost" style="position:relative"></div>
    <label class="lbl" style="margin-top:12px">Distance between the two lines (metres)</label>
    <input class="field sm" id="trapM" type="number" min="2" max="500" step="0.1"
           style="max-width:200px;margin-top:6px" value="${trap ? trap.metres : ''}"
           placeholder="e.g. 30">`,
    [{ label: 'Save', primary: true, act: async () => {
        const ls = ED ? ED.lines() : [];   // the editor exposes lines(), not current()
        const m = parseFloat($('#trapM').value);
        if (ls.length !== 2) return toast('Draw exactly two lines across the road', true);
        if (!m || m < 2) return toast('Enter the distance between the lines in metres', true);
        try {
          await api(`/api/stations/${id}/speed`, { a: ls[0], b: ls[1], metres: m });
          closeModal(); toast('Speed measurement set up'); viewStation(id);
        } catch (e) { toast(e.message, true); }
      } }], 'wide');
  ED = mountLineEditor($('#trapHost'), {
    frameUrl: () => `/api/stations/${id}/frame?at=0.25`,
    onSave: () => {},              // saved with the distance, by the Save button above
  });
  ED.load(0, pre, 0);
}

/* The editor owns its own Save button, and that is deliberate: it knows whether anything
   is unsaved and the modal does not. So the modal offers Done, which refuses to close on
   unsaved work rather than discarding it silently -- losing a line you just drew and
   being told nothing is the worst version of this screen. */
function openLine(id, lines) {
  modal('Draw the count line', `
    <p class="muted-sm" style="margin:0 0 10px">Drag across the lane where vehicles should
      be counted — roughly at right angles to the traffic, in the middle of the frame where
      vehicles are biggest. Avoid the far corners: a line in the distance counts almost
      nothing, because vehicles there are only a few pixels across.</p>
    <div id="lineHost" style="position:relative"></div>`,
    [{ label: 'Done', primary: true, act: async () => {
        if (ED && ED.isDirty()) {
          await ED.save();
          if (ED.isDirty()) return toast('Could not save the line', true);
        }
        closeModal(); viewStation(id);
      } }], 'wide');
  ED = mountLineEditor($('#lineHost'), {
    frameUrl: () => `/api/stations/${id}/frame?at=0.25`,
    onSave: async ls => {
      await api(`/api/stations/${id}/line`, { lines: ls });
      toast(ls.length ? 'Count line saved' : 'Count line cleared');
    },
  });
  ED.load(0, lines, 0);
}
let ED = null;

/* ── step 3: the hours ── */
/* Hours are SELECTED, not started. Clicking a tile used to send that hour straight to
   the detector -- a misclick began work that costs an hour of machine time, and with a
   rented GPU it also began spending money. Now clicking chooses, and one Run button
   starts what has been chosen. The set lives outside the render so a poll redrawing the
   grid does not lose the selection mid-choice. */
let PICKED = new Set();

function paintHours(id, d) {
  const el = $('#stepHours');
  const q = d.queue || {};
  const busyId = q.running ? q.running.video_id : null;
  const waiting = new Set((q.waiting || []).map(w => w.video_id));
  const speed = d.device.speed || 1;

  const stateOf = h => h.files.some(f => f.video_id === busyId || waiting.has(f.video_id))
    ? 'busy' : h.state;
  // An hour that finished or is already working cannot be picked, so a stale tick from
  // before it started must not survive into the Run.
  d.hours.forEach(h => { if (stateOf(h) !== 'todo' && stateOf(h) !== 'part') PICKED.delete(h.hour); });

  const pickable = d.hours.filter(h => ['todo', 'part'].includes(stateOf(h)));
  const chosen = pickable.filter(h => PICKED.has(h.hour));
  const mn = chosen.reduce((a, h) => a + h.minutes, 0);

  el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body">
    <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:4px">
      <h2 style="margin:0;font-size:17px">Footage by hour</h2>
      <span class="muted-sm" style="flex:1">Tick the hours you want, then press Run.
        Nothing starts on its own.</span>
      ${pickable.length ? `<button class="btn sm ghost" id="pickAll">${
        chosen.length === pickable.length ? 'Clear all' : 'Select all'}</button>` : ''}
    </div>
    <div id="qbar"></div>
    <div class="hours" style="margin-top:12px">${d.hours.map(h => {
      const state = stateOf(h);
      const on = PICKED.has(h.hour);
      const pct = h.total ? Math.round(100 * h.extracted / h.total) : 0;
      return `<button class="hr ${state === 'done' ? 'done' : ''}
                ${state === 'busy' ? 'busy' : ''} ${on ? 'picked' : ''}"
                data-hour="${esc(h.hour)}"
                ${state === 'done' || state === 'busy' ? 'disabled' : ''}>
        <div class="t">${on ? '<span class="tick">✓</span> ' : ''}${esc(h.label)}${
          h.night ? '<span class="night">night</span>' : ''}</div>
        <div class="d">${h.minutes} min filmed${h.coverage < 0.99
          ? ` · ${Math.round(h.coverage * 100)}% of the hour` : ''}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="cta">${state === 'done' ? '✓ done'
          : state === 'busy' ? 'working…'
          : on ? 'selected'
          : state === 'part' ? `finish (${h.total - h.extracted} left)`
          : `≈${mins(h.minutes * 60 / speed)}`}</div>
      </button>`;
    }).join('')}</div>

    ${chosen.length ? `<div class="runbar">
      <div style="flex:1"><b>${chosen.length} hour${chosen.length > 1 ? 's' : ''} selected</b>
        <span class="muted-sm">${mn} min of footage · about ${mins(mn * 60 / speed)}${
          d.device.cloud ? ' on a rented GPU, which costs money' : ''}</span></div>
      <button class="btn ghost sm" id="pickNone">Clear</button>
      <button class="btn primary" id="runPicked">Run ${chosen.length} hour${
        chosen.length > 1 ? 's' : ''}</button>
    </div>` : ''}
  </div></div>`;

  el.querySelectorAll('[data-hour]').forEach(b => b.onclick = () => {
    const h = b.dataset.hour;
    PICKED.has(h) ? PICKED.delete(h) : PICKED.add(h);
    paintHours(id, d);                     // selection only — nothing is sent
  });
  const all = $('#pickAll');
  if (all) all.onclick = () => {
    if (chosen.length === pickable.length) PICKED.clear();
    else pickable.forEach(h => PICKED.add(h.hour));
    paintHours(id, d);
  };
  const none = $('#pickNone');
  if (none) none.onclick = () => { PICKED.clear(); paintHours(id, d); };

  const run = $('#runPicked');
  if (run) run.onclick = async () => {
    run.disabled = true; run.textContent = 'Starting…';
    const hours = chosen.map(h => h.hour);
    try {
      // Sequentially, so a rejection names the hour it belongs to rather than failing
      // the whole batch anonymously.
      for (const h of hours) {
        await api(`/api/stations/${id}/hours/${encodeURIComponent(h)}/extract`, {});
      }
      PICKED.clear();
      toast(`Started ${hours.length} hour${hours.length > 1 ? 's' : ''} — you can leave this running`);
      viewStation(id);
    } catch (e) {
      toast(e.message, true);
      run.disabled = false; run.textContent = `Run ${hours.length} hours`;
    }
  };
  paintQueue(d.queue);
}

function paintQueue(q) {
  const el = $('#qbar');
  if (!el) return;
  const all = (q && q.running_all) || (q && q.running ? [q.running] : []);
  if (!all.length && !(q && (q.waiting || []).length)) { el.innerHTML = ''; return; }
  const wait = (q.waiting || []).length;
  el.innerHTML = `<div style="border:1px solid var(--cc-acc);border-radius:var(--cc-r-md);
      padding:12px;background:var(--cc-acc-bg);margin-top:10px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <div style="flex:1"><b>${all.length
        ? `Detecting ${all.length} recording${all.length > 1 ? 's' : ''}` : 'Queued'}</b>
        <span class="muted-sm">${wait ? ` · ${wait} more waiting` : ''}${
          q.workers > 1 ? ` · ${q.workers} at a time` : ''}</span></div>
      <button class="btn ghost sm" id="qcancel">Stop after these</button>
    </div>
    ${/* One bar per running clip. With a pool, a single bar would jump between clips and
          read as progress going backwards. */''}
    <div id="qbars">${all.map((r, i) => `
      <div style="margin-top:${i ? 8 : 0}px">
        <div class="muted-sm" data-qsub="${i}">${esc(r.name)} — ${Math.round(r.progress || 0)}%${
          r.eta_s ? ` · ${mins(r.eta_s)} left` : ''}</div>
        <div style="height:6px;background:var(--cc-surface);border-radius:3px;overflow:hidden">
          <i data-qfill="${i}" style="display:block;height:100%;background:var(--cc-acc);
             width:${Math.round(r.progress || 0)}%"></i></div>
      </div>`).join('')}</div>
  </div>`;
  const c = $('#qcancel');
  if (c) c.onclick = async () => {
    const r2 = await api('/api/queue/cancel', {});
    toast(`${r2.dropped} queued clip(s) dropped`);
  };
}

/* Patch only the moving parts. Re-rendering the whole station every 3 seconds would
   reset the folder picker and fight the user for the scroll position. */
async function tick(id) {
  const q = await api('/api/queue', undefined, 'GET').catch(() => null);
  if (!q) return;
  const all = q.running_all || (q.running ? [q.running] : []);
  if (!all.length && !(q.waiting || []).length) { clearInterval(POLL); return viewStation(id); }
  const bars = document.querySelectorAll('[data-qfill]');
  // The number of running clips changes as the pool drains; a full repaint is the only
  // honest way to add or remove a bar.
  if (bars.length !== all.length) return viewStation(id);
  all.forEach((r, i) => {
    const f = document.querySelector(`[data-qfill="${i}"]`);
    const t = document.querySelector(`[data-qsub="${i}"]`);
    if (f) f.style.width = `${Math.round(r.progress || 0)}%`;
    if (t) t.textContent = `${r.name} — ${Math.round(r.progress || 0)}%`
      + (r.eta_s ? ` · ${mins(r.eta_s)} left` : '');
  });
}

/* ── steps 4 & 5: review and report ── */
function paintAfter(id, d) {
  const p = d.progress;

  /* Detection and counting are different things, and the gap between them is the line.
     The detector has found every vehicle in the footage; none of them has "crossed"
     anything until there is a line to cross. Showing Review and Report before that
     would offer two buttons that come back empty and explain nothing. */
  if (!p.line) {
    $('#stepAfter').innerHTML = `<div class="card" style="border-color:var(--cc-acc)">
      <div class="card-body" style="text-align:center;padding:28px">
        <div class="big">${num(p.tracks)}</div>
        <p class="muted-sm" style="margin:4px 0 0">vehicles found in the footage</p>
        <p style="margin:14px 0 0;max-width:520px;margin-inline:auto">
          None of them is <b>counted</b> yet. A vehicle counts when it crosses the line,
          so nothing can be reviewed or reported until you draw one.</p>
        <button class="btn primary" id="afterLine" style="margin-top:14px">
          Draw the count line</button>
      </div></div>`;
    const b = $('#afterLine');
    if (b) b.onclick = () => openLine(id, d.line || []);
    return;
  }

  $('#stepAfter').innerHTML = `<div class="grid g2">
    <div class="card"><div class="card-body">
      <h2 style="margin:0 0 4px;font-size:17px">Check the model's work</h2>
      <p class="muted-sm" style="margin:0 0 12px">${num(p.tracks)} vehicles detected,
        ${num(p.verified)} checked by you. Start with the ones that matter most —
        heavy vehicles and anything the model was unsure about.</p>
      <a class="btn primary" href="#review/${id}">Review</a>
      <a class="btn ghost" href="#review/${id}/all">Review everything</a>
    </div></div>
    <div class="card"><div class="card-body">
      <h2 style="margin:0 0 4px;font-size:17px">Report</h2>
      <p class="muted-sm" style="margin:0 0 12px">Vehicle counts by class and by
        15-minute period, with PCU.</p>
      <a class="btn primary" href="#report/${id}">Open report</a>
      <a class="btn ghost" href="/api/stations/${id}/report.xlsx">Download Excel</a>
    </div></div>
  </div>`;
}

/* Keys for the six classes that run past 1-9.
   Mnemonic where possible, and checked against the keys already taken (A attribute,
   X reject, U unclear, Enter confirm) — a shortcut that shadows another is worse than
   no shortcut, because it answers something the reviewer did not mean. */
const CLASS_KEY = {
  '3Axle_Truck': 'T',      // Three-axle
  MAV: 'M',
  Cycle: 'C',
  Cycle_Rickshaw: 'R',     // Rickshaw
  Animal_Cart: 'N',        // aNimal — A and C are taken
  Other: 'O',
};

/* ─────────────────────────── settings: the cloud GPU ─────────────────────────── */
/* The screen where somebody hands the app a way to spend their money, so it is built
   around telling them what it is costing rather than around the connection working. */
async function viewSettings() {
  const c = await api('/api/cloud', undefined, 'GET');
  const sp = c.spend || {};
  const live = c.running || [];
  const pct = sp.limit_usd ? Math.min(100, 100 * sp.month_usd / sp.limit_usd) : 0;

  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:18px">
      <div><h1>Settings</h1><p>Detect on a rented GPU instead of this computer</p></div>
      <a class="btn ghost" href="#stations">Back</a></div>

    ${live.length ? `<div class="card" style="margin-bottom:14px;border-color:var(--cc-bad)">
      <div class="card-body" style="display:flex;align-items:center;gap:14px">
        <div style="flex:1"><b>${live.length} GPU running right now</b>
          <div class="muted-sm">${live.map(p =>
            `${esc(p.name || p.id)} — ${mins(p.uptime_s)}, $${p.spent_so_far} so far`
          ).join('<br>')}</div></div>
        <button class="btn danger" id="stopAll">Stop now</button>
      </div></div>` : ''}

    <div class="card" style="margin-bottom:14px"><div class="card-body">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
        <span class="status ${c.ok ? 'ok' : c.configured ? 'warn' : ''}">${
          c.ok ? 'connected' : c.configured ? 'not connecting' : 'not set up'}</span>
        ${/* "from", not "at". This is RunPod's lowest listed price for the card; the
              machine actually allocated can bill more than double it. The runs page shows
              what was really charged. */''}
        ${c.ok ? `<span class="muted-sm">balance $${c.balance_usd}${
          c.gpu_price ? ` · ${esc(c.gpu)} from $${c.gpu_price}/hr` : ''}</span>` : ''}
        ${c.error ? `<span class="muted-sm" style="color:var(--cc-bad-fg)">${esc(c.error)}</span>` : ''}
      </div>

      <label class="lbl">RunPod API key</label>
      <div style="display:flex;gap:8px;margin:6px 0 4px">
        <input class="field" id="ckey" type="password" style="flex:1"
          placeholder="${c.configured ? esc(c.key_hint) + '  (leave blank to keep)' : 'paste your key'}">
        <button class="btn secondary" id="csave">Save</button>
      </div>
      <p class="muted-sm" style="margin:0 0 16px">From runpod.io → Settings → API Keys.
        Stored on this computer only.</p>

      <div class="grid g2">
        <div><label class="lbl">Graphics card to rent</label>
          <select class="field sm" id="cgpu" style="margin-top:6px">
            ${['NVIDIA GeForce RTX 4090','NVIDIA GeForce RTX 3090','NVIDIA GeForce RTX 5090',
               'NVIDIA L40S','NVIDIA RTX A4000'].map(g =>
              `<option${g === c.gpu ? ' selected' : ''}>${esc(g)}</option>`).join('')}
          </select>
          <p class="muted-sm" style="margin:6px 0 0">A 3090 costs a third less than a 4090
            and is nearly as quick for this — measured, not guessed.</p></div>
        <div><label class="lbl">Spending limit this month (US$)</label>
          <input class="field sm" id="climit" type="number" min="1" step="1"
                 value="${sp.limit_usd ?? 25}" style="margin-top:6px">
          <p class="muted-sm" style="margin:6px 0 0">Detection refuses to start once this
            is used up, and a GPU already running is stopped. It is a stop, not a
            warning. The card's real price is set when a machine is allocated and can be
            higher than the listed one — <a href="#runs">the runs page</a> shows what was
            actually charged.</p></div>
      </div>

      <label class="chk" style="margin-top:16px"><input type="checkbox" id="cen"${
        c.enabled ? ' checked' : ''}> Use the rented GPU for detection</label>
      <p class="muted-sm" style="margin:6px 0 0">Off means everything runs on this computer,
        free and slower. A rented GPU stops on its own after
        ${Math.round((c.idle_seconds || 300) / 60)} minutes with nothing to do, and again
        when the app next starts.</p>
    </div></div>

    <div class="card"><div class="card-body">
      <div style="display:flex;align-items:baseline;gap:12px">
        <h2 style="margin:0;font-size:17px">Spent this month</h2>
        <span style="flex:1"></span>
        <span class="big" style="font-size:22px">$${(sp.month_usd ?? 0).toFixed(2)}</span>
        <span class="muted-sm">of $${(sp.limit_usd ?? 0).toFixed(2)}</span>
      </div>
      <div style="height:8px;background:var(--cc-hover);border-radius:4px;overflow:hidden;
                  margin:10px 0 6px">
        <i style="display:block;height:100%;width:${pct}%;background:${
          pct > 90 ? 'var(--cc-bad)' : pct > 70 ? 'var(--cc-warn)' : 'var(--cc-ok)'}"></i></div>
      <p class="muted-sm" style="margin:0">${sp.runs_this_month ?? 0} run(s)${
        sp.live_usd ? ` · $${sp.live_usd} of that is still running` : ''}
        · <a href="#runs">see every run</a></p>
    </div></div>
  </div>`;

  const sa = $('#stopAll');
  if (sa) sa.onclick = async () => {
    sa.disabled = true; sa.textContent = 'Stopping…';
    const r = await api('/api/cloud/stop', {});
    toast(`Stopped ${r.stopped.length} GPU(s)`); viewSettings();
  };
  $('#csave').onclick = async () => {
    const key = $('#ckey').value.trim();
    try {
      await api('/api/cloud/settings', {
        key: key || null, gpu: $('#cgpu').value,
        limit_usd: parseFloat($('#climit').value), enabled: $('#cen').checked,
      });
      toast('Saved'); viewSettings();
    } catch (e) { toast(e.message, true); }
  };
  ['cgpu', 'climit', 'cen'].forEach(idd => {
    const el = $('#' + idd);
    if (el) el.onchange = () => $('#csave').click();
  });
}

async function viewRuns(_) {
  const d = await api('/api/cloud/runs', undefined, 'GET');
  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:18px">
      <div><h1>GPU runs</h1><p>every rented machine, what it did and what it cost</p></div>
      <a class="btn ghost" href="#settings">Back</a></div>
    <div class="card"><table><thead><tr><th>Started</th><th>Card</th>
      <th class="right">Ran for</th><th class="right">Cost</th><th>State</th><th>Note</th>
      </tr></thead><tbody>
      ${(d.runs || []).length ? d.runs.map(r => `<tr>
        <td class="mono">${r.started ? new Date(r.started * 1000).toLocaleString() : '—'}</td>
        <td>${esc(r.gpu || '')}</td>
        <td class="right">${r.seconds ? mins(r.seconds) : '—'}</td>
        <td class="right num">$${(r.usd ?? 0).toFixed(3)}</td>
        <td><span class="status ${r.status === 'stopped' ? 'ok' : 'warn'}">${esc(r.status)}</span></td>
        <td class="muted-sm">${esc(r.note || '')}</td></tr>`).join('')
        : '<tr><td colspan="6" style="padding:24px;text-align:center;color:var(--cc-fg-3)">No GPU has been rented yet.</td></tr>'}
      </tbody></table></div>
  </div>`;
}

/* ─────────────────────────── voice ─────────────────────────── */
/* Voice drives exactly the same answer() the buttons and keys do. One path to a verdict,
   so there is no way for a spoken answer to be saved differently from a clicked one. */
const VOICE = createVoice({
  onAction: hit => {
    if (hit.kind === 'control') {
      const it = RQ && RQ.items[RI];
      if (!it) return;
      if (hit.value === '__yes') return answer(it.class);
      if (hit.value === '__skip') { RI = Math.min(RQ.items.length, RI + 1); return drawReview(); }
      if (hit.value === '__back') { RI = Math.max(0, RI - 1); return drawReview(); }
      return answer(hit.value);
    }
    answer(hit.value);
  },
  onHeard: (text, status) => {
    const el = $('#vHeard');
    if (!el) return;
    el.textContent = status === 'ok' ? `“${text}”`
      : status === 'unsure' ? `“${text}” — not sure, say it again`
      : `“${text}” — not a command`;
    el.className = 'muted-sm vheard' + (status === 'ok' ? ' ok' : ' warn');
  },
  onState: (on, why) => {
    if (why) toast(why, true);
    const b = $('#vBtn');
    if (b) { b.classList.toggle('on', on); b.textContent = on ? '🎙 Listening' : '🎙 Voice'; }
    try { localStorage.setItem('tl_voice', on ? '1' : '0'); } catch { /* private mode */ }
  },
});

/* ── teaching the recogniser this speaker's voice ──
   The built-in phrase list is a guess about how people say these words. It is wrong often
   enough to matter: "MAV" comes back as "I am way", "yes" as "s". Rather than keep
   guessing, the app records what the recogniser ACTUALLY returns for this person on this
   microphone and stores that as an alias. One pass costs two minutes and fixes the
   speaker's own worst words for good. */
function openVoiceTraining() {
  const rows = () => {
    const learned = loadAliases();
    const byId = {};
    for (const [phrase, id] of Object.entries(learned)) (byId[id] ||= []).push(phrase);
    return TRAINABLE.map(t => `
      <tr data-id="${esc(t.id)}">
        <td><b>${esc(t.label)}</b><div class="muted-sm">say “${esc(t.say)}”</div></td>
        <td class="learned">${(byId[t.id] || []).map(p =>
          `<span class="chip ok">${esc(p)}</span>`).join(' ')
          || '<span class="muted-sm">not trained</span>'}</td>
        <td class="right"><button class="btn sm ghost" data-rec="${esc(t.id)}">Record</button></td>
      </tr>`).join('');
  };

  modal('Teach it your voice', `
    <p class="muted-sm" style="margin:0 0 12px">Press Record, then say the word once,
      normally. Whatever the recogniser hears is stored as your way of saying it — so if
      “MAV” comes back as “I am way”, that becomes a valid command instead of a mistake.
      Stored on this computer only.</p>
    <div class="lv-table" style="max-height:52vh;overflow:auto">
      <table><thead><tr><th>Command</th><th>Your words</th><th></th></tr></thead>
      <tbody id="vtBody">${rows()}</tbody></table></div>
    <p class="muted-sm" id="vtStatus" style="min-height:18px;margin:10px 0 0"></p>`,
    [{ label: 'Clear all training', act: () => {
        forgetAliases();
        $('#vtBody').innerHTML = rows(); wireRec();
        toast('Voice training cleared');
      } },
     { label: 'Done', primary: true, act: closeModal }], 'wide');

  function wireRec() {
    document.querySelectorAll('[data-rec]').forEach(b => b.onclick = async () => {
      const id = b.dataset.rec;
      const st = $('#vtStatus');
      document.querySelectorAll('[data-rec]').forEach(x => { x.disabled = true; });
      b.textContent = 'Listening…';
      st.textContent = 'Say it now…';
      const alts = await VOICE.captureNext(6000);
      if (!alts.length) {
        st.textContent = 'Heard nothing — check the microphone and try again.';
      } else {
        // Only the top result is stored. Saving every alternative would map a handful of
        // near-miss phrases to this command, and one of them will collide with another.
        saveAlias(alts[0], id);
        st.textContent = `Learned “${alts[0]}”` +
          (alts.length > 1 ? ` (also heard: ${alts.slice(1, 3).join(', ')})` : '');
      }
      $('#vtBody').innerHTML = rows();
      wireRec();
    });
  }
  wireRec();
}

/* ─────────────────────────── review ─────────────────────────── */
let RQ = null, RI = 0, RID = null, RMODE = 'critical', RCLS = '';

async function viewReview(id, mode) {
  RID = id; RMODE = ['all', 'done'].includes(mode) ? mode : 'critical';
  await reloadReview();
}

async function reloadReview() {
  app.innerHTML = `<div class="wrap"><div class="boot">Loading vehicles…</div></div>`;
  RQ = await api(`/api/stations/${RID}/review?mode=${RMODE}`
    + `&cls=${encodeURIComponent(RCLS)}`, undefined, 'GET');
  RI = 0;
  drawReview();
}

function drawReview() {
  const it = RQ.items[RI];
  const A = RQ.answers || { classes: [], attributes: [] };

  /* Which vehicles to work through. Two independent choices, deliberately kept apart:
     WHAT needs looking at (everything, or only what the model could not settle) and
     WHICH class. Combining them into one dropdown would hide "all the buses, including
     the ones the model was confident about" — which is exactly the audit somebody asks
     for when a bus count looks wrong. */
  function filterBar() {
    const total = (RQ.classes || []).reduce((a, [, n]) => a + n, 0);
    return `<div class="card rvfilter"><div class="card-body">
      ${/* Three views, not two. Without the third, an answer went in and became
            unreachable — a surveyor who miscalled a lorry had no way back to it, and
            "0 to go" was simply the end of the screen. Answering again overwrites the
            same row, so nothing is ever listed or counted twice. */''}
      <div class="seg" role="group">
        <button data-mode="critical" class="${RMODE === 'critical' ? 'on' : ''}">
          Needs a check</button>
        <button data-mode="all" class="${RMODE === 'all' ? 'on' : ''}">Everything</button>
        <button data-mode="done" class="${RMODE === 'done' ? 'on' : ''}">
          Already checked</button>
      </div>
      <label class="lbl" for="rvCls">Vehicle type</label>
      <select class="field sm" id="rvCls">
        <option value="">All types${total ? ` (${num(total)})` : ''}</option>
        ${(RQ.classes || []).map(([c, n]) =>
          `<option value="${esc(c)}"${c === RCLS ? ' selected' : ''}>${esc(c)} (${n})</option>`
        ).join('')}
      </select>
      <span style="flex:1"></span>
      <span class="muted-sm">${num(RQ.items.length)} to go · ${num(RQ.answered)} done</span>
    </div></div>`;
  }
  function wireFilter() {
    app.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => {
      if (RMODE === b.dataset.mode) return;
      RMODE = b.dataset.mode; reloadReview();
    });
    const sel = $('#rvCls');
    if (sel) sel.onchange = () => { RCLS = sel.value; reloadReview(); };
  }

  if (!it) {
    app.innerHTML = `<div class="wrap">
      <div class="page-head"><div><h1>All done</h1>
        <p>${RMODE === 'critical'
          ? 'Every vehicle that needed a second opinion has been checked.'
          : RMODE === 'done'
            ? 'You have not answered any vehicles yet — nothing to look back at.'
            : 'Every detected vehicle has been checked.'}</p></div>
        <a class="btn ghost" href="#station/${RID}">Back to station</a></div>
      ${filterBar()}
      <div class="card"><div class="card-body" style="text-align:center;padding:36px">
        <div class="big">${num(RQ.answered)}</div>
        <p class="muted-sm">vehicles you have checked</p>
        <a class="btn primary" href="#report/${RID}" style="margin-top:14px">See the report</a>
        ${RMODE === 'critical' ? `<button class="btn ghost" id="rvAll"
           style="margin-top:14px">Check the rest too</button>` : ''}
      </div></div></div>`;
    wireFilter();
    const ra = $('#rvAll');
    if (ra) ra.onclick = () => { RMODE = 'all'; reloadReview(); };
    return;
  }
  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:14px">
      <div><h1 style="font-size:22px">Is this a ${esc(it.class)}?</h1>
        <p>${RI + 1} of ${RQ.items.length}${RMODE === 'critical'
          ? ' needing a check' : ''} · ${esc(it.clock)}</p></div>
      ${/* Voice is the ONE part of this app that needs a connection: the browser sends
            the audio to its own speech service. Everything else — detection, counting,
            the report — runs entirely on this machine. Say so plainly rather than
            letting a surveyor in a site office press a button that cannot work. */''}
      ${VOICE.supported ? `<button class="btn ghost" id="vBtn"${navigator.onLine ? '' : ' disabled'}
        title="${navigator.onLine
          ? 'Say the vehicle type instead of clicking. Sends what you say to the browser\'s speech service.'
          : 'No internet connection — speech recognition is the only part of this app that needs one.'}">
        🎙 Voice${navigator.onLine ? '' : ' (needs internet)'}</button>
      <button class="btn ghost" id="vTrain"${navigator.onLine ? '' : ' disabled'}
        title="Record how you say each word">Teach</button>` : ''}
      ${/* Not "Stop". Every answer is already written the moment it is pressed, and
            "Stop" reads like abandoning unsaved work — which is exactly the doubt that
            makes somebody sit through a queue they meant to leave. */''}
      <a class="btn ghost" href="#station/${RID}">Finish later</a></div>

    ${filterBar()}

    <div class="card"><div class="card-body rv">
      <div class="stage"><div class="imgs">
        <img class="main" src="/api/review/${it.video_id}/${it.track_id}/crop.jpg" alt="vehicle">
        <img class="ctx" src="/api/review/${it.video_id}/${it.track_id}/ctx.jpg" alt="in frame">
      </div></div>
      ${/* Always rendered, empty when there is nothing to say — an appearing line would
            push every button down by its own height on flagged vehicles only. */''}
      <p class="muted-sm why">${it.verdict
        ? `You answered <b>${esc(it.verdict)}</b>${
            it.revisions ? ` · changed ${it.revisions} time(s)` : ''} — answering again replaces it`
        : it.mandatory ? esc((it.reasons || []).join(' · ')) : ''}</p>
      ${/* Every answer has a key, and the key is printed on the button. A surveyor does
            a few hundred of these in a sitting; the difference between reaching for the
            mouse each time and pressing Enter is the difference between an afternoon and
            a morning. Printing the key on the button is what makes it get learned --
            shortcuts hidden behind a help page are shortcuts nobody uses. */''}
      ${/* Four fixed columns: confirm · attribute · reject · abstain. The attribute
            column belongs to whichever attribute applies to this class, and when none
            does it stays as an invisible placeholder — collapsing it would slide the
            next two buttons left and make the mouse re-aim on every second vehicle. */''}
      <div class="ans">
        <button class="btn ok" data-a="${esc(it.class)}"
          title="Confirm ${esc(it.class)}">✓ Yes, ${esc(it.class)} <kbd>Enter</kbd></button>
        ${(() => {
          const a = A.attributes.find(x => x.parents.includes(it.class));
          return a
            ? `<button class="btn acc" data-a="${esc(a.key)}">${esc(a.label)} <kbd>A</kbd></button>`
            : `<button class="btn secondary slot-empty" tabindex="-1" aria-hidden="true">—</button>`;
        })()}
        <button class="btn danger" data-a="not_a_vehicle">✗ Not a vehicle <kbd>X</kbd></button>
        <button class="btn secondary" data-a="unclear">Can't tell <kbd>U</kbd></button>
      </div>
      ${/* The class grid never changes: same 15 classes, same order, same five columns,
            so "LCV is the fourth one on the top row" stays true all afternoon. */''}
      <div class="more">
        <span class="muted-sm lead">or pick the right one:</span>
        ${A.classes.map((c, n) => {
          const key = n < 9 ? String(n + 1) : CLASS_KEY[c];
          return `<button class="btn sm ghost" data-a="${esc(c)}" title="${esc(c)}">${
            key ? `<kbd>${esc(key)}</kbd> ` : ''}${esc(c)}</button>`;
        }).join('')}
      </div>
      <div class="nav">
        <button class="btn ghost sm" id="rvBack" ${RI ? '' : 'disabled'}>
          <kbd>←</kbd> Back</button>
        <button class="btn ghost sm" id="rvSkip">Skip <kbd>→</kbd></button>
      </div>
      <p class="muted-sm vheard" id="vHeard"></p>
      <p class="muted-sm" style="margin:10px 0 0">Each answer is saved as you press it and
        goes straight into the report. Leave whenever you like — you carry on from here.</p>
    </div></div>
  </div>`;

  wireFilter();
  app.querySelectorAll('[data-a]').forEach(b => b.onclick = () => answer(b.dataset.a));

  /* The attribute phrase is only a live command while an attribute button is on screen,
     so the vocabulary is rebuilt per vehicle rather than once per session. */
  const mine = A.attributes.filter(x => x.parents.includes(it.class));
  VOICE.setContext(A.classes, mine.map(x => ({
    key: x.key,
    spoken: x.key === 'apsrtc' ? ['government bus', 'govt bus', 'apsrtc', 'state bus']
          : x.key === 'taxi' ? ['taxi', 'yellow board', 'yellow plate']
          : x.key === 'maxi' ? ['maxi', 'seven seater', 'big auto']
          : [x.label],
  })));
  const vb = $('#vBtn');
  if (vb) {
    vb.classList.toggle('on', VOICE.listening());
    vb.textContent = VOICE.listening() ? '🎙 Listening' : '🎙 Voice';
    vb.onclick = () => VOICE.toggle();
  }
  const vt = $('#vTrain');
  if (vt) vt.onclick = () => openVoiceTraining();
  $('#rvBack').onclick = () => { RI = Math.max(0, RI - 1); drawReview(); };
  $('#rvSkip').onclick = () => { RI = Math.min(RQ.items.length, RI + 1); drawReview(); };
  const nx = RQ.items[RI + 1];
  if (nx) new Image().src = `/api/review/${nx.video_id}/${nx.track_id}/crop.jpg`;
}

async function answer(a) {
  const it = RQ.items[RI];
  if (!it) return;
  RI++; drawReview();
  try {
    await api('/api/review', { video_id: it.video_id, track_id: it.track_id, answer: a });
    RQ.answered = (RQ.answered || 0) + 1;
  } catch (e) {
    RI = RQ.items.indexOf(it); drawReview();
    toast(`Not saved: ${e.message}`, true);
  }
}

/* The review keyboard.

   Guarded three ways, each of which was a real way to answer a vehicle by accident: not
   while typing in a field, not while a modal is open on top, and not with a modifier held
   (Cmd-R to reload must reload, not record "Tractor"). */
document.addEventListener('keydown', e => {
  if (!RQ || !location.hash.startsWith('#review')) return;
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if (e.target.isContentEditable || $('#modal')) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const it = RQ.items[RI];
  if (!it) return;
  const A = RQ.answers || { classes: [], attributes: [] };
  const k = e.key.toLowerCase();

  if (e.key === 'Enter') { e.preventDefault(); return answer(it.class); }
  if (k === 'x') { e.preventDefault(); return answer('not_a_vehicle'); }
  if (k === 'u') { e.preventDefault(); return answer('unclear'); }
  if (e.key === 'ArrowLeft') { RI = Math.max(0, RI - 1); return drawReview(); }
  if (e.key === 'ArrowRight') { RI = Math.min(RQ.items.length, RI + 1); return drawReview(); }

  // A — the attribute button, when this class has one. Only the key that is printed on
  // a visible button does anything; an invisible second binding is a way to record an
  // answer nobody meant to give.
  if (k === 'a') {
    const at = A.attributes.find(x => x.parents.includes(it.class));
    if (at) { e.preventDefault(); return answer(at.key); }
  }

  // 1-9 then the letters — whatever is printed on the button, and nothing else.
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= 9 && A.classes[n - 1]) {
    e.preventDefault();
    return answer(A.classes[n - 1]);
  }
  const byLetter = Object.entries(CLASS_KEY)
    .find(([cls, key]) => key.toLowerCase() === k && A.classes.includes(cls));
  if (byLetter) { e.preventDefault(); return answer(byLetter[0]); }
});

/* ─────────────────────────── report ─────────────────────────── */
async function viewReport(id) {
  const d = await api(`/api/stations/${id}/report`, undefined, 'GET');
  const wireAnnotate = () => app.querySelectorAll('[data-annot]').forEach(b => {
    b.onclick = async () => {
      const was = b.textContent;
      b.disabled = true; b.textContent = 'Queued…';
      try {
        await api(`/api/clips/${b.dataset.annot}/annotate`, {});
        toast('Making the video — it queues behind any detection still running');
        // Poll for it rather than leaving a dead "Queued…" button: a render takes
        // minutes, and until now the only way to learn it had finished was to reload.
        watchRender(+b.dataset.annot, b);
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = was; }
    };
  });
  if (d.empty) {
    app.innerHTML = `<div class="wrap"><div class="page-head"><div><h1>Report</h1></div>
      <a class="btn ghost" href="#station/${id}">Back</a></div>
      <div class="card"><div class="card-body" style="padding:40px;text-align:center;
        color:var(--cc-fg-3)">${esc(d.note)}</div></div></div>`;
    return;
  }
  const maxh = Math.max(1, ...d.hourly.map(h => h.n));
  app.innerHTML = `<div class="wrap">
    <div class="page-head" style="margin-bottom:18px">
      <div><h1>${esc(d.station.name)}</h1><p>Classified vehicle count</p></div>
      <a class="btn ghost" href="#station/${id}">Back</a>
      <a class="btn primary" href="/api/stations/${id}/report.xlsx">Download Excel</a></div>

    <div class="grid g4" style="margin-bottom:16px">
      <div class="card"><div class="card-body"><div class="muted-sm">Vehicles</div>
        <div class="big">${num(d.total)}</div></div></div>
      <div class="card"><div class="card-body"><div class="muted-sm">PCU (IRC:64)</div>
        <div class="big">${num(d.pcu_total)}</div></div></div>
      <div class="card"><div class="card-body"><div class="muted-sm">Checked by you</div>
        <div class="big">${num(d.reviewed)}</div></div></div>
      <div class="card"><div class="card-body"><div class="muted-sm">Periods</div>
        <div class="big">${num(d.bins_15min.length)}</div>
        <div class="muted-sm">15-minute bins</div></div></div>
    </div>

    <div class="grid g2">
      <div class="card"><div class="card-head"><div><h2>By class</h2>
        <p>with passenger-car units</p></div></div>
        <table><thead><tr><th>Class</th><th class="right">Count</th>
          <th class="right">PCU</th><th class="right">Share</th></tr></thead><tbody>
        ${d.composition.map(c => `<tr><td>${esc(c.class)}</td>
          <td class="right num">${num(c.n)}</td>
          <td class="right num">${num(d.pcu_by_class[c.class])}</td>
          <td class="right num">${c.share}%</td></tr>`).join('')}
        <tr class="tot"><td><b>Total</b></td><td class="right num"><b>${num(d.total)}</b></td>
          <td class="right num"><b>${num(d.pcu_total)}</b></td><td></td></tr>
        </tbody></table></div>

      <div class="card"><div class="card-head"><div><h2>By hour</h2>
        <p>when the traffic came</p></div></div>
        <div class="bars" style="padding:0 20px 18px">${d.hourly.map(h => `
          <div class="barrow"><div class="k">${esc(h.hour.slice(11))}</div>
          <div class="t"><i style="width:${100 * h.n / maxh}%"></i></div>
          <div class="v">${num(h.n)}</div></div>`).join('')}</div></div>
    </div>

    ${(d.attributes || []).length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><div><h2>Inside the classes</h2>
        <p>splits a person confirmed — already counted in the classes above</p></div></div>
      <table><thead><tr><th>Split</th><th class="right">Yes</th><th class="right">No</th>
        <th class="right">Not checked</th></tr></thead><tbody>
      ${d.attributes.map(a => `<tr><td>${esc(a.yes_label)}</td>
        <td class="right num">${num(a.yes)}</td><td class="right num">${num(a.no)}</td>
        <td class="right num">${a.unreviewed
          ? `<span class="status warn">${num(a.unreviewed)}</span>` : '0'}</td></tr>`).join('')}
      </tbody></table></div>` : ''}

    <div class="card" style="margin-top:14px"><div class="card-head"><div>
      <h2>Recordings</h2><p>every file that went into this total</p></div></div>
      ${/* An annotated video is how somebody believes the number. Watching a lorry
            cross the line and the count tick is worth more than any accuracy figure, and
            it is what gets sent to a client who disputes a total. Per recording, because
            rendering re-encodes every frame and nobody wants all of them. */''}
      <table><thead><tr><th>File</th><th>Starts</th><th class="right">Vehicles</th>
        <th class="right">PCU</th><th>Notes</th><th>Video</th></tr></thead><tbody>
      ${d.clips.map(c => `<tr>
        <td>${esc(c.name || `clip ${c.video_id}`)}</td>
        <td class="mono">${esc((c.start || '').slice(0, 16))}</td>
        <td class="right num">${num(c.total)}</td>
        <td class="right num">${num(c.pcu)}</td>
        <td>${c.error ? `<span class="status warn">${esc(c.error)}</span>`
          : (c.checks || []).length
            ? `<span class="status warn">${c.checks.length} to look at</span>`
            : '<span class="status ok">fine</span>'}</td>
        ${/* A stale render is worse than none: it is an older answer in the most
              convincing possible format. Offer the remake, and say why. */''}
        <td>${c.annotated
          ? `<a class="btn sm ghost" href="/api/clips/${c.video_id}/annotated.mp4"
               download>${c.annotated_stale ? 'Watch (old)' : '▶ Watch'}</a>
             ${c.annotated_stale ? `<button class="btn sm ghost" data-annot="${c.video_id}"
               title="Made before your corrections">Remake</button>` : ''}`
          : `<button class="btn sm ghost" data-annot="${c.video_id}">Make video</button>`}
        </td></tr>`).join('')}
      </tbody></table></div>
  </div>`;
  wireAnnotate();
}

/* Renders run in the same queue as detection, so "queued" can mean several minutes. The
   button reports what the job is actually doing instead of going quiet. */
function watchRender(vid, btn) {
  const t = setInterval(async () => {
    if (!document.body.contains(btn)) return clearInterval(t);
    let s;
    try { s = await api(`/api/clips/${vid}/render_state`, undefined, 'GET'); }
    catch { return; }
    if (s.job === 'waiting') { btn.textContent = 'Waiting its turn…'; return; }
    if (s.job === 'running') {
      btn.textContent = s.progress ? `Rendering ${Math.round(s.progress)}%` : 'Rendering…';
      return;
    }
    clearInterval(t);
    if (s.job === 'error') {
      btn.disabled = false; btn.textContent = 'Failed — retry';
      return toast(`Video failed: ${s.message || 'no reason recorded'}`, true);
    }
    toast('Video ready'); route();
  }, 4000);
}

/* ─────────────────────────── modal + router ─────────────────────────── */
function modal(title, body, actions, wide) {
  closeModal();
  const w = document.createElement('div');
  w.id = 'modal';
  w.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:80;
    display:grid;place-items:center;padding:20px`;
  w.innerHTML = `<div class="card" style="max-width:${wide ? 1000 : 560}px;width:100%;
      max-height:88vh;overflow:auto">
    <div class="card-body">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
        <h2 style="margin:0;font-size:18px;flex:1">${esc(title)}</h2>
        <button class="btn ghost sm" id="mx">Close</button></div>
      ${body}
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px"
           id="macts"></div>
    </div></div>`;
  document.body.appendChild(w);
  $('#mx').onclick = closeModal;
  w.onclick = e => { if (e.target === w) closeModal(); };
  (actions || []).forEach(a => {
    const b = document.createElement('button');
    b.className = `btn ${a.primary ? 'primary' : 'ghost'}`;
    b.textContent = a.label;
    b.disabled = !!a.disabled;
    b.onclick = a.act;
    $('#macts').appendChild(b);
  });
}
function closeModal() {
  const m = $('#modal');
  if (m) m.remove();
  if (ED && ED.destroy) ED.destroy();   // it holds document-level key and resize handlers
  ED = null;
}

async function route() {
  clearInterval(POLL);
  const [name, a, b] = location.hash.replace(/^#/, '').split('/');
  try {
    if (name === 'settings') return await viewSettings();
    if (name === 'runs') return await viewRuns();
    if (name === 'station' && a) return await viewStation(+a);
    if (name === 'review' && a) return await viewReview(+a, b);
    if (name === 'report' && a) return await viewReport(+a);
    return await viewStations();
  } catch (e) {
    app.innerHTML = `<div class="wrap"><div class="card"><div class="card-body">
      <h2 style="margin:0 0 6px">Something went wrong</h2>
      <p class="muted-sm">${esc(e.message)}</p>
      <a class="btn ghost" href="#stations" style="margin-top:10px">Start again</a>
    </div></div></div>`;
  }
}
window.addEventListener('hashchange', route);
route();
