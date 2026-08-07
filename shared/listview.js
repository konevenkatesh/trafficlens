/* Search, filter, sort and a table/grid toggle — one implementation for every list.

   Written as a shared control because the Lab has four lists that want the same thing
   (stations, footage, datasets, counts) and four hand-rolled versions would drift apart
   within a week.

   Two details that are easy to get wrong and matter:

   - **"Nothing here" and "nothing matches" are different states.** An empty station list
     should offer to create one; a filtered-to-zero list should offer to clear the filter.
     Showing the same message for both sends people looking for data that is right there.
   - **The view choice is remembered per list.** Someone who prefers cards for stations and
     a table for footage should not have to re-pick every visit.  */

const LS = k => `tl.listview.${k}`;

export function mountListView(el, opt) {
  const {
    items = [], storageKey = 'default',
    searchText = () => '', searchPlaceholder = 'Search…',
    sorts = [], filters = [], columns = [], card = null,
    emptyHead = 'Nothing here yet', emptyBody = '', emptyAction = '',
    onRender = null,
  } = opt;

  // Read the stored preference fresh rather than capturing it once. A remount (route
  // re-entry, a stray second mount) would otherwise leave an older instance holding a
  // stale copy, and any render from it would write that stale copy back — the toggle
  // silently flipping to its previous value.
  const load = () => { try { return JSON.parse(localStorage.getItem(LS(storageKey)) || '{}'); }
                       catch { return {}; } };
  const saved = load();
  let state = {
    q: '', sort: saved.sort ?? 0, dir: saved.dir ?? 'asc',
    view: saved.view ?? 'table', active: new Set(saved.active || []),
  };
  let alive = true;                    // a superseded instance must never write again
  const persist = () => {
    if (!alive) return;
    localStorage.setItem(LS(storageKey), JSON.stringify({
      sort: state.sort, dir: state.dir, view: state.view, active: [...state.active] }));
  };

  function visible() {
    let out = items;
    const q = state.q.trim().toLowerCase();
    if (q) out = out.filter(i => searchText(i).toLowerCase().includes(q));
    for (const key of state.active) {
      const f = filters.find(x => x.key === key);
      if (f) out = out.filter(f.test);
    }
    const s = sorts[state.sort];
    if (s) {
      const sign = state.dir === 'desc' ? -1 : 1;
      out = [...out].sort((a, b) => {
        const av = s.get(a), bv = s.get(b);
        if (av == null && bv == null) return 0;
        if (av == null) return 1;                    // blanks last, both directions
        if (bv == null) return -1;
        return (typeof av === 'string' ? av.localeCompare(bv) : av - bv) * sign;
      });
    }
    return out;
  }

  function controls(n) {
    return `<div class="lv-bar">
      <div class="lv-search">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="search" data-q value="${esc(state.q)}" placeholder="${esc(searchPlaceholder)}"
               aria-label="${esc(searchPlaceholder)}">
      </div>
      ${filters.length ? `<div class="lv-chips">${filters.map(f => `
        <button class="lv-chip${state.active.has(f.key) ? ' on' : ''}" data-filter="${esc(f.key)}"
          aria-pressed="${state.active.has(f.key)}">${esc(f.label)}</button>`).join('')}</div>` : ''}
      <span style="flex:1"></span>
      ${sorts.length ? `<label class="lv-sort">
        <span>Sort</span>
        <select class="field sm" data-sort aria-label="Sort by">
          ${sorts.map((s, i) => `<option value="${i}"${i === state.sort ? ' selected' : ''}>${esc(s.label)}</option>`).join('')}
        </select>
        <button class="icon-btn sm" data-dir title="${state.dir === 'asc' ? 'Ascending' : 'Descending'}"
          aria-label="Toggle sort direction">${state.dir === 'asc' ? '↑' : '↓'}</button>
      </label>` : ''}
      ${card ? `<div class="lv-toggle" role="group" aria-label="View">
        <button data-view="table" class="${state.view === 'table' ? 'on' : ''}" title="Table view"
          aria-pressed="${state.view === 'table'}">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h18M3 12h18M3 18h18"/></svg></button>
        <button data-view="grid" class="${state.view === 'grid' ? 'on' : ''}" title="Grid view"
          aria-pressed="${state.view === 'grid'}">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></button>
      </div>` : ''}
      <span class="lv-count">${n} of ${items.length}</span>
    </div>`;
  }

  function body(rows) {
    if (!items.length) {
      return `<div class="empty"><h3>${esc(emptyHead)}</h3><p>${esc(emptyBody)}</p>${emptyAction}</div>`;
    }
    if (!rows.length) {
      // Different state, different offer: the data exists, the filter is hiding it.
      return `<div class="empty"><h3>Nothing matches</h3>
        <p>${items.length} item(s) are hidden by the current search or filters.</p>
        <button class="btn secondary" data-clear style="margin-top:12px">Clear search and filters</button></div>`;
    }
    if (state.view === 'grid' && card) {
      return `<div class="lv-grid">${rows.map(card).join('')}</div>`;
    }
    return `<div class="lv-table"><table><thead><tr>${columns.map((c, i) =>
        `<th${c.right ? ' class="right"' : ''}${c.sortable !== false && c.sortIndex != null
          ? ` data-sortcol="${c.sortIndex}" class="${c.right ? 'right ' : ''}lv-sortable"` : ''}>${esc(c.label)}</th>`).join('')}
      </tr></thead><tbody>${rows.map(r => `<tr>${columns.map(c =>
        `<td${c.right ? ' class="right"' : ''}${c.cls ? ` class="${c.cls}"` : ''}>${c.get(r)}</td>`).join('')}</tr>`).join('')}
      </tbody></table></div>`;
  }

  function render() {
    if (!alive || !el.isConnected) return;   // detached by a newer mount
    const rows = visible();
    el.innerHTML = controls(rows.length) + body(rows);
    wire();
    if (onRender) onRender(el, rows);
  }

  function wire() {
    const q = el.querySelector('[data-q]');
    if (q) {
      q.oninput = () => { state.q = q.value; const at = q.selectionStart; render();
        const nq = el.querySelector('[data-q]'); nq.focus(); nq.setSelectionRange(at, at); };
    }
    el.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
      const k = b.dataset.filter;
      state.active.has(k) ? state.active.delete(k) : state.active.add(k);
      persist(); render();
    });
    const sel = el.querySelector('[data-sort]');
    if (sel) sel.onchange = () => { state.sort = +sel.value; persist(); render(); };
    const dir = el.querySelector('[data-dir]');
    if (dir) dir.onclick = () => { state.dir = state.dir === 'asc' ? 'desc' : 'asc'; persist(); render(); };
    el.querySelectorAll('[data-view]').forEach(b => b.onclick = () => {
      state.view = b.dataset.view; persist(); render();
    });
    el.querySelectorAll('[data-sortcol]').forEach(th => th.onclick = () => {
      const i = +th.dataset.sortcol;
      if (state.sort === i) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      else { state.sort = i; state.dir = 'asc'; }
      persist(); render();
    });
    const clr = el.querySelector('[data-clear]');
    if (clr) clr.onclick = () => { state.q = ''; state.active.clear(); persist(); render(); };
  }

  render();
  return {
    render,
    state: () => ({ ...state, active: [...state.active] }),
    destroy() { alive = false; },
  };
}

const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
