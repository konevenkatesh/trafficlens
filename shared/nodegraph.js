/* Node-graph pipeline view — a ComfyUI-style read-out of where a job actually is.

   Used where the pipeline genuinely forks: the survey app's three attribute passes run
   in parallel off counting, and the Lab fans out across segments and three judges. A
   straight list hides that; a graph shows it at a glance.

   Deliberately NOT a general graph editor. Nodes are laid out from the data (longest-path
   layering), not dragged by hand — the shape of a pipeline is a fact, not a preference.

   Implementation: HTML nodes absolutely positioned over an SVG edge layer, the whole
   thing inside one transformed viewport. HTML nodes because they style from our own
   tokens and wrap text properly; SVG edges because curves are what SVG is for.
   No dependencies. */

const NS = 'http://www.w3.org/2000/svg';
const NODE_W = 190, NODE_MIN_H = 62, COL_GAP = 96, ROW_GAP = 22;

const TONE = {
  done:    { dot: 'var(--cc-ok)',     edge: 'var(--cc-ok)' },
  running: { dot: 'var(--cc-accent)', edge: 'var(--cc-accent)' },
  error:   { dot: 'var(--cc-bad)',    edge: 'var(--cc-bad)' },
  blocked: { dot: 'var(--cc-warn)',   edge: 'var(--cc-line-strong)' },
  idle:    { dot: 'var(--cc-idle)',   edge: 'var(--cc-line-strong)' },
};
const tone = s => TONE[s] || TONE.idle;
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** Longest-path layering: a node sits one column right of its deepest input. */
function layout(nodes, edges) {
  const byId = new Map(nodes.map(n => [n.id, { ...n }]));
  const incoming = new Map(nodes.map(n => [n.id, []]));
  for (const e of edges) if (incoming.has(e.to)) incoming.get(e.to).push(e.from);

  const col = new Map();
  const depth = (id, seen = new Set()) => {
    if (col.has(id)) return col.get(id);
    if (seen.has(id)) return 0;                       // cycle guard; pipelines shouldn't have one
    seen.add(id);
    const ins = incoming.get(id) || [];
    const d = ins.length ? Math.max(...ins.map(p => depth(p, seen) + 1)) : 0;
    col.set(id, d);
    return d;
  };
  nodes.forEach(n => depth(n.id));

  const rows = new Map();                              // column -> next free row
  const placed = [...byId.values()].sort((a, b) => col.get(a.id) - col.get(b.id));
  for (const n of placed) {
    const c = n.col ?? col.get(n.id);
    const r = rows.get(c) ?? 0;
    rows.set(c, r + 1);
    n._col = c;
    n._row = n.row ?? r;
  }
  // centre each column vertically against the tallest one
  const perCol = new Map();
  placed.forEach(n => perCol.set(n._col, Math.max(perCol.get(n._col) || 0, n._row + 1)));
  const tallest = Math.max(1, ...perCol.values());
  for (const n of placed) {
    const h = n.height || NODE_MIN_H;
    n._x = n._col * (NODE_W + COL_GAP);
    const offset = (tallest - perCol.get(n._col)) / 2;
    n._y = (n._row + offset) * (NODE_MIN_H + ROW_GAP);
    n._h = h;
  }
  return { nodes: placed, tallest };
}

function nodeHtml(n) {
  const t = tone(n.status);
  const meta = (n.meta || []).slice(0, 3).map(m =>
    `<div style="display:flex;justify-content:space-between;gap:8px;font-size:11px;
       color:var(--cc-fg-3)"><span>${esc(m.k)}</span>
       <span class="num" style="color:var(--cc-fg-2)">${esc(m.v)}</span></div>`).join('');
  const pct = n.status === 'done' ? 100 : (n.progress || 0);
  return `<div class="ng-node" data-node="${esc(n.id)}" tabindex="0" role="button"
      aria-label="${esc(n.title)}: ${esc(n.status || 'idle')}"
      style="left:${n._x}px;top:${n._y}px;width:${NODE_W}px;
             border-color:${n.status === 'running' ? 'var(--cc-accent)' : 'var(--cc-line)'}">
    <div class="ng-title">
      <span class="ng-dot" style="background:${t.dot}"></span>
      <span class="ng-name">${esc(n.title)}</span>
    </div>
    ${n.subtitle ? `<div class="ng-sub">${esc(n.subtitle)}</div>` : ''}
    ${meta ? `<div class="ng-meta">${meta}</div>` : ''}
    ${(n.status === 'running' || (pct > 0 && pct < 100))
      ? `<div class="ng-bar"><i style="width:${pct}%"></i></div>` : ''}
  </div>`;
}

/**
 * Render a pipeline graph.
 * spec: { nodes: [{id,title,subtitle,status,progress,meta,col,row}], edges: [{from,to,label}] }
 * Returns { update(spec), destroy() }.
 */
export function renderGraph(el, spec, { onNodeClick } = {}) {
  el.classList.add('ng-canvas');
  el.innerHTML = `<div class="ng-viewport"><svg class="ng-edges"></svg><div class="ng-nodes"></div></div>
    <div class="ng-hint">drag to pan · scroll to zoom</div>`;
  const viewport = el.querySelector('.ng-viewport');
  const svg = el.querySelector('.ng-edges');
  const layer = el.querySelector('.ng-nodes');
  let tx = 16, ty = 16, scale = 1;

  const apply = () => viewport.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;

  function draw(s) {
    const { nodes } = layout(s.nodes || [], s.edges || []);
    const pos = new Map(nodes.map(n => [n.id, n]));
    layer.innerHTML = nodes.map(nodeHtml).join('');

    /* Nodes are HTML, so their height depends on how many meta rows they carry — a
       fixed-height layout overlaps the taller ones. Measure what was actually rendered
       and re-stack each column, then centre the columns against the tallest. */
    const cols = new Map();
    for (const n of nodes) {
      n._el = layer.querySelector(`[data-node="${CSS.escape(n.id)}"]`);
      n._h = n._el ? n._el.offsetHeight : NODE_MIN_H;
      (cols.get(n._col) || cols.set(n._col, []).get(n._col)).push(n);
    }
    let tallestCol = 0;
    for (const list of cols.values()) {
      list.sort((a, b) => a._row - b._row);
      let y = 0;
      for (const n of list) { n._y = y; y += n._h + ROW_GAP; }
      tallestCol = Math.max(tallestCol, y - ROW_GAP);
    }
    for (const list of cols.values()) {
      const colH = list.reduce((a, n) => a + n._h + ROW_GAP, 0) - ROW_GAP;
      const pad = (tallestCol - colH) / 2;
      for (const n of list) {
        n._y += pad;
        if (n._el) n._el.style.top = `${n._y}px`;
      }
    }

    const w = Math.max(...nodes.map(n => n._x + NODE_W), 200) + 40;
    const h = Math.max(...nodes.map(n => n._y + n._h), 120) + 40;
    svg.setAttribute('width', w);
    svg.setAttribute('height', h);
    svg.innerHTML = '';
    for (const e of (s.edges || [])) {
      const a = pos.get(e.from), b = pos.get(e.to);
      if (!a || !b) continue;
      // anchor to each node's real middle, now that heights are known
      const x1 = a._x + NODE_W, y1 = a._y + a._h / 2;
      const x2 = b._x, y2 = b._y + b._h / 2;
      const mid = Math.max(28, (x2 - x1) / 2);
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', `M${x1},${y1} C${x1 + mid},${y1} ${x2 - mid},${y2} ${x2},${y2}`);
      p.setAttribute('fill', 'none');
      // an edge is "live" only when what it feeds is actually working
      const live = b.status === 'running';
      p.setAttribute('stroke', live ? 'var(--cc-accent)' : tone(a.status).edge);
      p.setAttribute('stroke-width', live ? 2.5 : 1.5);
      p.setAttribute('opacity', a.status === 'idle' ? '.45' : '.9');
      if (live) p.setAttribute('stroke-dasharray', '6 5');
      svg.appendChild(p);
      // a port dot where the edge lands reads as a connection, not a stray line
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', x2); dot.setAttribute('cy', y2); dot.setAttribute('r', 3);
      dot.setAttribute('fill', tone(b.status).dot);
      svg.appendChild(dot);
    }
    if (onNodeClick) {
      layer.querySelectorAll('[data-node]').forEach(nd => {
        const go = () => onNodeClick(nd.dataset.node);
        nd.onclick = go;
        nd.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } };
      });
    }
  }

  // pan
  let dragging = false, sx = 0, sy = 0;
  el.addEventListener('pointerdown', ev => {
    if (ev.target.closest('.ng-node')) return;         // let nodes take their own clicks
    dragging = true; sx = ev.clientX - tx; sy = ev.clientY - ty;
    el.setPointerCapture(ev.pointerId);
    el.style.cursor = 'grabbing';
  });
  el.addEventListener('pointermove', ev => {
    if (!dragging) return;
    tx = ev.clientX - sx; ty = ev.clientY - sy; apply();
  });
  const stop = () => { dragging = false; el.style.cursor = 'grab'; };
  el.addEventListener('pointerup', stop);
  el.addEventListener('pointercancel', stop);
  // zoom about the cursor
  el.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = el.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const next = Math.min(1.8, Math.max(0.4, scale * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)));
    tx = mx - (mx - tx) * (next / scale);
    ty = my - (my - ty) * (next / scale);
    scale = next; apply();
  }, { passive: false });

  el.style.cursor = 'grab';
  draw(spec);
  apply();
  return {
    update(s) { draw(s); },
    reset() { tx = 16; ty = 16; scale = 1; apply(); },
    destroy() { el.innerHTML = ''; el.classList.remove('ng-canvas'); },
  };
}
