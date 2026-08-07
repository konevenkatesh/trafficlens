/* TrafficLens shared shell — sidebar, topbar, theme, hash router, four-states helpers
   and the small chart set. Vanilla, no build step, used by both apps.

   Charts follow the project's viz rules: one series means no legend and identity comes
   from the labels, so magnitude is the only job colour does. The two-hue pair used for
   direction splits is validated (light and dark) — see ui.css --cc-series-*. */

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export const num = (v, d = 0) => Number(v || 0).toLocaleString('en-IN',
  { minimumFractionDigits: d, maximumFractionDigits: d });

export const ago = ts => {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
};

export const dur = s => {
  s = Number(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
};

/* ── API. Errors carry the server's message, never a bare status code. ── */
export async function api(path, opt) {
  const r = await fetch(path, opt);
  const t = await r.text();
  let d;
  try { d = t ? JSON.parse(t) : {}; } catch { d = { detail: t.slice(0, 300) }; }
  if (!r.ok) throw new Error(d.detail || d.message || `${r.status} ${r.statusText}`);
  return d;
}
const send = (method, p, body) => api(p, {
  method, headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});
export const post = (p, body) => send('POST', p, body);
export const patch = (p, body) => send('PATCH', p, body);

/* ── the four states ── */
export const skeleton = (rows = 3) =>
  `<div>${Array.from({ length: rows },
    (_, i) => `<div class="sk sk-line" style="width:${[92, 74, 60][i % 3]}%"></div>`).join('')}</div>`;

/** Two distinct empties: nothing exists yet (offer the action) vs nothing matches (offer to clear). */
export const empty = (head, body, action = '') =>
  `<div class="empty"><h3>${esc(head)}</h3><p>${esc(body)}</p>${action}</div>`;

/** Scoped and inline — a failed card must not blank the page around it. */
export const errorBox = (msg, retryAttr = '') =>
  `<div class="err"><div style="flex:1"><b>Could not load this.</b>
    <div class="mono" style="font-size:12px;margin-top:4px">${esc(msg)}</div></div>
    ${retryAttr ? `<button class="btn sm secondary" ${retryAttr}>Retry</button>` : ''}</div>`;

let toastTimer;
/** Toasts only for results you cannot see. State change is its own confirmation. */
export function toast(msg, bad = false) {
  let el = $('#toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.innerHTML = esc(msg);
  el.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  if (!bad) toastTimer = setTimeout(() => (el.className = 'toast'), 5000);
  else el.onclick = () => (el.className = 'toast');
}

/* ── shell ── */
export function mountShell({ wordmark, groups, onRoute }) {
  document.body.innerHTML = `
<a class="skip" href="#main">Skip to main content</a>
<div class="app" id="app" data-rail="0">
  <nav class="side" aria-label="Main">
    <div class="side-top">
      <div class="wordmark"><i>◈</i> <span>${esc(wordmark)}</span></div>
      <button class="icon-btn" id="rail" aria-label="Collapse sidebar" aria-expanded="true">
        <svg class="ico-close" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m16 9-3 3 3 3"/></svg>
        <svg class="ico-open" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m13 9 3 3-3 3"/></svg>
      </button>
    </div>
    <div class="side-scroll">${groups.map(g => `
      <div class="grp"><span>${esc(g.label)}</span></div>
      <div class="nav">${g.items.map(it => `
        <button class="item" data-route="${esc(it.id)}" title="${esc(it.label)}">
          ${it.icon}<span>${esc(it.label)}</span></button>`).join('')}</div>`).join('')}
    </div>
  </nav>
  <div class="scrim" id="scrim"></div>
  <div class="main">
    <header class="top">
      <button class="icon-btn hamburger" id="burger" aria-label="Open navigation" aria-expanded="false">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>
      <div class="crumb" id="crumb"></div>
      <div id="topslot" style="display:flex;gap:8px;align-items:center"></div>
      <button class="icon-btn" id="theme" aria-label="Toggle theme" title="Toggle theme">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.4-6.4-.7.7M6.3 17.7l-.7.7m12.8 0-.7-.7M6.3 6.3l-.7-.7"/><circle cx="12" cy="12" r="4"/></svg>
      </button>
    </header>
    <main id="main"></main>
  </div>
</div>`;

  const app = $('#app');
  if (localStorage.tlTheme) document.documentElement.dataset.theme = localStorage.tlTheme;
  if (localStorage.tlRail) app.dataset.rail = localStorage.tlRail;

  $('#rail').onclick = () => {
    const on = app.dataset.rail === '1';
    app.dataset.rail = on ? '0' : '1';
    $('#rail').setAttribute('aria-expanded', on ? 'true' : 'false');
    $('#rail').setAttribute('aria-label', on ? 'Collapse sidebar' : 'Expand sidebar');
    localStorage.tlRail = app.dataset.rail;
    localStorage.tlRailByUser = '1';       // an explicit choice outranks the auto-rail
  };

  const setDrawer = open => {
    app.dataset.drawer = open ? '1' : '0';
    $('#burger').setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  $('#burger').onclick = () => setDrawer(app.dataset.drawer !== '1');
  $('#scrim').onclick = () => setDrawer(false);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setDrawer(false); });

  // Auto-rail on narrow desktops is a default, not a lock — never a hard-coded CSS width.
  const autoRail = () => {
    if (localStorage.tlRailByUser === '1') return;
    app.dataset.rail = window.innerWidth < 1024 && window.innerWidth > 768 ? '1' : '0';
  };
  autoRail();
  window.addEventListener('resize', () => { autoRail(); if (innerWidth > 768) setDrawer(false); });
  $('#theme').onclick = () => {
    const d = document.documentElement.dataset.theme === 'dark';
    document.documentElement.dataset.theme = d ? 'light' : 'dark';
    localStorage.tlTheme = document.documentElement.dataset.theme;
    window.dispatchEvent(new Event('tl-theme'));
  };
  // Choosing in the rail expands it — you cannot pick confidently from icons alone.
  $$('[data-route]').forEach(b => b.onclick = () => {
    location.hash = '#' + b.dataset.route;
    setDrawer(false);                     // picking a destination closes the mobile drawer
    // Choosing in the rail expands it — you cannot pick confidently from icons alone.
    if (app.dataset.rail === '1' && innerWidth > 768) {
      app.dataset.rail = '0'; localStorage.tlRail = '0';
    }
  });
  window.addEventListener('hashchange', () => onRoute(currentRoute()));
  return { route: () => onRoute(currentRoute()) };
}

export const currentRoute = () => {
  const [name, ...rest] = (location.hash || '#').slice(1).split('/');
  return { name: name || '', arg: rest.join('/') };
};

export function markNav(name) {
  $$('[data-route]').forEach(b =>
    b.setAttribute('aria-current', b.dataset.route === name ? 'page' : 'false'));
}

/** One paint per navigation: keep the old view until the new HTML is ready, and only
    show a skeleton if the fetch is slow enough to notice. Blanking first is what makes
    an app flicker. */
export function navProgress(on) {
  let el = document.getElementById('navbar-progress');
  if (!el) { el = document.createElement('div'); el.id = 'navbar-progress'; document.body.appendChild(el); }
  if (on) { el.classList.add('on'); el.style.width = '0'; requestAnimationFrame(() => el.style.width = '70%'); }
  else { el.style.width = '100%'; setTimeout(() => { el.classList.remove('on'); el.style.width = '0'; }, 220); }
}

export function makeRenderer(mainSel = '#main') {
  let token = 0;
  return async function render(build, { skeletonHtml } = {}) {
    const me = ++token, main = $(mainSel);
    navProgress(true);
    let t = null;
    if (skeletonHtml && !main.firstChild) {
      t = setTimeout(() => { if (me === token) main.innerHTML = skeletonHtml; }, 200);
    }
    try {
      const html = await build();
      clearTimeout(t);
      if (me !== token) return false;
      main.innerHTML = html;
      navProgress(false);
      return true;
    } catch (e) {
      clearTimeout(t);
      if (me !== token) return false;
      main.innerHTML = `<div class="page">${errorBox(e.message)}</div>`;
      navProgress(false);
      return false;
    }
  };
}

/* ═══════════════ charts ═══════════════
   Deliberately small: a column chart for volume over time and a ranked bar list for
   composition. Both single-series, so no legend and no categorical palette — the
   labels carry identity. Hover is on by default. */

/** Vertical columns for a time series. data: [{label, value, sub}]
    Bars are width-capped so a three-hour survey doesn't render as giant slabs, and the
    peak is marked with a direct label rather than a second violet — the accent and the
    viz hue are too close to read as a difference. */
export function columnChart(data, { height = 180, valueFmt = num, emphasis = null,
                                    barMax = 72 } = {}) {
  if (!data.length) return empty('No data yet', 'Counts appear here once a video is processed.');
  const max = Math.max(1, ...data.map(d => d.value));
  // Budget: value label + axis label + (peak chip). Reserve it, or the tallest column
  // pushes its chip out of the box and into the card header.
  const chrome = 34 + (emphasis != null ? 20 : 0);
  return `<div class="chart" style="height:${height}px;display:flex;align-items:flex-end;
      gap:8px;padding-top:4px;justify-content:${data.length < 6 ? 'flex-start' : 'space-between'}">
    ${data.map((d, i) => {
      const h = Math.max(2, (d.value / max) * (height - chrome));
      const hot = emphasis != null && i === emphasis;
      return `<div style="flex:1 1 auto;max-width:${barMax}px;display:flex;flex-direction:column;
          align-items:center;justify-content:flex-end;gap:4px;min-width:0"
          tabindex="0" role="img"
          aria-label="${esc(d.label)}: ${valueFmt(d.value)} vehicles${hot ? ', busiest hour' : ''}${
            d.muted ? ', partial footage coverage' : ''}"
          title="${esc(d.label)} — ${valueFmt(d.value)}${d.sub ? ' · ' + esc(d.sub) : ''}">
        ${hot ? '<span class="chip" style="font-size:10px;padding:0 6px">peak</span>' : ''}
        <span style="font-size:11px;color:var(--cc-fg-3);font-variant-numeric:tabular-nums">${
          d.value ? valueFmt(d.value) : ''}</span>
        <i class="${d.muted ? 'col-partial' : ''}"
           style="display:block;width:100%;height:${h}px;border-radius:4px 4px 0 0;
           background:var(--cc-viz);outline:${hot ? '2px solid var(--cc-accent)' : 'none'};
           outline-offset:1px"></i>
        <span style="font-size:10px;color:var(--cc-fg-3);white-space:nowrap;overflow:hidden;
           text-overflow:ellipsis;max-width:100%">${esc(d.label)}</span>
      </div>`;
    }).join('')}
  </div>`;
}

/** Ranked horizontal bars. Sorted by magnitude, single hue — labels give identity. */
export function rankedBars(items, { valueFmt = num, max: forced } = {}) {
  if (!items.length) return empty('Nothing counted yet', 'Draw a count line and extract a video.');
  const sorted = [...items].sort((a, b) => b.value - a.value);
  const max = forced || Math.max(1, ...sorted.map(d => d.value));
  return `<div class="bars">${sorted.map(d => `
    <div class="barrow" title="${esc(d.label)} — ${valueFmt(d.value)}">
      <div class="k">${esc(d.label)}</div>
      <div class="t"><i style="width:${(100 * d.value / max).toFixed(1)}%"></i></div>
      <div class="v">${valueFmt(d.value)}</div>
    </div>`).join('')}</div>`;
}

/** Grade strip — status colours, never reused for series, always with the letter shown. */
export function gradeStrip(grades) {
  const tone = { A: 'ok', B: 'ok', C: 'warn', D: 'bad' };
  if (!grades.length) return '';
  return `<div style="display:flex;gap:6px;flex-wrap:wrap">${grades.map(g =>
    `<span class="status ${tone[g.grade] || 'idle'}" title="${esc(g.name)}">
       ${esc(g.grade || '?')} <span style="opacity:.7">${esc(g.short || '')}</span></span>`).join('')}</div>`;
}
