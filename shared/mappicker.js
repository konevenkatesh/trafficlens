/* Station map picker — pick a position and a camera direction on satellite imagery,
   embedded in the app. For the office half of the job; standing at the station, the
   phone's GPS and compass do the same thing in two taps.

   Leaflet is vendored under shared/vendor/leaflet (BSD-2-Clause) so the app does not
   depend on a CDN at run time. Tiles do need a connection — that is the one part of
   this feature that cannot work offline, which is why the on-site capture path exists.

   Markers are DivIcons styled from our own tokens, so no image assets are needed and
   they follow the light/dark theme. */

const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
/* Attribution is required and must not be removed. Maxar Intelligence was renamed
   Vantor in October 2025, so the older "Maxar" credit is now stale. */
const ESRI_ATTR = 'Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community';

/* Measured by fetching real tiles at our own stations and LOOKING at them.

   Over Bhalki, Esri World Imagery is genuinely detailed down to z18 (~0.57 m/px —
   individual buildings and compound walls are legible). z19 and deeper return a 2,521-byte
   "map data not yet available" placeholder, which loads successfully and so cannot be
   detected as a broken image — only by size. An earlier byte-size threshold of 18 KB
   wrongly classified the perfectly good z17/z18 tiles as placeholders and capped the map
   at z16, which is what made it look terrible.

   Requesting no deeper than what exists and letting Leaflet upscale means zooming past
   z18 goes soft rather than blank. */
const ESRI_NATIVE_MAX = 18;             // fallback when a station has not been probed

/* A second Esri service with independently-flown imagery. It is sometimes sharper than
   World Imagery over the same spot, so it is offered as an alternative rather than a
   replacement — coverage differs by region and neither wins everywhere. */
const CLARITY = 'https://clarity.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
const OSM = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const OSM_ATTR = '&copy; OpenStreetMap contributors';

let loading = null;

/** Load the vendored Leaflet once, on demand — the map is not on the critical path. */
export function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/shared/vendor/leaflet/leaflet.css';
    document.head.appendChild(css);
    const s = document.createElement('script');
    s.src = '/shared/vendor/leaflet/leaflet.js';
    s.onload = () => resolve(window.L);
    s.onerror = () => reject(new Error('could not load the map library'));
    document.head.appendChild(s);
  });
  return loading;
}

/** Forward azimuth from one point to another, in degrees clockwise from true north. */
export function bearingBetween(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const φ1 = lat1 * r, φ2 = lat2 * r, Δλ = (lon2 - lon1) * r;
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

/** Point `dist` metres from (lat,lon) along `bearing`. Used to place the look-at handle. */
export function project(lat, lon, bearing, dist = 120) {
  const R = 6371000, r = Math.PI / 180;
  const δ = dist / R, θ = bearing * r, φ1 = lat * r, λ1 = lon * r;
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ) + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ));
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(δ) * Math.cos(φ1),
                             Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2));
  return [φ2 / r, ((λ2 / r) + 540) % 360 - 180];
}

const pin = (label, tone) => window.L.divIcon({
  className: '',
  html: `<div style="width:26px;height:26px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);
    background:${tone};border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5);
    display:grid;place-items:center">
    <span style="transform:rotate(45deg);font-size:11px;color:#fff;font-weight:700">${label}</span></div>`,
  iconSize: [26, 26], iconAnchor: [13, 26],
});

/**
 * Embed a picker. Returns { value(), setValue(), invalidate(), destroy() }.
 * onChange({lat, lon, bearing}) fires on every drag/click.
 */
export async function createMapPicker(el, { lat, lon, bearing, onChange, nativeZoom } = {}) {
  const NZ = nativeZoom || ESRI_NATIVE_MAX;
  const L = await loadLeaflet();
  const start = (lat != null && lon != null) ? [lat, lon] : [17.385, 78.4867];  // Hyderabad
  const map = L.map(el, { zoomControl: true, attributionControl: true, maxZoom: 22 })
    .setView(start, lat != null ? 18 : 6);

  // Esri imagery runs out above 19 in most of India. maxNativeZoom keeps requesting the
  // z19 tile and lets Leaflet upscale it, so zooming closer goes soft rather than hitting
  // "map data not yet available" — a blurry junction you can still aim at beats a blank one.
  const sat = L.tileLayer(ESRI, { maxZoom: 22, maxNativeZoom: NZ, attribution: ESRI_ATTR });
  const clarity = L.tileLayer(CLARITY, { maxZoom: 22, maxNativeZoom: 17, attribution: ESRI_ATTR });
  const street = L.tileLayer(OSM, { maxZoom: 22, maxNativeZoom: 19, attribution: OSM_ATTR });
  // Street is the default. Over rural India it is crisper than the imagery at every zoom
  // and shows junction geometry unambiguously; satellite stays a click away for context.
  street.addTo(map);
  L.control.layers({ Street: street, Satellite: sat, 'Satellite (alt)': clarity },
                   null, { position: 'topright' }).addTo(map);

  // ── place search ──────────────────────────────────────────────────────────────
  const Search = L.Control.extend({
    options: { position: 'topleft' },
    onAdd() {
      const box = L.DomUtil.create('div', 'leaflet-bar tl-search');
      box.innerHTML = `
        <input type="search" placeholder="Search a place…" aria-label="Search for a place"
          style="width:210px;height:30px;border:0;padding:0 8px;font:inherit;font-size:13px;
                 border-radius:3px;outline:none" />
        <div class="tl-results" style="display:none;max-height:190px;overflow:auto;
             background:#fff;color:#111;font-size:12px;border-top:1px solid #ddd"></div>`;
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      const input = box.querySelector('input');
      const list = box.querySelector('.tl-results');
      let timer = null;

      const show = items => {
        if (!items.length) {
          list.innerHTML = '<div style="padding:8px">no match</div>';
        } else {
          list.innerHTML = items.map((r, i) =>
            `<div data-i="${i}" style="padding:7px 8px;cursor:pointer;border-bottom:1px solid #eee">
               ${r.name.replace(/</g, '&lt;')}</div>`).join('');
          [...list.children].forEach(el => {
            el.onmouseenter = () => el.style.background = '#eef';
            el.onmouseleave = () => el.style.background = '';
            el.onclick = () => {
              const r = items[+el.dataset.i];
              map.setView([r.lat, r.lon], 17);
              place(L.latLng(r.lat, r.lon));      // drop the camera pin right there
              list.style.display = 'none';
              input.value = r.name.split(',')[0];
            };
          });
        }
        list.style.display = 'block';
      };

      const run = async () => {
        const q = input.value.trim();
        if (q.length < 3) { list.style.display = 'none'; return; }
        list.innerHTML = '<div style="padding:8px">searching…</div>';
        list.style.display = 'block';
        try {
          const r = await fetch('/api/geocode?q=' + encodeURIComponent(q));
          show((await r.json()).results || []);
        } catch { list.innerHTML = '<div style="padding:8px">search unavailable</div>'; }
      };
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(); }
        if (e.key === 'Escape') list.style.display = 'none';
      });
      input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 600); });
      return box;
    },
  });
  map.addControl(new Search());

  let station = null, look = null, ray = null, cone = null;

  const emit = () => {
    const v = value();
    if (onChange) onChange(v);
    draw();
  };

  function draw() {
    if (!station) return;
    const a = station.getLatLng();
    if (!look) return;
    const b = look.getLatLng();
    const brg = bearingBetween(a.lat, a.lng, b.lat, b.lng);
    if (ray) ray.remove();
    if (cone) cone.remove();
    ray = L.polyline([a, b], { color: '#6C47FF', weight: 3, opacity: .95 }).addTo(map);
    // a 60-degree field of view, so the drawing reads as "what the camera sees"
    const left = project(a.lat, a.lng, brg - 30, a.distanceTo(b));
    const right = project(a.lat, a.lng, brg + 30, a.distanceTo(b));
    cone = L.polygon([[a.lat, a.lng], left, right],
                     { color: '#6C47FF', weight: 1, fillOpacity: .18 }).addTo(map);
  }

  function place(latlng) {
    // Dropping a pin from a wide view means "show me this junction" — frame it, but
    // never pull back if the user has already zoomed in to aim precisely.
    if (map.getZoom() < 17) map.setView(latlng, 18);
    if (!station) {
      station = L.marker(latlng, { draggable: true, icon: pin('C', '#6C47FF') }).addTo(map);
      station.on('drag', emit).on('dragend', emit);
      const p = project(latlng.lat, latlng.lng, bearing ?? 90, 120);
      look = L.marker(p, { draggable: true, icon: pin('→', '#DC6803') }).addTo(map)
        .bindTooltip('drag me to where the camera looks', { permanent: false });
      look.on('drag', emit).on('dragend', emit);
    } else {
      station.setLatLng(latlng);
    }
    emit();
  }

  map.on('click', e => place(e.latlng));
  if (lat != null && lon != null) place(L.latLng(lat, lon));

  // A map built inside a hidden container has no viewport and loads a single tile.
  // Watch the element instead of relying on every caller to remember to invalidate.
  let ro = null;
  if (window.ResizeObserver) {
    let last = 0;
    ro = new ResizeObserver(() => {
      const h = el.clientHeight, w = el.clientWidth;
      if (h > 0 && w > 0 && (h + w) !== last) {
        last = h + w;
        map.invalidateSize();
        if (station) map.setView(station.getLatLng(), map.getZoom());
      }
    });
    ro.observe(el);
  }

  function value() {
    if (!station || !look) return { lat: null, lon: null, bearing: null };
    const a = station.getLatLng(), b = look.getLatLng();
    return {
      lat: +a.lat.toFixed(6), lon: +a.lng.toFixed(6),
      bearing: Math.round(bearingBetween(a.lat, a.lng, b.lat, b.lng)),
    };
  }

  return {
    value,
    setValue({ lat, lon, bearing }) {
      if (lat == null) return;
      place(L.latLng(lat, lon));
      if (bearing != null && station) {
        look.setLatLng(project(lat, lon, bearing, 120));
        emit();
      }
      map.setView([lat, lon], 18);
    },
    invalidate: () => setTimeout(() => map.invalidateSize(), 60),
    fit: () => { if (station) map.setView(station.getLatLng(), 18); },
    destroy: () => { ro?.disconnect(); map.remove(); },
  };
}

/** Read-only mini map for a station card, with its view cone drawn. */
export async function createMiniMap(el, { lat, lon, bearing, nativeZoom }) {
  const L = await loadLeaflet();
  const map = L.map(el, { zoomControl: false, attributionControl: false,
                          dragging: false, scrollWheelZoom: false,
                          doubleClickZoom: false, boxZoom: false, keyboard: false })
    .setView([lat, lon], 17);
  L.tileLayer(ESRI, { maxZoom: 22, maxNativeZoom: nativeZoom || ESRI_NATIVE_MAX }).addTo(map);
  // Esri's terms require attribution even where the control is hidden.
  el.insertAdjacentHTML('beforeend',
    `<div style="position:absolute;right:2px;bottom:1px;z-index:500;font-size:9px;
       background:rgba(0,0,0,.5);color:#eee;padding:0 3px;border-radius:2px;
       pointer-events:none">Esri</div>`);
  el.style.position = 'relative';
  L.marker([lat, lon], { icon: pin('C', '#6C47FF') }).addTo(map);
  if (bearing != null) {
    const tip = project(lat, lon, bearing, 90);
    const left = project(lat, lon, bearing - 30, 90);
    const right = project(lat, lon, bearing + 30, 90);
    L.polygon([[lat, lon], left, right], { color: '#6C47FF', weight: 1, fillOpacity: .2 }).addTo(map);
    L.polyline([[lat, lon], tip], { color: '#6C47FF', weight: 3 }).addTo(map);
  }
  setTimeout(() => map.invalidateSize(), 60);
  return map;
}
