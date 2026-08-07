/* Exhaustive box labelling on one frame.

   Built for speed rather than features, because the whole gold set has to fit inside two
   or three hours per station. Every common action is one keystroke, the mouse is only
   needed to draw a box the model missed, and nothing requires a round trip until the
   frame is submitted.

   Deliberate: the frame opens with the model's boxes HIDDEN. If a reviewer sees them
   first they confirm what is there and stop looking, and the misses — the only reason
   this set exists — never get found. Reveal is a separate, recorded action. */

export function mountLabeler(el, opts = {}) {
  const classes = opts.classes || [];
  const onSave = opts.onSave || (() => {});
  const onSkip = opts.onSkip || (() => {});

  let img = null, boxes = [], sel = -1, revealed = false, t0 = 0;
  let drag = null, scale = 1;

  el.innerHTML = `
    <div class="bl">
      <div class="bl-bar">
        <button class="btn sm primary" data-a="reveal">Reveal model boxes <kbd>R</kbd></button>
        <span class="bl-count" data-c></span>
        <span class="sp" style="flex:1"></span>
        <span class="bl-hint">drag on empty space to add a missed vehicle ·
          click a box to select · <kbd>1-9</kbd> class · <kbd>Del</kbd> remove · <kbd>Enter</kbd> next</span>
      </div>
      <div class="bl-note" data-veil>Model boxes are hidden — sweep the frame yourself
        and add anything you see, then reveal.</div>
      <div class="bl-stage" data-stage>
        <canvas data-canvas></canvas>
      </div>
      <div class="bl-classes" data-classes></div>
      <div class="bl-bar">
        <button class="btn secondary" data-a="skip">Skip frame</button>
        <span class="sp" style="flex:1"></span>
        <button class="btn primary" data-a="save">Save &amp; next <kbd>⏎</kbd></button>
      </div>
    </div>`;

  const cv = el.querySelector('[data-canvas]');
  const ctx = cv.getContext('2d');
  const veil = el.querySelector('[data-veil]');
  const counter = el.querySelector('[data-c]');

  el.querySelector('[data-classes]').innerHTML = classes.map((c, i) =>
    `<button class="bl-cls" data-cls="${i}">${i < 9 ? `<kbd>${i + 1}</kbd> ` : ''}${c}</button>`).join('');

  function load(frame, seeded) {
    img = new Image();
    img.onload = () => { fit(); draw(); };
    img.src = `/api/gold/frame/${frame.id}/image`;
    boxes = (seeded || []).map(b => ({ ...b, cls: b.cls, source: b.source || 'model',
                                       verdict: 'confirmed', hidden: true }));
    revealed = false; sel = -1; t0 = performance.now();
    veil.style.display = '';
    draw();
  }

  function fit() {
    const stage = el.querySelector('[data-stage]');
    // Height-bound as well as width-bound: a 1280x720 frame squeezed into a card was
    // rendering ~470px wide, which is far too small to judge a distant motorcycle.
    const w = stage.clientWidth || 900;
    const hCap = Math.max(360, window.innerHeight - 330);
    if (!img) { cv.width = w; cv.height = 500; scale = 1; return; }
    scale = Math.min(1, w / img.naturalWidth, hCap / img.naturalHeight);
    cv.width = Math.round(img.naturalWidth * scale);
    cv.height = Math.round(img.naturalHeight * scale);
  }

  function reveal() {
    if (revealed) return;
    revealed = true;
    boxes.forEach(b => (b.hidden = false));
    veil.style.display = 'none';
    draw();
  }

  const COLOR = { model: '#2E6BE6', human: '#17B26A' };
  function draw() {
    if (!cv.width) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (img) ctx.drawImage(img, 0, 0, cv.width, cv.height);
    boxes.forEach((b, i) => {
      if (b.hidden) return;
      const x = b.x1 * scale, y = b.y1 * scale;
      const w = (b.x2 - b.x1) * scale, h = (b.y2 - b.y1) * scale;
      ctx.lineWidth = i === sel ? 3 : 2;
      ctx.strokeStyle = i === sel ? '#DC6803' : COLOR[b.source] || COLOR.model;
      ctx.strokeRect(x, y, w, h);
      const label = `${classes[b.cls] ?? '?'}`;
      ctx.font = '11px ui-sans-serif, system-ui';
      const tw = ctx.measureText(label).width + 8;
      ctx.fillStyle = i === sel ? '#DC6803' : COLOR[b.source] || COLOR.model;
      ctx.fillRect(x, Math.max(0, y - 15), tw, 15);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, x + 4, Math.max(11, y - 4));
    });
    if (drag) {
      ctx.setLineDash([5, 4]); ctx.strokeStyle = COLOR.human; ctx.lineWidth = 2;
      ctx.strokeRect(drag.x, drag.y, drag.w, drag.h);
      ctx.setLineDash([]);
    }
    const shown = boxes.filter(b => !b.hidden);
    counter.textContent = revealed
      ? `${shown.length} vehicle(s) · ${shown.filter(b => b.source === 'human').length} added by you`
      : `${shown.filter(b => b.source === 'human').length} marked so far`;
    el.querySelectorAll('.bl-cls').forEach(btn =>
      btn.classList.toggle('on', sel >= 0 && +btn.dataset.cls === boxes[sel]?.cls));
  }

  const pos = e => {
    // Map CSS px to canvas-attribute px: the two diverge whenever CSS narrows the
    // canvas after mount (rail toggle, scrollbar), and boxes live in attribute space.
    const r = cv.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (cv.width / (r.width || cv.width)),
             y: (e.clientY - r.top) * (cv.height / (r.height || cv.height)) };
  };
  const hit = p => {
    // Smallest box containing the point wins, so a vehicle inside a bus-sized box
    // is still selectable.
    let best = -1, area = Infinity;
    boxes.forEach((b, i) => {
      if (b.hidden) return;
      const x1 = b.x1 * scale, y1 = b.y1 * scale, x2 = b.x2 * scale, y2 = b.y2 * scale;
      if (p.x >= x1 && p.x <= x2 && p.y >= y1 && p.y <= y2) {
        const a = (x2 - x1) * (y2 - y1);
        if (a < area) { area = a; best = i; }
      }
    });
    return best;
  };

  cv.addEventListener('pointerdown', e => {
    const p = pos(e);
    const i = hit(p);
    if (i >= 0) { sel = i; draw(); return; }
    drag = { ox: p.x, oy: p.y, x: p.x, y: p.y, w: 0, h: 0 };
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener('pointermove', e => {
    if (!drag) return;
    const p = pos(e);
    drag.x = Math.min(drag.ox, p.x); drag.y = Math.min(drag.oy, p.y);
    drag.w = Math.abs(p.x - drag.ox); drag.h = Math.abs(p.y - drag.oy);
    draw();
  });
  cv.addEventListener('pointerup', () => {
    if (!drag) return;
    if (drag.w > 6 && drag.h > 6) {
      boxes.push({ x1: drag.x / scale, y1: drag.y / scale,
                   x2: (drag.x + drag.w) / scale, y2: (drag.y + drag.h) / scale,
                   cls: 0, source: 'human', verdict: 'added', hidden: false });
      sel = boxes.length - 1;
    }
    drag = null;
    draw();
  });

  el.querySelectorAll('.bl-cls').forEach(b => b.onclick = () => setClass(+b.dataset.cls));
  function setClass(c) {
    if (sel < 0) return;
    boxes[sel].cls = c;
    if (boxes[sel].source === 'model') boxes[sel].verdict = 'corrected';
    draw();
  }

  function remove() {
    if (sel < 0) return;
    boxes.splice(sel, 1); sel = -1; draw();
  }

  function save() {
    // A frame saved without revealing would claim "these are all the vehicles" while the
    // model's own boxes were never looked at. Reveal first, then submit.
    if (!revealed) { reveal(); return; }
    onSave({ boxes: boxes.filter(b => !b.hidden).map(({ hidden, ...b }) => b),
             seconds: (performance.now() - t0) / 1000, revealed: true });
  }

  el.addEventListener('click', e => {
    const a = e.target.closest('[data-a]');
    if (!a) return;
    ({ reveal, save, skip: onSkip })[a.dataset.a]?.();
  });

  function keys(e) {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    if (e.key >= '1' && e.key <= '9') { setClass(+e.key - 1); e.preventDefault(); }
    else if (e.key === 'r' || e.key === 'R') reveal();
    else if (e.key === 'Delete' || e.key === 'Backspace') { remove(); e.preventDefault(); }
    else if (e.key === 'Enter') { save(); e.preventDefault(); }
    else if (e.key === 'Escape') { sel = -1; draw(); }
  }
  document.addEventListener('keydown', keys);
  window.addEventListener('resize', () => { fit(); draw(); });

  return { load, destroy: () => document.removeEventListener('keydown', keys) };
}
