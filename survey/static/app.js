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

    <p class="muted-sm" style="margin-top:20px">Running on ${esc(dev.name)}.</p>
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
}

/* ─────────────────────────── one station ─────────────────────────── */
async function viewStation(id) {
  const d = await api(`/api/stations/${id}`);
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

    <div id="stepFolder"></div>
    ${p.folder ? `<div id="stepLine"></div>` : ''}
    ${p.folder && p.line ? `<div id="stepHours"></div>` : ''}
    ${p.tracks ? `<div id="stepAfter"></div>` : ''}
  </div>`;

  paintFolder(id, d);
  if (p.folder) paintLine(id, d);
  if (p.folder && p.line) paintHours(id, d);
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
        <div class="muted-sm" style="font-family:ui-monospace,monospace">${esc(p.folder)}</div></div>
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

async function openPicker(id, start) {
  let cur = start || '';
  const draw = async () => {
    let b;
    try { b = await api(`/api/browse?path=${encodeURIComponent(cur)}`, undefined, 'GET'); }
    catch (e) { return toast(e.message, true); }
    cur = b.path;
    modal(`Choose the footage folder`, `
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
             toast(`${r.added.length} recording(s) attached`
                   + (r.skipped.length ? `, ${r.skipped.length} skipped` : ''));
             closeModal(); viewStation(id);
           } catch (e) { toast(e.message, true); }
         } }]);
    document.querySelectorAll('[data-go]').forEach(b2 => {
      b2.onclick = () => { cur = b2.dataset.go; draw(); };
    });
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
        : 'Vehicles are counted when they cross this line. Draw it once.'}</div>
    </div>
    <button class="btn ${has ? 'ghost' : 'primary'}" id="drawLine">
      ${has ? 'Redraw' : 'Draw the line'}</button>
  </div></div>`;
  $('#drawLine').onclick = () => openLine(id, d.line || []);
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
function paintHours(id, d) {
  const el = $('#stepHours');
  const q = d.queue || {};
  const busyId = q.running ? q.running.video_id : null;
  const waiting = new Set((q.waiting || []).map(w => w.video_id));

  el.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="card-body">
    <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:4px">
      <h2 style="margin:0;font-size:17px">Footage by hour</h2>
      <span class="muted-sm" style="flex:1">Pick an hour to detect vehicles in it.</span>
      ${d.remaining_estimate_s ? `<span class="muted-sm">all remaining ≈
        ${mins(d.remaining_estimate_s)}</span>` : ''}
    </div>
    <div id="qbar"></div>
    <div class="hours" style="margin-top:12px">${d.hours.map(h => {
      const state = h.files.some(f => f.video_id === busyId) ? 'busy'
        : h.files.some(f => waiting.has(f.video_id)) ? 'busy' : h.state;
      const pct = h.total ? Math.round(100 * h.extracted / h.total) : 0;
      return `<button class="hr ${state === 'done' ? 'done' : ''}
                ${state === 'busy' ? 'busy' : ''}" data-hour="${esc(h.hour)}"
                ${state === 'done' || state === 'busy' ? 'disabled' : ''}>
        <div class="t">${esc(h.label)}${h.night ? '<span class="night">night</span>' : ''}</div>
        <div class="d">${h.minutes} min filmed${h.coverage < 0.99
          ? ` · ${Math.round(h.coverage * 100)}% of the hour` : ''}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="cta">${state === 'done' ? '✓ done'
          : state === 'busy' ? 'working…'
          : state === 'part' ? `finish (${h.total - h.extracted} left)`
          : `detect · ≈${mins(h.minutes * 60 / (d.device.speed || 1))}`}</div>
      </button>`;
    }).join('')}</div>
  </div></div>`;

  el.querySelectorAll('[data-hour]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {
      await api(`/api/stations/${id}/hours/${encodeURIComponent(b.dataset.hour)}/extract`, {});
      toast('Started — you can leave this running');
      viewStation(id);
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });
  paintQueue(d.queue);
}

function paintQueue(q) {
  const el = $('#qbar');
  if (!el) return;
  const r = q && q.running;
  if (!r && !(q && (q.waiting || []).length)) { el.innerHTML = ''; return; }
  el.innerHTML = `<div style="border:1px solid var(--cc-acc);border-radius:var(--cc-r-md);
      padding:12px;background:var(--cc-acc-bg);margin-top:10px">
    <div style="display:flex;align-items:center;gap:12px">
      <div style="flex:1">
        <b>${r ? esc(r.name) : 'Queued'}</b>
        <div class="muted-sm" id="qsub">${r
          ? `${Math.round(r.progress || 0)}%${r.eta_s ? ` · ${mins(r.eta_s)} left` : ''}`
          : ''}${(q.waiting || []).length ? ` · ${q.waiting.length} more waiting` : ''}</div>
      </div>
      <button class="btn ghost sm" id="qcancel">Stop after this one</button>
    </div>
    <div class="bar" style="height:6px;background:var(--cc-surface);border-radius:3px;
         margin-top:10px;overflow:hidden">
      <i id="qbarfill" style="display:block;height:100%;background:var(--cc-acc);
         width:${Math.round((r && r.progress) || 0)}%"></i></div>
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
  const r = q.running;
  if (!r && !(q.waiting || []).length) { clearInterval(POLL); return viewStation(id); }
  const fill = $('#qbarfill'), sub = $('#qsub');
  if (!fill) return viewStation(id);
  fill.style.width = `${Math.round((r && r.progress) || 0)}%`;
  if (sub) sub.textContent = (r
    ? `${Math.round(r.progress || 0)}%${r.eta_s ? ` · ${mins(r.eta_s)} left` : ''}` : '')
    + ((q.waiting || []).length ? ` · ${q.waiting.length} more waiting` : '');
}

/* ── steps 4 & 5: review and report ── */
function paintAfter(id, d) {
  const p = d.progress;
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
  RID = id; RMODE = mode === 'all' ? 'all' : 'critical';
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
      <div class="seg" role="group">
        <button data-mode="critical" class="${RMODE === 'critical' ? 'on' : ''}">
          Needs a check</button>
        <button data-mode="all" class="${RMODE === 'all' ? 'on' : ''}">Everything</button>
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
      <p class="muted-sm why">${it.mandatory
        ? esc((it.reasons || []).join(' · ')) : ''}</p>
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
      <table><thead><tr><th>File</th><th>Starts</th><th class="right">Vehicles</th>
        <th class="right">PCU</th><th>Notes</th></tr></thead><tbody>
      ${d.clips.map(c => `<tr>
        <td>${esc(c.name || `clip ${c.video_id}`)}</td>
        <td class="mono">${esc((c.start || '').slice(0, 16))}</td>
        <td class="right num">${num(c.total)}</td>
        <td class="right num">${num(c.pcu)}</td>
        <td>${c.error ? `<span class="status warn">${esc(c.error)}</span>`
          : (c.checks || []).length
            ? `<span class="status warn">${c.checks.length} to look at</span>`
            : '<span class="status ok">fine</span>'}</td></tr>`).join('')}
      </tbody></table></div>
  </div>`;
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
