/* Draw the counting lines on a still frame.

   A count is only as good as where this line sits, and the two mistakes that matter are
   both visible here: a line too short for the carriageway silently drops every crossing
   past its ends, and a line placed across one carriageway of a two-way road produces a
   90/10 direction split that looks like traffic. So the frame is shown large, the line
   endpoints are draggable rather than redrawn, and the "in" side is labelled — direction
   is decided by which side of the line a vehicle came from, and nothing else on screen
   says which side that is.

   Coordinates are stored in SOURCE PIXELS, never in display pixels: the same line has to
   mean the same thing when the browser window is a different width tomorrow. */

export function mountLineEditor(el, opts = {}) {
  const onSave = opts.onSave || (() => {});
  let img = null, lines = [], scale = 1, drawing = null, drag = null, sel = -1;

  el.innerHTML = `
    <div class="le">
      <div class="le-bar">
        <button class="btn sm primary" data-a="add">Draw a line</button>
        <button class="btn sm secondary" data-a="del" disabled>Delete selected</button>
        <button class="btn sm secondary" data-a="save">Save</button>
        <span class="le-status" data-status></span>
        <span class="sp" style="flex:1"></span>
        <span class="bl-hint" data-hint></span>
      </div>
      <div class="le-stage" data-stage><canvas data-canvas></canvas></div>
      <div class="le-list" data-list></div>
    </div>`;

  const cv = el.querySelector('[data-canvas]');
  const ctx = cv.getContext('2d');
  const list = el.querySelector('[data-list]');

  /* Where the backdrop frame comes from is the caller's business. A line is normally
     drawn on a clip, but a station with footage and no clips yet still needs one — and
     the frame then has to come from the raw footage. Coordinates are stored in SOURCE
     pixels either way, and a clip is a stream copy of its footage, so the two frames
     share dimensions and a line drawn on one is valid on the other. */
  const frameUrl = opts.frameUrl || ((v, f) => `/api/frame/${v}/${f}`);

  function load(videoId, existing, frameIdx = 0) {
    lines = (existing || []).map(l => ({ ...l }));
    hint();
    status(lines.length ? `${lines.length} line(s) loaded` : 'no line yet');
    img = new Image();
    img.onload = () => { fit(); draw(); };
    img.onerror = () => status('could not load a frame to draw on');
    img.src = frameUrl(videoId, frameIdx);
    draw();
  }

  function fit() {
    const w = el.querySelector('[data-stage]').clientWidth || 900;
    const hCap = Math.max(320, window.innerHeight - 400);
    if (!img) { cv.width = w; cv.height = 480; scale = 1; return; }
    scale = Math.min(1, w / img.naturalWidth, hCap / img.naturalHeight);
    cv.width = Math.round(img.naturalWidth * scale);
    cv.height = Math.round(img.naturalHeight * scale);
  }

  const S = p => ({ x: p[0] * scale, y: p[1] * scale });

  function draw() {
    if (!cv.width) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (img) ctx.drawImage(img, 0, 0, cv.width, cv.height);
    lines.forEach((l, i) => {
      const a = S(l.start), b = S(l.end);
      const on = i === sel;
      // The "in" side, shaded. Direction is the whole point of the line and is otherwise
      // invisible until the counts come out wrong.
      const nx = -(b.y - a.y), ny = b.x - a.x;
      const len = Math.hypot(nx, ny) || 1;
      ctx.fillStyle = 'rgba(23,178,106,.16)';
      ctx.beginPath();
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      ctx.lineTo(b.x + nx / len * 46, b.y + ny / len * 46);
      ctx.lineTo(a.x + nx / len * 46, a.y + ny / len * 46);
      ctx.closePath(); ctx.fill();

      ctx.strokeStyle = on ? '#DC6803' : '#2E6BE6';
      ctx.lineWidth = on ? 4 : 3;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      [a, b].forEach(p => {
        ctx.beginPath(); ctx.arc(p.x, p.y, on ? 7 : 5, 0, 7);
        ctx.fillStyle = on ? '#DC6803' : '#2E6BE6'; ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke();
      });
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      ctx.font = '600 12px ui-sans-serif, system-ui';
      const t = l.name || `line${i + 1}`;
      const tw = ctx.measureText(t).width + 12;
      ctx.fillStyle = on ? '#DC6803' : '#2E6BE6';
      ctx.fillRect(mx - tw / 2, my - 22, tw, 18);
      ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
      ctx.fillText(t, mx, my - 9); ctx.textAlign = 'start';
    });
    if (drawing) {
      ctx.setLineDash([6, 5]); ctx.strokeStyle = '#17B26A'; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(drawing.a.x, drawing.a.y);
      ctx.lineTo(drawing.b.x, drawing.b.y); ctx.stroke(); ctx.setLineDash([]);
    }
    renderList();
    el.querySelector('[data-a="del"]').disabled = sel < 0;
  }

  function renderList() {
    list.innerHTML = lines.length ? lines.map((l, i) => `
      <div class="le-row${i === sel ? ' on' : ''}" data-pick="${i}">
        <span class="le-dot"></span>
        <input class="pn-input" data-name="${i}" value="${(l.name || '').replace(/"/g, '&quot;')}"
               placeholder="line${i + 1}" style="width:150px">
        <span class="mono" style="font-size:11px;color:var(--cc-fg-3)">
          ${l.start.map(Math.round).join(',')} → ${l.end.map(Math.round).join(',')}</span>
        <span style="font-size:11px;color:var(--cc-fg-3)">length ${Math.round(
          Math.hypot(l.end[0] - l.start[0], l.end[1] - l.start[1]))}px</span>
      </div>`).join('')
      : `<div class="empty" style="padding:16px">No line yet — nothing can be counted until one is drawn.</div>`;
    list.querySelectorAll('[data-pick]').forEach(r => r.onclick = e => {
      if (e.target.tagName === 'INPUT') return;
      sel = +r.dataset.pick; draw();
    });
    list.querySelectorAll('[data-name]').forEach(inp => inp.onchange = () => {
      lines[+inp.dataset.name].name = inp.value.trim() || `line${+inp.dataset.name + 1}`;
      draw(); touch();
    });
  }

  const pos = e => {
    // Map CSS px to canvas-attribute px. The two diverge whenever CSS narrows the canvas
    // after mount -- toggling the sidebar rail, or a scrollbar appearing as lines are
    // added -- because neither fires a window resize, so fit() never re-runs. Endpoints
    // are stored in SOURCE pixels, so a skew here moves the count line without looking
    // wrong on screen (draw() uses the same scale), and every number in the report moves
    // with it. Measured before the fix: a 292->184px squeeze put a right-edge click ~710
    // source px out on a 1920-wide frame.
    const r = cv.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (cv.width / (r.width || cv.width)),
             y: (e.clientY - r.top) * (cv.height / (r.height || cv.height)) };
  };
  const nearHandle = p => {
    for (let i = 0; i < lines.length; i++) {
      for (const k of ['start', 'end']) {
        const q = S(lines[i][k]);
        if (Math.hypot(q.x - p.x, q.y - p.y) < 12) return { i, k };
      }
    }
    return null;
  };

  cv.addEventListener('pointerdown', e => {
    const p = pos(e);
    const h = nearHandle(p);
    if (h && !pendingAdd) {
      drag = h; sel = h.i;
      try { cv.setPointerCapture(e.pointerId); } catch { /* no active pointer */ }
      draw(); return;
    }
    if (!pendingAdd) { sel = -1; draw(); return; }
    if (drawing) finish(p);              // second click of a click-click
    else {
      drawing = { a: p, b: p, moved: false };
      // Capture keeps a drag alive past the canvas edge, but it is not essential to
      // drawing and it throws for synthetic pointers — never let it break the gesture.
      try { cv.setPointerCapture(e.pointerId); } catch { /* no active pointer */ }
    }
  });
  cv.addEventListener('pointermove', e => {
    const p = pos(e);
    if (drag) { lines[drag.i][drag.k] = [p.x / scale, p.y / scale]; draw(); return; }
    if (drawing) {
      if (Math.hypot(p.x - drawing.a.x, p.y - drawing.a.y) > 4) drawing.moved = true;
      drawing.b = p; draw();
    }
  });
  cv.addEventListener('pointerup', e => {
    if (drag) { drag = null; touch(); return; }
    if (!drawing) return;
    const p = pos(e);
    // A DRAG ends the line here. A CLICK leaves it open so the next click sets the far
    // end. The hint promised click-two-points while only drag was implemented, so anyone
    // following the instructions got nothing and the button silently disarmed itself.
    if (drawing.moved && Math.hypot(p.x - drawing.a.x, p.y - drawing.a.y) > 20) finish(p);
    else { drawing.b = p; draw(); }
  });

  function finish(p) {
    if (Math.hypot(p.x - drawing.a.x, p.y - drawing.a.y) > 20) {
      lines.push({ name: `line${lines.length + 1}`,
                   start: [drawing.a.x / scale, drawing.a.y / scale],
                   end: [p.x / scale, p.y / scale] });
      sel = lines.length - 1;
      touch();
    }
    drawing = null; disarm(); draw();
  }

  function disarm() {
    pendingAdd = false;
    el.querySelector('[data-a="add"]').classList.remove('armed');
    hint();
  }

  function hint() {
    el.querySelector('[data-hint]').innerHTML = pendingAdd
      ? 'Now click once on each side of the carriageway — or drag across it. <kbd>Esc</kbd> cancels.'
      : 'Press <b>Draw a line</b>, then click two points across the carriageway. '
        + 'Drag an endpoint to adjust. Traffic crossing toward the green side counts as <b>in</b>.';
  }

  let pendingAdd = false;
  el.addEventListener('click', e => {
    const a = e.target.closest('[data-a]');
    if (!a) return;
    if (a.dataset.a === 'add') {
      pendingAdd = !pendingAdd;
      a.classList.toggle('armed', pendingAdd);
      if (!pendingAdd) drawing = null;
      hint(); draw();
    } else if (a.dataset.a === 'del' && sel >= 0) {
      lines.splice(sel, 1); sel = -1; touch(); draw();
    } else if (a.dataset.a === 'save') {
      commit();
    }
  });
  const onKey = e => {
    if (e.key === 'Escape' && (pendingAdd || drawing)) { drawing = null; disarm(); draw(); }
  };
  document.addEventListener('keydown', onKey);

  function status(t, tone) {
    const n = el.querySelector('[data-status]');
    if (n) { n.textContent = t; n.className = 'le-status' + (tone ? ' ' + tone : ''); }
  }
  /** Saving is EXPLICIT. It used to autosave 400 ms after any change, which meant a
      stray drag — or a click that landed on the canvas while the editor happened to be
      armed — was written to the server before anyone could see it. A count line decides
      every number in the report, so it changes only when someone presses Save. Edits
      mark the editor dirty and say so; nothing leaves the browser until then. */
  let dirty = false;
  function touch() {
    dirty = true;
    status(lines.length ? `${lines.length} line(s) — not saved yet` : 'no lines — not saved yet',
           'warn');
  }
  async function commit() {
    try {
      await onSave(lines.map(l => ({
        name: l.name, start: l.start.map(Math.round), end: l.end.map(Math.round),
      })));
      dirty = false;
      status(lines.length ? `saved · ${lines.length} line(s)` : 'saved · no lines', 'ok');
    } catch { status('not saved', 'bad'); }
  }

  const onResize = () => { fit(); draw(); };
  window.addEventListener('resize', onResize);
  return {
    load, lines: () => lines, isDirty: () => dirty, save: commit,
    destroy() {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onResize);
    },
  };
}
