/* TrafficLens Lab — client. Hash-routed, no build step, ES module.

   Navigation is deliberately three working destinations: Overview, Footage, Runs.
   Review, segments, judgments and costs all belong to a run, so they live inside one
   rather than competing for the sidebar. An earlier version put nine entries there and
   scattered stage buttons across the run page, where a single click started real work
   and real spending — the pipeline editor replaces those with one explicit Run. */
const $ = (s, r = document) => r.querySelector(s);
const api = async (p, opt) => {
  const r = await fetch(p, opt);
  const t = await r.text();
  let d; try { d = t ? JSON.parse(t) : {}; } catch { d = { detail: t }; }
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
};
const post = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const put = (p, body) => api(p, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
/* Every money figure in this UI is rupees. `usd()` keeps its name because that is what
   the value IS — the providers bill in dollars and the ledger stores dollars — but what a
   person reads is the currency they think in. Converting at the formatter rather than at
   28 call sites means no screen can quietly stay in dollars. */
const usd = v => inr(v);
const dollars = v => '$' + (Number(v || 0)).toFixed(Math.abs(v) < 1 ? 4 : 2);
const num = v => Number(v || 0).toLocaleString('en-IN');

/* Money is shown in rupees, because that is the currency the business is run in and a
   figure you have to convert in your head is a figure you do not check.

   The providers bill in dollars, so the dollar amount is what is stored — converting on
   write would freeze a rate into the ledger and make historical costs drift as the rate
   moves. Conversion happens here, at the moment of display, against a rate you set in
   Settings; the ledger stays in the currency it was actually charged in. */
let FX = { rate: 88, updated: null };
const inr = usd => {
  const v = Number(usd || 0) * FX.rate;
  if (!v) return '₹0';
  if (v < 1) return '₹' + v.toFixed(2);
  if (v < 1000) return '₹' + v.toFixed(v < 100 ? 1 : 0);
  if (v < 100000) return '₹' + (v / 1000).toFixed(1) + 'k';
  return '₹' + (v / 100000).toFixed(2) + ' lakh';        // Indian numbering, not millions
};
const inrFull = usd => '₹' + (Number(usd || 0) * FX.rate).toLocaleString('en-IN',
  { maximumFractionDigits: 2 });
const ago = ts => { if (!ts) return '—'; const s = Date.now() / 1000 - ts;
  if (s < 60) return Math.round(s) + 's ago'; if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago'; return Math.round(s / 86400) + 'd ago'; };
/** "6m 20s left" reads as an answer; "3.76x realtime" is arithmetic homework. */
const etaText = s => { s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`; };
const dur = s => { s = Number(s || 0); const m = Math.floor(s / 60); return m ? `${m}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`; };

let TOAST_T;
function toast(msg, bad) {
  const el = $('#toast'); el.innerHTML = esc(msg); el.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(TOAST_T); TOAST_T = setTimeout(() => el.className = 'toast', bad ? 7000 : 3500);
}
const ic = {
  overview: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  videos: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="5" width="14" height="14" rx="2"/><path d="m16 12 6-3.5v11L16 16z"/></svg>',
  runs: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 6h16M4 12h10M4 18h6"/></svg>',
  judges: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 3v18M5 8l7-5 7 5"/><path d="M3 14h6l-3-6zM15 14h6l-3-6z"/></svg>',
  review: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="m9 12 2 2 4-4"/><rect x="3" y="4" width="18" height="16" rx="2"/></svg>',
  training: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" stroke-linecap="round"/></svg>',
  costs: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
  stations: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>',
  logs: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 6h10M4 12h16M4 18h7"/><circle cx="18" cy="6" r="2"/><circle cx="14" cy="18" r="2"/></svg>',
  counts: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 17h18"/><path d="M6 17V9M11 17V5M16 17v-6M21 17v-9"/></svg>',
  datasets: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
  settings: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 8.9 19a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 5 8.9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9.5a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
};
/* Three working destinations, then reference, then setup. A run's own screens
   (review, segments, judgments, cost) are reached from the run, not from here. */
/* Footage, runs and counts are a STATION's footage, runs and counts — they live inside
   one, not beside it. Their routes still resolve so links and bookmarks keep working;
   they are simply no longer top-level destinations. */
const NAV = [
  { g: 1, id: 'overview', label: 'Overview' },
  { g: 1, id: 'stations', label: 'Stations' },
  { g: 2, id: 'datasets', label: 'Datasets' },
  { g: 2, id: 'training', label: 'Training' },
  { g: 3, id: 'judges', label: 'Judges' },
  { g: 3, id: 'logs', label: 'Logs' },
  { g: 3, id: 'settings', label: 'Settings' },
];

/* ─────────────────────────── shell ─────────────────────────── */
function buildNav() {
  for (const g of [1, 2, 3]) {
    $('#nav' + g).innerHTML = NAV.filter(n => n.g === g).map(n =>
      `<button class="item" data-route="${n.id}" title="${n.label}">${ic[n.id]}<span>${n.label}</span></button>`).join('');
  }
  document.querySelectorAll('[data-route]').forEach(b => b.onclick = () => {
    // `data-arg` lets a back button return to a specific place — "← Station 4" has to
    // land on that station's clips, not on the stations list.
    location.hash = '#' + b.dataset.route + (b.dataset.arg ? '/' + b.dataset.arg : '');
    if ($('#app').dataset.rail === '1') $('#app').dataset.rail = '0';   // picking in the rail expands it
  });
}
$('#rail').onclick = () => {
  const a = $('#app'), on = a.dataset.rail === '1';
  a.dataset.rail = on ? '0' : '1';
  $('#rail').setAttribute('aria-expanded', on ? 'true' : 'false');
  $('#rail').setAttribute('aria-label', on ? 'Collapse sidebar' : 'Expand sidebar');
  localStorage.rail = a.dataset.rail;
};
$('#theme').onclick = () => {
  const d = document.documentElement.dataset.theme === 'dark';
  document.documentElement.dataset.theme = d ? 'light' : 'dark';
  localStorage.theme = document.documentElement.dataset.theme;
};
if (localStorage.theme) document.documentElement.dataset.theme = localStorage.theme;
if (localStorage.rail) $('#app').dataset.rail = localStorage.rail;

/* A run's children (pipeline, review) keep Runs highlighted — otherwise the sidebar
   goes blank exactly when you most need to know where you are. */
const PARENT = {
  station: 'stations', gold: 'stations', goldreview: 'stations', errors: 'stations',
  videos: 'stations', runs: 'stations', counts: 'stations',
  review: 'stations', pipeline: 'stations',
  scene: 'stations', reportcard: 'stations', preview: 'stations', implied: 'stations',
  axles: 'judges', attrs: 'judges', attr: 'judges', verify: 'stations',
  trainingrun: 'training', costs: 'overview', logs: 'logs',
};
function markNav(route) {
  const lit = PARENT[route] || route;
  document.querySelectorAll('[data-route]').forEach(b =>
    b.setAttribute('aria-current', b.dataset.route === lit ? 'page' : 'false'));
}
async function refreshPills() {
  try {
    const s = await api('/api/state');
    const o = s.openrouter || {}, r = s.runpod || {};
    if (s.fx && s.fx.rate) FX.rate = s.fx.rate;   // server owns the rate, not the page
    $('#orbal').textContent = o.ok ? usd(o.remaining) : 'no key';
    $('#orpill').querySelector('.dot').className = 'dot' + (!o.ok ? ' idle' : o.remaining < 0.5 ? ' bad' : o.remaining < 2 ? ' warn' : '');
    $('#rpbal').textContent = r.ok ? usd(r.remaining) : 'no key';
    $('#rppill').querySelector('.dot').className = 'dot' + (!r.ok ? ' idle' : r.remaining < 2 ? ' bad' : r.remaining < 5 ? ' warn' : '');
    return s;
  } catch (e) { return null; }
}

/* ─────────────────────────── views ─────────────────────────── */
const stageRow = s => `<div class="stage" data-s="${s.status}">
  <div class="nm">${esc(s.stage)}</div>
  <div class="bar"><i style="width:${s.status === 'done' ? 100 : (s.progress || 0)}%"></i></div>
  <div class="msg" title="${esc(s.message || '')}">${s.status === 'running' ? '<span class="spin"></span> ' : ''}${esc(s.message || s.status)}</div>
  <div class="cost">${s.cost_usd ? usd(s.cost_usd) : ''}</div></div>`;

const statusPill = st => {
  const m = { done: 'ok', running: 'run', error: 'bad', queued: 'idle', draft: 'idle', idle: 'idle', pending: 'idle' };
  return `<span class="status ${m[st] || 'idle'}">${esc(st)}</span>`;
};

/* Overview: what is true right now, and the stations to work in.

   A station is the project — the run-centric framing this page used to have was wrong,
   so the run buttons and the recent-runs list are gone. Activity moved to its own Logs
   tab: it is reference material, not a landing page. */
async function viewOverview() {
  const [s, { stations }] = await Promise.all([
    api('/api/state'), api('/api/stations').catch(() => ({ stations: [] }))]);
  const or = s.openrouter || {}, rp = s.runpod || {};
  const withFootage = stations.filter(x => (x.footage || {}).files);

  return `
  <div class="page">
    <div class="page-head"><div>
      <h1>Lab</h1>
      <p>Footage in, fine-tuned model out. Work happens inside a station — open one to
         see its footage, datasets and models.</p>
    </div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico ok">${ic.costs}</div><div>
        <div class="lbl">OpenRouter credit</div><div class="val">${or.ok ? usd(or.remaining) : '—'}</div>
        <div class="sub">${or.ok ? dollars(or.remaining) + ' — topped up in USD' : 'no key'}</div>
        <div class="sub">${or.ok ? usd(or.used) + ' used of ' + usd(or.total) : 'key not set'}</div></div></div>
      <div class="tile"><div class="ico acc">${ic.training}</div><div>
        <div class="lbl">RunPod balance</div><div class="val">${rp.ok ? usd(rp.remaining) : '—'}</div>
        <div class="sub">${rp.ok ? dollars(rp.remaining) + ' — topped up in USD' : 'no key'}</div>
        <div class="sub">${s.live_pods.length} pod(s) live</div></div></div>
      <button class="tile" data-route="costs" style="text-align:left;cursor:pointer"
        title="every charge, itemised">
        <div class="ico info">${ic.costs}</div><div>
        <div class="lbl">Lab spend (all time)</div><div class="val">${usd(s.spend_total)}</div>
        <div class="sub">${usd(s.spend_24h)} in last 24h · ${dollars(s.spend_total)} at ₹${FX.rate}/$</div></div></button>
      <button class="tile" data-route="stations" style="text-align:left;cursor:pointer">
        <div class="ico warn">${ic.stations}</div><div>
        <div class="lbl">Stations</div><div class="val">${stations.length}</div>
        <div class="sub">${withFootage.length} with footage →</div></div></button>
    </div>

    ${s.live_pods.length ? `<div class="card"><div class="card-head"><h2>Live GPU</h2>
      <button class="btn sm secondary" data-route="training">Open training</button></div>
      ${s.live_pods.map(p => `<div class="stage"><div class="nm">${esc(p.gpu || '?')}</div>
        <div class="bar"><i style="width:${(p.telemetry || {}).gpu_util || 0}%"></i></div>
        <div class="msg">${(p.telemetry || {}).gpu_util ?? '—'}% GPU · ${esc(p.status)}</div>
        <div class="cost">${usd(p.cost_usd)}</div></div>`).join('')}</div>` : ''}

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><div>
      <h2>Stations</h2><p>each one is a project — its own footage, datasets and models</p></div>
      <button class="btn sm secondary" data-route="stations">Manage</button></div></div>
      <table><thead><tr><th>Station</th><th class="right">Footage</th><th class="right">Days</th>
        <th class="right">Datasets</th><th class="right">Models</th><th></th></tr></thead>
      <tbody>${stations.map(x => {
        const f = x.footage || {};
        return `<tr><td class="id">${esc(x.code)} · ${esc(x.name)}
            <div style="font-size:12px;color:var(--cc-fg-3)">${esc(x.road_name || '')}</div></td>
          <td class="right num">${f.hours ? f.hours + ' h' : '—'}</td>
          <td class="right num">${f.days || 0}</td>
          <td class="right num">${x.datasets || 0}</td>
          <td class="right num">${(x.models || []).length || '—'}</td>
          <td class="right"><button class="btn sm secondary" data-station="${x.id}">Open</button></td>
        </tr>`; }).join('')
        || '<tr><td colspan="6" class="empty">No stations yet — create one to begin.</td></tr>'}
      </tbody></table></div>
  </div>`;
}

/* Logs: its own destination, because an activity feed is something you go and look at
   when you want to know what happened, not something a landing page should spend
   its space on. */
async function viewLogs() {
  const s = await api('/api/state');
  const ev = s.events || [];
  return `<div class="page">
    <div class="page-head"><div><h1>Logs</h1>
      <p>Everything the Lab has done, newest first — runs started, judgments made,
         sessions assigned, files deleted, money spent.</p></div>
      <div class="head-actions"><button class="btn secondary" data-route="costs">Spend ledger</button></div></div>
    <div class="card"><div class="feed">${ev.length ? ev.map(e => `<div class="event">
      <div class="txt"><b>${esc(e.verb)}</b> <span>${esc(e.object)}</span>
        ${e.detail ? `<div style="color:var(--cc-fg-3);font-size:12px">${esc(e.detail)}</div>` : ''}
        ${e.run_id ? `<div style="font-size:11px;color:var(--cc-fg-off)">run ${e.run_id}</div>` : ''}</div>
      <time>${ago(e.ts)}</time></div>`).join('') : empty('Nothing logged yet',
        'Actions appear here as you work.')}</div></div>
  </div>`;
}

/* The geocode hit is a town centre; the survey needs the camera's exact spot and the
   direction it looks. The map is where both get fixed: drag the pin to the roadside,
   drag the outer handle along the carriageway the camera faces. */
async function openStationMap(el, geo, onChange) {
  if (!el) return;
  el.hidden = false;
  const brg = document.getElementById('st_brg');
  if (brg) brg.hidden = false;
  const { createMapPicker } = await import('/shared/mappicker.js');
  el.innerHTML = '';
  await createMapPicker(el, {
    lat: geo.lat, lon: geo.lon, bearing: geo.bearing ?? 90,
    onChange: g => onChange(g),
  });
  // Leaflet measures its container at mount; a card revealed in the same frame measures
  // as zero and no tiles are ever requested. One resize after layout settles it.
  setTimeout(() => window.dispatchEvent(new Event('resize')), 150);
}

async function viewStations() {
  const [{ stations }, { sessions }] = await Promise.all([
    api('/api/stations'), api('/api/sessions').catch(() => ({ sessions: [] }))]);
  const unattached = sessions.filter(x => !x.station).reduce((a, x) => a + x.files, 0);
  STATION_DATA = stations;
  const withF = stations.filter(s => (s.footage || {}).files);

  return `<div class="page">
    <div class="page-head"><div><h1>Stations</h1>
      <p>Each station is a project — its own footage, datasets and models. Footage is
         attached to a station by hand, because a DVR channel number identifies nothing.</p></div>
      <div class="head-actions">
        <button class="btn secondary" data-route="counts">All counts &amp; renders</button>
        <button class="btn secondary" id="scanfootage">Scan footage folders</button>
        <button class="btn primary" id="newstation">+ New station</button></div></div>

    <div class="card" id="stationform" hidden>
      <div class="card-head"><div><h2>New count station</h2>
        <p>Shared with the survey app — a station made here is the same station there.
           Only the name is required; the rest can be filled in later.</p></div></div>
      <div class="grid g2" style="gap:16px">
        <div><label class="lbl">Name <b style="color:var(--cc-bad)">*</b></label>
          <input class="field" id="st_name" placeholder="e.g. FID33 PK5"></div>
        <div><label class="lbl">Code</label>
          <input class="field" id="st_code" placeholder="left blank = generated, e.g. FID-01"></div>
        <div><label class="lbl">Road name</label>
          <input class="field" id="st_road" placeholder="e.g. SH-14 Bidar – Bhalki"></div>
        <div><label class="lbl">Chainage</label>
          <input class="field" id="st_chain" placeholder="e.g. Km 12+400"></div>
        <div><label class="lbl">District</label><input class="field" id="st_dist"></div>
        <div><label class="lbl">State</label><input class="field" id="st_state"></div>
        <div style="grid-column:1/-1"><label class="lbl">Location</label>
          <input class="field" id="st_geo" placeholder="type a place and pick a result — fills lat/lon">
          <div id="st_geo_out" class="geo-results"></div>
          <div class="muted" id="st_geo_pick" style="margin-top:4px">no location set</div>
          <div id="st_map" class="st-map" hidden></div>
          <div class="muted" id="st_brg" hidden>drag the outer handle to point it the way
            the camera looks — bearing is saved with the station</div></div>
        <div><label class="lbl">Camera id</label>
          <input class="field" id="st_cam" placeholder="e.g. ch01 — a hint only"></div>
        <div><label class="lbl">Carriageway</label>
          <input class="field" id="st_cw" placeholder="e.g. two-lane undivided"></div>
      </div>
      <div style="margin-top:20px;display:flex;gap:8px">
        <button class="btn primary" id="savestation">Create station</button>
        <button class="btn secondary" id="cancelstation">Cancel</button>
      </div>
    </div>

    <div class="grid g4">
      <div class="tile"><div class="ico acc">${ic.stations}</div><div><div class="lbl">Stations</div>
        <div class="val">${stations.length}</div><div class="sub">${withF.length} with footage</div></div></div>
      <div class="tile"><div class="ico info">${ic.videos}</div><div><div class="lbl">Footage held</div>
        <div class="val">${stations.reduce((a, s) => a + ((s.footage || {}).hours || 0), 0).toFixed(1)} h</div>
        <div class="sub">duplicates excluded</div></div></div>
      <div class="tile"><div class="ico ok">${ic.datasets}</div><div><div class="lbl">Station datasets</div>
        <div class="val">${stations.reduce((a, s) => a + (s.datasets || 0), 0)}</div></div></div>
      <button class="tile" data-route="videos" style="text-align:left;cursor:pointer"
        title="every video file on disk, attached or not">
        <div class="ico ${unattached ? 'warn' : 'ok'}">${ic.videos}</div>
        <div><div class="lbl">Footage not attached</div>
        <div class="val">${unattached}</div>
        <div class="sub">${unattached ? 'file(s) excluded from every station total →'
                                      : 'every file has a station · browse all →'}</div></div></button>
    </div>

    <div class="card tight"><div id="stationlist"></div></div>

  </div>`;
}
let STATION_DATA = [];

/** Search, filter, sort and table/grid for the station list. */
let STATION_LIST = null;
async function mountStationList() {
  const el = $('#stationlist');
  if (STATION_LIST) { STATION_LIST.destroy(); STATION_LIST = null; }
  if (!el) return;
  const { mountListView } = await import('/shared/listview.js');
  const hrs = s => (s.footage || {}).hours || 0;
  const lv = mountListView(el, {
    items: STATION_DATA,
    storageKey: 'stations',
    searchPlaceholder: 'Search station, code, road, district…',
    searchText: s => [s.code, s.name, s.road_name, s.district, s.state, s.camera_id]
      .filter(Boolean).join(' '),
    filters: [
      { key: 'footage', label: 'Has footage', test: s => hrs(s) > 0 },
      { key: 'nofootage', label: 'No footage', test: s => !hrs(s) },
      { key: 'models', label: 'Has a station model', test: s => (s.models || []).length > 0 },
      { key: 'datasets', label: 'Has datasets', test: s => (s.datasets || 0) > 0 },
      { key: 'geo', label: 'Located on the map', test: s => s.lat != null && s.lon != null },
    ],
    sorts: [
      { label: 'Code', get: s => s.code || '' },
      { label: 'Name', get: s => s.name || '' },
      { label: 'Footage (hours)', get: s => hrs(s) },
      { label: 'Days covered', get: s => (s.footage || {}).days || 0 },
      { label: 'Datasets', get: s => s.datasets || 0 },
      { label: 'Models', get: s => (s.models || []).length },
      { label: 'Progress', get: s => (s.progress || {}).done_count || 0 },
    ],
    columns: [
      { label: 'Station', sortIndex: 0, get: s => `
          <div style="display:flex;align-items:center;gap:10px">
            ${(s.footage || {}).files
              ? `<img src="/api/stations/${s.id}/thumb" alt="" loading="lazy"
                   style="width:56px;height:32px;object-fit:cover;border-radius:4px;flex:0 0 56px">`
              : '<span style="width:56px;height:32px;border-radius:4px;background:var(--cc-hover);flex:0 0 56px"></span>'}
            <div><span class="id">${esc(s.code)} · ${esc(s.name)}</span>
              <div style="font-size:12px;color:var(--cc-fg-3)">${esc(s.road_name || '—')}</div></div>
          </div>` },
      { label: 'Footage', right: true, sortIndex: 2,
        get: s => (s.footage || {}).hours
          ? `${(s.footage || {}).hours} h<div style="font-size:11px;color:var(--cc-fg-3)">${
              (s.footage || {}).days} day(s)</div>`
          : '<span style="color:var(--cc-fg-off)">—</span>' },
      { label: 'Progress', sortIndex: 6, get: s => {
          const p = s.progress || { stages: [], done_count: 0, total: 6 };
          const nextIdx = p.stages.findIndex(x => !x.done);
          return `<div style="min-width:120px">
            <div class="st-track" style="margin-bottom:4px">${p.stages.map((x, i) =>
              `<i class="${x.done ? 'on' : i === nextIdx ? 'next' : ''}" title="${esc(x.label)} — ${esc(x.detail)}"></i>`).join('')}</div>
            <span style="font-size:11px;color:var(--cc-fg-3)">${p.done_count} of ${p.total}</span></div>`;
        } },
      { label: 'Next step', get: s => `<span style="font-size:12px">${esc((s.progress || {}).next_label || '—')}</span>` },
      { label: 'Models', right: true, sortIndex: 5,
        get: s => (s.models || []).length
          ? `<span class="status ok">${s.models.length}</span>`
          : '<span class="status idle">global</span>' },
      { label: '', right: true, get: s => `
          <button class="btn sm ghost" data-gold="${s.id}">Gold</button>
          <button class="btn sm secondary" data-station="${s.id}">Open</button>` },
    ],
    card: s => {
      const p = s.progress || { stages: [], done_count: 0, total: 6 };
      const nextIdx = p.stages.findIndex(x => !x.done);
      return `<div class="st-card">
        <div class="st-thumb">
          ${(s.footage || {}).files
            ? `<img src="/api/stations/${s.id}/thumb" alt="a frame from ${esc(s.name)}" loading="lazy">`
            : '<div class="none">no footage yet</div>'}
          <span class="code">${esc(s.code)}</span>
        </div>
        <div class="st-body">
          <div><h3>${esc(s.name)}</h3>
            <div class="where">${esc(s.road_name || 'road not recorded')}${
              s.chainage ? ' · ' + esc(s.chainage) : ''}</div></div>

          <div class="st-stats">
            <span><b>${(s.footage || {}).hours || 0}</b>hours</span>
            <span><b>${(s.footage || {}).days || 0}</b>days</span>
            <span><b>${s.datasets || 0}</b>datasets</span>
            <span><b>${(s.models || []).length || '—'}</b>models</span>
          </div>

          <div>
            <div class="st-prog"><b>Progress</b><span>${p.done_count} of ${p.total} steps</span></div>
            <div class="st-track">${p.stages.map((x, i) =>
              `<i class="${x.done ? 'on' : i === nextIdx ? 'next' : ''}"
                  title="${esc(x.label)} — ${esc(x.detail)}"></i>`).join('')}</div>
          </div>

          <div class="st-next${p.next ? '' : ' done'}">
            ${p.next
              ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>'
              : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m5 13 4 4L19 7"/></svg>'}
            <span>Next: <b style="font-weight:500;color:var(--cc-fg)">${esc(p.next_label)}</b></span>
          </div>

          <div class="st-actions">
            <button class="btn sm secondary" data-gold="${s.id}">Gold set</button>
            <button class="btn sm primary" data-station="${s.id}" style="flex:1">Open station</button>
          </div>
        </div></div>`;
    },
    emptyHead: 'No stations yet',
    emptyBody: 'A station is the project — create one, then attach its footage.',
    emptyAction: '<button class="btn primary" id="newstation2" style="margin-top:12px">+ New station</button>',
    onRender: () => wire('stations'),
  });
  STATION_LIST = lv;
}

/** Minutes of footage per hour. Colour is magnitude only — the labels carry identity. */
function coverageGrid(cov) {
  if (!cov || !cov.days || !cov.days.length)
    return `<div class="empty">No footage with a readable start time yet.</div>`;
  const cell = m => {
    if (!m) return `<td class="cv cv0" title="no footage"></td>`;
    const pct = Math.min(100, Math.round(m / 60 * 100));
    const lvl = pct >= 92 ? 3 : pct >= 50 ? 2 : 1;
    return `<td class="cv cv${lvl}" title="${Math.round(m)} min">${lvl === 3 ? '' : Math.round(m)}</td>`;
  };
  return `<div style="overflow-x:auto"><table class="cvgrid">
    <thead><tr><th></th>${Array.from({ length: 24 }, (_, h) =>
      `<th>${String(h).padStart(2, '0')}</th>`).join('')}</tr></thead>
    <tbody>${cov.days.map(d => `<tr><th>${esc(d)}</th>${
      Array.from({ length: 24 }, (_, h) => cell((cov.grid[d] || {})[h] || 0)).join('')}</tr>`).join('')}
    </tbody></table></div>
    <div class="cvkey"><span><i class="cv3"></i>full hour</span><span><i class="cv2"></i>partial</span>
      <span><i class="cv1"></i>sparse</span><span><i class="cv0"></i>none</span></div>`;
}

/* ═══════════════════════ station workspace ═══════════════════════
   A station is the project, so this is where the work lives: its footage, its runs, its
   labels, its datasets, its models. Sub-tabs rather than one long scroll, and a stage
   rail across the top so "where is this station and what does it need next" is answered
   before you click anything. */
/* Footage, Runs and Counts were three tabs describing the same eight clips from three
   angles, and none of them answered "what state is this survey in". They are one tab now.
   A run is how a clip got extracted — provenance, shown at the bottom of Clips, not a
   place to navigate to. */
/* No 'line' tab: the editor lives on the clip whose count it decides. The station
   DEFAULT line survives — it is chosen there via "Save to: whole station" — and its
   status is shown on Overview. */
const WS_TABS = ['overview', 'clips', 'labels', 'datasets', 'models'];

async function viewStation(id, tab) {
  const s = await api('/api/stations/' + id);
  WS = { id, s, tab: WS_TABS.includes(tab) ? tab : 'overview' };
  const p = s.progress || { stages: [], done_count: 0, total: 6 };
  const cov = s.coverage || {};
  const live = (s.footage || []).filter(f => !f.dup_of);
  const counts = {
    clips: (s.videos || []).length, datasets: (s.datasets || []).length,
    line: (s.default_line || []).length,
    models: (s.models || []).length,
    labels: (s.gold || {}).verdicts || 0,
  };
  const nextIdx = p.stages.findIndex(x => !x.done);

  return `<div class="page" style="gap:16px">
    <div class="ws-head">
      <div class="ws-shot">${live.length
        ? `<img src="/api/stations/${id}/thumb" alt="a frame from ${esc(s.name)}">`
        : '<div class="none">no footage yet</div>'}</div>
      <div class="ws-id">
        <h1>${esc(s.code)} · ${esc(s.name)}</h1>
        <div class="ws-meta">
          <span>${esc(s.road_name || 'road not recorded')}</span>
          ${s.chainage ? `<span><b>${esc(s.chainage)}</b></span>` : ''}
          <span>${esc([s.district, s.state].filter(Boolean).join(', ') || '—')}</span>
          ${s.camera_id ? `<span>camera <b>${esc(s.camera_id)}</b></span>` : ''}
        </div>
        <div class="ws-meta" style="margin-top:10px">
          <span><b>${cov.total_hours || 0} h</b> footage</span>
          <span><b>${(cov.days || []).length}</b> day(s)</span>
          <span><b>${cov.hours_covered || 0}</b> full hours</span>
          <span><b>${p.done_count} of ${p.total}</b> steps done</span>
        </div>
      </div>
      ${/* The count line lives here, not buried in the folder card. It is the one thing
            that blocks everything downstream — no line, no count — so it belongs where a
            station's actions actually are, and it is always present so the header does
            not change shape between stations. Error sources is a diagnostic, not the main
            action, so it stops being the primary button. */''}
      <div class="head-actions">
        <button class="btn secondary" data-route="stations">All stations</button>
        <button class="btn secondary" id="editStation">Edit station</button>
        <button class="btn secondary" data-errors="${id}">Error sources</button>
        <button class="btn ${(s.default_line || []).length ? 'secondary' : 'primary'}"
          id="stationLine" ${live.length ? '' : 'disabled title="attach footage first — the line is drawn on a frame from it"'}>
          ${(s.default_line || []).length ? 'Edit count line' : 'Draw count line'}</button>
      </div>
    </div>

    <div class="ws-rail">${p.stages.map((x, i) => `
      <div class="ws-step ${x.done ? 'done' : i === nextIdx ? 'now' : ''}" title="${esc(x.detail)}">
        <div class="n"><span class="tick">${x.done ? '✓' : i + 1}</span>${esc(x.label)}</div>
        <div class="d">${esc(x.detail)}</div>
      </div>`).join('')}</div>

    <div class="ws-tabs">${WS_TABS.map(t => `
      <button data-wstab="${t}" class="${t === WS.tab ? 'on' : ''}">${
        t[0].toUpperCase() + t.slice(1)}${counts[t] != null
          ? `<span class="n">${counts[t]}</span>` : ''}</button>`).join('')}</div>

    <div id="wsbody">${wsBody(WS.tab, s, id)}</div>
  </div>`;
}
let WS = null;

/* The gold set, as it actually exists.

   It used to be 60 frames labelled exhaustively by hand, of which 3 were ever reviewed —
   a good idea that never reached the scale to be useful. Clip verification has since
   produced 1,608 verdicts on vehicles that were actually counted, each with an image and
   a trail back to the number it produced. That is the better gold set on every axis, so
   this tab reads it instead of maintaining a parallel collection.

   The distinction the screen leads with is confirmation versus correction. A confirmation
   says the model was already right and teaches it nothing; a correction is a measured
   failure with the picture that caused it, and is the only part worth retraining on. */
/* Per-clip verification rolled up for the Overview: one row per clip, shared records
   with the verify screens — status, never a second judgement of the same vehicle. */
async function mountOvGold(siteId) {
  const el = document.getElementById('ovGold');
  if (!el) return;
  const [t, g] = await Promise.all([
    api(`/api/stations/${siteId}/footage-tree`).catch(() => ({})),
    api(`/api/stations/${siteId}/gold`).catch(() => ({})),
  ]);
  const clips = (t.footage || []).flatMap(f => f.clips || []);
  if (!clips.length) { el.innerHTML = empty('No clips yet', 'Segment footage first — verification follows extraction.'); return; }
  el.innerHTML = `<table><thead><tr><th>Clip</th><th class="right">Counted</th>
      <th class="right">Verified</th><th>Status</th></tr></thead><tbody>
    ${clips.map(c => { const V = c.verify || {}; return `<tr>
      <td class="mono">${esc(c.clock)}–${esc(c.end_clock || '?')}</td>
      <td class="right num">${c.counted != null ? num(c.counted) : '—'}</td>
      <td class="right num">${num(V.verified || 0)}</td>
      <td>${!c.tracks ? '<span class="chip">not extracted</span>'
           : V.verified ? '<span class="chip ok">verified</span>'
           : '<span class="chip warn">awaiting review</span>'}</td></tr>`; }).join('')}
    </tbody></table>
    ${g.verdicts ? `<p style="margin:10px 0 0;font-size:12px;color:var(--cc-fg-3)">
      ${num(g.verdicts)} verdicts · model right ${g.model_accuracy != null
        ? Math.round(100 * g.model_accuracy) + '%' : '—'} ·
      ${num(g.corrections)} corrections feed the next training</p>` : ''}`;
}

async function mountGold(siteId) {
  const el = document.getElementById('goldBody');
  if (!el) return;
  const g = await api(`/api/stations/${siteId}/gold`).catch(e => ({ error: String(e) }));
  if (g.error) { el.innerHTML = `<div class="card">${empty('No gold set yet', g.error)}</div>`; return; }
  const acc = g.model_accuracy != null ? Math.round(g.model_accuracy * 100) : null;
  const maxc = Math.max(1, ...(g.by_class || []).map(c => c.n));

  el.innerHTML = `
    <div class="grid g4" style="margin-bottom:14px">
      <div class="tile"><div class="ico ok">${ic.judges}</div><div>
        <div class="lbl">Verdicts by you</div><div class="val">${num(g.verdicts)}</div>
        <div class="sub">on vehicles the report counts</div></div></div>
      <div class="tile"><div class="ico ${acc >= 95 ? 'ok' : acc >= 85 ? 'warn' : 'acc'}">${ic.training}</div><div>
        <div class="lbl">Model was right</div><div class="val">${acc == null ? '—' : acc + '%'}</div>
        <div class="sub">measured on ${num(g.scored)} judged vehicles</div></div></div>
      <div class="tile"><div class="ico acc">${ic.review}</div><div>
        <div class="lbl">Corrections</div><div class="val">${num(g.corrections)}</div>
        <div class="sub">the only part worth retraining on</div></div></div>
      <div class="tile"><div class="ico info">${ic.overview}</div><div>
        <div class="lbl">Attributes · rejects</div>
        <div class="val">${num(g.attributes)} · ${num(g.rejects)}</div>
        <div class="sub">taxi / APSRTC / maxi · not-a-vehicle</div></div></div>
    </div>

    <div class="grid g2">
      <div class="card"><div class="card-head"><h2>What the model gets wrong</h2>
        <span class="muted">every correction, most common first</span></div>
        ${(g.confusions || []).length ? `<table><thead><tr><th>Model said</th><th>Actually</th>
          <th class="right">Times</th></tr></thead><tbody>
          ${(g.confusions || []).map(c => `<tr><td>${esc(c.said)}</td>
            <td><b>${esc(c.actually)}</b></td>
            <td class="right num">${c.n}</td></tr>`).join('')}</tbody></table>`
        : '<div class="empty">No corrections — the model agreed with you every time.</div>'}
      </div>

      <div class="card"><div class="card-head"><h2>Gold labels by class</h2>
        <span class="muted">what a person confirmed this station contains</span></div>
        <div class="bars" style="padding:0 20px 18px">${(g.by_class || []).map(c => `
          <div class="barrow"><div class="k">${esc(c.class)}</div>
          <div class="t"><i style="width:${100 * c.n / maxc}%"></i></div>
          <div class="v">${num(c.n)}</div></div>`).join('')
          || '<div class="empty">Nothing verified yet.</div>'}</div></div>
    </div>

    ${/* Per-class performance. One accuracy number is not enough to act on: 90% overall
          can mean everything is fine or that one class is broken and the common classes
          are carrying the average. And a class fails in two independent directions --
          over-called (it says LCV and it is not) and missed (it IS an LCV and the model
          said something else) -- which inflate and deflate the same proforma column and
          need opposite fixes. Both are shown, with the confusion behind each. */''}
    ${(g.performance || []).length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><div><h2>How the model performs, class by class</h2>
        <p>the overall ${acc == null ? '—' : acc + '%'} split into the two ways a class
           goes wrong — over-called inflates its column, missed deflates it</p></div></div>
      <table class="perf"><thead><tr>
        <th>Class</th>
        <th class="right">Really is</th>
        <th class="right">Model got</th>
        <th>Correct when it says this</th>
        <th>Found of what is really there</th>
        <th>Where the errors go</th>
      </tr></thead><tbody>
      ${g.performance.map(p => {
        const pc = p.precision, rc = p.recall;
        const bad = v => v == null ? 'muted' : v >= 0.95 ? 'ok' : v >= 0.8 ? 'warn' : 'bad';
        const bar = (v, cls) => v == null
          ? '<span class="muted">—</span>'
          : `<div class="perfbar ${cls}"><span class="t"><i
             style="width:${Math.round(100 * v)}%"></i></span
             ><b>${Math.round(100 * v)}%</b></div>`;
        const notes = [];
        if (p.over) notes.push(`<span class="chip warn">${p.over} not really
          ${esc(p.class)}${p.mostly_really ? ` — mostly ${esc(p.mostly_really)}` : ''}</span>`);
        if (p.missed) notes.push(`<span class="chip">${p.missed} missed${
          p.mistaken_for ? ` — called ${esc(p.mistaken_for)}` : ''}</span>`);
        return `<tr>
          <td class="id"><strong>${esc(p.class)}</strong></td>
          <td class="right num">${num(p.actual)}</td>
          <td class="right num">${num(p.kept)}</td>
          <td>${bar(pc, bad(pc))}</td>
          <td>${bar(rc, bad(rc))}</td>
          <td>${notes.join(' ') || '<span class="chip ok">clean</span>'}</td></tr>`;
      }).join('')}</tbody></table>
      <p class="muted" style="margin:14px 0 0">
        Measured only on the ${num(g.scored)} vehicles a person judged, so both
        percentages have real denominators. A class can be fine on one and bad on the
        other: over-calling needs more negative examples, missing needs more positive
        ones.</p>
    </div>` : ''}

    <div class="card" style="margin-top:14px"><div class="card-body muted">
      Gold here means a person looked at the vehicle and said what it is. Every verdict is
      tied to the crossing it produced, so a label can be traced to the number it changed —
      and the ${num(g.corrections)} corrections are the training set this station actually
      needs: not a random sample, but the specific vehicles the current model gets wrong.
    </div></div>`;
}

/* One row per clip, in clock order, every stage visible.

   The columns are chosen so a person can answer "what is left to do" without opening
   anything: whether the clip has a line, whether it counted, who decided its trucks
   (the model, you, or nobody yet), and whether a preview exists and what it costs on
   disk. `state` names the NEXT action rather than a status word, because "blocked" tells
   you nothing you can act on. */
async function mountClips(siteId) {
  const el = document.getElementById('clipsBody');
  if (!el) return;
  const d = await api(`/api/stations/${siteId}/footage-tree`).catch(e => ({ error: String(e) }));
  if (d.error) { el.innerHTML = `<div class="card">${empty('Could not load clips', d.error)}</div>`; return; }
  const S = d.summary || {}, F = d.footage || [], R = d.runs || [], RD = d.raw_dataset || {};
  const MIX = d.class_mix || [], SP = d.spend || [], ACT = d.active || [];
  const maxc = Math.max(1, ...MIX.map(m => m.n));
  const dead = R.filter(r => r.dead);

  const chip = c => c.state === 'ready' ? '<span class="chip ok">ready</span>'
    : c.state.endsWith('to review') ? `<span class="chip warn">${esc(c.state)}</span>`
    : `<span class="chip bad">${esc(c.state)}</span>`;

  el.innerHTML = `
    <div class="grid g4" style="margin-bottom:14px">
      <div class="tile"><div class="ico info">${ic.overview}</div><div>
        ${/* "Footage 0 h" sat directly under a header reading "9.3 h footage" — the tile
              counts CLIPS CUT, the header counts footage held, and sharing a word made
              them look like a contradiction. */''}
        <div class="lbl">Cut into clips</div><div class="val">${S.hours} h</div>
        <div class="sub">${S.clips} clip(s) · ${S.minutes} min</div></div></div>
      <div class="tile"><div class="ico ok">${ic.runs}</div><div>
        <div class="lbl">Counted</div><div class="val">${num(S.vehicles)}</div>
        <div class="sub">${S.counted_clips} of ${S.clips} clips</div></div></div>
      <div class="tile"><div class="ico ${S.pending_review ? 'warn' : 'ok'}">${ic.judges}</div><div>
        <div class="lbl">Waiting on you</div><div class="val">${num(S.pending_review)}</div>
        <div class="sub">${S.needs_line ? S.needs_line + ' clip(s) need a line · ' : ''}${
          S.needs_extraction ? S.needs_extraction + ' need extraction' : 'nothing blocked'}</div></div></div>
      <div class="tile"><div class="ico acc">${ic.training}</div><div>
        <div class="lbl">Spent here</div><div class="val">${usd(S.spend_usd)}</div>
        <div class="sub">${S.labels_from_here} labels from this station${
          S.model ? ` · axle model #${S.model.id} ${Math.round(S.model.accuracy*100)}%` : ''}</div></div></div>
    </div>

    <div class="card" style="margin-bottom:14px"><div class="card-head">
      <h2>Dataset from this station</h2>
      <span class="muted">verification is the gold set — corrections are what a retrain needs</span></div>
      <div class="card-body ds-flow">
        <div><span class="lbl">Crops sampled</span><b>${num(RD.crops)}</b>
          <span class="sub">${num(RD.judged)} judged by AI</span></div>
        <div class="arrow">→</div>
        <div><span class="lbl">Verified by you</span><b>${num(RD.verdicts)}</b>
          <span class="sub">on counted vehicles</span></div>
        <div class="arrow">→</div>
        <div><span class="lbl">Corrections</span><b>${num(RD.corrections)}</b>
          <span class="sub">the training set</span></div>
        <div style="flex:1"></div>
        <div class="${RD.pending_review ? 'pend on' : 'pend'}">
          <span class="lbl">Still to review</span><b>${num(RD.pending_review)}</b>
          <span class="sub">${num(RD.contested)} contested crops</span>
          ${/* Always offered, never conditional on the count: a review screen that
                disappears whenever the queue empties reads as a missing feature. */''}
          ${RD.contested_run
            ? `<button class="btn sm ${RD.contested_open ? 'primary' : 'ghost'}"
                 data-review="${RD.contested_run}" style="margin-top:8px">Review${
                 RD.contested_open ? ' ' + num(RD.contested_open) : ''}</button>`
            : '<span class="sub">nothing judged yet</span>'}</div>
      </div></div>

    <div class="cl-split"><div>
    ${/* One layout, whatever state the file is in. A recording ALREADY contains its
          15-minute windows — segmenting only makes them separately addressable — so the
          page shows the same grid before and after, with uncut windows greyed. Cutting
          then fills them in rather than replacing a paragraph with a grid, and the clip
          names are visible beforehand because they are computed by the same function
          the segmenter uses. */''}
    ${F.map(f => {
      const W = f.windows || [], cut = W.filter(w => w.video_id).length;
      return `
      <div class="card fold" style="margin-bottom:12px">
        <div class="card-head">
          <div><h2 style="font-size:17px">${esc((f.start_clock || '').slice(11, 16))}–${
              esc((W[W.length - 1] || {}).end_clock || '')} ·
              <span style="color:var(--cc-fg-3);font-weight:400">${
              esc((f.start_clock || '').slice(0, 10))}</span></h2>
            <div class="muted mono" style="font-size:12px">${esc(f.name)} ·
              ${f.minutes} min · ${f.size_mb} MB</div></div>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="chip ${cut === 0 ? '' : cut >= W.length ? 'ok' : 'warn'}">${
              cut} of ${W.length} cut</span>
            ${f.extracted ? `<span class="muted">${f.extracted} extracted · ${f.counted} counted</span>` : ''}
            ${cut < W.length
              ? `<button class="btn sm primary" data-segment="${f.footage_id}">Cut into ${
                  W.length} clips</button>`
              : ''}
          </div>
        </div>
        ${W.length ? `<div class="clipgrid">${W.map(w => {
          const c = w.clip;
          if (!c) return `<div class="clipcard planned" title="not cut yet">
            <div class="ch"><div><b>${esc(w.clock)}–${esc(w.end_clock)}</b>
              <span class="muted">${w.minutes} min</span></div>
              <span class="chip">not cut</span></div>
            <div class="cnum"><b>—</b><span>cut this file to work on it</span></div>
            <div class="crow mono" style="font-size:11px;color:var(--cc-fg-off)">${esc(w.name)}</div>
          </div>`;
          const V = c.verify || {};
          const pct = V.model_accuracy != null ? Math.round(V.model_accuracy * 100) : null;
          return `<div class="clipcard${V.verified ? ' done' : ''}" data-vid="${c.video_id}">
            <div class="ch">
              <div><b>${esc(w.clock)}–${esc(w.end_clock)}</b>
                <span class="muted">${w.minutes} min</span></div>
              ${c.line === 'none' ? '<span class="status warn">no line</span>'
                : V.verified ? '<span class="chip ok">verified</span>'
                : c.tracks ? '<span class="chip">not verified</span>'
                : '<span class="chip warn">not extracted</span>'}
            </div>
            <div class="cnum"><b>${c.counted != null ? num(c.counted) : '—'}</b>
              <span>vehicles counted</span></div>
            <div class="crow"><span>Verified by you</span><b>${num(V.verified || 0)}</b></div>
            <div class="crow"><span>Model was right</span>
              <b class="${pct == null ? '' : pct >= 95 ? 'good' : pct >= 85 ? 'mid' : 'bad'}">${
                pct == null ? '—' : pct + '%'}</b></div>
            <div class="crow"><span>You corrected</span><b>${num(V.reclassed || 0)}</b></div>
            <div class="cacts">
              <button class="btn sm ${c.tracks ? 'secondary' : 'primary'}"
                data-x-extract="${c.video_id}">${c.tracks ? 'Re-extract' : 'Extract'}</button>
              <button class="btn sm secondary" data-x-count="${c.video_id}"
                ${c.line === 'none' ? 'disabled title="draw the station count line first"' : ''}>Count</button>
              <button class="btn sm ${V.verified ? 'secondary' : 'primary'}"
                data-verify="${c.video_id}"
                ${c.counted == null ? 'disabled title="needs a line and detections"' : ''}>Verify</button>
              <button class="btn sm ghost" data-reportcard="${c.video_id}"
                ${c.counted == null ? 'disabled title="nothing counted yet"' : ''}>Report</button>
            </div></div>`; }).join('')}</div>`
        : `<div class="card-body muted">No readable clock on this file, so its clips
            cannot be placed in time. Set the start time on the Overview tab first.</div>`}
      </div>`; }).join('') || `<div class="card">${empty('No footage attached',
        'Attach footage to this station first.')}</div>`}
      </div>
      <div class="cl-rail" id="clRail"></div></div>

    <details class="card" style="margin-top:14px"${dead.length ? ' open' : ''}>
      <summary style="padding:14px 20px;cursor:pointer"><b>Where these clips came from</b>
        <span class="muted"> — ${R.length} extraction run(s)${
          dead.length ? `, ${dead.length} produced nothing` : ''}</span></summary>
      <table><thead><tr><th>Run</th><th>Source</th><th>Parts</th>
        <th>Produced</th><th class="right">Cost</th><th class="right"></th></tr></thead><tbody>
      ${R.map(r => `<tr${r.dead ? ' style="opacity:.55"' : ''}>
        <td class="id">${r.run_id} · ${esc(r.name)}</td>
        <td style="font-size:12px">${esc(r.source || '—')}</td>
        <td class="mono">${esc(JSON.stringify(r.parts || []))}</td>
        <td>${r.produced.length ? 'clips ' + r.produced.join(', ')
             : '<span class="status warn">nothing — safe to delete</span>'}</td>
        <td class="right num">${usd(r.spend_usd)}</td>
        <td class="right">${r.dead
          ? `<button class="btn sm ghost" data-delrun="${r.run_id}">Delete</button>`
          : `<span class="muted" style="font-size:12px">${r.stages_done} stage(s) done</span>`}</td>
      </tr>`).join('')}
      </tbody></table></details>`;

  /* Extraction moved to the clip's own page, where the model can be chosen alongside it.
     The handler that lived here had no button left, and a handler with no emitter is the
     same latent trap as a button with no handler. */
  // While anything runs, keep the page truthful without hammering the API.
  if (ACT.length) setTimeout(() => {
    if (WS && WS.tab === 'clips') mountClips(siteId);
  }, 12000);

  /* The rail: whichever clip is selected, its state and its actions, beside the grid.
     Extraction is started from here because the model belongs with it — a count has to be
     able to name the detector that produced it — and because the progress of a running
     job has somewhere to live that is not a separate page. */
  let SEL = null, POLL = null;

  async function paintRail() {
    const rail = document.getElementById('clRail');
    if (!rail) return;
    if (!SEL) {
      rail.innerHTML = `<div class="card"><div class="card-body muted">
        Select a clip to see its state, run the detector on it, and watch progress here.</div></div>`;
      return;
    }
    const d = await api('/api/clip/' + SEL).catch(e => ({ error: String(e) }));
    if (d.error) { rail.innerHTML = `<div class="card">${empty('Could not read that clip', d.error)}</div>`; return; }
    const c = d.clip, E = d.extract, V = d.verified, job = d.job || {};
    const running = ['queued', 'running'].includes(job.status) && job.kind === 'extract';
    const step = (lbl, done, detail) => `<div class="cw-step ${done ? 'done' : ''}">
      <span class="tick">${done ? '✓' : '·'}</span><span><b style="font-weight:500">${esc(lbl)}</b>
      <div style="color:var(--cc-fg-3);font-size:12px">${esc(detail)}</div></span></div>`;
    rail.innerHTML = `
      <div class="card"><div class="card-head"><div>
        <h2 style="font-size:16px">${esc((c.start_clock || '').slice(11, 16))}–${
          esc(c.name.slice(-4, -2) + ':' + c.name.slice(-2))}</h2>
        <p class="mono" style="font-size:11px">${esc(c.name)}</p></div></div>
        ${step('Count line', d.line.drawn, d.line.drawn ? `from the ${esc(d.line.source)}` : 'not drawn')}
        ${step('Detections', E.tracks > 0, E.tracks ? `${num(E.tracks)} tracks · ${esc(E.model || '?')}` : 'not extracted')}
        ${step('Counted', d.counted != null, d.counted != null ? `${num(d.counted)} vehicles` : '—')}
        ${step('Verified', V.n > 0, V.n ? `${num(V.n)} checked · ${num(V.changed)} corrected` : 'not checked')}
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--cc-line)">
          <div class="lbl" style="font-size:12px;color:var(--cc-fg-3)">Next</div>
          <div style="font-weight:500;font-size:13px">${esc(d.next)}</div></div>
      </div>

      ${running ? `<div class="card"><div class="card-head"><div>
        <h2 style="font-size:15px">Extracting</h2>
        ${/* Time left, not a speed multiple. "3.76x realtime" is a fact about the
              machine; the question being asked is when this finishes. */''}
        <p id="clEta">${job.eta_s != null ? esc(etaText(job.eta_s)) + ' left' : 'starting…'}</p></div></div>
        <div class="cl-prog"><i style="width:${job.progress || 0}%"></i></div>
        <p class="muted" id="clProgSub" style="margin:8px 0 0;font-size:12px">${Math.round(job.progress || 0)}% done${
          job.started ? ' · ' + esc(etaText(Math.round(Date.now() / 1000 - job.started))) + ' so far' : ''
        }. You can keep working — it does not stop if you move away.</p></div>`
        : `<div class="card"><div class="card-head"><div><h2 style="font-size:15px">Run the detector</h2></div></div>
        <label class="lbl">Model</label>
        <select class="field sm" id="clModel">${d.models.map(m =>
          `<option value="${esc(m.id)}"${m.id === E.model ? ' selected'
            : m.is_default && !E.model ? ' selected' : ''}>${esc(m.id)}${
            m.is_default ? ' — default' : ''}</option>`).join('')}</select>
        <p class="muted" style="margin:8px 0 10px;font-size:12px">${E.tracks
          ? `Extracted with <b>${esc(E.model || '?')}</b>. Re-running replaces these detections and discards any verification — the tracker renumbers every vehicle.`
          : 'The model is stamped on every track, so a count can always name what produced it.'}</p>
        <button class="btn ${E.tracks ? 'secondary' : 'primary'}" id="clExtract" style="width:100%">
          ${E.tracks ? 'Re-extract' : 'Extract detections'}</button></div>`}

      <div class="card"><div class="card-head"><div><h2 style="font-size:15px">Clip</h2></div></div>
        <div class="cw-facts">
          <div><dt>Starts</dt><dd class="mono">${esc((c.start_clock || '—').slice(0, 19))}</dd></div>
          <div><dt>Length</dt><dd>${c.minutes} min</dd></div>
          <div><dt>Frames</dt><dd>${num(c.frames)} @ ${c.fps} fps</dd></div>
          <div><dt>Size</dt><dd>${c.width}×${c.height}</dd></div>
          ${d.source ? `<div><dt>Cut from</dt><dd style="font-size:12px">${esc(d.source.name)}
            <br><span class="muted">part ${d.source.part}</span></dd></div>` : ''}
        </div></div>`;
    wire('station', siteId);
    const ex = document.getElementById('clExtract');
    if (ex) ex.onclick = () => startExtract(SEL, (document.getElementById('clModel') || {}).value, ex);
    clearInterval(POLL);
    if (running) POLL = setInterval(tickProgress, 3000);
  }

  /* While a job runs the rail must NOT be re-rendered every three seconds: rewriting
     innerHTML resets the rail's own scroll position, so the clip facts scrolled back out
     of reach as you read them, and it would discard a half-changed model selection too.
     Only the numbers that actually move get patched; a full repaint happens once, when
     the job stops. */
  async function tickProgress() {
    if (!WS || WS.tab !== 'clips' || !SEL) { clearInterval(POLL); return; }
    const d = await api('/api/clip/' + SEL).catch(() => null);
    if (!d) return;
    const job = d.job || {};
    const live = ['queued', 'running'].includes(job.status) && job.kind === 'extract';
    if (!live) { clearInterval(POLL); paintRail(); mountClips(siteId); return; }
    const bar = document.querySelector('#clRail .cl-prog i');
    const eta = document.getElementById('clEta');
    const sub = document.getElementById('clProgSub');
    if (bar) bar.style.width = (job.progress || 0) + '%';
    if (eta) eta.textContent = job.eta_s != null ? etaText(job.eta_s) + ' left' : 'starting…';
    if (sub) sub.textContent = `${Math.round(job.progress || 0)}% done`
      + (job.started ? ` · ${etaText(Math.round(Date.now() / 1000 - job.started))} so far` : '')
      + '. You can keep working — it does not stop if you move away.';
  }

  async function startExtract(vid, model, btn) {
    const go = async force => {
      if (btn) { btn.disabled = true; btn.textContent = 'Queued…'; }
      await post(`/api/clips/${vid}/extract`, { force, model_id: model || null });
      toast('Extraction started — progress shows in the panel');
      setTimeout(paintRail, 1200);
    };
    try { await go(false); }
    catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Extract detections'; }
      if (/verdict\(s\)/.test(e.message)) {
        if (!confirm(e.message + '\n\nDiscard that verification and re-extract?')) return;
        try { await go(true); } catch (e2) { toast(e2.message, true); }
      } else toast(e.message, true);
    }
  }

  el.querySelectorAll('.clipcard[data-vid]').forEach(card => card.onclick = e => {
    if (e.target.closest('button, a')) return;          // buttons keep their own jobs
    SEL = card.dataset.vid;
    el.querySelectorAll('.clipcard').forEach(x => x.classList.toggle('sel', x === card));
    paintRail();
  });
  el.querySelectorAll('[data-x-extract]').forEach(b => b.onclick = () => {
    SEL = b.dataset.xExtract;
    el.querySelectorAll('.clipcard').forEach(x => x.classList.toggle('sel', x.dataset.vid === SEL));
    paintRail().then(() => startExtract(SEL, null, null));
  });
  el.querySelectorAll('[data-x-count]').forEach(b => b.onclick = async () => {
    // Counting is a query, not a job: show the answer immediately rather than navigating.
    b.disabled = true;
    try {
      const r = await api('/api/count/' + b.dataset.xCount);
      toast(`${num(r.total)} vehicles counted`);
      SEL = b.dataset.xCount; paintRail(); mountClips(siteId);
    } catch (e) { toast(e.message, true); } finally { b.disabled = false; }
  });
  paintRail();

  el.querySelectorAll('[data-segment]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = 'Cutting…';
    try {
      const r = await post(`/api/stations/${siteId}/footage/${b.dataset.segment}/segment`, {});
      toast(`cut into ${r.clips} clip(s)`);
      mountClips(siteId);
    } catch (e) { b.disabled = false; b.textContent = 'Segment into clips'; toast(e.message, true); }
  });
  wire();      // data-delrun is bound globally there — the run page needs it too
}

/* The survey as a strip of clock time: every recording a block, every gap a hole.

   A 7x24 grid answers "which hours do I have"; this answers "is the day continuous",
   which is the question a missing half-hour actually shows up in. Duplicated stretches
   are marked rather than merged, because two copies of one hour is a storage fact, not
   a coverage fact, and the grid deliberately counts it once. */
function timelineSvg(recs) {
  const present = (recs || []).filter(r => r.present);
  if (present.length < 1) return '';
  const t = s => new Date(s.replace(' ', 'T')).getTime();
  const t0 = Math.min(...present.map(r => t(r.start)));
  const t1 = Math.max(...present.map(r => t(r.end)));
  const span = Math.max(1, t1 - t0);
  const W = 1000, H = 34;
  const x = ms => ((ms - t0) / span) * W;
  const hhmm = ms => new Date(ms).toTimeString().slice(0, 5);
  const blocks = present.map(r => {
    const a = x(t(r.start)), b = x(t(r.end));
    return `<rect x="${a.toFixed(1)}" y="6" width="${Math.max(1.5, b - a).toFixed(1)}" height="22"
      rx="3" fill="var(--cc-${r.n_copies > 1 ? 'series-2' : 'series-1'})"><title>${
      esc(r.name)}\n${esc(r.start.replace('T', ' '))} → ${esc(r.end)} · ${r.minutes} min${
      r.n_copies > 1 ? `\n${r.n_copies} copies on disk` : ''}</title></rect>`;
  }).join('');
  // Gaps are drawn from the holes between blocks rather than from the gap list, so what
  // you see is literally the absence of a block.
  const sorted = [...present].sort((p, q) => t(p.start) - t(q.start));
  const gaps = sorted.slice(1).map((r, i) => {
    const prevEnd = t(sorted[i].end), thisStart = t(r.start);
    if (thisStart - prevEnd < 90000) return '';
    const a = x(prevEnd), b = x(thisStart);
    const mins = Math.round((thisStart - prevEnd) / 60000);
    return `<rect x="${a.toFixed(1)}" y="6" width="${Math.max(1.5, b - a).toFixed(1)}" height="22"
      fill="var(--cc-bad)" opacity=".18"/>
      <title>${mins} min gap</title>`;
  }).join('');
  return `<div style="overflow-x:auto">
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none"
         role="img" aria-label="recording timeline from ${hhmm(t0)} to ${hhmm(t1)}">
      <rect x="0" y="6" width="${W}" height="22" rx="3" fill="var(--cc-sunken)"/>
      ${gaps}${blocks}
    </svg></div>
    <div class="rc-legend" style="margin-top:6px">
      <span>${hhmm(t0)}</span><span style="flex:1"></span><span>${hhmm(t1)}</span></div>`;
}

/* There is no separate clip page. It existed briefly and was a detour: selecting a clip
   navigated away from the list you were working through, and coming back cost a reload.
   Everything a clip needs — its state, its actions, and the progress of anything running
   on it — happens inside the Clips tab, in a rail beside the grid. */
/* The station count line, in a floating editor over a frame from the FOOTAGE.

   Drawn here, not on a clip: there is one line per camera, and the station is the only
   thing allowed to write it. The frame comes straight from a source file, so the line can
   be placed before anything is segmented or extracted. Saving is explicit — see
   lineeditor.js — because a stray drag used to be persisted before anyone could see it. */
let LINE_MODAL = null;
async function openStationLine(siteId, stationCode) {
  if (LINE_MODAL) return;
  const m = document.createElement('div');
  m.className = 'modal-wrap';
  m.innerHTML = `<div class="modal" style="width:min(1100px,96vw)">
    <div class="card-head"><div>
      <h2>Count line — ${esc(stationCode || 'station')}</h2>
      <p>drawn on this station's own footage · one line counts every clip from this camera</p></div>
      <div class="head-actions">
        <label class="lbl" style="margin:0;align-self:center">Frame</label>
        <select class="field sm" id="lmAt" style="width:auto">
          <option value="300">early</option>
          <option value="9000" selected>a few minutes in</option>
          <option value="30000">later</option>
        </select>
        <button class="btn secondary" id="lmClose">Close</button>
      </div></div>
    <div class="modal-body"><div id="lmEditor"></div>
      <div id="lmCheck" style="margin-top:12px"></div>
      <p class="muted" style="margin:10px 0 0">Traffic crossing toward the shaded side counts
        as <b>in</b>. Saving stores source-pixel coordinates, so the line means the same
        thing at any window size.</p></div></div>`;
  document.body.appendChild(m);
  LINE_MODAL = m;

  const close = () => {
    if (ED && ED.isDirty && ED.isDirty()
        && !confirm('This line has not been saved.\n\nClose and discard the change?')) return;
    if (ED && ED.destroy) ED.destroy();
    m.remove(); LINE_MODAL = null;
    route(true);            // header button, stage rail and body all move together
  };
  m.querySelector('#lmClose').onclick = close;
  m.onclick = e => { if (e.target === m) close(); };
  const esc_ = e => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc_); } };
  document.addEventListener('keydown', esc_);

  const { mountLineEditor } = await import('/shared/lineeditor.js');
  let at = 9000, ED = null;

  /* The verdict is measured, not guessed: the station's stored trajectories are replayed
     against the line, so "59 vehicles would cross this" is a fact about its own traffic. */
  async function check(lines) {
    const box = m.querySelector('#lmCheck');
    if (!box) return;
    box.innerHTML = '<div class="muted" style="font-size:12px">checking this line against the '
      + 'vehicles already tracked here…</div>';
    const r = await post(`/api/stations/${siteId}/line-check`, { lines: lines || [] })
      .catch(e => ({ ok: false, why: String(e) }));
    if (!r.ok) { box.innerHTML = `<div class="muted" style="font-size:12px">${esc(r.why)}</div>`; return; }
    box.innerHTML = r.lines.map(L => `
      <div class="ov-flag ${L.findings.some(f => f.level === 'bad') ? 'bad'
                          : L.findings.some(f => f.level === 'warn') ? 'warn' : ''}">
        <span class="n">${num(L.crossings)}</span>
        <span class="t"><b>${L.crossings ? 'vehicles would cross this line'
                                         : 'nothing crosses this line'}</b>
          ${L.findings.map(f => esc(f.text)).join('<br>')}
          <br><span class="muted">measured on ${esc(r.checked_on.name)}${
            L.median_height_px ? ` · vehicles ${Math.round(L.median_height_px)} px tall at the line` : ''}</span>
        </span></div>`).join('');
  }

  const build = async () => {
    if (ED && ED.destroy) ED.destroy();
    const host = m.querySelector('#lmEditor');
    host.innerHTML = '';
    ED = mountLineEditor(host, {
      onSave: async lines => {
        try {
          await put(`/api/stations/${siteId}/line`, { lines });
          toast('Count line saved');
          check(lines);
        } catch (e) { toast(e.message, true); }
      },
      frameUrl: () => `/api/stations/${siteId}/frame?at=${at}`,
    });
    const d = await api(`/api/stations/${siteId}/line`).catch(() => ({ lines: [] }));
    ED.load(0, d.lines || [], 0);
  };
  m.querySelector('#lmAt').onchange = e => { at = +e.target.value; build(); };
  build().then(() => check(null));
}

/* The station Overview. One fetch, one source of truth: /audit resolves copies to
   recordings, checks every path against the disk, and names the next action. */
async function mountOverview(siteId) {
  const el = document.getElementById('ovBody');
  if (!el) return;
  const a = await api(`/api/stations/${siteId}/audit`).catch(e => ({ error: String(e) }));
  if (a.error) { el.innerHTML = `<div class="card">${empty('Could not read the station', a.error)}</div>`; return; }
  const T = a.totals, F = a.folder;

  /* Each problem is one compact row in a narrow column, not another full-width table.
     A count you can scan beside the folder it belongs to is worth more than six stacked
     panels, and it keeps the page a layout rather than a list. */
  const flag = (n, tone, title, body) => !n ? '' : `<div class="ov-flag ${tone}">
    <span class="n">${num(n)}</span><span class="t"><b>${esc(title)}</b>${body}</span></div>`;
  const names = xs => xs.slice(0, 4).map(x => esc(x.name)).join(', ')
    + (xs.length > 4 ? ` and ${xs.length - 4} more` : '');
  const flags = [
    flag((a.incomplete || []).length, 'bad', 'not a complete file yet',
      (a.incomplete || []).slice(0, 4).map(i =>
        `${esc(i.name)} — ${esc(i.why)}`).join('<br>')
      + '<br>held out of every total; press Process again once the copy finishes'),
    flag(a.missing.length, 'bad', 'gone from disk',
      `${names(a.missing)} — still on record, excluded from every total until the drive is back`),
    flag(a.unattached.length, 'warn', 'in the folder, not attached',
      `${names(a.unattached)} — press Process footage`),
    flag(a.undated.length, 'warn', 'no readable start time',
      `${names(a.undated)} — a guessed clock puts vehicles in the wrong 15-minute bin`),
    flag(a.foreign.length, 'warn', 'may be another station',
      a.foreign.slice(0, 3).map(f => `${esc(f.name)} (${esc(f.why)})`).join('<br>')),
    flag(a.outside.length, 'warn', 'outside this folder',
      'attached to the station but stored elsewhere — counted nowhere'),
    flag(a.duplicates.length, 'warn', 'held twice',
      'same clock time stored more than once — counted once'),
    flag(a.gaps.length, 'warn', 'gap(s) in the survey',
      `${num(a.gaps.reduce((x, g) => x + g.minutes, 0))} minutes of road not recorded`),
  ].filter(Boolean).join('');

  el.innerHTML = `<div class="ov">

    <div class="card" style="border-left:3px solid var(--cc-accent)">
      <div class="card-body" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <b style="font-weight:500">Next:</b>
        <span style="color:var(--cc-fg)">${esc(a.next)}</span></div></div>

    <div class="tile sp3"><div class="ico ok">${ic.videos}</div><div>
      <div class="lbl">Footage</div><div class="val">${T.hours} h</div>
      <div class="sub">${T.recordings} recording(s)${
        T.files !== T.recordings ? ` · ${T.files} files incl. copies` : ''}</div></div></div>
    <div class="tile sp3"><div class="ico ${a.gaps.length ? 'warn' : 'ok'}">${ic.overview}</div><div>
      <div class="lbl">Continuity</div><div class="val">${a.gaps.length || 'none'}</div>
      <div class="sub">${a.gaps.length
        ? 'gap(s) — ' + num(a.gaps.reduce((x, g) => x + g.minutes, 0)) + ' min missing'
        : 'unbroken run of footage'}</div></div></div>
    <div class="tile sp3"><div class="ico ${a.outside.length + a.duplicates.length ? 'warn' : 'ok'}">${ic.datasets}</div><div>
      <div class="lbl">Not counted</div>
      <div class="val">${a.outside.length + a.duplicates.length}</div>
      <div class="sub">${[a.outside.length ? a.outside.length + ' outside the folder' : '',
                          a.duplicates.length ? a.duplicates.length + ' duplicate(s)' : '']
                         .filter(Boolean).join(' · ') || 'everything attached is counted'}</div></div></div>
    <div class="tile sp3"><div class="ico info">${ic.counts}</div><div>
      <div class="lbl">Survey window</div>
      <div class="val" style="font-size:20px">${T.span
        ? esc(T.span.from.slice(11, 16)) + '–' + esc(T.span.to.slice(11, 16)) : '—'}</div>
      <div class="sub">${T.days} day(s) · camera ${esc(T.camera || '—')}</div></div></div>

    ${/* ONE card, THREE states, because a station is only ever in one of them:

          1. no footage, no folder  -- a brand-new station. Pick the folder. This is the
             path every new station follows.
          2. footage but no folder  -- attached by the older scan-and-assign flow, before
             folders existed. It used to render state 1, which asked you to complete
             "step 1" directly above a list of files that were plainly already attached.
          3. folder set             -- the working state: fetch, process, draw the line.

          State 2 does NOT offer to adopt the directory the files sit in. On this archive
          that directory is /Volumes/RK/Traffic, shared by BHK-01, ATP-01 and an
          unassigned Tamil Nadu survey, so adopting it would pull every one of them into
          whichever station you pressed the button on. */''}
    ${(() => {
      const H = a.folder_hint;
      const legacy = !F && T.recordings > 0;
      return `<div class="card sp8"><div class="card-head"><div>
        <h2>Station folder</h2>
        <p>${F ? `<span class="mono">${esc(F)}</span>${a.folder_exists ? ''
                : ' <span class="status bad">not reachable</span>'}`
              : legacy ? 'footage is attached, but no folder is recorded for this station'
                       : 'step 1 — point this station at the folder its footage lives in'}</p></div>
        ${F ? '<button class="btn sm ghost" id="changeFolder">Change folder</button>' : ''}</div>

        <div class="card-body" style="padding:0 0 14px;color:var(--cc-fg-2);font-size:13px">
          Source video is <b>read where it is and never copied</b> — a 7-day survey is far
          too large to move. What the Lab writes goes under the project: clips cut from the
          source, crops, gold frames and rendered previews.
        </div>

        ${legacy ? `<div class="err" style="border-left-color:var(--cc-warn)"><div>
          <b>Attached before folders existed.</b>
          <div style="font-size:12px;margin-top:4px">
            ${T.recordings} recording(s) are filed under this station and counted normally,
            but nothing is watching a folder — so adding or deleting a file on disk will not
            show up here until one is set.
            ${H && H.exclusive
              ? `Every file sits in <span class="mono">${esc(H.dir)}</span> and nothing else
                 uses it, so it is safe to adopt.`
              : H && H.dir
                ? `They all sit in <span class="mono">${esc(H.dir)}</span>, but that folder
                   also holds ${[...(H.shared_with || []).map(c => esc(c) + "'s footage"),
                                 H.unassigned ? H.unassigned + ' unassigned file(s)' : '']
                                .filter(Boolean).join(' and ')} — adopting it would pull
                   those into this station too. Copy this station's files into a folder of
                   their own first.`
                : `They are spread across ${(H && H.dirs || []).length} directories, so there
                   is no single folder to adopt. Copy them into one folder per station.`}
          </div></div></div>` : ''}

        <div id="folderChoose" ${F ? 'hidden' : ''}>
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
            <input class="field" id="folderPath" style="flex:1;min-width:220px"
              placeholder="/path/to/this/station's/footage folder"
              value="${esc(F || (H && H.exclusive ? H.dir : '') || '')}">
            <button class="btn secondary" id="browseFolder">Browse…</button>
            <button class="btn primary js-fetch">Fetch videos</button>
            <button class="btn primary" id="attachFolder" hidden>Attach these files</button>
          </div>
        </div>

        ${/* Two different conditions, so two different gates. Fetch and Process act on a
              FOLDER and only make sense once one is recorded. The count line acts on
              FOOTAGE — it is drawn on a frame from a source file — so it must be offered
              to any station that has footage, folder or not. Hanging it off the folder
              branch hid it from every legacy station, including ones that already had a
              line and no way to edit it. */''}
        ${F ? `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
          <button class="btn secondary js-fetch">Fetch videos</button>
          <button class="btn primary" id="processFolder">Process footage →</button>
          ${/* The line button moved to the station header — it belongs with the
                station's actions, and having it in two places made this card messy. */''}
          <span class="muted" style="font-size:12px;flex:1;min-width:180px">Fetch reads the
            folder as it is now. Process applies the difference — attaches new files, flags
            deleted ones, re-probes anything that changed — and updates every number here.</span>
        </div>` : ''}

        ${a.recordings.length ? `<table style="margin-top:6px"><thead><tr><th>File</th>
          <th>Starts</th><th class="right">Length</th><th class="right">On disk</th></tr></thead>
          <tbody>${a.recordings.map(r => `<tr>
            <td class="id">${esc(r.name)}${r.n_copies > 1
              ? ` <span class="chip">${r.n_copies} copies</span>` : ''}</td>
            <td class="mono">${esc(r.start.replace('T', ' ').slice(0, 16))}</td>
            <td class="right num">${r.minutes} min</td>
            <td class="right">${r.present ? '<span class="chip ok">yes</span>'
                                          : '<span class="status bad">gone</span>'}</td>
          </tr>`).join('')}</tbody></table>` : ''}
        <div id="folderPreview"></div></div>`;
    })()}

    <div class="card sp4"><div class="card-head"><div><h2>Needs attention</h2>
      <p>${flags ? 'each one blocks or distorts something downstream' : 'nothing to resolve'}</p></div></div>
      <div class="stack">${flags || `<div class="ov-ok">Folder and records agree.<br>
        Every attached file is present, timed and counted.</div>`}</div>
      <div style="margin-top:18px;padding-top:16px;border-top:1px solid var(--cc-line)">
        <b style="font-weight:500;font-size:13px">Count line</b>
        <p class="muted" style="margin:2px 0 10px;font-size:12px">one line per camera,
          inherited by every clip — drawn on a clip, saved to the whole station</p>
        ${a.line_drawn
          ? '<span class="chip ok">station line drawn</span>'
          : '<span class="chip warn">not drawn — nothing can be counted</span>'}
      </div>
      ${(B => `<div style="margin-top:18px;padding-top:16px;border-top:1px solid var(--cc-line)">
        <b style="font-weight:500;font-size:13px">Built from this station</b>
        <p class="muted" style="margin:2px 0 12px;font-size:12px">derived work — the source
          folder is never written to, and all of this can be rebuilt from it</p>
        <table><tbody>
          <tr><td class="id">Clips cut</td><td class="right num">${num(B.clips)}</td></tr>
          <tr><td class="id">Clips extracted</td><td class="right num">${num(B.extracted)}</td></tr>
          <tr><td class="id">Vehicles tracked</td><td class="right num">${num(B.tracks)}</td></tr>
          <tr><td class="id">Verified by you</td><td class="right num">${num(B.verified)}</td></tr>
        </tbody></table></div>`)(a.built || {})}</div>

    <div class="card"><div class="card-head"><div><h2>Survey coverage</h2>
      <p>the same footage two ways — as a run of clock time, and as minutes per hour</p></div></div>
      ${timelineSvg(a.recordings) || '<div class="empty">No footage with a readable clock yet.</div>'}
      ${a.gaps.length ? `<table style="margin:14px 0 0"><thead><tr><th>Gap starts</th>
        <th>Resumes</th><th class="right">Missing</th></tr></thead><tbody>
        ${a.gaps.map(g => `<tr><td class="mono">${esc(g.from)}</td>
          <td class="mono">${esc(g.to.replace('T', ' '))}</td>
          <td class="right num">${g.minutes} min</td></tr>`).join('')}</tbody></table>` : ''}
      ${a.overlaps.length ? `<p class="muted" style="margin:10px 0 0">
        ${a.overlaps.length} overlapping pair(s): ${a.overlaps.map(o =>
          `${esc(o.a)} / ${esc(o.b)} (${o.minutes} min)`).join(', ')} — two recordings covering
        the same clock time double-count if both are extracted.</p>` : ''}
      <div style="margin-top:16px">${coverageGrid(a.coverage)}</div>
    </div>

    ${a.outside.length ? `<div class="card sp7"><div class="card-head"><div>
      <h2>Attached from outside this folder</h2>
      <p>${a.outside.length} file(s) carry this station but live elsewhere —
         <b>counted nowhere above</b></p></div></div>
      <table><thead><tr><th>File</th><th>Where it is</th><th>Starts</th>
        <th class="right">Length</th><th class="right"></th></tr></thead><tbody>
      ${a.outside.map(o => `<tr>
        <td class="id">${esc(o.name)}</td>
        <td class="mono" style="font-size:12px">${esc(o.dir)}</td>
        <td class="mono">${esc((o.start || '—').slice(0, 16))}</td>
        <td class="right num">${o.minutes} min${o.on_disk ? ''
          : ' <span class="status bad">gone</span>'}</td>
        <td class="right"><button class="btn sm ghost" data-detach="${o.id}"
          title="remove it from this station — the file and its record both stay">Detach</button></td>
      </tr>`).join('')}</tbody></table>
      <div class="card-body muted" style="padding:12px 0 0">
        <b style="font-weight:500;color:var(--cc-fg)">To bring one in:</b> copy the file into
        <span class="mono">${esc(F)}</span> and press Process — it is picked up on the next
        reconcile. <b style="font-weight:500;color:var(--cc-fg)">To remove one:</b> Detach
        drops the station link only; the file and its record stay, so it can be re-attached
        from any station later. Nothing here is counted meanwhile.</div></div>` : ''}

    <div class="card ${a.outside.length ? 'sp5' : ''}"><div class="card-head"><div>
      <h2>Verification</h2>
      <p>the gold set, built from clips you have already checked</p></div>
      <button class="btn sm secondary" data-wstab-go="labels">Open</button></div>
      <div id="ovGold"><div class="card-body muted">Loading…</div></div></div>

  </div>`;


  wire('station', siteId);
  mountOvGold(siteId);
}

function wsBody(tab, s, id) {
  const g = s.gold || {};

  if (tab === 'overview') {
    // Every number here comes from /audit, fetched by mountOverview(). The page used to
    // assemble its headline from two unrelated queries -- a file count from the station's
    // non-duplicate rows, a folder name from sites.footage_dir -- and the two described
    // different drives, so deleting a file from the named folder changed nothing.
    return `<div id="ovBody"><div class="card"><div class="card-body muted">
      Reading the station folder…</div></div></div>`;
  }

  if (tab === 'clips') {
    // Filled by mountClips() once the data arrives: this view derives every clip's state
    // from counts, axle checks and files on disk, which is a handful of queries per clip
    // and not worth blocking the whole station page on.
    return `<div id="clipsBody"><div class="card"><div class="card-body muted">
      Loading clips…</div></div></div>`;
  }

  if (tab === 'labels') {
    // Filled by mountGold(): the gold set is derived from verdicts, so it is a query
    // rather than a stored collection and is fetched with the tab.
    return `<div id="goldBody"><div class="card"><div class="card-body muted">
      Loading gold set…</div></div></div>`;
  }

  if (tab === 'datasets') {
    return `<div class="card tight"><table>
      <thead><tr><th>Dataset</th><th class="right">Train</th><th class="right">Val</th>
        <th>Fingerprint</th><th class="right"></th></tr></thead>
      <tbody>${(s.datasets || []).map(d => `<tr>
        <td class="id">${esc(d.name)}</td>
        <td class="right num">${num(d.n_train)}</td>
        <td class="right num">${num(d.n_val)}</td>
        <td class="mono" style="font-size:11px">${esc(d.fingerprint || '')}</td>
        <td class="right"><button class="btn sm secondary" data-route="datasets">Open</button></td>
      </tr>`).join('') || `<tr><td colspan="5" class="empty">
        No dataset built from this station yet — it is the last step before training.</td></tr>`}
      </tbody></table></div>`;
  }

  if (tab === 'models') {
    return `<div class="card tight">
      <div style="padding:20px 20px 0"><div class="card-head"><div>
        <h2>Models for this station</h2>
        <p>a station model is only worth training when the error is actually in the model —
           check Error sources first</p></div>
        <button class="btn sm secondary" data-errors="${id}">Error sources</button></div></div>
      <table><thead><tr><th>Model</th><th>Status</th><th class="right">mAP50</th>
        <th class="right">Recall</th><th class="right">Cost</th></tr></thead>
      <tbody>${(s.models || []).map(m => `<tr>
        <td class="id">#${m.id} · ${esc(m.tag)}</td>
        <td>${statusPill(m.status)}</td>
        <td class="right num">${m.map50 ? m.map50.toFixed(3) : '—'}</td>
        <td class="right num">${m.recall ? m.recall.toFixed(3) : '—'}</td>
        <td class="right num">${usd(m.cost_usd)}</td></tr>`).join('')
        || `<tr><td colspan="5" class="empty">No station model — this station uses the global model.</td></tr>`}
      </tbody></table></div>`;
  }
  return '';
}


async function viewVideos() {
  const vids = await api('/api/videos-on-disk');
  return `<div class="page">
    <div class="page-head"><div><h1>Footage</h1>
      <p>Source files on disk. Starting a run cuts the file into clips — nothing heavy starts
         until you press Run there.</p></div>
      <div class="head-actions"><button class="btn primary" id="startnew">Start New Run</button></div></div>
    <div class="card tight"><table>
      <thead><tr><th>File</th><th>Station</th><th>Folder</th><th class="right">Size</th>
        <th class="right">Modified</th><th class="right">Action</th></tr></thead>
      <tbody>${vids.map(v => `<tr>
        <td class="id">${esc(v.name)}${v.used ? ' <span class="chip">has run</span>' : ''}</td>
        <td>${v.station
          ? `<button class="btn sm ghost" data-station="${v.station.site_id}"
               title="open ${esc(v.station.name)}">${esc(v.station.code)}</button>`
          : '<span class="status warn">not attached</span>'}</td>
        <td>${esc(v.dir)}</td>
        <td class="right num">${v.size_mb} MB</td>
        <td class="right">${ago(v.mtime)}</td>
        <td class="right"><button class="btn sm secondary" data-new="${esc(v.path)}" data-name="${esc(v.name)}">Start a run</button></td>
      </tr>`).join('') || '<tr><td colspan="6" class="empty">No .mp4 files found.</td></tr>'}</tbody>
    </table>
    <div class="card-body muted">A file with no station is counted by nothing — attach it
      from that station's folder card on its Overview tab.</div></div></div>`;
}

const CLASSES = ["2W", "3W_Auto", "Car_Jeep_Van", "LCV", "Mini_Bus", "Bus", "Tractor",
  "Tractor_Trailer", "2Axle_Truck", "3Axle_Truck", "MAV", "Cycle", "Cycle_Rickshaw", "Animal_Cart", "Other"];

async function viewJudges() {
  const [m, b, at, ax] = await Promise.all([
    api('/api/models'), api('/api/bakeoff'),
    api('/api/attrs').catch(() => ({ attributes: [] })),
    api('/api/axles/summary').catch(() => ({})),
  ]);
  const sel = new Set(m.selected);
  const rows = m.models.slice(0, 40).map(x => `<tr>
    <td><input type="checkbox" data-model="${esc(x.id)}" ${sel.has(x.id) ? 'checked' : ''}></td>
    <td class="id">${esc(x.id)}${m.recommended.includes(x.id) ? ' <span class="chip">recommended</span>' : ''}</td>
    <td class="right num">$${x.in_per_m.toFixed(3)}</td><td class="right num">$${x.out_per_m.toFixed(3)}</td>
    <td class="right num">${x.context ? (x.context / 1000).toFixed(0) + 'k' : '—'}</td></tr>`).join('');
  const ev = (b.result && b.result.results) || [];
  const ens = b.result && b.result.ensemble;
  return `<div class="page">
    <div class="page-head"><div><h1>Judges</h1>
      <p>Three vision models vote on every crop. Different families on purpose — their mistakes should not agree.</p></div>
      <div class="head-actions">
        <button class="btn secondary" id="savejudges">Save selection</button>
        <button class="btn primary" id="runbake">Bake-off on gold set</button>
      </div></div>

    <div class="card"><div class="card-head"><div><h2>What the judges cannot answer</h2>
      <p>axle count and the operator/use attributes are measured human skills the cheap
         vision models do not have, so both are settled by a person here — these two
         screens are where that happens</p></div></div>
      <div class="grid g2" style="gap:12px">
        <button class="tile" data-route="axles" style="text-align:left;cursor:pointer">
          <div class="ico ${ax.unresolved ? 'warn' : 'ok'}">${ic.judges}</div><div>
          <div class="lbl">Axle audit</div>
          <div class="val">${num(ax.unresolved || 0)}</div>
          <div class="sub">${ax.unresolved ? 'heavy track(s) need your call' : 'nothing waiting'}
            · ${num(ax.resolved || 0)} of ${num(ax.total || 0)} settled →</div></div></button>
        <button class="tile" data-route="attrs" style="text-align:left;cursor:pointer">
          <div class="ico info">${ic.review}</div><div>
          <div class="lbl">Attributes</div>
          <div class="val">${(at.attributes || []).length}</div>
          <div class="sub">${(at.attributes || []).map(a =>
            `${esc(a.attribute)} ${num(a.usable)}`).join(' · ') || 'taxi / APSRTC / axle class'} →</div>
          </div></button>
      </div></div>

    ${ens ? `<div class="card"><div class="card-head"><h2>What agreement is worth</h2>
      <p>measured on your hand-graded crops — this is the rule the pipeline now follows</p></div>
      <div class="grid g3" style="gap:16px">
        ${['unanimous', 'majority', 'split'].map(t => {
          const v = ens.tiers[t] || {}; const a = v.accuracy == null ? '—' : Math.round(v.accuracy * 100) + '%';
          const good = t === 'unanimous';
          return `<div class="tile"><div class="ico ${good ? 'ok' : 'warn'}">${good ? ic.review : ic.judges}</div><div>
            <div class="lbl">${t === 'unanimous' ? 'All three agree' : t === 'majority' ? 'Only two agree' : 'Three-way split'}</div>
            <div class="val">${a}</div>
            <div class="sub">${v.n || 0} crops · ${Math.round((v.share || 0) * 100)}% of set</div></div></div>`;
        }).join('')}
      </div>
      <p style="margin-top:16px;color:var(--cc-fg-2)">A two-of-three majority is barely better than a coin flip, so it is not treated as consensus.
      <b>Only unanimous verdicts are auto-accepted</b>; everything else goes to the review queue. That auto-labels about
      ${Math.round((ens.tiers.unanimous?.share || 0) * 100)}% of crops and puts a human on the rest.</p></div>` : ''}

    <div class="card"><div class="card-head"><h2>Why these three</h2></div>
      <div class="bars" style="gap:14px">
        <div><b>qwen/qwen3-vl-32b-instruct</b> — accuracy anchor. Native-resolution encoder, so a 200px crop is read at true detail rather than letterboxed. Best of the field here and the fastest.</div>
        <div><b>google/gemini-2.5-flash-lite</b> — decorrelation. Separate tiling pipeline and training corpus, highest MMMU of the cheap tier, strict JSON output.</div>
        <div><b>meta-llama/llama-4-scout</b> — third independent lineage, so the three sets of mistakes stay uncorrelated.</div>
        <div style="color:var(--cc-fg-3);font-size:13px">Dropped after testing: <b>mistral-small-3.2</b> — OpenRouter routes it to providers that answer image requests with “Image content is not supported by this model”, so a quarter of its calls failed here. Excluded on published evidence: the Gemma family (structured output broken on OpenRouter), qwen3.7-flash (#23 of 24 on vision evals despite being cheapest), qwen3-vl-235b (4.6× the output price, worse on reasoning). Judges are never asked to count axles — axle counting is the weakest measured VLM skill, so 2/3/multi-axle collapse to one Heavy_Truck answer and the detector keeps the axle split.</div>
      </div></div>

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Bake-off results</h2>
      <p>scored against the box verdicts you graded by hand — status: ${esc(b.status)}</p></div></div>
      ${ev.length ? `<table><thead><tr><th>Model</th><th class="right">Graded</th><th class="right">Accuracy</th>
        <th class="right">Cost</th><th class="right">$ / 1k crops</th><th class="right">Latency</th></tr></thead>
        <tbody>${ev.map(x => `<tr><td class="id">${esc(x.model)}</td><td class="right num">${x.n}</td>
          <td class="right num"><b>${(x.accuracy * 100).toFixed(1)}%</b></td>
          <td class="right num">${usd(x.cost)}</td><td class="right num">${usd(x.usd_per_1k)}</td>
          <td class="right num">${x.latency_ms} ms</td></tr>`).join('')}</tbody></table>`
        : `<div class="empty">No bake-off run yet. It cuts crops from your graded boxes and scores each candidate on accuracy per dollar.</div>`}
    </div>

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Vision models on OpenRouter</h2>
      <p>live pricing, cheapest first</p></div></div>
      <table><thead><tr><th style="width:44px"></th><th>Model</th><th class="right">$/M in</th>
        <th class="right">$/M out</th><th class="right">Context</th></tr></thead><tbody>${rows}</tbody></table></div>
  </div>`;
}

async function viewReview(runId) {
  const s = await api('/api/state');
  const run = (runId && s.runs.find(r => String(r.id) === String(runId)))
    || s.runs.find(r => (r.stages || []).some(x => x.stage === 'judge' && x.status === 'done'))
    || s.runs.find(r => r.status !== 'draft') || s.runs[0];
  if (!run) return `<div class="page"><h1>Review</h1><div class="card"><div class="empty">No runs yet.</div></div></div>`;
  const d = await api(`/api/review/${run.id}`);
  if (d.done) return `<div class="page"><div class="page-head"><div><h1>Review</h1>
    <p>Run ${run.id} · ${esc(run.name)}</p></div></div>
    <div class="card"><div class="empty">Nothing contested is waiting. ${d.reviewed || 0} crop(s) confirmed by hand.</div></div></div>`;
  const c = d.crop;
  return `<div class="page">
    <div class="page-head"><div><h1>Review</h1>
      <p>Run ${run.id} · ${d.remaining} contested crop(s) left — the judges disagreed on these.</p></div></div>
    <div class="card"><div class="card-head"><h2>What is this vehicle?</h2>
      <p>detector said <b>${esc(c.det_name)}</b> · track ${c.track_id} · frame ${c.frame}</p></div>
      <div class="crops">
        <img class="cropimg" src="/api/crop/${c.id}" alt="vehicle crop">
        <img class="ctximg" src="/api/crop/${c.id}?kind=ctx" alt="full frame context">
        <div style="min-width:220px">
          <div class="lbl" style="font-size:12px;color:var(--cc-fg-3);margin-bottom:8px">Judge votes</div>
          ${c.judgments.map(j => `<div class="event" style="padding:8px 12px;margin-bottom:6px">
            <div class="txt"><b>${esc(j.verdict_name || 'unparsed')}</b>
            <div style="font-size:12px;color:var(--cc-fg-3)">${esc(j.model.split('/').pop())}</div></div></div>`).join('')}
        </div>
      </div>
      <div class="classgrid">
        ${CLASSES.map((n, i) => `<button class="btn sm secondary" data-verdict="${c.id}" data-class="${i}">${n}</button>`).join('')}
        <button class="btn sm danger" data-verdict="${c.id}" data-class="-1">Not a vehicle</button>
      </div>
    </div></div>`;
}

/* one small inline chart — no library, and it survives the theme toggle */
function sparkline(curve, key, color) {
  if (!curve || curve.length < 2) return '';
  const W = 560, H = 120, pad = 6;
  const vals = curve.map(c => c[key] || 0);
  const max = Math.max(0.001, ...vals);
  const pts = vals.map((v, i) => {
    const x = pad + i * (W - 2 * pad) / (vals.length - 1);
    const y = H - pad - (v / max) * (H - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="120" role="img"
      aria-label="${key} over ${vals.length} epochs, latest ${vals[vals.length-1].toFixed(3)}">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

async function viewTrainingReport(tid) {
  const t = await api('/api/trainings/' + tid);
  const d = t.dataset_detail || {};
  const cfg = t.config || {};
  const keyCfg = ['model', 'imgsz', 'epochs', 'patience', 'batch', 'optimizer', 'lr0',
    'mosaic', 'close_mosaic', 'seed', 'device'];
  const mix = Object.entries(d.class_mix || {});
  const maxMix = Math.max(1, ...mix.map(m => m[1]));
  const hrs = t.finished && t.started ? ((t.finished - t.started) / 3600).toFixed(2)
    : ((Date.now() / 1000 - t.started) / 3600).toFixed(2);
  return `<div class="page">
    <div class="page-head"><div>
      <h1>Training #${t.id} · ${esc(t.tag)}</h1>
      <p>${statusPill(t.status)} · base ${esc(t.base_model || '?')} · ${esc(t.gpu || '?')} · started ${ago(t.started)}</p>
    </div><div class="head-actions">
      <button class="btn secondary" data-route="training">← All trainings</button>
    </div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico ok">${ic.judges}</div><div><div class="lbl">mAP50 (best)</div>
        <div class="val">${t.map50 ? t.map50.toFixed(3) : '—'}</div>
        <div class="sub">v3 was 0.647</div></div></div>
      <div class="tile"><div class="ico acc">${ic.review}</div><div><div class="lbl">mAP50-95</div>
        <div class="val">${t.map5095 ? t.map5095.toFixed(3) : '—'}</div>
        <div class="sub">precision ${t.precision ? t.precision.toFixed(2) : '—'}</div></div></div>
      <div class="tile"><div class="ico info">${ic.runs}</div><div><div class="lbl">Recall</div>
        <div class="val">${t.recall ? t.recall.toFixed(3) : '—'}</div>
        <div class="sub">v3 was 0.627</div></div></div>
      <div class="tile"><div class="ico warn">${ic.costs}</div><div><div class="lbl">GPU cost</div>
        <div class="val">${usd(t.cost_usd)}</div>
        <div class="sub">${hrs}h at ${usd(t.hourly)}/hr</div></div></div>
    </div>

    <div class="card"><div class="card-head"><h2>Progress</h2>
      <p>epoch ${t.epochs_done || 0} of ${t.epochs_planned || '?'} — mAP50 per epoch</p></div>
      ${sparkline(t.curve, 'map50', 'var(--cc-viz)') || '<div class="empty">No epochs yet.</div>'}
      ${t.curve && t.curve.length ? `<div class="legend"><span><i style="background:var(--cc-viz)"></i>mAP50 —
        latest ${t.curve[t.curve.length-1].map50.toFixed(3)}, best ${t.map50.toFixed(3)}</span></div>` : ''}
    </div>

    <div class="card"><div class="card-head"><h2>What went in</h2>
      <p>${esc(d.replication || '')}</p></div>
      <table><thead><tr><th>Source</th><th class="right">Train</th><th class="right">Val</th><th>Note</th></tr></thead>
      <tbody>${(d.sources || []).map(s => `<tr><td class="id">${esc(s.name)}</td>
        <td class="right num">${s.train}</td><td class="right num">${s.val}</td>
        <td>${esc(s.note || '')}</td></tr>`).join('')}
        <tr class="tot"><td class="id"><b>Total</b></td><td class="right num"><b>${t.n_train}</b></td>
        <td class="right num"><b>${t.n_val}</b></td><td></td></tr></tbody></table>
      ${d.why ? `<p style="margin-top:16px;color:var(--cc-fg-2)">${esc(d.why)}</p>` : ''}
      ${d.judging_cost_usd ? `<p style="color:var(--cc-fg-3);font-size:13px">Label verification for the new data cost ${usd(d.judging_cost_usd)} in judge calls.</p>` : ''}
    </div>

    <div class="grid g2">
      <div class="card"><div class="card-head"><h2>Class mix</h2><p>boxes in the training set</p></div>
        <div class="bars">${mix.map(([k, v]) => `<div class="barrow">
          <div class="k">${esc(k)}</div><div class="t"><i style="width:${100 * v / maxMix}%"></i></div>
          <div class="v">${v}</div></div>`).join('') || '<div class="empty">—</div>'}</div></div>

      <div class="card"><div class="card-head"><h2>Settings</h2><p>as recorded by the trainer itself</p></div>
        <table><tbody>${keyCfg.filter(k => cfg[k] !== undefined).map(k =>
          `<tr><td class="id">${esc(k)}</td><td class="right mono">${esc(cfg[k])}</td></tr>`).join('')
          || '<tr><td class="empty">config not read yet</td></tr>'}</tbody></table>
        ${t.pod_id ? `<p style="margin-top:12px;color:var(--cc-fg-3);font-size:12px">
          pod <span class="mono">${esc(t.pod_id)}</span> · weights <span class="mono">${esc(t.weights_path || '—')}</span></p>` : ''}
      </div>
    </div>

    ${t.notes ? `<div class="card"><div class="card-head"><h2>Notes</h2></div>
      <p style="color:var(--cc-fg-2)">${esc(t.notes)}</p></div>` : ''}
  </div>`;
}

async function viewTraining() {
  const [p, tr] = await Promise.all([api('/api/pods'), api('/api/trainings')]);
  const rows = (tr.trainings || []).map(t => `<tr>
    <td class="id">#${t.id} · ${esc(t.tag)}</td>
    <td>${statusPill(t.status)}</td>
    <td class="right num">${t.epochs_done || 0}/${t.epochs_planned || '?'}</td>
    <td class="right num">${t.map50 ? t.map50.toFixed(3) : '—'}</td>
    <td class="right num">${t.recall ? t.recall.toFixed(3) : '—'}</td>
    <td class="right num">${usd(t.cost_usd)}</td>
    <td class="right"><button class="btn sm secondary" data-training="${t.id}">Report</button></td>
  </tr>`).join('');
  return `<div class="page">
    <div class="page-head"><div><h1>Training</h1>
      <p>Every fine-tune keeps its own record — what data went in, what settings, what came out, what it cost.</p></div></div>

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Training runs</h2>
      <p>newest first</p></div></div>
      <table><thead><tr><th>ID</th><th>Status</th><th class="right">Epochs</th>
        <th class="right">mAP50</th><th class="right">Recall</th><th class="right">Cost</th><th class="right"></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="empty">No trainings recorded yet.</td></tr>'}</tbody></table></div>

    <div class="page-head" style="margin-top:8px"><div><h1 style="font-size:20px">GPU</h1>
      <p>Live telemetry and the meter. Nothing here is assumed dead — termination is verified.</p></div>
      <div class="head-actions">
        <input class="field" id="podid" placeholder="pod id to track" style="width:220px">
        <button class="btn secondary" id="adopt">Track pod</button></div></div>

    <div class="grid g3">
      <div class="tile"><div class="ico ok">${ic.costs}</div><div><div class="lbl">RunPod balance</div>
        <div class="val">${p.balance.ok ? usd(p.balance.remaining) : '—'}</div></div></div>
      <div class="tile"><div class="ico acc">${ic.training}</div><div><div class="lbl">Live pods</div>
        <div class="val">${p.live.length}</div><div class="sub">${p.live.length ? 'meter running' : 'nothing billing'}</div></div></div>
      <div class="tile"><div class="ico info">${ic.runs}</div><div><div class="lbl">Burn rate</div>
        <div class="val">${usd(p.live.reduce((a, x) => a + (x.hourly || 0), 0))}<span style="font-size:14px">/hr</span></div></div></div>
    </div>

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Live on RunPod</h2>
      <p>everything on the account, whether the Lab started it or not</p></div></div>
      <table><thead><tr><th>Pod</th><th>GPU</th><th>Status</th><th class="right">GPU util</th>
        <th class="right">Uptime</th><th class="right">$/hr</th><th class="right">Action</th></tr></thead>
      <tbody>${p.live.map(x => {
        const t = p.tracked.find(t => t.pod_id === x.id && !t.terminated);
        return `<tr><td class="id">${esc(x.name || x.id)}<div style="font-size:12px;color:var(--cc-fg-3)" class="mono">${esc(x.id)}</div></td>
        <td>${esc(x.gpu)}</td><td>${statusPill(x.status || '?')}</td>
        <td class="right num">${x.gpu_util ?? '—'}%</td><td class="right num">${dur(x.uptime_s)}</td>
        <td class="right num">${usd(x.hourly)}</td>
        <td class="right">${t ? `<button class="btn sm danger" data-stop="${t.id}">Stop</button>`
          : `<button class="btn sm secondary" data-adopt="${esc(x.id)}">Track</button>`}</td></tr>`;
      }).join('') || '<tr><td colspan="7" class="empty">No pods running — nothing is billing.</td></tr>'}</tbody></table></div>

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Pod history</h2></div></div>
      <table><thead><tr><th>Pod</th><th>GPU</th><th>Purpose</th><th>Status</th><th class="right">Cost</th><th class="right">Ended</th></tr></thead>
      <tbody>${p.tracked.map(t => `<tr><td class="id mono">${esc(t.pod_id)}</td><td>${esc(t.gpu || '?')}</td>
        <td>${esc(t.purpose || '—')}</td><td>${statusPill(t.status)}</td>
        <td class="right num">${usd(t.cost_usd)}</td><td class="right">${t.terminated ? ago(t.terminated) : '—'}</td></tr>`).join('')
        || '<tr><td colspan="6" class="empty">No pods tracked yet.</td></tr>'}</tbody></table></div>

    <div class="card"><div class="card-head"><h2>Available GPUs</h2><p>on-demand price per hour</p></div>
      <div class="bars">${p.gpu_types.map(g => `<div class="barrow">
        <div class="k" title="${esc(g.name)}">${esc(g.name)}</div>
        <div class="t"><i style="width:${Math.min(100, (g.on_demand / 4) * 100)}%"></i></div>
        <div class="v">${usd(g.on_demand)}</div></div>`).join('') || '<div class="empty">Could not read GPU catalog.</div>'}</div></div>
  </div>`;
}

async function viewCosts() {
  const c = await api('/api/costs');
  const max = Math.max(1, ...c.by_stage.map(x => x.usd || 0));
  return `<div class="page">
    <div class="page-head"><div><h1>Costs</h1>
      <p>Every charge the Lab has made, booked the moment it happened.</p></div></div>
    <div class="card"><div class="card-head"><h2>By stage</h2></div>
      <div class="bars">${c.by_stage.map(x => `<div class="barrow">
        <div class="k">${esc(x.stage || '—')} · ${esc(x.provider)}</div>
        <div class="t"><i style="width:${100 * (x.usd || 0) / max}%"></i></div>
        <div class="v">${usd(x.usd)}</div></div>`).join('') || '<div class="empty">No spend recorded.</div>'}</div></div>
    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><h2>Ledger</h2>
      <p>most recent first</p></div></div>
      <table><thead><tr><th>When</th><th>Run</th><th>Stage</th><th>Item</th><th class="right">Qty</th><th class="right">USD</th></tr></thead>
      <tbody>${c.items.slice(0, 60).map(i => `<tr><td>${ago(i.ts)}</td><td class="num">${i.run_id || '—'}</td>
        <td>${esc(i.stage || '—')}</td><td class="id" style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc(i.item)}</td>
        <td class="right num">${(i.qty || 0).toFixed(2)} ${esc(i.unit || '')}</td>
        <td class="right num">${usd(i.usd)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">Empty ledger.</td></tr>'}</tbody></table></div>
  </div>`;
}

async function viewSettings() {
  const s = await api('/api/settings');
  return `<div class="page">
    <div class="page-head"><div><h1>Settings</h1><p>Keys stay on this machine, in the Lab database.</p></div></div>
    <div class="card"><div class="card-head"><h2>API keys</h2></div>
      <div class="formrow">
        <div><label class="lbl">OpenRouter key ${s.openrouter_key_set ? `<span class="status ok">set ···${esc(s.openrouter_key_tail)}</span>` : '<span class="status bad">missing</span>'}</label>
          <input class="field" id="k_or" type="password" placeholder="sk-or-v1-..."></div>
        <div><label class="lbl">RunPod key ${s.runpod_key_set ? `<span class="status ok">set ···${esc(s.runpod_key_tail)}</span>` : '<span class="status bad">missing</span>'}</label>
          <input class="field" id="k_rp" type="password" placeholder="rpa_..."></div>
        <div><button class="btn primary" id="savekeys">Save keys</button></div>
      </div></div>
    <div class="card"><div class="card-head"><h2>Guard rails</h2>
      <p>the Lab stops itself before these are exceeded</p></div>
      <div class="formrow">
        <div><label class="lbl">Judge budget per run (USD)</label>
          <input class="field" id="s_budget" value="${esc(s.judge_budget_usd || '1.5')}"></div>
        <div><label class="lbl">Auto-stop an idle pod after (minutes at &lt;5% GPU)</label>
          <input class="field" id="s_idle" value="${esc(s.idle_stop_minutes || '20')}"></div>
        <div><button class="btn primary" id="saveguards">Save guard rails</button></div>
      </div></div>
  </div>`;
}



/* ═══════════════════════════════ datasets ═══════════════════════════════
   A dataset is identified by a hash of its labels plus an inventory of its images, so
   two trainings are only comparable when that hash matches. Weights are archived and
   never overwritten — a bad dataset should cost one training, not the ability to go back. */
async function viewDatasets() {
  const [{ datasets }, { weights, verify }] = await Promise.all([
    api('/api/datasets'), api('/api/weights')]);
  const probs = (verify || {}).problems || [];

  const dsCard = d => {
    const mix = Object.entries(d.class_mix || {})
      .map(([k, v]) => [CLASSES[k] || k, v]).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...mix.map(m => m[1]));
    return `<div class="card">
      <div class="card-head"><div>
        <h2 style="font-size:17px">${esc(d.name)}</h2>
        <p>${num(d.n_train)} train · ${num(d.n_val)} val · ${num(d.n_boxes)} boxes
           ${d.exists ? '' : ' · <span class="status bad">files missing</span>'}</p></div>
        <span class="chip mono" title="hash of the labels plus the image inventory">${esc(d.fingerprint)}</span></div>
      ${d.note ? `<p style="color:var(--cc-fg-2);margin:0 0 14px">${esc(d.note)}</p>` : ''}
      <div class="bars">${mix.slice(0, 8).map(([k, v]) => `<div class="barrow">
        <div class="k">${esc(k)}</div><div class="t"><i style="width:${100 * v / max}%"></i></div>
        <div class="v">${num(v)}</div></div>`).join('')}</div>
      ${mix.length > 8 ? `<p style="color:var(--cc-fg-3);font-size:12px;margin:10px 0 0">
        + ${mix.length - 8} more classes</p>` : ''}
      <p style="color:var(--cc-fg-3);font-size:12px;margin:14px 0 0">
        labels from ${esc(d.label_source || 'unrecorded')} ·
        ${d.trainings && d.trainings.length
          ? `used by ${d.trainings.map(t => `<a href="#trainingrun/${t.id}">#${t.id} ${esc(t.tag)}</a>`).join(', ')}`
          : 'not yet trained on'}</p>
      <p class="mono" style="color:var(--cc-fg-off);font-size:11px;margin:6px 0 0">${esc(d.path)}</p>
    </div>`;
  };

  return `<div class="page">
    <div class="page-head"><div><h1>Datasets</h1>
      <p>Every dataset the Lab has built, and every model weight kept for comparison.</p></div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico acc">${ic.datasets}</div><div><div class="lbl">Datasets</div>
        <div class="val">${datasets.length}</div></div></div>
      <div class="tile"><div class="ico info">${ic.review}</div><div><div class="lbl">Labelled boxes</div>
        <div class="val">${num(datasets.reduce((a, d) => a + (d.n_boxes || 0), 0))}</div></div></div>
      <div class="tile"><div class="ico ok">${ic.training}</div><div><div class="lbl">Archived weights</div>
        <div class="val">${weights.length}</div><div class="sub">previous versions kept</div></div></div>
      <div class="tile"><div class="ico ${probs.length ? 'warn' : 'ok'}">${ic.judges}</div><div>
        <div class="lbl">Archive integrity</div>
        <div class="val">${probs.length ? probs.length : 'OK'}</div>
        <div class="sub">${probs.length ? 'file(s) missing or altered' : 'every file re-hashed and matching'}</div></div></div>
    </div>

    ${probs.length ? `<div class="err"><div><b>The weight archive does not match its records.</b>
      <div style="font-size:12px;margin-top:4px">${probs.map(p =>
        `${esc(p.tag)}: ${esc(p.problem)}`).join(' · ')}</div></div></div>` : ''}

    ${datasets.length ? `<div class="grid g2">${datasets.map(dsCard).join('')}</div>`
      : `<div class="card">${empty('No datasets yet',
          'Build one from the dataset node at the end of a run pipeline.')}</div>`}

    <div class="card tight"><div style="padding:24px 24px 0"><div class="card-head"><div>
      <h2>Model archive</h2>
      <p>Weights are copied here when a training finishes and never overwritten.</p></div>
      <button class="btn sm secondary" id="verifyw">Re-verify files</button></div></div>
      <table><thead><tr><th>Version</th><th>Trained on</th><th class="right">mAP50</th>
        <th class="right">Recall</th><th class="right">Size</th><th>Checksum</th><th></th></tr></thead>
      <tbody>${weights.map(w => `<tr>
        <td class="id">${esc(w.tag || '—')}${w.is_active ? ' <span class="status ok">in use</span>' : ''}
          ${w.exists ? '' : ' <span class="status bad">missing</span>'}</td>
        <td>${w.training_id ? `<a href="#trainingrun/${w.training_id}">training #${w.training_id}</a>` : '—'}</td>
        <td class="right num">${w.map50 ? w.map50.toFixed(3) : '—'}</td>
        <td class="right num">${w.recall ? w.recall.toFixed(3) : '—'}</td>
        <td class="right num">${w.size_mb} MB</td>
        <td class="mono" style="font-size:11px">${esc((w.sha256 || '').slice(0, 12))}</td>
        <td>${esc(w.note || '')}</td></tr>`).join('')
        || '<tr><td colspan="7" class="empty">No weights archived yet.</td></tr>'}</tbody></table></div>
  </div>`;
}
const empty = (h, b) => `<div class="empty"><h3 style="margin:0 0 6px">${esc(h)}</h3><p style="margin:0">${esc(b)}</p></div>`;

/* ═══════════════════════════ annotated preview ═══════════════════════════
   Watching the render is how a wrong line or a swallowed vehicle is actually noticed —
   the numbers alone never show it. This one IS a job (decode every frame, draw, re-encode:
   60–110s for a 15-minute clip), so unlike counting it gets real progress. */
let RENDER_POLL = null;

async function viewPreview(videoId) {
  const [st, card] = await Promise.all([
    api('/api/render/' + videoId),
    api('/api/reportcard/' + videoId).catch(() => ({})),
  ]);
  const v = await api('/api/scene/' + videoId);
  const job = st.job || {};
  const running = ['queued', 'running'].includes(job.status);
  return `<div class="page" style="gap:12px">
    <div class="page-head"><div>
      <h1 style="font-size:22px;line-height:30px">Preview — ${esc(v.video.name)}</h1>
      <p>${st.exists ? `rendered ${ago(st.made)} · ${st.size_mb} MB` : 'not rendered yet'}
         ${card.total ? ` · ${num(card.total)} vehicles counted` : ''}</p></div>
      <div class="head-actions">
        ${WS && WS.id ? `<button class="btn secondary" data-route="station" data-arg="${WS.id}/clips">← Station ${WS.id}</button>` : ''}
        <button class="btn secondary" data-scene="${videoId}">Edit line</button>
        <button class="btn secondary" data-reportcard="${videoId}">Report card</button>
        <button class="btn primary" data-render="${videoId}" ${running ? 'disabled' : ''}>
          ${st.exists ? 'Re-render' : 'Render'}${running ? '…' : ''}</button>
      </div></div>

    ${!st.has_line ? `<div class="err"><div><b>No count line on this video.</b>
      <div style="font-size:12px;margin-top:4px">The render will still show detections and
      tracks, but no line and no crossing counts.</div></div></div>` : ''}

    <div class="card" id="renderprogress" ${running ? '' : 'hidden'}>
      <div class="card-head"><div><h2 style="font-size:17px">Rendering</h2>
        <p>decoding every frame, drawing boxes and the count line, then re-encoding</p></div></div>
      <div class="stage" data-s="${esc(job.status || 'queued')}">
        <div class="nm">${esc(job.status || 'queued')}</div>
        <div class="bar"><i style="width:${job.progress || 0}%"></i></div>
        <div class="msg">${esc(job.message || 'starting…')}</div>
        <div class="cost">${Math.round(job.progress || 0)}%</div>
      </div>
      <p style="margin:12px 0 0;color:var(--cc-fg-3);font-size:12px">
        Past renders on this footage took 60–110 seconds. You can leave this page — the job
        keeps running and the video will be here when you come back.</p>
    </div>

    ${st.exists ? `<div class="card" style="padding:0;overflow:hidden">
        <video id="annvid" controls preload="metadata" playsinline
               style="width:100%;display:block;background:#000;max-height:70vh"
               src="/api/annotated/${videoId}"></video>
      </div>
      <p style="margin:0;color:var(--cc-fg-3);font-size:12px">
        Boxes are the tracked vehicles, the line is yours, and the sidebar tallies each
        class as it crosses. If a vehicle passes without the tally moving, that is a
        counting problem you can see rather than infer.</p>`
      : `<div class="card">${empty('Nothing rendered yet',
          'Render to watch the detections, tracks and crossings on the footage itself.')}</div>`}
  </div>`;
}

function pollRender(videoId) {
  clearInterval(RENDER_POLL);
  RENDER_POLL = setInterval(async () => {
    const onRenderPage = location.hash.startsWith('#preview/')
      || location.hash.startsWith('#scene/');
    if (!onRenderPage) { clearInterval(RENDER_POLL); return; }
    try {
      const st = await api('/api/render/' + videoId);
      const job = st.job || {};
      const box = $('#renderprogress');
      if (!box) return;
      const bar = box.querySelector('.bar i'), msg = box.querySelector('.msg');
      const nm = box.querySelector('.nm'), pct = box.querySelector('.cost');
      if (bar) bar.style.width = (job.progress || 0) + '%';
      if (msg) msg.textContent = job.message || '';
      if (nm) nm.textContent = job.status || '';
      if (pct) pct.textContent = Math.round(job.progress || 0) + '%';
      box.querySelector('.stage')?.setAttribute('data-s', job.status || '');
      if (!['queued', 'running'].includes(job.status)) {
        clearInterval(RENDER_POLL);
        toast(job.status === 'done' ? 'Render finished' : 'Render failed: ' + (job.message || ''),
              job.status !== 'done');
        route(true);
      }
    } catch { /* transient */ }
  }, 2000);
}

/* ═══════════════════════════════ counts ═══════════════════════════════
   Counting had no door: the line editor and the report card could only be reached from
   each other. This is the list of every extracted video and where it stands. */
async function viewCounts() {
  const [{ videos }, all, disk] = await Promise.all([
    api('/api/countable-videos').catch(() => ({ videos: [] })),
    api('/api/all-videos').catch(() => ({ videos: [] })),
    api('/api/annotated-usage').catch(() => ({ files: [], total_mb: 0, orphans: [], temps: [] })),
  ]);
  const withLine = new Set(videos.map(v => v.id));
  const rendered = new Map((disk.files || []).filter(f => f.video_id).map(f => [f.video_id, f]));
  const rows = (all.videos || []).map(v => {
    const lined = withLine.has(v.id);
    return `<tr>
      <td class="id">${esc(v.name)}
        <div style="font-size:12px;color:var(--cc-fg-3)">${esc(v.start_clock || 'clock not set')}
          ${v.station ? ' · ' + esc(v.station) : ''}</div></td>
      <td class="right num">${v.frames ? num(v.frames) : '—'}</td>
      <td>${lined ? `<span class="status ok">${v.n_lines} line(s)</span>`
                  : '<span class="status warn">no line</span>'}</td>
      <td class="right num">${v.counted != null ? num(v.counted) : '—'}</td>
      <td class="right num">${rendered.has(v.id)
        ? `<span title="rendered ${ago(rendered.get(v.id).made)}">${rendered.get(v.id).mb} MB</span>`
        : '<span style="color:var(--cc-fg-off)">—</span>'}</td>
      <td class="right">
        <button class="btn sm ${lined ? 'ghost' : 'primary'}" data-scene="${v.id}">
          ${lined ? 'Edit line' : 'Draw line'}</button>
        ${lined ? `<button class="btn sm secondary" data-reportcard="${v.id}">Report card</button>` : ''}
        <button class="btn sm secondary" data-preview="${v.id}">Preview</button>
        ${rendered.has(v.id) ? `<button class="btn sm ghost" data-delrender="${v.id}"
          title="frees ${rendered.get(v.id).mb} MB — a re-render takes about a minute">✕ render</button>` : ''}
      </td></tr>`;
  }).join('');
  const nLined = (all.videos || []).filter(v => withLine.has(v.id)).length;
  const total = (all.videos || []).reduce((a, v) => a + (v.counted || 0), 0);
  return `<div class="page">
    <div class="page-head"><div><h1>Counts</h1>
      <p>Counting is a query over trajectories that are already stored — it takes well under
         a second and re-runs nothing. Draw a line, then read the count.</p></div></div>

    <div class="grid g3">
      <div class="tile"><div class="ico acc">${ic.videos}</div><div><div class="lbl">Extracted videos</div>
        <div class="val">${(all.videos || []).length}</div>
        <div class="sub">${nLined} with a count line</div></div></div>
      <div class="tile"><div class="ico ok">${ic.counts}</div><div><div class="lbl">Vehicles counted</div>
        <div class="val">${num(total)}</div></div></div>
      <div class="tile"><div class="ico ${(all.videos || []).length - nLined ? 'warn' : 'ok'}">${ic.judges}</div>
        <div><div class="lbl">Waiting on a line</div>
        <div class="val">${(all.videos || []).length - nLined}</div>
        <div class="sub">cannot be counted until one is drawn</div></div></div>
    </div>

    <div class="card" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:280px">
        <b style="font-weight:500">Rendered videos are using ${disk.total_mb} MB</b>
        <p style="margin:4px 0 0;color:var(--cc-fg-2);font-size:13px">
          Renders are derived — about a minute of CPU rebuilds any of them exactly, so they
          are the cheapest thing here to delete and the most expensive to keep.
          ${disk.orphans.length || disk.temps.length
            ? `<b>${disk.orphans.length} orphan(s)</b> and ${disk.temps.length} interrupted
               temp file(s) can go right now.` : ''}</p>
      </div>
      <button class="btn secondary" id="cleanrenders"
        ${disk.orphans.length || disk.temps.length ? '' : 'disabled title="nothing orphaned"'}>
        Clean up orphans</button>
    </div>

    <div class="card tight"><table>
      <thead><tr><th>Video</th><th class="right">Frames</th><th>Line</th>
        <th class="right">Counted</th><th class="right">Render</th><th class="right"></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="empty">Nothing extracted yet — run a pipeline first.</td></tr>'}</tbody>
    </table></div>
  </div>`;
}

/* ═══════════════════ counting layer: line, count, report card ═══════════════════
   The Lab could diagnose a count but not produce one — the line lived in the other app.
   These three screens close the loop: draw the line, run the count, read the card. */
let LINE_ED = null, LINE_CTX = null;

async function viewScene(videoId) {
  const [d, rs] = await Promise.all([
    api('/api/scene/' + videoId),
    api('/api/render/' + videoId).catch(() => ({})),
  ]);
  LINE_CTX = { videoId, ...d };
  const v = d.video;
  const busy = ['queued', 'running'].includes((rs.job || {}).status);
  return `<div class="page" style="gap:12px">
    <div class="page-head"><div>
      <h1 style="font-size:22px;line-height:30px">Count line — ${esc(v.name)}</h1>
      <p>${v.width}×${v.height} · ${v.fps} fps · ${esc(v.start_clock || 'clock not set')}
         · ${d.lines.length} line(s) drawn</p></div>
      <div class="head-actions">
        ${WS && WS.id ? `<button class="btn secondary" data-route="station" data-arg="${WS.id}/clips">← Station ${WS.id}</button>` : '<button class="btn secondary" data-route="stations">All stations</button>'}
        <button class="btn secondary" data-render="${videoId}"
          ${!d.lines.length || busy ? 'disabled' : ''}
          title="${busy ? 'a render is already running'
                        : d.lines.length ? 'draws this line onto the footage — 60–110 seconds'
                                         : 'draw a line first'}">
          ${busy ? 'Rendering…' : rs.exists ? 'Re-render video' : 'Render video'}</button>
        <button class="btn primary" data-reportcard="${videoId}"
          ${d.lines.length ? '' : 'disabled title="draw a line first — there is nothing to count without one"'}>
          Count now →</button>
      </div></div>
    <div class="card"><div id="lineeditor"></div></div>

    ${rs.stale ? `<div class="err"><div>
      <b>The rendered video still shows the old line.</b>
      <div style="font-size:12px;margin-top:4px">It was made ${ago(rs.made)}, before this line
      was last changed. The count is already up to date — only the video is behind.
      Re-render to watch the current line.</div></div></div>` : ''}

    <div class="card" id="renderprogress" ${busy ? '' : 'hidden'}>
      <div class="card-head"><div><h2 style="font-size:17px">Rendering</h2>
        <p>drawing the boxes and this line onto the footage, then re-encoding</p></div></div>
      <div class="stage" data-s="${esc((rs.job || {}).status || 'queued')}">
        <div class="nm">${esc((rs.job || {}).status || 'queued')}</div>
        <div class="bar"><i style="width:${(rs.job || {}).progress || 0}%"></i></div>
        <div class="msg">${esc((rs.job || {}).message || 'starting…')}</div>
        <div class="cost">${Math.round((rs.job || {}).progress || 0)}%</div>
      </div>
    </div>

    <div class="grid g2">
      <div class="card" style="border-left:3px solid var(--cc-ok);padding:16px 20px">
        <b style="font-weight:500">Count now — instant</b>
        <p style="margin:4px 0 0;color:var(--cc-fg-2);font-size:13px">
          Reads the stored trajectories against your line and builds the report card in
          well under a second. Nothing re-runs, so you can move the line and re-read the
          count as often as you like.</p></div>
      <div class="card" style="border-left:3px solid var(--cc-warn);padding:16px 20px">
        <b style="font-weight:500">Render video — 60 to 110 seconds</b>
        <p style="margin:4px 0 0;color:var(--cc-fg-2);font-size:13px">
          Draws every box, track and this line onto the footage. Worth it to <i>see</i> a
          missed or double-counted vehicle rather than infer it from a number. The job keeps
          running if you leave this page.</p></div>
    </div>
  </div>`;
}


/** Repaint the sub-tab counts from the station data currently in memory. */
function refreshWsTabs() {
  if (!WS) return;
  const s = WS.s;
  const counts = {
    line: (s.default_line || []).length,
    footage: (s.footage || []).filter(f => !f.dup_of).length,
    runs: (s.runs || []).length, counts: (s.videos || []).length,
    labels: (s.gold || {}).verdicts || 0,
    datasets: (s.datasets || []).length, models: (s.models || []).length,
  };
  document.querySelectorAll('[data-wstab]').forEach(b => {
    const n = counts[b.dataset.wstab];
    const badge = b.querySelector('.n');
    if (n == null) return;
    if (badge) badge.textContent = n;
    else b.insertAdjacentHTML('beforeend', `<span class="n">${n}</span>`);
  });
}

async function mountLineEditor_() {
  const el = $('#lineeditor');
  // Same trap as the gold labeller: navigating scene -> scene remounts without tearing
  // the old one down, leaving its document-level key handler alive.
  if (LINE_ED && LINE_ED.destroy) LINE_ED.destroy();
  LINE_ED = null;
  if (!el || !LINE_CTX) return;
  const { mountLineEditor } = await import('/shared/lineeditor.js');
  LINE_ED = mountLineEditor(el, {
    onSave: async lines => {
      try { await post(`/api/scene/${LINE_CTX.videoId}`, { lines }); }
      catch (e) { toast(e.message, true); }
    },
  });
  LINE_ED.load(LINE_CTX.videoId, LINE_CTX.lines, 0);
}

/* Clip-level verification — the screen where a survey is made correct.

   Every vehicle here is one the report counts, shown at its clearest frame, with the
   answer list carrying classes AND attributes together: a bus and "is it APSRTC" are one
   question about one vehicle, and splitting them into two passes means the second never
   happens. A verdict writes through to the count immediately; the clips table and the
   report card both read the same tracks, so the number moves as you answer. */
let VQ = null, VKEYS = null, VERIFY_CLIP = null;
let CRUMB_CTX = null;

function paintCrumb() {
  if (!CRUMB_CTX || !window.CRUMBS) return;
  const [name, arg, extra] = CRUMB_CTX;
  const parts = window.CRUMBS[name]
    ? window.CRUMBS[name](arg, extra)
    : [[(NAV.find(n => n.id === name) || {}).label || 'Overview', null]];
  const el = $('#crumb');
  if (!el) return;
  el.innerHTML = parts.map(([label, href], i) =>
    (href ? `<a href="${href}">${esc(label)}</a>` : `<span>${esc(label)}</span>`)
    + (i < parts.length - 1 ? ' <em>›</em> ' : '')).join('');
}

async function viewVerify(videoId) {
  const q = await api(`/api/verify/${videoId}?cls=&mandatory=0`).catch(() => ({}));
  const c = q.clip || {};
  VERIFY_CLIP = c;
  paintCrumb();          // now that the clip is known, the crumb can name it
  const where = c.station_code
    ? `${c.station_code} · ${c.station || ''}` : `clip ${videoId}`;
  const when = c.clock ? `${c.clock}–${c.end_clock || '?'} on ${c.date}` : '';
  return `<div class="page">
    <div class="page-head"><div>
      <h1>Verify — ${esc(when || ('clip ' + videoId))}</h1>
      <p>${esc(where)}${c.name ? ` · ${esc(c.name)}` : ''} · ${c.minutes || '?'} min ·
         clip ${videoId}. Every vehicle the count reports, at its clearest frame —
         your answer changes the number immediately.</p></div>
      <div class="head-actions">
        <button class="btn secondary" data-route="station" data-arg="${
          c.site_id || (WS && WS.id) || ''}/clips">← ${esc(c.station_code || 'Station')}</button>
        <button class="btn secondary" data-reportcard="${videoId}">Report card</button>
      </div></div>
    <div id="vfyBody"><div class="card"><div class="card-body muted">Loading…</div></div></div>
  </div>`;
}

async function mountVerify(videoId) {
  const el = document.getElementById('vfyBody');
  if (!el) return;
  let filterClass = '', mandOnly = false, doneOnly = false, i = 0;

  async function load() {
    const q = await api(`/api/verify/${videoId}?cls=${encodeURIComponent(filterClass)}`
      + `&mandatory=${mandOnly ? 1 : 0}`
      + (doneOnly ? '&answered=1' : '')).catch(e => ({ error: String(e) }));
    if (q.error) { el.innerHTML = `<div class="card">${empty('Cannot verify', q.error)}</div>`; return; }
    VQ = q;
    // In a re-verification pass every item already has a verdict, so "first unanswered"
    // would skip straight to the end. Start at the top instead.
    i = doneOnly ? 0 : q.items.findIndex(x => !x.verdict);
    if (i < 0) i = q.items.length;
    draw();
  }

  function draw() {
    const A = VQ.answers, it = VQ.items[i];
    const head = `
      <div class="grid g4" style="margin-bottom:12px">
        <div class="tile"><div class="ico info">${ic.overview}</div><div>
          <div class="lbl">Counted vehicles</div><div class="val">${num(VQ.total)}</div>
          <div class="sub">${num(VQ.answered)} verified</div></div></div>
        <div class="tile"><div class="ico ${VQ.mandatory_left ? 'warn' : 'ok'}">${ic.judges}</div><div>
          <div class="lbl">Must be reviewed</div><div class="val">${num(VQ.mandatory_left)}</div>
          <div class="sub">of ${num(VQ.mandatory)} flagged</div></div></div>
        <div class="tile"><div class="ico acc">${ic.runs}</div><div>
          <div class="lbl">Showing</div><div class="val">${num(VQ.items.length)}</div>
          <div class="sub">${filterClass || 'all classes'}${mandOnly ? ' · mandatory only' : ''}</div></div></div>
        <div class="tile"><div class="ico ok">${ic.training}</div><div>
          <div class="lbl">Position</div><div class="val">${Math.min(i + 1, VQ.items.length)}</div>
          <div class="sub">of ${VQ.items.length}</div></div></div>
      </div>
      <div class="card" style="margin-bottom:12px"><div class="card-body vfy-filters">
        <label class="lbl">Class</label>
        <select class="field sm" id="vfyClass">
          <option value="">All classes (${num(VQ.total)})</option>
          ${VQ.classes.map(([c, n]) => `<option value="${esc(c)}"${
            c === filterClass ? ' selected' : ''}>${esc(c)} (${n})</option>`).join('')}
        </select>
        <label class="chk"><input type="checkbox" id="vfyMand"${mandOnly ? ' checked' : ''}>
          Only vehicles that must be reviewed</label>
        ${/* Re-verification pass. The same vehicle, the same row — answering again
              overwrites the one verdict and appends to its change log, so nothing is
              listed or counted twice. */''}
        <label class="chk"><input type="checkbox" id="vfyDone"${doneOnly ? ' checked' : ''}>
          Only ones I have already answered</label>
        <span style="flex:1"></span>
        <span class="muted">heavy vehicles, judge disagreements and low-confidence
          axle calls are flagged automatically</span>
      </div></div>`;

    if (!it) {
      el.innerHTML = head + `<div class="card">${empty('Nothing left in this view',
        mandOnly ? 'Every mandatory vehicle in this clip has been settled.'
                 : 'Every vehicle matching the filter has been answered.')}</div>`;
      wireFilters(); return;
    }

    el.innerHTML = head + `
      <div class="card"><div class="vfy-stage">
        <div class="vfy-imgs">
          <img src="/api/verify/${videoId}/${it.track_id}/crop.jpg" alt="vehicle">
          <img class="ctx" src="/api/verify/${videoId}/${it.track_id}/ctx.jpg" alt="in frame">
        </div>
        <div class="vfy-tag">
          <b>${it.reclassed ? 'You set:' : 'AI says:'} ${esc(it.class)}</b>
          <span class="muted">${esc(it.clock)} · ${esc(it.direction)} · ${it.box_w}px · track ${it.track_id}</span>
          ${it.mandatory ? `<span class="chip warn">${esc(it.reasons.join(' · '))}</span>` : ''}
          ${it.reclassed ? `<span class="chip ok">reclassified — add an attribute below,
            or press Enter to move on</span>` : ''}
          ${it.verdict ? `<span class="chip">you answered <b>${esc(it.verdict)}</b>${
            it.revisions ? ` · changed ${it.revisions} time(s)` : ''} — answering again
            replaces it</span>` : ''}
        </div>
        <div class="vfy-answers">
          <button class="btn ok big" data-ans="${esc(it.class)}">${
            it.reclassed ? `✓ Done — ${esc(it.class)}` : `✓ Correct — ${esc(it.class)}`
          } <kbd>Enter</kbd></button>
          ${/* An attribute is a toggle, not a one-way stamp. It is the only answer that
                is invisible in the class — a car marked Taxi still reads "Car_Jeep_Van" —
                so the button has to carry its own state, and pressing it again has to
                take it back. Without that a mis-press was permanent, and the report card
                went on counting a taxi the reviewer had already changed their mind about. */''}
          ${A.attributes.filter(a => a.parents.includes(it.class)).map(a => {
            const on = (it.attrs || []).includes(a.key);
            return `<button class="btn ${on ? 'ok' : 'acc'}" data-ans="${esc(a.key)}"
              title="${on ? 'Press to remove this mark' : 'Mark this vehicle'}">${
              on ? `✓ ${esc(a.label)} — press to undo` : esc(a.label)}</button>`;
          }).join('')}
          <button class="btn danger" data-ans="not_a_vehicle">✗ Not a vehicle</button>
          <button class="btn secondary" data-ans="unclear">Can't tell</button>
          ${/* Browsing, not answering. Back/Next only move the cursor — nothing is
                written until an answer button is pressed, so a re-verification pass
                can be walked end to end leaving every verdict as it was. */''}
          <button class="btn ghost" data-back${i ? '' : ' disabled'}>‹ Back</button>
          <button class="btn ghost" data-skip${
            i >= VQ.items.length - 1 ? ' disabled' : ''}>Next ›</button>
        </div>
        <div class="vfy-classes">${A.classes.map((c, n) =>
          `<button class="btn sm ${c === it.class ? 'secondary' : 'ghost'}" data-ans="${esc(c)}">
            ${n < 9 ? `<kbd>${n + 1}</kbd> ` : ''}${esc(c)}</button>`).join('')}</div>
      </div></div>`;

    el.querySelectorAll('[data-ans]').forEach(b => b.onclick = () => answer(b.dataset.ans));
    const bk = el.querySelector('[data-back]');
    if (bk) bk.onclick = () => { i = Math.max(0, i - 1); draw(); };
    const sk = el.querySelector('[data-skip]');
    if (sk) sk.onclick = () => { i = Math.min(VQ.items.length - 1, i + 1); draw(); };
    const nx = VQ.items[i + 1];
    if (nx) new Image().src = `/api/verify/${videoId}/${nx.track_id}/crop.jpg`;
    wireFilters();
  }

  function wireFilters() {
    const c = document.getElementById('vfyClass');
    if (c) c.onchange = () => { filterClass = c.value; load(); };
    const m = document.getElementById('vfyMand');
    if (m) m.onchange = () => { mandOnly = m.checked; load(); };
    const dn = document.getElementById('vfyDone');
    if (dn) dn.onchange = () => { doneOnly = dn.checked; load(); };
  }

  async function answer(a) {
    const it = VQ.items[i];
    if (!it) return;

    // Reclassifying into a class that carries an attribute must NOT advance. A bus the
    // detector called a car is two facts — it is a Bus, and it is an APSRTC bus — and
    // advancing after the first makes the second unreachable without coming back round.
    const opens = VQ.answers.classes.includes(a)
      && VQ.answers.attributes.some(x => x.parents.includes(a))
      && a !== it.class;

    const prev = it.class;
    if (opens) { it.class = a; it.reclassed = true; }
    else { it.verdict = a; i++; }
    draw();
    try { await post(`/api/verify/${videoId}`, { track_id: it.track_id, answer: a }); }
    catch (e) {
      if (opens) { it.class = prev; it.reclassed = false; }
      else { it.verdict = null; i = VQ.items.indexOf(it); }
      draw();
      toast(`Not saved: ${e.message}`, true);
    }
  }

  VKEYS = ev => {
    if (!document.getElementById('vfyBody') || !VQ) return;
    const t = ev.target.tagName;
    if (t === 'INPUT' || t === 'SELECT') return;
    const it = VQ.items[i];
    if (!it) return;
    if (ev.key === 'Enter') { ev.preventDefault(); answer(it.class); return; }
    if (ev.key === 'x' || ev.key === 'X') { ev.preventDefault(); answer('not_a_vehicle'); return; }
    if (ev.key === 'ArrowLeft') { i = Math.max(0, i - 1); draw(); return; }
    if (ev.key === 'ArrowRight') { i = Math.min(VQ.items.length - 1, i + 1); draw(); return; }
    const n = parseInt(ev.key, 10);
    if (n >= 1 && n <= 9) { ev.preventDefault(); answer(VQ.answers.classes[n - 1]); }
  };
  document.addEventListener('keydown', VKEYS);
  load();
}

/* Labelling for the fine-grained attributes — axle class, bus operator, auto size, car use.

   Built for throughput, because the labels are the bottleneck: 419 axle crops are waiting
   and the whole subsystem is blocked until a few hundred are answered. So one big image at
   a time, number keys for the answers, and the next card straight after — no scrolling, no
   Save, no confirmation dialog.

   The crop is shown WITHOUT the label the dataset already carries. That label is used to
   choose which crops to show (otherwise the queue is 300 identical common trucks before
   the first multi-axle rig), but showing it would anchor the answer, and an anchored label
   is worth less than no label — it re-confirms the bias instead of measuring it. */
let ATTR_Q = null;

async function viewAttrs() {
  const d = await api('/api/attrs').catch(() => ({ attributes: [] }));
  const A = d.attributes || [];
  return `<div class="page">
    <div class="page-head"><div><h1>Attributes</h1>
      <p>The questions the detector cannot answer: which axle class a truck is, whether a
         bus is APSRTC, whether an auto is a 7-seater, whether a car is a taxi.</p></div></div>
    <div class="lv-table"><table><thead><tr>
      <th>Question</th><th>Applies to</th><th>Answered by</th>
      <th class="right">Labelled</th><th>Status</th><th></th>
    </tr></thead><tbody>
      ${A.map(a => `<tr>
        <td><strong>${esc(a.label)}</strong><div class="muted">${esc(a.hint)}</div></td>
        <td>${a.parents.map(esc).join(', ')}<div class="muted">${
              a.kind === 'class' ? 'replaces the class' : 'adds an attribute'}</div></td>
        <td>${{model: 'trained model', human: 'a person, every one',
               undecided: 'not decided yet'}[a.mode] || 'a person'}</td>
        <td class="right">${num(a.usable)}${a.mode === 'model' ? ` / ${num(a.min_labels)}` : ''}</td>
        <td>${a.mode === 'human' ? '<span class="chip">human in the loop</span>'
             : a.mode === 'undecided' ? '<span class="chip warn">label a few to decide</span>'
             : a.trainable ? '<span class="chip ok">ready to train</span>'
             : `<span class="chip warn">${num(a.shortfall)} more</span>`}</td>
        <td class="right"><button class="btn sm primary" data-attr="${esc(a.attribute)}">Label</button></td>
      </tr>`).join('')}
    </tbody></table></div>
  </div>`;
}

async function viewAttr(name, videoFilter) {
  // `#attr/axles/8` labels only that clip — the batch a freshly extracted video adds.
  const vq = videoFilter ? `&video=${encodeURIComponent(videoFilter)}` : '';
  const [d, q] = await Promise.all([
    api('/api/attrs').catch(() => ({ attributes: [] })),
    api(`/api/attrs/${name}/queue?limit=400${vq}`).catch(() => ({ items: [] })),
  ]);
  const spec = (d.attributes || []).find(a => a.attribute === name);
  if (!spec) return `<div class="page"><div class="card">${empty('Unknown attribute', name)}</div></div>`;
  ATTR_Q = { name, items: q.items || [], i: 0, spec, done: 0 };

  return `<div class="page">
    <div class="page-head"><div><h1>${esc(spec.label)}</h1>
      <p>${esc(spec.hint)}${videoFilter ? ` · <strong>only video ${esc(videoFilter)}</strong>
         — the fresh clip, never used for training</strong>` : ''}</p></div>
      <div class="head-actions"><button class="btn secondary" data-route="attrs">All attributes</button></div>
    </div>
    <div class="grid g4">
      <div class="tile"><div class="ico info">${ic.overview}</div><div>
        <div class="lbl">Labelled</div><div class="val" data-alab>${num(spec.usable)}</div>
        <div class="sub">of ${num(spec.min_labels)} needed to train</div></div></div>
      <div class="tile"><div class="ico acc">${ic.runs}</div><div>
        <div class="lbl">Waiting</div><div class="val" data-aleft>${num(ATTR_Q.items.length)}</div>
        <div class="sub">crops ready to answer</div></div></div>
      <div class="tile"><div class="ico ok">${ic.training}</div><div>
        <div class="lbl">This session</div><div class="val" data-adone>0</div>
        <div class="sub">press 1–${spec.values.length}, or click</div></div></div>
      <div class="tile"><div class="ico warn">${ic.judges}</div><div>
        <div class="lbl">Shortfall</div><div class="val">${num(spec.shortfall)}</div>
        <div class="sub">${spec.shortfall ? 'still short' : 'enough to train'}</div></div></div>
    </div>
    <div class="card"><div id="attrStage" class="attr-stage"></div></div>
  </div>`;
}

function mountAttrLabeller() {
  const st = document.getElementById('attrStage');
  if (!st || !ATTR_Q) return;
  const { spec } = ATTR_Q;

  function draw() {
    const it = ATTR_Q.items[ATTR_Q.i];
    if (!it) {
      st.innerHTML = `<div class="empty"><h3>Nothing left in the queue</h3>
        <p>Every crop harvested for this attribute has been answered.</p></div>`;
      return;
    }
    st.innerHTML = `
      <div class="attr-imgs">
        <img src="/api/attrs/sample/${it.id}/crop.jpg" alt="Vehicle to classify">
        <img class="ctx" src="/api/attrs/sample/${it.id}/ctx.jpg" alt="Where it is in the frame">
      </div>
      <div class="attr-meta">${it.box_w}px wide · ${ATTR_Q.i + 1} of ${ATTR_Q.items.length}
        ${it.source === 'dataset' ? '· from the labelled dataset' : `· video ${it.video_id} track ${it.track_id}`}</div>
      <div class="attr-answers">${spec.values.map((v, n) =>
        `<button class="btn ${v === 'unclear' ? 'secondary' : 'primary'}" data-val="${esc(v)}">
           <kbd>${n + 1}</kbd> ${esc(v.replace(/_/g, ' '))}</button>`).join('')}
        <button class="btn secondary" data-skip><kbd>&larr;</kbd> back</button></div>`;
    st.querySelectorAll('[data-val]').forEach(b => b.onclick = () => answer(b.dataset.val));
    const back = st.querySelector('[data-skip]');
    if (back) back.onclick = () => { ATTR_Q.i = Math.max(0, ATTR_Q.i - 1); draw(); };
    // Warm the next crop so answering never waits on a download.
    const nxt = ATTR_Q.items[ATTR_Q.i + 1];
    if (nxt) new Image().src = `/api/attrs/sample/${nxt.id}/crop.jpg`;
  }

  async function answer(v) {
    const it = ATTR_Q.items[ATTR_Q.i];
    if (!it) return;
    ATTR_Q.i++; draw();                       // advance immediately; labelling is a rhythm
    try {
      await post(`/api/attrs/sample/${it.id}`, { value: v });
      ATTR_Q.done++;
      const set = (k, f) => { const e = document.querySelector(k); if (e) e.textContent = f(e); };
      set('[data-adone]', () => ATTR_Q.done);
      set('[data-aleft]', e => Math.max(0, +e.textContent.replace(/\D/g, '') - 1).toLocaleString());
      if (v !== 'unclear') set('[data-alab]', e => (+e.textContent.replace(/\D/g, '') + 1).toLocaleString());
    } catch (e) {
      // A dropped label is invisible otherwise, and this is the one thing that must not
      // be silently lost — put the card back rather than carry on.
      ATTR_Q.i = ATTR_Q.items.indexOf(it); draw();
      toast(`Not saved: ${e.message}`, true);
    }
  }

  ATTR_KEYS = ev => {
    if (!document.getElementById('attrStage')) return;
    if (ev.key === 'ArrowLeft') { ATTR_Q.i = Math.max(0, ATTR_Q.i - 1); draw(); return; }
    const n = parseInt(ev.key, 10);
    if (n >= 1 && n <= spec.values.length) { ev.preventDefault(); answer(spec.values[n - 1]); }
  };
  document.addEventListener('keydown', ATTR_KEYS);
  draw();
}
let ATTR_KEYS = null;

/* The axle audit: the one label nobody has ever actually checked.

   Every heavy-truck label in this system was either the detector's own axle guess echoed
   back by a judge, or the constant `HEAVY_DEFAULT = 8`. This screen is where that gets
   settled — the split votes need a person, and the unanimous ones deserve a spot-check
   before they are trusted enough to retrain on. */
const AXLE_OPTS = [
  ['2_axle', '2 axles'], ['3_axle', '3 axles'], ['4_or_more_axle', '4+ / articulated'],
  ['not_a_truck', 'Not a truck'], ['unclear', "Can't tell"],
];

async function viewAxles() {
  const [m, q, r] = await Promise.all([
    api('/api/axles/summary').catch(() => ({})),
    api('/api/axles/queue').catch(() => ({ items: [] })),
    api('/api/axles/resolved').catch(() => ({ items: [] })),
  ]);
  const queue = q.items || [], done = (r.items || []);
  const total = (m.total || 0), answered = (m.answered || 0);
  const changed = done.filter(x => x.changed);

  const card = (x, showVotes) => `
    <figure class="ev-card" data-check="${x.id}">
      <img loading="lazy" src="/api/axles/${x.id}/crop.jpg" alt="Heavy vehicle, track ${x.track_id}">
      <figcaption>
        <div class="ev-top">
          <strong>claimed ${esc(x.det || '—')}</strong>
          <span class="chip ${x.box_w >= 300 ? 'ok' : 'warn'}">${x.box_w}px wide</span>
        </div>
        <div class="muted">video ${x.video_id} · track ${x.track_id}</div>
        ${showVotes ? `<div class="votes">${(x.votes || []).map(v => `
          <span class="vote"><em>${esc(v.model.split('/').pop().slice(0, 18))}</em>
            ${esc(v.answer || (v.error ? 'error' : '—'))}</span>`).join('')}</div>`
          : `<div class="votes"><span class="vote"><em>all three judges</em>
              ${esc(x.truth_name || x.verdict || '—')}</span></div>`}
        <div class="axle-btns">${AXLE_OPTS.map(([k, lbl]) =>
          `<button class="btn sm ${x.human === k ? 'primary' : 'secondary'}"
             data-axle="${x.id}" data-ans="${k}">${lbl}</button>`).join('')}
          <span class="saved${x.human ? ' on' : ''}" data-saved>${x.human ? 'Saved' : ''}</span>
        </div>
      </figcaption>
    </figure>`;

  const mrows = (m.matrix || []).filter(x => x.det !== x.truth);
  return `<div class="page">
    <div class="page-head"><div>
      <h1>Axle audit</h1>
      <p>Every 2Axle / 3Axle / MAV label, re-derived by counting axles instead of
         inheriting the detector's guess. Judges never saw the claimed class.</p></div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico info">${ic.overview}</div><div>
        <div class="lbl">Heavy tracks audited</div><div class="val">${num(m.total || 0)}</div>
        <div class="sub">${num(m.resolved || 0)} settled · ${num(m.unresolved || 0)} open</div></div></div>
      <div class="tile"><div class="ico ${m.accuracy != null && m.accuracy < 0.6 ? 'warn' : 'ok'}">${ic.judges}</div><div>
        <div class="lbl">Detector was right</div>
        <div class="val">${m.accuracy != null ? (100 * m.accuracy).toFixed(0) + '%' : '—'}</div>
        <div class="sub">of the settled heavy tracks</div></div></div>
      <div class="tile"><div class="ico acc">${ic.runs}</div><div>
        <div class="lbl">You have answered</div>
        <div class="val" data-answered>${num(answered)}</div>
        <div class="sub">of ${num(total)} · saved as you click, no Save needed</div></div></div>
      <div class="tile"><div class="ico warn">${ic.training}</div><div>
        <div class="lbl">Unanimously reclassified</div><div class="val">${num(changed.length)}</div>
        <div class="sub">worth a spot-check before retraining</div></div></div>
    </div>

    ${mrows.length ? `<div class="card"><div class="card-head"><h2>Where the claim and the axles disagree</h2></div>
      <div class="lv-table"><table><thead><tr><th>Detector claimed</th><th>Axles say</th>
        <th class="right">Tracks</th></tr></thead><tbody>
        ${mrows.map(x => `<tr><td>${esc(x.det)}</td><td>${esc(x.truth)}</td>
          <td class="right">${x.n}</td></tr>`).join('')}
      </tbody></table></div></div>` : ''}

    <div class="card"><div class="card-head"><h2>Read the crop, not the label</h2></div>
      <div class="card-body"><p class="muted">An axle is one position along the vehicle where
      wheels meet the road — count positions, not tyres, and count a tractor and its trailer
      together. Anything under about 300px wide is usually too coarse to be sure; answer
      <strong>Can't tell</strong> rather than guessing, because a guess recorded here becomes
      a training label.</p></div></div>

    <h2 class="sec">Needs your call — ${queue.length}</h2>
    ${queue.length ? `<div class="ev-grid">${queue.map(x => card(x, true)).join('')}</div>`
      : `<div class="card">${empty('Nothing waiting', 'Every audited track was settled unanimously.')}</div>`}

    ${changed.length ? `<h2 class="sec">Unanimously reclassified — ${changed.length}</h2>
      <div class="ev-grid">${changed.map(x => card(x, false)).join('')}</div>` : ''}
  </div>`;
}

/* Every count that nobody watched happen, with the reasoning drawn on the frame.

   These are the vehicles first detected *past* the line — mostly fast two-wheelers, which
   at 40-58 px/frame can clear the line entirely between two detections. The count rests
   on back-projecting their heading, so unlike an ordinary crossing it is an inference, and
   an inference that cannot be inspected is one you have to take on trust. */
async function viewImplied(videoId) {
  const d = await api('/api/crossings/' + videoId + '/implied').catch(e => ({ error: String(e) }));
  if (d.error) return `<div class="page"><div class="page-head"><div><h1>Implied crossings</h1></div></div>
    <div class="card">${empty('Nothing to review', d.error)}</div></div>`;

  const rows = d.added || [];
  const pct = d.after ? (100 * rows.length / d.after).toFixed(1) : '0';
  return `<div class="page">
    <div class="page-head"><div>
      <h1>Implied crossings</h1>
      <p>Vehicles counted from a back-projected heading, because they were first detected
         after they had already passed the line.</p></div>
      <div class="head-actions">
        <button class="btn secondary" data-reportcard="${videoId}">Report card</button>
        <button class="btn secondary" data-preview="${videoId}">Preview video</button>
      </div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico info">${ic.overview}</div><div>
        <div class="lbl">Witnessed crossings</div><div class="val">${num(d.before)}</div>
        <div class="sub">seen on both sides of the line</div></div></div>
      <div class="tile"><div class="ico acc">${ic.runs}</div><div>
        <div class="lbl">Implied crossings</div><div class="val">${num(rows.length)}</div>
        <div class="sub">${pct}% of the total — each one below</div></div></div>
      <div class="tile"><div class="ico ok">${ic.training}</div><div>
        <div class="lbl">Reported total</div><div class="val">${num(d.after)}</div>
        <div class="sub">what the report card and xlsx say</div></div></div>
      <div class="tile"><div class="ico warn">${ic.judges}</div><div>
        <div class="lbl">Dropped as unreliable</div><div class="val">${num((d.removed || []).length)}</div>
        <div class="sub">heading not trusted — too few consecutive frames</div></div></div>
    </div>

    <div class="card"><div class="card-head"><h2>How to read these</h2></div>
      <div class="card-body legend-row">
        <span class="ev-key"><i style="background:#2882e6"></i>the count line</span>
        <span class="ev-key"><i style="background:#78dc50"></i>the vehicle where it was first
          detected, and the path it then took</span>
        <span class="ev-key"><i style="background:#f0961e"></i>the back-projected path, ending
          at the point it is credited with crossing</span>
      </div>
      <div class="card-body" style="padding-top:0">
        <p class="muted">If the orange path runs along the carriageway and meets the line where a
        vehicle plainly would, the count is right. If it runs through a wall, a verge or a tree,
        it is not — say so and it comes out.</p></div></div>

    ${rows.length ? `<div class="ev-grid">${rows.map(r => `
      <figure class="ev-card">
        <img loading="lazy" src="/api/crossings/${videoId}/${r.track_id}/image"
             alt="Track ${r.track_id} at the frame it was first detected">
        <figcaption>
          <div class="ev-top"><strong>${esc(r.class)}</strong>
            <span class="chip ${r.direction === 'in' ? 'ok' : 'info'}">${esc(r.direction)}</span></div>
          <div class="muted">${esc(r.clock)} · track ${r.track_id}</div>
          <dl class="ev-stats">
            <div><dt>Speed</dt><dd>${r.speed_px_per_frame} px/frame</dd></div>
            <div><dt>Unbroken run</dt><dd>${r.run} frames</dd></div>
            <div><dt>Tracked</dt><dd>${r.frames} frames</dd></div>
            <div><dt>Box height</dt><dd>${r.max_box_h} px</dd></div>
          </dl>
        </figcaption>
      </figure>`).join('')}</div>`
    : `<div class="card">${empty('No implied crossings',
        'Every vehicle in this count was seen on both sides of the line.')}</div>`}
  </div>`;
}

const RC_MODEL = {};
async function viewReportCard(videoId) {
  const d = await api('/api/reportcard/' + videoId);
  // Which detector produced these tracks. A count that cannot name its model cannot be
  // compared with another one, and /api/clip already knows.
  try { RC_MODEL[videoId] = (await api('/api/clip/' + videoId)).extract.model; } catch { }
  REPORT_DATA[videoId] = d;
  if (d.error) return `<div class="page"><div class="page-head"><div><h1>Report card</h1></div>
    <div class="head-actions"><button class="btn primary" data-scene="${videoId}">Draw a line</button></div></div>
    <div class="card">${empty('Cannot build a report yet', d.error)}</div></div>`;

  const v = d.video;
  const series = d.bins_15min.length > 12 ? d.hourly : d.bins_15min;
  const unit = d.bins_15min.length > 12 ? 'hour' : 'quarter-hour';
  const dir = Object.entries(d.direction)[0];
  const dirTotals = dir ? dir[1] : null;
  // The tile and the chart must agree. `d.peak` is the hourly peak, but the chart shows
  // quarter-hours when the span is short — quoting 281 beside columns of 198 and 83 reads
  // as a third number nobody can find.
  const full = series.filter(b => !b.partial);
  const peakOf = (full.length ? full : series).reduce(
    (a, b) => (b.n > (a ? a.n : -1) ? b : a), null);

  return `<div class="page">
    <div class="page-head"><div>
      <h1>Report card — ${esc(v.name)}</h1>
      <p>${esc(v.start_clock || '')} · ${v.duration_min} min · line(s): ${d.lines.map(esc).join(', ')}</p></div>
      <div class="head-actions">
        ${WS && WS.id ? `<button class="btn secondary" data-route="station" data-arg="${WS.id}/clips">← Station ${WS.id}</button>` : '<button class="btn secondary" data-route="stations">All stations</button>'}
        <button class="btn secondary" data-scene="${videoId}">Edit line</button>
        <button class="btn secondary" data-preview="${videoId}">Preview video</button>
        <button class="btn secondary" data-implied="${videoId}">Implied crossings</button>
        <a class="btn primary" href="/api/report-xlsx/${videoId}" download>Download xlsx</a>
      </div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico acc">${ic.runs}</div><div><div class="lbl">Vehicles counted</div>
        <div class="val">${num(d.total)}</div>
        <div class="sub">${v.duration_min} min of footage</div></div></div>
      <div class="tile"><div class="ico info">${ic.overview}</div><div><div class="lbl">Volume in PCU</div>
        <div class="val">${num(d.pcu_total)}</div>
        <div class="sub">${d.pcu_per_vehicle} PCU per vehicle · IRC:64-1990</div></div></div>
      <div class="tile"><div class="ico ok">${ic.training}</div><div>
        <div class="lbl">Busiest ${unit}</div>
        <div class="val">${peakOf ? esc(peakOf.label) : '—'}</div>
        <div class="sub">${peakOf ? num(peakOf.n) + ' vehicles'
          + (peakOf.partial ? ' · partial coverage' : '') : ''}</div></div></div>
      <div class="tile"><div class="ico ${d.checks.some(c => c.level !== 'ok') ? 'warn' : 'ok'}">${ic.judges}</div>
        <div><div class="lbl">Shape checks</div>
        <div class="val">${d.checks.filter(c => c.level !== 'ok').length || 'OK'}</div>
        <div class="sub">${d.checks.filter(c => c.level !== 'ok').length ? 'need a look' : 'nothing anomalous'}</div></div></div>
    </div>

    <div class="card"><div class="card-head"><div><h2>Volume over time</h2>
      <p>every ${unit} in the covered span, zero-filled — an empty period and a missing
         period are different facts, and hiding the empty ones changes the shape of the day</p></div></div>
      <div id="volchart"></div>
      ${series.some(b => b.partial) ? `<p style="margin:10px 0 0;color:var(--cc-fg-3);font-size:12px">
        Hatched columns have partial footage coverage — their counts are real but not comparable
        to a full period.</p>` : ''}
    </div>

    <div class="grid g2">
      <div class="card"><div class="card-head"><div><h2>Class composition</h2>
        <p>share of every vehicle counted</p></div></div>
        <div id="mixchart"></div></div>

      <div class="card"><div class="card-head"><div><h2>Direction split</h2>
        <p>which way traffic crossed the line</p></div></div>
        ${dirTotals ? `<div class="rc-split">
            <i style="width:${100 * dirTotals.in / (dirTotals.total || 1)}%"></i>
            <i style="width:${100 * dirTotals.out / (dirTotals.total || 1)}%"></i></div>
          <div class="rc-legend">
            <span><i style="background:var(--cc-series-1)"></i>in — ${num(dirTotals.in)}
              (${(100 * dirTotals.in / (dirTotals.total || 1)).toFixed(0)}%)</span>
            <span><i style="background:var(--cc-series-2)"></i>out — ${num(dirTotals.out)}
              (${(100 * dirTotals.out / (dirTotals.total || 1)).toFixed(0)}%)</span></div>`
          : '<div class="empty">No crossings recorded.</div>'}
        <table style="margin-top:20px"><thead><tr><th>Class</th><th class="right">Count</th>
          <th class="right">PCU</th><th class="right">Share</th></tr></thead>
          <tbody>${d.composition.map(c => `<tr><td class="id">${esc(c.class)}</td>
            <td class="right num">${num(c.n)}</td>
            <td class="right num">${num(d.pcu_by_class[c.class])}</td>
            <td class="right num">${c.share}%</td></tr>`).join('')}</tbody></table>
      </div>
    </div>

    ${/* The operator/use split. These are answers a person gave on Verify, and they used
          to vanish -- an APSRTC bus was reported as "Bus" and nothing else. It is shown as
          a breakdown INSIDE the class, not beside it, because that is what it is: the bus
          is already in the Bus row above and already carries a Bus's 3.0 PCU. */''}
    ${(d.attributes || []).length ? `<div class="card">
      <div class="card-head"><div><h2>Inside the classes</h2>
        <p>the splits only a person can call — already counted in the classes above,
           shown here because a classified count reports them separately</p></div></div>
      <table><thead><tr><th>Split</th><th>Within</th><th class="right">Yes</th>
        <th class="right">No</th><th class="right">Not reviewed</th>
        <th class="right">Share of reviewed</th></tr></thead><tbody>
      ${d.attributes.map(a => {
        const seen = a.yes + a.no;
        return `<tr><td><strong>${esc(a.yes_label)}</strong>
            <div class="muted">vs ${esc(a.no_label)}</div></td>
          <td class="id">${esc(a.of_class)} (${num(a.pool)})</td>
          <td class="right num">${num(a.yes)}</td>
          <td class="right num">${num(a.no)}</td>
          <td class="right num">${a.unreviewed
            ? `<span class="status warn">${num(a.unreviewed)}</span>` : '0'}</td>
          <td class="right num">${seen ? `${(100 * a.yes / seen).toFixed(0)}%`
            : '<span class="muted">—</span>'}</td></tr>`;
      }).join('')}</tbody></table>
      ${d.attributes.some(a => a.unreviewed) ? `<p class="muted" style="margin:14px 0 0">
        Vehicles not yet reviewed are left out of the share rather than assumed “no” —
        verify them to make the split reportable.</p>` : ''}
    </div>` : ''}

    ${/* The 15-minute bin is the unit an IRC/MoRTH proforma reports in, and the
          diagnostics say where the other tracks went. Both were already in the payload
          and neither was shown, so the card said "59" without saying 59 out of what. */''}
    <div class="grid g2">
      <div class="card"><div class="card-head"><div><h2>Every 15 minutes</h2>
        <p>the unit the proforma reports in · zero-filled, partial bins marked</p></div></div>
        <table><thead><tr><th>From</th><th class="right">Vehicles</th>
          <th class="right">Coverage</th></tr></thead><tbody>
        ${d.bins_15min.map(b => `<tr><td class="mono">${esc(b.label)}</td>
          <td class="right num">${num(b.n)}</td>
          <td class="right num">${b.partial
            ? `<span class="status warn">${Math.round(b.coverage * 100)}%</span>`
            : '100%'}</td></tr>`).join('')}
        <tr class="tot"><td><b>Total</b></td><td class="right num"><b>${num(d.total)}</b></td>
          <td></td></tr></tbody></table></div>

      <div class="card"><div class="card-head"><div><h2>Where the tracks went</h2>
        <p>every detection the counter saw, and why it did or did not become a vehicle</p></div></div>
        <table><tbody>
          ${[['Tracks formed', 'tracks_total'], ['Long enough to count', 'eligible'],
             ['Fragments rejoined', 'fragments_merged'], ['Too short to count', 'dropped_too_short'],
             ['Suppressed as duplicate', 'dropped_duplicate'],
             ['Crossed outside the drawn line', 'crossings_off_segment'],
             ['Cancelled as line jitter', 'crossings_debounced'],
             ['Credited by back-projection', 'implied_birth_crossings'],
             ['Back-projection rejected', 'implied_birth_off_segment']]
            .filter(([, k]) => (d.diagnostics || {})[k] != null)
            .map(([lbl, k]) => `<tr><td class="id">${lbl}</td>
              <td class="right num">${num(d.diagnostics[k])}</td></tr>`).join('')}
          <tr class="tot"><td class="id"><b>Counted</b></td>
            <td class="right num"><b>${num(d.total)}</b></td></tr>
        </tbody></table></div>
    </div>

    <div class="card"><div class="card-head"><div><h2>How this number was produced</h2>
      <p>every figure above traces to these — a count cannot be compared without them</p></div></div>
      <table><tbody>
        <tr><td class="id">Clip</td><td class="right mono">${esc(v.name)}</td></tr>
        <tr><td class="id">Starts</td><td class="right mono">${esc(v.start_clock || '—')}</td></tr>
        <tr><td class="id">Footage</td><td class="right num">${v.duration_min} min · ${
          num(v.frames)} frames @ ${v.fps} fps</td></tr>
        <tr><td class="id">Detector</td><td class="right mono">${esc(RC_MODEL[videoId] || '—')}</td></tr>
        <tr><td class="id">Count line</td><td class="right mono" style="font-size:11px">${
          d.lines.map(esc).join(', ')}</td></tr>
        <tr><td class="id">PCU basis</td><td class="right">IRC:64-1990</td></tr>
      </tbody></table></div>

    <div class="card"><div class="card-head"><div><h2>Does this count look right?</h2>
      <p>the checks a reader would do by eye, already done</p></div></div>
      <div class="rc-checks">${d.checks.map(c => `<div class="rc-check ${esc(c.level)}">
        <div><b>${esc(c.what)}</b><p>${esc(c.why)}</p></div></div>`).join('')}</div>
    </div>
  </div>`;
}

async function mountReportCharts(videoId) {
  const box = $('#volchart');
  if (!box) return;
  const d = REPORT_DATA[videoId];
  if (!d) return;
  const { columnChart, rankedBars } = await import('/shared/shell.js');
  const series = d.bins_15min.length > 12 ? d.hourly : d.bins_15min;
  // One series, so no legend — the axis labels carry identity and colour carries only
  // magnitude. `emphasis` is an INDEX, not a label; passing the peak's name marked
  // nothing. The peak is the one direct label worth having.
  const full = series.filter(b => !b.partial);
  const peak = (full.length ? full : series).reduce((a, b) => (b.n > (a ? a.n : -1) ? b : a), null);
  const peakIdx = peak ? series.indexOf(peak) : -1;
  box.innerHTML = columnChart(
    series.map(b => ({ label: b.label, value: b.n, muted: b.partial,
                       sub: b.partial ? `${Math.round(b.coverage * 100)}% covered` : '' })),
    { height: 200, emphasis: peakIdx >= 0 ? peakIdx : null });
  const mix = $('#mixchart');
  if (mix) mix.innerHTML = rankedBars(
    d.composition.map(c => ({ label: c.class, value: c.n })));
}
const REPORT_DATA = {};

/* ═══════════════════════ where the count error comes from ═══════════════════════
   The screen that answers "is a station model worth training?". Free fixes are listed
   first on purpose: every large counting error in this project so far turned out to be
   line geometry or tracking, and both cost nothing. */
async function viewErrors(siteId) {
  const [station, vids] = await Promise.all([
    api('/api/stations/' + siteId),
    api('/api/countable-videos?site_id=' + siteId).catch(() => ({ videos: [] })),
  ]);
  const vid = ERR_VIDEO[siteId] || (vids.videos[0] || {}).id;
  const d = vid || siteId ? await api(`/api/errors?site_id=${siteId}` +
      (vid ? `&video_id=${vid}` : '')) : null;
  const v = (d && d.gold_validation) || {};
  const fl = (d && d.frame_level) || {};
  const cl = (d && d.count_level) || {};
  const SEV = { high: 'bad', medium: 'warn', low: 'idle' };

  const bar = (label, n, total, tone) => `<div class="barrow">
    <div class="k">${esc(label)}</div>
    <div class="t"><i style="width:${total ? Math.min(100, 100 * n / total) : 0}%
      ${tone ? `;background:var(--cc-${tone})` : ''}"></i></div>
    <div class="v">${num(n)}</div></div>`;

  return `<div class="page">
    <div class="page-head"><div>
      <h1>Error sources — ${esc(station.code)}</h1>
      <p>A count can be wrong in four ways with four different prices. This separates
         them, so effort goes where the error actually is.</p></div>
      <div class="head-actions">
        ${vids.videos.length > 1 ? `<select class="field" id="errvid" style="width:auto">
          ${vids.videos.map(x => `<option value="${x.id}"${String(x.id) === String(vid) ? ' selected' : ''}>
            ${esc(x.name)}</option>`).join('')}</select>` : ''}
        <button class="btn secondary" data-gold="${siteId}">Gold set</button>
        <button class="btn secondary" data-station="${siteId}">Station</button>
      </div></div>

    <div class="card" style="border-left:3px solid var(--cc-accent)">
      <div style="font-size:15px;line-height:23px;color:var(--cc-fg)">${esc((d && d.verdict) || '—')}</div>
    </div>

    <div class="grid g2">
      <div class="card"><div class="card-head"><div><h2>1–2 · Detection &amp; classification</h2>
        <p>measured against the gold set — the only thing that can see a missed vehicle</p></div></div>
        ${fl.error ? empty('Not measured', fl.error)
          : `${v.circular || v.thin ? `<div class="err" style="margin-bottom:16px"><div>
                <b>${v.circular ? 'These numbers are circular.' : 'Thin evidence.'}</b>
                <div style="font-size:12px;margin-top:4px">${esc(v.note)}</div></div></div>` : ''}
             <div class="grid g2" style="gap:12px">
               <div class="tile" style="min-height:auto"><div><div class="lbl">Recall</div>
                 <div class="val">${fl.recall != null ? (fl.recall * 100).toFixed(1) + '%' : '—'}</div>
                 <div class="sub">${num(fl.missed)} never found</div></div></div>
               <div class="tile" style="min-height:auto"><div><div class="lbl">Class accuracy</div>
                 <div class="val">${fl.class_accuracy != null ? (fl.class_accuracy * 100).toFixed(1) + '%' : '—'}</div>
                 <div class="sub">${num(fl.wrong_class)} in the wrong column</div></div></div>
             </div>
             <p style="color:var(--cc-fg-3);font-size:12px;margin:14px 0 0">
               ${v.frames_reviewed || 0} frame(s) reviewed · ${num(v.added)} vehicle(s) the
               human added · ${num(v.removed)} box(es) removed</p>`}
      </div>

      <div class="card"><div class="card-head"><div><h2>3–4 · Tracking &amp; counting</h2>
        <p>the counting code's own record of every track it dropped and why</p></div></div>
        ${cl.error ? empty('Not measured', cl.error)
          : `<div class="bars">
              ${bar('Tracks formed', cl.tracks_total || 0, cl.tracks_total || 1)}
              ${bar('Counted a crossing', cl.tracks_that_counted || 0, cl.tracks_total || 1, 'ok')}
              ${bar('Too short to count', cl.dropped_too_short || 0, cl.tracks_total || 1, 'warn')}
              ${bar('Suppressed as duplicate', cl.dropped_duplicate || 0, cl.tracks_total || 1, 'warn')}
              ${bar('Rejoined fragments', cl.fragments_merged || 0, cl.tracks_total || 1)}
             </div>
             <table style="margin-top:16px"><tbody>
               <tr><td class="id">Crossings counted</td><td class="right num">${num(cl.counted_crossings)}</td></tr>
               <tr><td class="id">Rejected — outside the drawn line</td>
                   <td class="right num">${num(cl.crossings_off_segment)}</td></tr>
               <tr><td class="id">Cancelled as line jitter</td><td class="right num">${num(cl.crossings_debounced)}</td></tr>
               <tr><td class="id">Removed by the duplicate guard</td><td class="right num">${num(cl.crossings_deduped)}</td></tr>
               <tr><td class="id">Added by the implied-birth rule</td><td class="right num">${num(cl.implied_birth_crossings)}</td></tr>
             </tbody></table>`}
      </div>
    </div>

    <div class="card"><div class="card-head"><div><h2>What to fix, cheapest first</h2>
      <p>free fixes are instant and reversible; training costs a GPU and labelling hours</p></div></div>
      ${(d && d.findings || []).length ? `<div class="stages">${d.findings.map(f => `
        <div class="stage" style="align-items:flex-start;padding:14px">
          <div class="nm" style="width:auto;flex:0 0 116px">
            <span class="status ${SEV[f.severity] || 'idle'}">${esc(f.severity)}</span></div>
          <div style="flex:1;min-width:0">
            <div style="font-weight:500;color:var(--cc-fg)">${esc(f.what)}</div>
            <div style="font-size:12px;color:var(--cc-fg-3);margin-top:3px">${esc(f.why)}</div>
          </div>
          <div class="cost" style="flex:0 0 120px">
            <span class="chip">${f.cost === 'free' ? 'free' : esc(f.cost)}</span></div>
        </div>`).join('')}</div>`
        : empty('Nothing stands out', 'No error source crossed its threshold on what has been measured so far.')}
    </div>
  </div>`;
}
const ERR_VIDEO = {};

/* ═══════════════════════════════ gold set ═══════════════════════════════
   The only artifact that can measure RECALL. Every other screen scores the model
   against crops the model itself proposed, which can never show a vehicle it missed —
   and a count is exactly as wrong as its misses. */
async function viewGold(siteId) {
  // Scoring is NOT fetched here: it runs the detector over every gold frame, which is
  // seconds of work and turns simply opening this page into a model evaluation.
  const [st, station] = await Promise.all([
    api(`/api/gold/${siteId}`), api('/api/stations/' + siteId)]);
  const sc = GOLD_SCORE[siteId] || null;
  const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
  const scored = sc && !sc.error;
  return `<div class="page">
    <div class="page-head"><div>
      <h1>Gold set — ${esc(station.code)}</h1>
      <p>Frames where a human has marked <b>every</b> vehicle, including the ones no model
         found. Frozen: the dataset builder refuses to train on these.</p></div>
      <div class="head-actions">
        <button class="btn secondary" data-station="${siteId}">Station</button>
        ${st.done ? `<button class="btn secondary" data-goldscore="${siteId}">Score a model</button>` : ''}
        ${st.total ? `<button class="btn primary" data-goldreview="${siteId}"
          ${st.done >= st.total ? 'disabled title="every frame reviewed"' : ''}>
          ${st.done ? 'Continue' : 'Start'} labelling</button>`
        : `<button class="btn primary" data-goldbuild="${siteId}">Build gold set</button>`}
      </div></div>

    <div class="grid g4">
      <div class="tile"><div class="ico acc">${ic.review}</div><div><div class="lbl">Frames reviewed</div>
        <div class="val">${st.done} / ${st.total}</div>
        <div class="sub">${pct}% complete</div></div></div>
      <div class="tile"><div class="ico info">${ic.videos}</div><div><div class="lbl">Vehicles marked</div>
        <div class="val">${num(st.boxes)}</div>
        <div class="sub">${num(st.human_added)} the model had missed</div></div></div>
      <div class="tile"><div class="ico ok">${ic.overview}</div><div><div class="lbl">Pace</div>
        <div class="val">${st.avg_seconds ? st.avg_seconds + 's' : '—'}</div>
        <div class="sub">per frame</div></div></div>
      <div class="tile"><div class="ico warn">${ic.costs}</div><div><div class="lbl">Time left</div>
        <div class="val">${st.minutes_left != null ? st.minutes_left + 'm' : '—'}</div>
        <div class="sub">at the current pace</div></div></div>
    </div>

    ${scored ? `<div class="card"><div class="card-head"><div><h2>Model measured against the gold set</h2>
      <p>${sc.frames} frame(s), ${num(sc.gold_boxes)} vehicles — recall is the number the other screens cannot show</p></div></div>
      <div class="grid g4" style="gap:16px">
        <div class="tile"><div><div class="lbl">Recall</div>
          <div class="val">${sc.recall != null ? (sc.recall * 100).toFixed(1) + '%' : '—'}</div>
          <div class="sub">${num(sc.missed)} vehicle(s) never found</div></div></div>
        <div class="tile"><div><div class="lbl">Precision</div>
          <div class="val">${sc.precision != null ? (sc.precision * 100).toFixed(1) + '%' : '—'}</div>
          <div class="sub">${num(sc.false_positives)} box(es) that were not there</div></div></div>
        <div class="tile"><div><div class="lbl">Class accuracy</div>
          <div class="val">${sc.class_accuracy != null ? (sc.class_accuracy * 100).toFixed(1) + '%' : '—'}</div>
          <div class="sub">${num(sc.wrong_class)} in the wrong column</div></div></div>
        <div class="tile"><div><div class="lbl">Count error</div>
          <div class="val">${sc.count_error_pct != null
            ? (sc.count_error_pct > 0 ? '+' : '') + sc.count_error_pct + '%' : '—'}</div>
          <div class="sub">misses minus false positives</div></div></div>
      </div>
      <table style="margin-top:20px"><thead><tr><th>Class</th><th class="right">In gold</th>
        <th class="right">Found &amp; right</th><th class="right">Missed</th><th class="right">Wrong class</th></tr></thead>
        <tbody>${Object.entries(sc.per_class || {}).map(([k, v]) => `<tr>
          <td class="id">${esc(k)}</td><td class="right num">${v.gold}</td>
          <td class="right num">${v.tp}</td>
          <td class="right num"${v.miss ? ' style="color:var(--cc-bad)"' : ''}>${v.miss}</td>
          <td class="right num">${v.wrong}</td></tr>`).join('')}</tbody></table></div>`
      : st.done ? `<div class="card">${empty('Not scored yet',
          'Press “Score a model” to run the detector over these frames and see its recall.')}</div>` : ''}

    ${!st.total ? `<div class="card">${empty('No gold set for this station yet',
        'Build one and label the frames. Around 60 frames is enough to measure recall, and at the pace this screen tracks it fits inside a couple of hours.')}</div>` : ''}
  </div>`;
}

let LABELER = null;
const GOLD_SCORE = {};      // siteId -> last score, so it survives a re-render
async function viewGoldReview(siteId) {
  const d = await api(`/api/gold/${siteId}/next`);
  GOLD_CTX = { siteId, data: d };
  if (d.finished) return `<div class="page">
    <div class="page-head"><div><h1>Gold set complete</h1>
      <p>All ${d.total} frame(s) reviewed · ${num(d.boxes)} vehicles marked, ${num(d.human_added)} of them the model had missed.</p></div>
      <div class="head-actions"><button class="btn primary" data-gold="${siteId}">See the score</button></div></div>
    <div class="card"><div class="empty">Nothing left to label.</div></div></div>`;
  const f = d.frame;
  return `<div class="page" style="gap:12px">
    <div class="page-head"><div>
      <h1 style="font-size:22px;line-height:30px">Frame ${d.done + 1} of ${d.total}</h1>
      <p>${esc(f.clock || '')} · ${esc(f.band || '')} · ${esc((f.footage_path || '').split('/').pop())}</p></div>
      <div class="head-actions"><button class="btn secondary" data-gold="${siteId}">Stop</button></div></div>
    <div class="card"><div id="labeler"></div></div>
  </div>`;
}
let GOLD_CTX = null;

async function mountLabeler_() {
  const el = $('#labeler');
  // Saving a frame re-renders straight back into goldreview, so without this the old
  // labeller stayed alive with its keydown listener attached and every Enter fired in
  // BOTH instances -- silently marking extra frames done and losing their boxes.
  if (LABELER) { LABELER.destroy(); LABELER = null; }
  if (!el || !GOLD_CTX || GOLD_CTX.data.finished) return;
  const { mountLabeler } = await import('/shared/boxlabeler.js');
  const { siteId, data } = GOLD_CTX;
  LABELER = mountLabeler(el, {
    classes: data.classes,
    onSave: async payload => {
      try {
        await post('/api/gold/save', { frame_id: data.frame.id, ...payload });
        route();                                  // straight into the next frame
      } catch (e) { toast(e.message, true); }
    },
    onSkip: async () => {
      try {
        await post('/api/gold/save', { frame_id: data.frame.id, boxes: [],
                                       seconds: 0, revealed: false });
        route();
      } catch (e) { toast(e.message, true); }
    },
  });
  LABELER.load(data.frame, data.frame.boxes || []);
}

function livePipeline(runId) {
  if (STREAM) { STREAM.close(); STREAM = null; }
  STREAM = new EventSource(`/api/runs/${runId}/stream`);
  STREAM.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.end) { STREAM.close(); STREAM = null; return; }
    if (d.stages && WS && location.hash.startsWith('#station/')) mountClips(WS.id);
  };
  STREAM.onerror = () => { if (STREAM) { STREAM.close(); STREAM = null; } };
}

/* Per-node output. Each node answers in its own terms — "what did this produce?"
   means parts for Segment and crops for Sample, so one generic table would be useless. */
async function showNodeOutput(runId, node) {
  const box = $('#pipe-out'), body = $('#pipe-out-b');
  if (!box) return;
  box.classList.add('on'); box.setAttribute('aria-hidden', 'false');
  $('#pipe-out-t').textContent = node.label + ' — output';
  body.innerHTML = `<div class="empty"><span class="spin"></span> loading…</div>`;
  const stage = node.def.stage || (node.def.id === 'consensus' ? 'judge' : node.def.id);
  try {
    const d = await api(`/api/runs/${runId}/output/${encodeURIComponent(stage)}`);
    body.innerHTML = nodeOutputHtml(d, node);
  } catch (e) {
    body.innerHTML = `<div class="err"><b>Could not load the output.</b>
      <div class="mono" style="font-size:12px">${esc(e.message)}</div></div>`;
  }
}
function closeNodeOutput() {
  const b = $('#pipe-out'); if (b) { b.classList.remove('on'); b.setAttribute('aria-hidden', 'true'); }
}

function nodeOutputHtml(d, node) {
  const st = d.stage || {};
  const head = `<div class="stage" data-s="${esc(st.status || 'pending')}" style="margin-bottom:16px">
    <div class="nm">${esc(st.status || 'not run')}</div>
    <div class="bar"><i style="width:${st.status === 'done' ? 100 : (st.progress || 0)}%"></i></div>
    <div class="cost">${st.cost_usd ? usd(st.cost_usd) : ''}</div></div>
    ${st.message ? `<p style="color:var(--cc-fg-2);margin:0 0 16px">${esc(st.message)}</p>` : ''}`;
  const rows = d.rows || [];
  if (!rows.length) return head + `<div class="empty">Nothing produced yet.</div>`;
  const tbl = (cols, mk) => `<table><thead><tr>${cols.map(c =>
      `<th${c.r ? ' class="right"' : ''}>${esc(c.t)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(mk).join('')}</tbody></table>`;

  if (d.kind === 'segments') return head + tbl(
    [{ t: 'Part' }, { t: 'Length', r: 1 }, { t: 'Size MB', r: 1 }, { t: 'Grade' }, { t: 'State' }],
    r => `<tr><td class="id">${r.idx}</td><td class="right num">${dur(r.dur_s)}</td>
      <td class="right num">${r.size_mb || 0}${r.compressed_mb ? ` → <b>${r.compressed_mb}</b>` : ''}</td>
      <td>${esc(r.grade || '—')}</td><td>${r.video_id ? '<span class="status ok">extracted</span>' : statusPill(r.status)}</td></tr>`);

  if (d.kind === 'extract') return head + tbl(
    [{ t: 'Part' }, { t: 'Frames', r: 1 }, { t: 'FPS', r: 1 }, { t: 'Size' }],
    r => `<tr><td class="id">${r.idx}</td><td class="right num">${num(r.frames)}</td>
      <td class="right num">${(r.fps || 0).toFixed ? (r.fps || 0).toFixed(1) : r.fps}</td>
      <td>${r.width}×${r.height}</td></tr>`);

  if (d.kind === 'crops') {
    const s = d.summary || {};
    return head + `<p style="color:var(--cc-fg-2);margin:0 0 12px">${num(s.total)} crops ·
      ${num(s.pending)} not yet judged</p>
      <div class="crops" style="gap:6px">${rows.slice(0, 24).map(r =>
        `<img src="/api/crop/${r.id}" alt="crop ${r.id}" title="${esc(CLASSES[r.det_class] || '')} ${(r.det_conf||0).toFixed(2)}"
          style="width:78px;height:78px;object-fit:cover;border-radius:6px;border:1px solid var(--cc-line)">`).join('')}</div>`;
  }

  if (d.kind === 'judgments') {
    const s = d.summary || {};
    const judged = (s.total || 0) - 0;
    return head + `<div class="grid g3" style="gap:8px;margin-bottom:16px">
      ${[['Unanimous', (s.agreed || 0) + (s.reclass || 0), 'ok'],
         ['Contested', s.contested || 0, 'warn'], ['Crops', s.total || 0, 'info']].map(([k, v, c]) =>
        `<div class="tile" style="min-height:auto;padding:12px"><div><div class="lbl">${k}</div>
          <div class="val" style="font-size:20px">${num(v)}</div></div></div>`).join('')}</div>
      ${tbl([{ t: 'Model' }, { t: 'Calls', r: 1 }, { t: 'Errors', r: 1 }, { t: 'Cost', r: 1 }, { t: 'ms', r: 1 }],
        r => `<tr><td class="id">${esc((r.model || '').split('/').pop())}</td>
          <td class="right num">${num(r.n)}</td><td class="right num">${r.errors || 0}</td>
          <td class="right num">${usd(r.usd)}</td><td class="right num">${r.ms || '—'}</td></tr>`)}`;
  }

  if (d.kind === 'dataset') return head + tbl(
    [{ t: 'Dataset' }, { t: 'Train', r: 1 }, { t: 'Val', r: 1 }, { t: 'Fingerprint' }],
    r => `<tr><td class="id">${esc(r.name)}</td><td class="right num">${num(r.n_train)}</td>
      <td class="right num">${num(r.n_val)}</td><td class="mono">${esc(r.fingerprint)}</td></tr>`);

  return head + `<pre class="mono" style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(rows.slice(0, 20), null, 1))}</pre>`;
}

/* The run page: where "Start a run" lands. One primary action computed from the
   stage rows — never a wall of per-stage buttons. Worker stages run in order on the
   backend thread; judging is its own endpoint and only offered once crops exist. */
const WORKER_STAGES = ['probe', 'segment', 'compress', 'extract', 'sample', 'existence', 'complete'];
async function viewPipeline(runId) {
  const r = await api('/api/runs/' + runId);
  PIPE_RUN = r;
  const done = new Set(r.stages.filter(s => s.status === 'done').map(s => s.stage));
  const busy = r.stages.some(s => ['running', 'queued'].includes(s.status));
  const remaining = WORKER_STAGES.filter(s => !done.has(s));
  const crops = r.crops || {};
  const judgeReady = !busy && (crops.pending || 0) > 0;
  const next =
    busy ? null
    : remaining.length ? { label: `Run pipeline — ${remaining.length} stage(s): ${remaining.join(' → ')}`, act: 'run' }
    : judgeReady ? { label: `Judge ${num(crops.pending)} crop(s)`, act: 'judge' }
    : null;
  return `<div class="page">
    <div class="page-head"><div>
      <h1>${esc(r.name || 'run ' + r.id)}</h1>
      <p>${esc((r.source_path || '').split('/').pop())} · ${statusPill(r.status)}
         ${r.cost_total ? ' · ' + usd(r.cost_total) : ''}</p></div>
      <div class="head-actions">
        ${r.site_id ? `<button class="btn secondary" data-route="station" data-arg="${r.site_id}/clips">← Station ${r.site_id}</button>` : ''}
        <button class="btn secondary" data-delrun="${r.id}">Delete run</button></div></div>
    <div class="card"><div class="card-head"><h2>Stages</h2>
      ${next ? `<button class="btn primary" id="pipe-next" data-act="${next.act}">${esc(next.label)}</button>`
             : busy ? `<span class="status run">working…</span>`
             : `<span class="status ok">pipeline complete</span>`}</div>
      <div id="stagebox">${r.stages.map(stageRow).join('')}</div></div>
    ${crops.total ? `<div class="card"><div class="card-head"><h2>Crops</h2>
      ${crops.contested ? `<button class="btn sm secondary" data-review="${r.id}">Review ${
        num(crops.contested)} contested</button>` : ''}</div>
      <div class="grid g4" style="gap:8px">
        ${[['Total', crops.total], ['Unanimous', (crops.agreed || 0) + (crops.reclass || 0)],
           ['Contested', crops.contested], ['Awaiting judge', crops.pending]].map(([k, v]) =>
          `<div class="tile" style="min-height:auto;padding:12px"><div><div class="lbl">${k}</div>
            <div class="val" style="font-size:20px">${num(v || 0)}</div></div></div>`).join('')}
      </div></div>` : ''}
    ${(r.events || []).length ? `<div class="card"><div class="card-head"><h2>Recent events</h2></div>
      <table><tbody>${r.events.slice(0, 12).map(e =>
        `<tr><td class="id">${esc(e.verb || '')}</td><td>${esc(e.object || '')}</td>
         <td style="color:var(--cc-fg-2)">${esc(e.detail || '')}</td>
         <td class="right" style="color:var(--cc-fg-3)">${ago(e.ts)}</td></tr>`).join('')}</tbody></table></div>` : ''}
  </div>`;
}
let PIPE_RUN = null;

function mountPipeline(runId) {
  const r = PIPE_RUN;
  if (!r) return;
  const btn = $('#pipe-next');
  if (btn) btn.onclick = async () => {
    btn.disabled = true;
    try {
      if (btn.dataset.act === 'judge') await post(`/api/runs/${runId}/judge`, {});
      else {
        const done = new Set(r.stages.filter(s => s.status === 'done').map(s => s.stage));
        await post(`/api/runs/${runId}/start`,
                   { stages: WORKER_STAGES.filter(s => !done.has(s)) });
      }
      route(true);
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  };
  if (r.stages.some(s => ['running', 'queued'].includes(s.status))) live(runId);
}

/* ─────────────────────────── router ─────────────────────────── */
let STREAM, RENDER = 0;

async function build(name, arg, extra) {
  if (name === 'videos') return viewVideos();
  if (name === 'pipeline') return viewPipeline(arg);
  if (name === 'stations') return viewStations();
  if (name === 'station') return viewStation(arg, extra);
  if (name === 'gold') return viewGold(arg);
  if (name === 'errors') return viewErrors(arg);
  if (name === 'counts') return viewCounts();
  if (name === 'scene') return viewScene(arg);
  if (name === 'reportcard') return viewReportCard(arg);
  if (name === 'implied') return viewImplied(arg);
  if (name === 'axles') return viewAxles();
  if (name === 'verify') return viewVerify(arg);
  if (name === 'attrs') return viewAttrs();
  if (name === 'attr') return viewAttr(arg, extra);
  if (name === 'preview') return viewPreview(arg);
  if (name === 'goldreview') return viewGoldReview(arg);
  if (name === 'logs') return viewLogs();
  if (name === 'datasets') return viewDatasets();
  if (name === 'judges') return viewJudges();
  if (name === 'review') return viewReview(arg);
  if (name === 'training') return viewTraining();
  if (name === 'trainingrun') return viewTrainingReport(arg);
  if (name === 'costs') return viewCosts();
  if (name === 'settings') return viewSettings();
  return viewOverview();
}

/* One paint per navigation. The old view stays on screen until the new HTML is
   ready, and the spinner only appears if the fetch is slow enough to notice --
   blanking the page first is what made every click flash. */
// A thin top bar: proof the click registered, without touching the content.
function navProgress(on) {
  let el = document.getElementById('navbar-progress');
  if (!el) { el = document.createElement('div'); el.id = 'navbar-progress'; document.body.appendChild(el); }
  if (on) { el.classList.add('on'); el.style.width = '0'; requestAnimationFrame(() => el.style.width = '70%'); }
  else { el.style.width = '100%'; setTimeout(() => { el.classList.remove('on'); el.style.width = '0'; }, 220); }
}

async function route(soft) {
  const h = (location.hash || '#overview').slice(1);
  const [name, arg, extra] = h.split('/');
  const token = ++RENDER;
  navProgress(true);
  markNav(name);
  /* The breadcrumb showed the hierarchy but was inert text, so the one obvious way back
     up did nothing. Every segment above the current page is now a real link. */
  const CRUMBS = {
    videos:      () => [['Stations', '#stations'], ['all footage', null]],
    pipeline:    a => (PIPE_RUN && PIPE_RUN.site_id)
      ? [['Stations', '#stations'], [`station ${PIPE_RUN.site_id}`, `#station/${PIPE_RUN.site_id}/clips`],
         [`run ${a}`, null]]
      : [['Stations', '#stations'], ['all footage', '#videos'], [`run ${a}`, null]],
    counts:      () => [['Stations', '#stations'], ['all counts', null]],
    review:      a => [['Stations', '#stations'], [`run ${a}`, `#run/${a}`], ['verify', null]],
    station:     (a, b) => b ? [['Stations', '#stations'], [`station ${a}`, `#station/${a}`], [b, null]]
                              : [['Stations', '#stations'], [`station ${a}`, null]],
    gold:        a => [['Stations', '#stations'], [`station ${a}`, `#station/${a}`], ['gold set', null]],
    goldreview:  a => [['Stations', '#stations'], [`station ${a}`, `#station/${a}`],
                       ['gold set', `#gold/${a}`], ['labelling', null]],
    errors:      a => [['Stations', '#stations'], [`station ${a}`, `#station/${a}`], ['error sources', null]],
    scene:       (a, b) => b
      ? [['Stations', '#stations'], [`station ${b}`, `#station/${b}/clips`], [`count line`, null]]
      : [['Stations', '#stations'], [`line on clip ${a}`, null]],
    reportcard:  (a, b) => b
      ? [['Stations', '#stations'], [`station ${b}`, `#station/${b}/clips`], ['report card', null]]
      : [['Stations', '#stations'], [`report card ${a}`, null]],
    implied:     a => [['Stations', '#stations'], ['counts', '#counts'],
                       ['report card', `#reportcard/${a}`], ['implied crossings', null]],
    axles:       () => [['Judges', '#judges'], ['axle audit', null]],
    attrs:       () => [['Judges', '#judges'], ['attributes', null]],
    verify:      a => {
      const c = VERIFY_CLIP || {};
      return c.site_id
        ? [['Stations', '#stations'],
           [`${c.station_code || 'station ' + c.site_id}`, `#station/${c.site_id}/clips`],
           [`clip ${c.clock || a}${c.end_clock ? '–' + c.end_clock : ''}`, null],
           ['verify', null]]
        : [['Stations', '#stations'], [`verify clip ${a}`, null]];
    },
    attr:        a => [['Judges', '#judges'], ['attributes', '#attrs'], [String(a), null]],
    preview:     (a, b) => b
      ? [['Stations', '#stations'], [`station ${b}`, `#station/${b}/clips`], ['preview', null]]
      : [['Stations', '#stations'], [`preview ${a}`, null]],
    costs:       () => [['Overview', '#overview'], ['spend ledger', null]],
    trainingrun: a => [['Training', '#training'], [`#${a}`, null]],
  };
  window.CRUMBS = CRUMBS;
  // Painted before the view resolves, so a page that only learns what it is AFTER an
  // await (the verify screen, which fetches its own clip) repaints it on mount.
  CRUMB_CTX = [name, arg, extra];
  paintCrumb();
  const main = $('#main');
  if (STREAM) { STREAM.close(); STREAM = null; }
  if (LABELER && name !== 'goldreview') { LABELER.destroy(); LABELER = null; }
  if (LINE_ED && name !== 'scene') { LINE_ED.destroy?.(); LINE_ED = null; }
  if (ATTR_KEYS) { document.removeEventListener('keydown', ATTR_KEYS); ATTR_KEYS = null; }
  if (VKEYS) { document.removeEventListener('keydown', VKEYS); VKEYS = null; }
  if (name !== 'verify') VQ = null;
  if (name !== 'attr') ATTR_Q = null;
  // A modal left open across a navigation keeps a live editor that can still autosave.
  if (LINE_MODAL) { LINE_MODAL.remove(); LINE_MODAL = null; }
  if (name !== 'preview' && name !== 'scene') clearInterval(RENDER_POLL);
  let slow = null;
  if (!soft && !main.firstChild) {
    slow = setTimeout(() => {
      if (token === RENDER) main.innerHTML =
        `<div class="page"><div class="card"><div class="empty"><span class="spin"></span> loading…</div></div></div>`;
    }, 250);
  }
  try {
    const html = await build(name, arg, extra);
    clearTimeout(slow);
    if (token !== RENDER) return;        // a newer navigation won the race
    main.innerHTML = html;
    wire(name, arg);
    if (name === 'stations') mountStationList();
    if (name === 'pipeline') { mountPipeline(arg); paintCrumb(); }
    if (name === 'goldreview') mountLabeler_();
    if (name === 'scene') mountLineEditor_();
    if (name === 'reportcard') mountReportCharts(arg);
    if (name === 'attr') mountAttrLabeller();
    if (name === 'verify') mountVerify(arg);
    if (name === 'station' && WS && WS.tab === 'clips') mountClips(WS.id);
    if (name === 'station' && WS && WS.tab === 'labels') mountGold(WS.id);
    if (name === 'station' && WS && WS.tab === 'overview') mountOverview(WS.id);
    if (name === 'preview' || name === 'scene') {
      const j = ((await api('/api/render/' + arg).catch(() => ({}))).job) || {};
      if (['queued', 'running'].includes(j.status)) pollRender(arg);
    }
  } catch (e) {
    clearTimeout(slow);
    if (token !== RENDER) return;
    main.innerHTML = `<div class="page"><div class="card"><div class="empty">
      Could not load this page.<div class="mono" style="margin-top:8px">${esc(e.message)}</div></div></div></div>`;
  } finally {
    if (token === RENDER) navProgress(false);
  }
  refreshPills();
}

function live(runId) {
  if (STREAM) { STREAM.close(); STREAM = null; }
  let last = '';
  STREAM = new EventSource(`/api/runs/${runId}/stream`);
  STREAM.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.end) {
      STREAM.close(); STREAM = null;
      route(true);                       // soft: swap content, never blank it
      return;
    }
    const box = $('#stagebox');
    if (!box || !d.stages) return;
    const html = d.stages.map(stageRow).join('');
    if (html === last) return;           // nothing moved -- leave the DOM alone
    last = html;
    box.innerHTML = html;
  };
  STREAM.onerror = () => { if (STREAM) { STREAM.close(); STREAM = null; } };
}

function wire(name, arg) {
  // Honours data-arg like the other binding. Two handlers for the same attribute is
  // already one too many; this one runs last, so a version that dropped the argument
  // silently undid the other and sent every back button to the bare route.
  document.querySelectorAll('[data-route]').forEach(b => b.onclick = () =>
    location.hash = '#' + b.dataset.route + (b.dataset.arg ? '/' + b.dataset.arg : ''));
  document.querySelectorAll('[data-review]').forEach(b => b.onclick = () => location.hash = '#review/' + b.dataset.review);
  // Bound here rather than inside the clips view, because the run page carries the same
  // button and wire() runs last anyway — a scoped copy would simply be overwritten.
  document.querySelectorAll('[data-delrun]').forEach(b => b.onclick = async () => {
    if (!confirm('Delete this run?\n\nIts stage rows and cost records go with it. A run '
               + 'that produced clips will be refused — those clips are still counted.')) return;
    b.disabled = true;
    try {
      await api('/api/runs/' + b.dataset.delrun, { method: 'DELETE' });
      toast('run deleted');
      if (location.hash.startsWith('#pipeline/'))
        location.hash = (PIPE_RUN && PIPE_RUN.site_id) ? `#station/${PIPE_RUN.site_id}/clips` : '#videos';
      else if (WS && WS.tab === 'clips') mountClips(WS.id);
      else route(true);
    } catch (e) { b.disabled = false; toast(e.message, true); }
  });
  document.querySelectorAll('[data-training]').forEach(b =>
    b.onclick = () => location.hash = '#trainingrun/' + b.dataset.training);
  document.querySelectorAll('[data-station]').forEach(b =>
    b.onclick = () => location.hash = '#station/' + b.dataset.station);
  document.querySelectorAll('[data-wstab]').forEach(b => b.onclick = () => {
    // Swap the body in place rather than re-route: the station data is already loaded,
    // and a refetch here would blank the page for a tab change.
    if (!WS) return;
    WS.tab = b.dataset.wstab;
    history.replaceState(null, '', `#station/${WS.id}/${WS.tab}`);
    document.querySelectorAll('[data-wstab]').forEach(x =>
      x.classList.toggle('on', x.dataset.wstab === WS.tab));
    $('#wsbody').innerHTML = wsBody(WS.tab, WS.s, WS.id);
    wire('station', WS.id);
    if (WS.tab === 'clips') mountClips(WS.id);
    if (WS.tab === 'labels') mountGold(WS.id);
    if (WS.tab === 'overview') mountOverview(WS.id);
  });
  const bb = $('#browseFolder');
  if (bb) bb.onclick = () => {
    const m = document.createElement('div');
    m.className = 'modal-wrap';
    m.innerHTML = `<div class="modal" style="width:min(560px,100%)">
      <div class="card-head"><div><h2>Pick the station folder</h2>
        <p id="bp_path" class="mono"></p></div>
        <div class="head-actions">
          <button class="btn secondary" id="bp_cancel">Cancel</button>
          <button class="btn primary" id="bp_use">Use this folder</button></div></div>
      <div class="modal-body" id="bp_list" style="max-height:50vh;overflow:auto"></div></div>`;
    document.body.appendChild(m);
    let cur = $('#folderPath').value.trim() || '/Volumes';
    async function show(path) {
      try {
        const r = await api('/api/browse?path=' + encodeURIComponent(path));
        cur = r.path;
        m.querySelector('#bp_path').textContent =
          `${r.path} — ${r.videos_here} video file(s) here`;
        m.querySelector('#bp_list').innerHTML =
          (r.parent ? `<button class="bp-dir" data-p="${esc(r.parent)}">↑ up one level</button>` : '')
          + r.dirs.map(d => `<button class="bp-dir" data-p="${esc(d.path)}">
              📁 ${esc(d.name)}${d.videos > 0 ? ` <span class="chip ok">${d.videos} videos</span>`
              : d.videos < 0 ? ' <span class="chip">no access</span>' : ''}</button>`).join('')
          || '<div class="empty">No subfolders.</div>';
        m.querySelectorAll('.bp-dir').forEach(b => b.onclick = () => show(b.dataset.p));
      } catch (e) { toast(e.message, true); }
    }
    m.querySelector('#bp_cancel').onclick = () => m.remove();
    m.onclick = e => { if (e.target === m) m.remove(); };
    m.querySelector('#bp_use').onclick = () => {
      $('#folderPath').value = cur; m.remove();
      $('#fetchFolder').click();          // picking a folder means you want to see it
    };
    show(cur);
  };
  document.querySelectorAll('[data-detach]').forEach(b => b.onclick = async () => {
    if (!confirm('Detach this file from the station?\n\nThe file and its record both stay — '
               + 'only the link to this station goes, so it can be attached again later.')) return;
    b.disabled = true;
    try {
      const r = await post(`/api/footage/${b.dataset.detach}/detach`, {});
      toast(r.already ? 'Already detached' : `Detached ${r.name}`);
      mountOverview(WS.id);
    } catch (e) { b.disabled = false; toast(e.message, true); }
  });
  const sl = $('#stationLine');
  if (sl) sl.onclick = () => openStationLine(WS.id, (WS.s || {}).code);
  const fp = $('#folderPath'), ab = $('#attachFolder'), pv = $('#folderPreview');
  // "Fetch videos" appears twice — in the picker and beside Process — so it is bound by
  // class. Two elements sharing an id meant only the hidden one ever got a handler.
  document.querySelectorAll('.js-fetch').forEach(fb => fb.onclick = async () => {
    const path = (fp && fp.value.trim()) || '';
    fb.disabled = true;
    try {
      // Reads the folder and writes nothing: this is the "what is actually in there
      // right now" answer, including files the station has never seen.
      const r = await post(`/api/stations/${WS.id}/folder-preview`, { path });
      pv.innerHTML = `<div class="card-body" style="padding-bottom:0"><b style="font-weight:500">
        ${r.files.length} video file(s) in ${esc(r.path)} right now</b></div>
        <table><thead><tr><th>File</th><th class="right">Size</th>
        <th>Clock from name</th><th>Status</th></tr></thead><tbody>
        ${r.files.map(x => `<tr><td class="id">${esc(x.name)}</td>
          <td class="right num">${x.size_mb} MB</td>
          <td class="mono">${esc(x.clock_guess || '— set manually after attach')}</td>
          <td>${x.already_known ? '<span class="chip">already attached</span>'
              : '<span class="chip ok">new</span>'}</td></tr>`).join('')
          || '<tr><td colspan="4" class="empty">No video files in that folder.</td></tr>'}
        </tbody></table>
        <div class="card-body muted">Nothing has been written. Press <b>Process footage</b>
          to apply the difference between this and what the station currently holds.</div>`;
      if (ab) ab.hidden = !r.files.some(x => !x.already_known);
    } catch (e) { pv.innerHTML = `<div class="card-body" style="color:var(--cc-bad)">${esc(e.message)}</div>`; }
    fb.disabled = false;
  });
  const runProcess = async btn => {
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = 'Processing…';
    try {
      const r = await post(`/api/stations/${WS.id}/process`, {});
      const bits = [
        r.added.length ? `<span class="chip ok">+${r.added.length} attached</span>` : '',
        r.missing_now.length ? `<span class="chip bad">${r.missing_now.length} gone from disk</span>` : '',
        r.changed.length ? `<span class="chip warn">${r.changed.length} re-probed</span>` : '',
        r.promoted.length ? `<span class="chip">${r.promoted.length} promoted to primary</span>` : '',
      ].filter(Boolean).join(' ');
      toast(bits ? 'Folder reconciled' : 'Already in step with the folder — nothing changed');
      // Everything on the page is derived from this, so re-render the whole station:
      // header hours, stage rail, tab counts and the Overview body all move together.
      // Updating only the panel that ran is how the header came to disagree with it.
      await route(true);
      const box = document.getElementById('folderPreview');
      if (box) box.innerHTML = `<div class="card-body">${bits || '<span class="muted">No differences found.</span>'}</div>`;
    } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = label; }
  };
  const cf = $('#changeFolder');
  if (cf) cf.onclick = () => { const c = $('#folderChoose'); c.hidden = !c.hidden; };
  const pb = $('#processFolder');
  if (pb) pb.onclick = () => runProcess(pb);
  if (ab) ab.onclick = async () => {
    ab.disabled = true;
    try {
      const r = await post(`/api/stations/${WS.id}/folder`, { path: fp.value.trim() });
      toast(`attached ${r.attached} file(s)`);
      await runProcess(ab);               // attach flows straight into processing
    } catch (e) { ab.disabled = false; toast(e.message, true); }
  };

  const eb = $('#editStation');
  if (eb) eb.onclick = async () => {
    const st = WS.s;
    const F = (id, label, val, ph='') => `<div><label class="lbl">${label}</label>
      <input class="field" id="${id}" value="${esc(val ?? '')}" placeholder="${esc(ph)}"></div>`;
    const m = document.createElement('div');
    m.className = 'modal-wrap'; m.id = 'stationModal';
    m.innerHTML = `<div class="modal">
      <div class="card-head"><div><h2>Edit station — ${esc(st.code || '')}</h2>
        <p>everything about this station in one place; Save writes it all at once</p></div>
        <div class="head-actions">
          <button class="btn secondary" id="mCancel">Cancel</button>
          <button class="btn primary" id="mSave">Save & close</button></div></div>
      <div class="modal-body">
        <div class="grid g2" style="gap:14px">
          ${F('m_name','Name *', st.name)}
          ${F('m_code','Code', st.code)}
          ${F('m_road','Road name', st.road_name, 'e.g. NH-167K')}
          ${F('m_chain','Chainage', st.chainage, 'e.g. Km 12+400')}
          ${F('m_dist','District', st.district)}
          ${F('m_state','State', st.state)}
          ${F('m_cam','Camera id', st.camera_id, 'e.g. ch01')}
          ${F('m_notes','Notes', st.notes)}
        </div>
        <label class="lbl" style="margin-top:12px;display:block">Location & camera direction</label>
        <div id="st_map" class="st-map"></div>
        <div class="muted" id="st_geo_pick" style="margin-top:6px">${st.lat
          ? `${(+st.lat).toFixed(6)}, ${(+st.lon).toFixed(6)} · camera facing ${Math.round(st.bearing ?? 0)}°`
          : 'no location set — drag the pin'}</div>
        <span id="st_brg" hidden></span>
      </div></div>`;
    document.body.appendChild(m);
    let g = { lat: st.lat || 15.5, lon: st.lon || 78.5, bearing: st.bearing ?? 90 };
    let moved = false;
    await openStationMap(m.querySelector('#st_map'), g, x => {
      g = x; moved = true;
      m.querySelector('#st_geo_pick').textContent =
        `${x.lat.toFixed(6)}, ${x.lon.toFixed(6)} · camera facing ${Math.round(x.bearing ?? 0)}°`;
    });
    const close = () => m.remove();
    m.querySelector('#mCancel').onclick = close;
    m.onclick = e => { if (e.target === m) close(); };
    m.querySelector('#mSave').onclick = async () => {
      const V = id => m.querySelector('#' + id).value.trim() || null;
      if (!V('m_name')) return toast('A station needs a name', true);
      try {
        await api('/api/stations/' + WS.id, { method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: V('m_name'), code: V('m_code'), road_name: V('m_road'),
            chainage: V('m_chain'), district: V('m_dist'), state: V('m_state'),
            camera_id: V('m_cam'), notes: V('m_notes'),
            // Only write the location if it was actually touched — saving the form
            // must not overwrite a surveyed position with the map's initial state.
            ...(moved ? { lat: g.lat, lon: g.lon, bearing: g.bearing, geo_source: 'map' } : {}),
          }) });
        toast('station saved'); close(); location.reload();
      } catch (e) { toast(e.message, true); }
    };
    return;
  };

  document.querySelectorAll('[data-wstab-go]').forEach(b => b.onclick = () =>
    document.querySelector(`[data-wstab="${b.dataset.wstabGo}"]`)?.click());
  document.querySelectorAll('[data-gold]').forEach(b =>
    b.onclick = () => location.hash = '#gold/' + b.dataset.gold);
  document.querySelectorAll('[data-errors]').forEach(b =>
    b.onclick = () => location.hash = '#errors/' + b.dataset.errors);
  // Carry the station through. These pages are reached FROM a station, so "back" must
  // return there; without it the breadcrumb pointed at #counts — a tab that no longer
  // exists — and the reader was stranded one click from where they started.
  const from = () => (WS && WS.id) ? '/' + WS.id : '';
  document.querySelectorAll('[data-scene]').forEach(b =>
    b.onclick = () => location.hash = '#scene/' + b.dataset.scene + from());
  document.querySelectorAll('[data-reportcard]').forEach(b =>
    b.onclick = () => location.hash = '#reportcard/' + b.dataset.reportcard + from());
  document.querySelectorAll('[data-implied]').forEach(b =>
    b.onclick = () => location.hash = '#implied/' + b.dataset.implied);
  document.querySelectorAll('[data-verify]').forEach(b =>
    b.onclick = () => location.hash = '#verify/' + b.dataset.verify);
  document.querySelectorAll('[data-attr]').forEach(b =>
    b.onclick = () => location.hash = '#attr/' + b.dataset.attr);
  document.querySelectorAll('[data-axle]').forEach(b => b.onclick = async () => {
    // Optimistic within the card only: the answer is a training label, so a failed
    // write must not leave a button looking chosen.
    const card = b.closest('[data-check]');
    const was = !!card.querySelector('.saved.on:not(.failed)');
    card.querySelectorAll('[data-axle]').forEach(o => o.className = 'btn sm secondary');
    b.className = 'btn sm primary';
    const flag = card.querySelector('[data-saved]');
    flag.textContent = 'Saving…'; flag.className = 'saved on pending';
    try {
      await post('/api/axles/' + b.dataset.axle, { answer: b.dataset.ans });
      flag.textContent = 'Saved'; flag.className = 'saved on';
      const c = document.querySelector('[data-answered]');
      if (c && !was) c.textContent = (+c.textContent.replace(/\D/g, '') + 1).toLocaleString();
    } catch (e) {
      // The label is the deliverable here, so a failed write must look failed.
      b.className = 'btn sm secondary';
      flag.textContent = 'Not saved'; flag.className = 'saved on failed';
      toast(e.message, true);
    }
  });
  const _fromPrev = () => (WS && WS.id) ? '/' + WS.id : '';
  document.querySelectorAll('[data-preview]').forEach(b =>
    b.onclick = () => location.hash = '#preview/' + b.dataset.preview + _fromPrev());
  document.querySelectorAll('[data-delrender]').forEach(b => b.onclick = async () => {
    // Destructive but cheap and reversible-by-rebuild — still worth confirming, because
    // the file is 100+ MB and a re-render is a minute of waiting.
    const id = b.dataset.delrender;
    if (!confirm('Delete this rendered video?\n\nThe count and the report card are '
      + 'unaffected — only the annotated playback file goes. Re-rendering takes about a minute.'))
      return;
    b.disabled = true;
    try {
      const r = await api('/api/annotated/' + id, { method: 'DELETE' });
      toast(r.deleted ? `Render deleted — ${r.mb} MB freed` : 'Nothing to delete');
      route(true);
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });

  const cr = $('#cleanrenders');
  if (cr) cr.onclick = async () => {
    cr.disabled = true;
    try {
      const r = await post('/api/annotated/cleanup', {});
      toast(r.removed.length ? `${r.removed.length} file(s) removed — ${r.freed_mb} MB freed`
                             : 'Nothing to clean up');
      route(true);
    } catch (e) { toast(e.message, true); cr.disabled = false; }
  };

  document.querySelectorAll('[data-render]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    const id = b.dataset.render;
    try {
      await post('/api/render/' + id, {});
      toast('Render started — about 60–110 seconds');
      $('#renderprogress')?.removeAttribute('hidden');
      pollRender(id);                      // works on the line page and the preview page
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });
  const ev = $('#errvid');
  if (ev) ev.onchange = () => { ERR_VIDEO[arg] = ev.value; route(true); };
  document.querySelectorAll('[data-goldreview]').forEach(b =>
    b.onclick = () => location.hash = '#goldreview/' + b.dataset.goldreview);

  document.querySelectorAll('[data-goldscore]').forEach(b => b.onclick = async () => {
    const id = b.dataset.goldscore;
    b.disabled = true; b.textContent = 'Running the model…';
    try {
      const r = await api(`/api/gold/${id}/score`);
      if (r.error) { toast(r.error, true); return; }
      GOLD_SCORE[id] = r;
      toast(`${r.model_id}: recall ${(r.recall * 100).toFixed(1)}%, ${r.missed} missed`);
      route(true);
    } catch (e) { toast(e.message, true); }
    finally { b.disabled = false; b.textContent = 'Score a model'; }
  });

  document.querySelectorAll('[data-goldbuild]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = 'Cutting frames…';
    try {
      const r = await post('/api/gold/build', { site_id: +b.dataset.goldbuild, n_frames: 60 });
      toast(`${r.frames} frame(s) ready to label`);
      route(true);
    } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = 'Build gold set'; }
  });

  const ns2 = $('#newstation2');
  if (ns2) ns2.onclick = () => { const f = $('#stationform'); f.hidden = false; $('#st_name').focus(); };
  const ns = $('#newstation'), sform = $('#stationform');
  if (ns) ns.onclick = () => {
    sform.hidden = !sform.hidden;
    if (!sform.hidden) $('#st_name').focus();
  };
  const cs = $('#cancelstation');
  if (cs) cs.onclick = () => { sform.hidden = true; };
  // Location search: type, pick, done — the same geocode the survey app uses.
  let GEO = null, geoT = null;
  const gi = $('#st_geo');
  if (gi) gi.oninput = () => {
    clearTimeout(geoT);
    geoT = setTimeout(async () => {
      const q = gi.value.trim();
      if (q.length < 3) return;
      const r = await api('/api/geocode?q=' + encodeURIComponent(q)).catch(() => ({results: []}));
      $('#st_geo_out').innerHTML = (r.results || []).slice(0, 5).map((x, i) =>
        `<button class="geo-hit" data-gi="${i}">${esc(x.name || x.display_name || '')}</button>`).join('');
      $('#st_geo_out').querySelectorAll('[data-gi]').forEach(b => b.onclick = () => {
        const x = r.results[+b.dataset.gi];
        GEO = { lat: +x.lat, lon: +x.lon, geo_source: 'map' };
        $('#st_geo_out').innerHTML = '';
        openStationMap($('#st_map'), GEO, g => {
          GEO = { ...g, geo_source: 'map' };
          $('#st_geo_pick').textContent =
            `${g.lat.toFixed(6)}, ${g.lon.toFixed(6)} · camera facing ${Math.round(g.bearing ?? 0)}°`;
        });
        $('#st_geo_pick').textContent = `${(+x.lat).toFixed(6)}, ${(+x.lon).toFixed(6)} — drag the pin to the exact camera spot`;
      });
    }, 350);
  };
  const ss = $('#savestation');
  if (ss) ss.onclick = async () => {
    const name = $('#st_name').value.trim();
    if (!name) return toast('A station needs a name', true);
    ss.disabled = true;
    try {
      const r = await post('/api/stations', {
        ...(GEO || {}),
        name, code: $('#st_code').value.trim() || null,
        road_name: $('#st_road').value.trim() || null,
        chainage: $('#st_chain').value.trim() || null,
        district: $('#st_dist').value.trim() || null,
        state: $('#st_state').value.trim() || null,
        camera_id: $('#st_cam').value.trim() || null,
        carriageway: $('#st_cw').value.trim() || null,
      });
      toast('Station created');
      location.hash = '#station/' + r.id;
    } catch (e) { toast(e.message, true); ss.disabled = false; }
  };

  const sf = $('#scanfootage');
  if (sf) sf.onclick = async () => {
    sf.disabled = true; sf.textContent = 'Scanning…';
    try {
      const r = await post('/api/stations/scan', {});
      toast(`${r.found} file(s) seen, ${r.added} new` +
            (r.duplicates ? `, ${r.duplicates} marked as a duplicate or excerpt` : ''));
      route(true);
    } catch (e) { toast(e.message, true); }
    finally { sf.disabled = false; sf.textContent = 'Scan footage folders'; }
  };

  // Starting a run from a station carries the station with it, so the dataset and any
  // model trained from it stay attributable to where the footage came from.
  /* Creating a run makes a draft row and opens the editor. It does NOT start work --
     that is the whole point of the change: the run begins when you press Run. */
  const mkRun = async (path, label, btn) => {
    if (btn) btn.disabled = true;
    try {
      const r = await post('/api/runs', { source_path: path, name: (label || '').replace(/\.mp4$/, '') });
      location.hash = '#pipeline/' + r.id;
    } catch (e) { toast(e.message, true); if (btn) btn.disabled = false; }
  };
  document.querySelectorAll('[data-new]').forEach(b =>
    b.onclick = () => mkRun(b.dataset.new, b.dataset.name, b));

  const sn = $('#startnew');
  if (sn) sn.onclick = async () => {
    // Straight to the editor when there is one obvious file; otherwise pick first.
    if (name !== 'videos') return location.hash = '#videos';
    const vids = await api('/api/videos-on-disk').catch(() => []);
    const fresh = vids.find(v => !v.used) || vids[0];
    if (!fresh) return toast('No footage on disk to run', true);
    mkRun(fresh.path, fresh.name, sn);
  };

  const vw = $('#verifyw');
  if (vw) vw.onclick = async () => {
    vw.disabled = true;
    try {
      const r = await post('/api/weights/verify', {});
      toast(r.problems.length
        ? `${r.problems.length} of ${r.checked} archived weight file(s) FAILED verification`
        : `All ${r.checked} archived weight files verified — hashes match`, !!r.problems.length);
      route(true);
    } catch (e) { toast(e.message, true); } finally { vw.disabled = false; }
  };

  /* `data-start`, `data-stages` and `data-judge` handlers lived here for the old run
     page, which offered one button per stage. mountPipeline() replaced them with a
     single computed action, so they had no emitters left — a handler with no button is
     the same latent trap as a button with no handler, and re-adding one would silently
     bypass the "what runs next" logic. */

  const sj = $('#savejudges');
  if (sj) sj.onclick = async () => {
    const models = [...document.querySelectorAll('[data-model]')].filter(c => c.checked).map(c => c.dataset.model);
    try { await post('/api/judges', { models }); toast(`${models.length} judge(s) saved`); }
    catch (e) { toast(e.message, true); }
  };
  const rb = $('#runbake');
  if (rb) rb.onclick = async () => {
    const models = [...document.querySelectorAll('[data-model]')].filter(c => c.checked).map(c => c.dataset.model);
    if (!models.length) return toast('Tick at least one model first', true);
    try {
      await post('/api/bakeoff', { models, n: 30 });
      toast('Bake-off running — results appear here when done');
      setTimeout(route, 20000);
    } catch (e) { toast(e.message, true); }
  };

  document.querySelectorAll('[data-verdict]').forEach(b => b.onclick = async () => {
    try {
      await post('/api/review/verdict', { crop_id: +b.dataset.verdict, class_id: +b.dataset.class });
      route(true);                       // next crop, without blanking the screen
    } catch (e) { toast(e.message, true); }
  });

  document.querySelectorAll('[data-adopt]').forEach(b => b.onclick = async () => {
    try { await post('/api/pods/adopt', { pod_id: b.dataset.adopt }); toast('Tracking pod'); route(); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll('[data-stop]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await post(`/api/pods/${b.dataset.stop}/stop`, {});
      toast(r.verified ? `Pod terminated — verified. Cost ${usd(r.cost_usd)}`
        : 'TERMINATION NOT CONFIRMED — pod may still be billing', !r.verified);
      route();
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });
  const ad = $('#adopt');
  if (ad) ad.onclick = async () => {
    const id = $('#podid').value.trim();
    if (!id) return toast('Enter a pod id', true);
    try { await post('/api/pods/adopt', { pod_id: id }); toast('Tracking pod'); route(); }
    catch (e) { toast(e.message, true); }
  };

  const sk = $('#savekeys');
  if (sk) sk.onclick = async () => {
    const or = $('#k_or').value.trim(), rp = $('#k_rp').value.trim();
    try {
      if (or) await post('/api/settings', { key: 'openrouter_key', value: or });
      if (rp) await post('/api/settings', { key: 'runpod_key', value: rp });
      toast('Keys saved'); route();
    } catch (e) { toast(e.message, true); }
  };
  const sg = $('#saveguards');
  if (sg) sg.onclick = async () => {
    try {
      await post('/api/settings', { key: 'judge_budget_usd', value: $('#s_budget').value.trim() });
      await post('/api/settings', { key: 'idle_stop_minutes', value: $('#s_idle').value.trim() });
      toast('Guard rails saved');
    } catch (e) { toast(e.message, true); }
  };
}

buildNav();
window.addEventListener('hashchange', () => route());
route();
setInterval(refreshPills, 30000);
