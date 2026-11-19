'use strict';

// ------------------------------------------------------------------ small helpers

const $ = (s, root = document) => root.querySelector(s);

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'style') e.style.cssText = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k in e && typeof v !== 'string') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c);
  return e;
}

function debounce(fn, ms) {
  let id = 0;
  return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); };
}

function fmt(x) {
  if (x == null || !Number.isFinite(x)) return '–';
  const a = Math.abs(x);
  if (a === 0) return '0';
  if (a >= 1000) return x.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (a >= 100) return x.toFixed(0);
  if (a >= 10) return x.toFixed(1);
  if (a >= 1) return x.toFixed(2);
  return x.toPrecision(2);
}

const store = {
  get(k) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window etc. */ } },
};

// ------------------------------------------------------------------ colour

// Categorical slots (validated reference palette); a hazard keeps its slot in every view.
const SLOTS = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
};
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === 'dark' : darkQuery.matches;
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const slotColor = (slot) => SLOTS[isDark() ? 'dark' : 'light'][(slot - 1) % 8];

function rgb(hex) {
  const n = parseInt(hex.replace('#', ''), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));

function rampLUT(stops) {  // stops: [[pos, [r,g,b]], ...] → 256×RGB
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const p = i / 255;
    let j = 0;
    while (j < stops.length - 2 && p > stops[j + 1][0]) j++;
    const [p0, c0] = stops[j], [p1, c1] = stops[j + 1];
    const c = mix(c0, c1, Math.min(1, Math.max(0, (p - p0) / (p1 - p0))));
    lut.set(c, i * 3);
  }
  return lut;
}

// Sequential: one hue, receding toward the surface at low values. Diverging: blue ↔ red with a grey midpoint.
function makeRamp(v) {
  const dark = isDark();
  const surf = rgb(css('--surface') || (dark ? '#1a1a19' : '#fcfcfb'));
  if (v.ramp === 'diverging') {
    const blue = rgb(SLOTS[dark ? 'dark' : 'light'][0]), red = rgb(SLOTS[dark ? 'dark' : 'light'][7]);
    const mid = rgb(dark ? '#383835' : '#f0efec');
    const deep = dark ? [255, 255, 255] : [0, 0, 0];
    return rampLUT([[0, mix(blue, deep, 0.35)], [0.25, blue], [0.5, mid], [0.75, red], [1, mix(red, deep, 0.35)]]);
  }
  const hue = rgb(slotColor(v.color_slot));
  return dark
    ? rampLUT([[0, mix(hue, surf, 0.85)], [0.6, hue], [1, mix(hue, [255, 255, 255], 0.55)]])
    : rampLUT([[0, mix(hue, surf, 0.9)], [0.55, hue], [1, mix(hue, [0, 0, 0], 0.5)]]);
}

// value → 0..1 for the background colour scale
function scaleFor(v) {
  const st = v.stats;
  if (v.ramp === 'diverging') {
    const mid = 0, span = Math.max(Math.abs(st.p999 - mid), Math.abs(mid - st.p01)) || 1;
    return { lo: mid - span, hi: mid + span, f: (x) => 0.5 + 0.5 * Math.max(-1, Math.min(1, (x - mid) / span)), inv: (t) => mid + (t - 0.5) * 2 * span };
  }
  const lo = st.min >= 0 ? 0 : st.p01;
  const hi = Math.max(st.p999, v.threshold, lo + 1e-6);
  const g = 0.5;  // square-root stretch: most hazard fields are heavily skewed toward zero
  return { lo, hi, f: (x) => Math.pow(Math.max(0, Math.min(1, (x - lo) / (hi - lo))), g), inv: (t) => lo + Math.pow(t, 1 / g) * (hi - lo) };
}

// ------------------------------------------------------------------ state

const S = {
  events: [], man: null, vars: [], byKey: {}, data: {}, T: 0, NX: 0, NY: 0, N: 0,
  inConus: null, corners: null, centers: null, states: null,
  cfg: null,
  exceed: {}, cellThr: {}, varCount: {}, varTotal: {},
  rules: [],          // computed: {flag, mask, count, total, note}
  flagRules: null,    // Uint8Array T*N, bit r = rule r flagged
  flagHaz: null,      // Uint16Array T*N, bit i = hazard i involved
  episodes: [], epLabel: null, selEpisode: null,
  t: 0, view: 'map', pinned: null, hover: null, playing: false, playTimer: 0,
};
const POP = new Uint8Array(1 << 16);
for (let i = 1; i < POP.length; i++) POP[i] = POP[i >> 1] + (i & 1);

const timeAt = (t) => new Date(Date.parse(S.man.start) + t * 3600e3);
const timeLabel = (t) => timeAt(t).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
const cellLatLon = (c) => [S.centers[c * 2 + 1], S.centers[c * 2]];

// ------------------------------------------------------------------ loading

async function fetchBin(url, Type) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return new Type(await r.arrayBuffer());
}

async function init() {
  try {
    S.events = await (await fetch('data/events.json')).json();
    S.states = await (await fetch('data/states.json')).json();
  } catch (e) {
    $('#loading').textContent = 'No data found. Run build_data.py first (see README.md).';
    return;
  }
  const sel = $('#event-select');
  for (const e of S.events) sel.append(h('option', { value: e.event_id, text: `${e.title} (${e.n_variables} variables)` }));
  const last = store.get('hx:event');
  if (last && S.events.some((e) => e.event_id === last)) sel.value = last;
  // shareable state in the URL: #event=marapr_2023&t=141&view=fields&cell=2345&theme=light
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get('theme')) document.documentElement.dataset.theme = hash.get('theme');
  if (S.events.some((e) => e.event_id === hash.get('event'))) sel.value = hash.get('event');
  sel.addEventListener('change', () => loadEvent(sel.value));
  wireStaticControls();
  await loadEvent(sel.value);
  if (hash.has('cell')) S.pinned = +hash.get('cell');
  if (hash.get('view') === 'fields') setView('fields');
  if (hash.has('t')) setT(+hash.get('t'));
}

const writeHash = debounce(() => {
  const p = new URLSearchParams({ event: S.man.event_id, t: S.t, view: S.view });
  if (S.pinned != null) p.set('cell', S.pinned);
  if (document.documentElement.dataset.theme) p.set('theme', document.documentElement.dataset.theme);
  history.replaceState(null, '', '#' + p);
}, 250);

async function loadEvent(id) {
  stopPlay();
  const loading = $('#loading');
  loading.hidden = false;
  loading.textContent = 'Loading…';
  const base = `data/${id}/`;
  const man = await (await fetch(base + 'manifest.json')).json();
  const data = {};
  for (const [i, v] of man.variables.entries()) {
    loading.textContent = `Loading ${v.label} (${i + 1}/${man.variables.length})…`;
    data[v.key] = await fetchBin(base + v.file, Float32Array);
  }
  Object.assign(S, {
    man, vars: man.variables, byKey: Object.fromEntries(man.variables.map((v) => [v.key, v])), data,
    T: man.n_hours, NX: man.grid.nx, NY: man.grid.ny, N: man.grid.nx * man.grid.ny,
    inConus: await fetchBin(base + 'in_conus.u8', Uint8Array),
    corners: await fetchBin(base + 'cells_lonlat.f32', Float32Array),
    exceed: {}, cellThr: {}, varCount: {}, varTotal: {}, rules: [], pinned: null, selEpisode: null, t: 0,
  });
  S.ramps = {};
  S.scales = Object.fromEntries(S.vars.map((v) => [v.key, scaleFor(v)]));
  S.centers = new Float32Array(S.N * 2);
  for (let iy = 0; iy < S.NY; iy++) for (let ix = 0; ix < S.NX; ix++) {
    const c = iy * S.NX + ix;
    for (let k = 0; k < 2; k++) {
      let s = 0;
      for (const [dy, dx] of [[0, 0], [0, 1], [1, 0], [1, 1]]) s += S.corners[((iy + dy) * (S.NX + 1) + ix + dx) * 2 + k];
      S.centers[c * 2 + k] = s / 4;
    }
  }
  store.set('hx:event', id);
  S.cfg = mergeConfig(defaultConfig(), store.get(`hx:cfg:${id}`));
  $('#t-slider').max = S.T - 1;
  buildVarCards();
  buildRuleCards();
  buildBgSelect();
  buildDataNotes();
  $('#conus-only').checked = S.cfg.conusOnly;
  recomputeAll();
  loading.hidden = true;
}

// ------------------------------------------------------------------ config

function defaultConfig() {
  const keys = S.vars.map((v) => v.key);
  const rules = [{ name: 'Any two hazards', enabled: true, hazards: keys, minCount: 2, mustInclude: '', radiusKm: 100, windowH: 3, anchor: true }];
  if (S.byKey.space_weather) {
    rules.push({ name: 'Space weather + a terrestrial hazard', enabled: true, hazards: keys, minCount: 2,
      mustInclude: 'space_weather', radiusKm: 150, windowH: 6, anchor: true });
  }
  const bg = (S.vars.find((v) => v.family === 'terrestrial') || S.vars[0] || {}).key || '';
  return {
    version: 1, event_id: S.man.event_id, conusOnly: true, bg,
    vars: Object.fromEntries(S.vars.map((v) => [v.key, { enabled: true, mode: 'abs', value: v.threshold, pct: 99, floor: null, direction: v.direction }])),
    rules,
  };
}

function mergeConfig(def, saved) {
  if (!saved || saved.event_id !== def.event_id) return def;
  const out = { ...def, ...saved, vars: { ...def.vars } };
  for (const k of Object.keys(def.vars)) if (saved.vars && saved.vars[k]) out.vars[k] = { ...def.vars[k], ...saved.vars[k] };
  out.rules = (saved.rules || def.rules).map((r) => ({ ...def.rules[0], ...r, hazards: (r.hazards || []).filter((k) => k in def.vars) }));
  if (!(out.bg in def.vars) && out.bg !== '') out.bg = def.bg;
  return out;
}

const saveConfig = debounce(() => store.set(`hx:cfg:${S.man.event_id}`, S.cfg), 300);

// ------------------------------------------------------------------ computation

function computeExceed(key) {
  const { T, N } = S, v = S.data[key], c = S.cfg.vars[key];
  S.cellThr[key] = null;
  if (!c.enabled) { S.exceed[key] = null; S.varCount[key] = null; S.varTotal[key] = 0; return; }
  const ex = new Uint8Array(T * N), ge = c.direction !== '<=';
  const mask = S.cfg.conusOnly ? S.inConus : null;
  if (c.mode === 'pct') {
    // Place-specific: each cell's own percentile over this event window; strict inequality so a cell that is
    // mostly zero (dry, calm) does not "exceed" at zero. Optional absolute floor on top.
    const q = Math.min(100, Math.max(0, c.pct)) / 100, thr = new Float32Array(N).fill(NaN), buf = new Float32Array(T);
    for (let cell = 0; cell < N; cell++) {
      let n = 0;
      for (let t = 0; t < T; t++) { const x = v[t * N + cell]; if (x === x) buf[n++] = x; }
      if (!n) continue;
      const s = buf.subarray(0, n).sort();
      thr[cell] = ge ? s[Math.min(n - 1, Math.floor(q * (n - 1)))] : s[Math.max(0, Math.ceil((1 - q) * (n - 1)))];
    }
    S.cellThr[key] = thr;
    const floor = c.floor == null || c.floor === '' ? NaN : +c.floor;
    for (let t = 0; t < T; t++) for (let cell = 0; cell < N; cell++) {
      const i = t * N + cell, x = v[i], th = thr[cell];
      if (x !== x || th !== th || (mask && !mask[cell])) continue;
      if (ge ? (x > th && !(x < floor)) : (x < th && !(x > floor))) ex[i] = 1;
    }
  } else {
    const thr = +c.value;
    for (let i = 0; i < T * N; i++) {
      const x = v[i];
      if (x !== x || (mask && !mask[i % N])) continue;
      if (ge ? x >= thr : x <= thr) ex[i] = 1;
    }
  }
  const cnt = new Int32Array(T);
  let tot = 0;
  for (let t = 0; t < T; t++) { let s = 0; for (let cell = 0, b = t * N; cell < N; cell++) s += ex[b + cell]; cnt[t] = s; tot += s; }
  S.exceed[key] = ex; S.varCount[key] = cnt; S.varTotal[key] = tot;
}

function diskOffsets(radiusKm) {
  const r = radiusKm / S.man.grid.dx_km, R = Math.floor(r + 1e-9), out = [];
  for (let dy = -R; dy <= R; dy++) for (let dx = -R; dx <= R; dx++) if (dx * dx + dy * dy <= r * r + 1e-9) out.push(dy, dx);
  return out;
}

// Spread exceedances by a disk in space, then ± w hours in time (separable, so cheap even for big masks).
function dilate(ex, offs, w) {
  const { T, N, NX, NY } = S, sp = new Uint8Array(T * N);
  for (let t = 0; t < T; t++) {
    const b = t * N;
    for (let c = 0; c < N; c++) {
      if (!ex[b + c]) continue;
      const y = (c / NX) | 0, x = c - y * NX;
      for (let k = 0; k < offs.length; k += 2) {
        const yy = y + offs[k], xx = x + offs[k + 1];
        if (yy >= 0 && yy < NY && xx >= 0 && xx < NX) sp[b + yy * NX + xx] = 1;
      }
    }
  }
  if (w <= 0) return sp;
  const out = new Uint8Array(T * N);
  for (let c = 0; c < N; c++) {
    let last = -1e9;
    for (let t = 0; t < T; t++) { if (sp[t * N + c]) last = t; if (t - last <= w) out[t * N + c] = 1; }
    let next = 1e9;
    for (let t = T - 1; t >= 0; t--) { if (sp[t * N + c]) next = t; if (next - t <= w) out[t * N + c] = 1; }
  }
  return out;
}

function computeRule(r) {
  const { T, N } = S;
  const res = { flag: null, mask: null, count: new Int32Array(T), total: 0, note: '' };
  if (!r.enabled) { res.note = 'off'; return res; }
  const active = r.hazards.filter((k) => S.exceed[k]);
  if (active.length < r.minCount) {
    res.note = `needs at least ${r.minCount} enabled hazards (has ${active.length})`;
    return res;
  }
  if (r.mustInclude && !S.exceed[r.mustInclude]) {
    res.note = `${S.byKey[r.mustInclude]?.label || r.mustInclude} is switched off`;
    return res;
  }
  const offs = diskOffsets(r.radiusKm), w = Math.max(0, Math.round(r.windowH));
  const mask = new Uint16Array(T * N), union = new Uint8Array(T * N);
  for (const k of active) {
    const bit = 1 << S.vars.indexOf(S.byKey[k]), ex = S.exceed[k], d = dilate(ex, offs, w);
    for (let i = 0; i < T * N; i++) { if (d[i]) mask[i] |= bit; if (ex[i]) union[i] = 1; }
  }
  const anchor = r.anchor ? dilate(union, [0, 0], w) : null;
  const mustBit = r.mustInclude ? 1 << S.vars.indexOf(S.byKey[r.mustInclude]) : 0;
  const flag = new Uint8Array(T * N);
  for (let i = 0; i < T * N; i++) {
    const m = mask[i];
    if (POP[m] >= r.minCount && (!mustBit || m & mustBit) && (!anchor || anchor[i])) { flag[i] = 1; res.count[(i / N) | 0]++; res.total++; }
  }
  res.flag = flag; res.mask = mask;
  return res;
}

function computeRulesAndEpisodes() {
  const { T, N } = S;
  S.rules = S.cfg.rules.map(computeRule);
  S.flagRules = new Uint8Array(T * N);
  S.flagHaz = new Uint16Array(T * N);
  S.rules.forEach((res, ri) => {
    if (!res.flag) return;
    for (let i = 0; i < T * N; i++) if (res.flag[i]) { S.flagRules[i] |= 1 << ri; S.flagHaz[i] |= res.mask[i]; }
  });
  computeEpisodes();
}

// Episodes: flagged cells connected through 8-neighbours in the same hour, or the same cell one hour apart.
function computeEpisodes() {
  const { T, N, NX, NY } = S, F = S.flagRules, lab = new Int32Array(T * N), q = new Int32Array(T * N);
  const eps = [];
  for (let i0 = 0; i0 < T * N; i0++) {
    if (!F[i0] || lab[i0]) continue;
    const id = eps.length + 1, perT = new Map();
    let head = 0, tail = 0, haz = 0, rules = 0, lat = 0, lon = 0, n = 0, t0 = T, t1 = -1;
    q[tail++] = i0; lab[i0] = id;
    while (head < tail) {
      const i = q[head++], t = (i / N) | 0, c = i - t * N, y = (c / NX) | 0, x = c - y * NX;
      n++; haz |= S.flagHaz[i]; rules |= F[i];
      lat += S.centers[c * 2 + 1]; lon += S.centers[c * 2];
      perT.set(t, (perT.get(t) || 0) + 1);
      if (t < t0) t0 = t;
      if (t > t1) t1 = t;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        if (!dy && !dx) continue;
        const yy = y + dy, xx = x + dx;
        if (yy < 0 || yy >= NY || xx < 0 || xx >= NX) continue;
        const j = t * N + yy * NX + xx;
        if (F[j] && !lab[j]) { lab[j] = id; q[tail++] = j; }
      }
      for (const j of [i - N, i + N]) if (j >= 0 && j < T * N && F[j] && !lab[j]) { lab[j] = id; q[tail++] = j; }
    }
    let peakT = t0, peakN = 0;
    for (const [t, k] of perT) if (k > peakN || (k === peakN && t < peakT)) { peakT = t; peakN = k; }
    eps.push({ id, t0, t1, peakT, peakN, cellHours: n, lat: lat / n, lon: lon / n, haz, rules });
  }
  S.epLabel = lab;
  S.episodes = eps.sort((a, b) => b.cellHours - a.cellHours || a.t0 - b.t0);
  if (S.selEpisode && !eps.some((e) => e.id === S.selEpisode)) S.selEpisode = null;
}

function recomputeAll() {
  for (const v of S.vars) computeExceed(v.key);
  computeRulesAndEpisodes();
  refreshCardsStatus();
  render();
}

const recomputeVar = debounce((key) => {
  computeExceed(key);
  computeRulesAndEpisodes();
  refreshCardsStatus();
  render();
  saveConfig();
}, 120);

const recomputeRules = debounce(() => {
  computeRulesAndEpisodes();
  refreshCardsStatus();
  render();
  saveConfig();
}, 120);

// ------------------------------------------------------------------ side panels

function hazardName(k) { return S.byKey[k]?.label || k; }

function thresholdText(key) {
  const c = S.cfg.vars[key], v = S.byKey[key], op = c.direction === '<=' ? '≤' : '≥';
  if (c.mode === 'pct') {
    const fl = c.floor == null || c.floor === '' ? '' : ` and ${op} ${fmt(+c.floor)} ${v.units}`;
    return `${c.direction === '<=' ? '<' : '>'} cell's p${c.pct}${fl}`;
  }
  return `${op} ${fmt(+c.value)} ${v.units}`;
}

function buildVarCards() {
  const list = $('#var-list');
  list.replaceChildren();
  for (const v of S.vars) {
    const c = S.cfg.vars[v.key], st = v.stats;
    const card = h('div', { class: 'card' + (c.enabled ? '' : ' off'), 'data-key': v.key });
    const changed = () => { card.classList.toggle('off', !c.enabled); recomputeVar(v.key); };

    const enable = h('input', { type: 'checkbox', checked: c.enabled, 'aria-label': `Use ${v.label}`,
      onchange: (e) => { c.enabled = e.target.checked; changed(); buildRuleCards(); } });
    const head = h('div', { class: 'card-head' }, enable,
      h('span', { class: 'swatch', style: `background:${slotColor(v.color_slot)}` }),
      h('span', { class: 'name', title: `${v.label}\n${v.source}`, text: v.label }));

    const mode = h('select', { 'aria-label': 'Threshold type', onchange: (e) => { c.mode = e.target.value; syncMode(); changed(); } },
      h('option', { value: 'abs', text: 'Absolute value' }), h('option', { value: 'pct', text: 'Place-specific percentile' }));
    mode.value = c.mode;
    const dir = h('select', { 'aria-label': 'Direction', onchange: (e) => { c.direction = e.target.value; changed(); } },
      h('option', { value: '>=', text: 'high (≥)' }), h('option', { value: '<=', text: 'low (≤)' }));
    dir.value = c.direction;

    // absolute threshold: number + slider over the variable's observed range
    const lo = st.min ?? 0, hi = st.max ?? 1, step = (hi - lo) / 500 || 0.01;
    const num = h('input', { type: 'number', value: c.value, step: 'any', 'aria-label': 'Threshold value' });
    const rng = h('input', { type: 'range', min: lo, max: hi, step, value: c.value, 'aria-label': 'Threshold slider' });
    num.addEventListener('input', () => { if (num.value !== '') { c.value = +num.value; rng.value = c.value; changed(); } });
    rng.addEventListener('input', () => { c.value = +(+rng.value).toPrecision(4); num.value = c.value; changed(); });
    const absRow = h('div', { class: 'row' }, num, h('span', { class: 'muted', text: v.units }), rng);

    const pct = h('input', { type: 'number', value: c.pct, min: 50, max: 100, step: 0.1, 'aria-label': 'Percentile' });
    pct.addEventListener('input', () => { if (pct.value !== '') { c.pct = +pct.value; changed(); } });
    const floor = h('input', { type: 'number', value: c.floor ?? '', step: 'any', placeholder: 'none', 'aria-label': 'Absolute floor' });
    floor.addEventListener('input', () => { c.floor = floor.value === '' ? null : +floor.value; changed(); });
    const pctRow = h('div', { class: 'row' }, h('span', { text: 'percentile' }), pct,
      h('span', { text: 'and at least' }), floor, h('span', { class: 'muted', text: v.units }));

    function syncMode() { absRow.hidden = c.mode !== 'abs'; pctRow.hidden = c.mode !== 'pct'; }
    syncMode();

    const range = h('div', { class: 'status muted',
      text: `event range ${fmt(st.min)}–${fmt(st.max)} ${v.units} · p99 ${fmt(st.p99)} · valid ${Math.round(st.valid_frac * 100)}%` });
    card.append(head, h('div', { class: 'row' }, mode, dir), absRow, pctRow, range, h('div', { class: 'status', 'data-status': '' }));
    list.append(card);
  }
}

function buildRuleCards() {
  const list = $('#rule-list');
  list.replaceChildren();
  S.cfg.rules.forEach((r, ri) => {
    const changed = () => recomputeRules();
    const card = h('div', { class: 'card' + (r.enabled ? '' : ' off'), 'data-rule': ri });
    const name = h('input', { type: 'text', class: 'rule-name', value: r.name, 'aria-label': 'Rule name',
      oninput: (e) => { r.name = e.target.value; saveConfig(); renderEpisodes(); } });
    const del = h('button', { class: 'icon-btn small', title: 'Delete rule', 'aria-label': 'Delete rule', text: '×',
      onclick: () => { S.cfg.rules.splice(ri, 1); buildRuleCards(); recomputeRules(); } });
    const en = h('input', { type: 'checkbox', checked: r.enabled, 'aria-label': 'Use this rule',
      onchange: (e) => { r.enabled = e.target.checked; card.classList.toggle('off', !r.enabled); changed(); } });

    const chips = h('div', { class: 'chips' }, S.vars.map((v) => {
      const on = S.cfg.vars[v.key].enabled;
      const cb = h('input', { type: 'checkbox', checked: r.hazards.includes(v.key), disabled: !on,
        onchange: (e) => {
          r.hazards = e.target.checked ? [...new Set([...r.hazards, v.key])] : r.hazards.filter((k) => k !== v.key);
          changed();
        } });
      return h('label', { class: 'chip' + (on ? '' : ' disabled'), title: on ? v.label : `${v.label} (switched off above)` },
        cb, h('span', { class: 'swatch', style: `background:${slotColor(v.color_slot)}` }), h('span', { text: shortName(v) }));
    }));

    const num = (val, attrs, set) => h('input', { type: 'number', value: val, ...attrs,
      oninput: (e) => { if (e.target.value !== '') { set(+e.target.value); changed(); } } });
    const must = h('select', { 'aria-label': 'Must include', onchange: (e) => { r.mustInclude = e.target.value; changed(); } },
      h('option', { value: '', text: 'any' }), S.vars.map((v) => h('option', { value: v.key, text: shortName(v) })));
    must.value = r.mustInclude || '';
    const anchor = h('input', { type: 'checkbox', checked: r.anchor, onchange: (e) => { r.anchor = e.target.checked; changed(); } });

    card.append(
      h('div', { class: 'card-head' }, en, name, del),
      chips,
      h('div', { class: 'row' }, h('span', { text: 'at least' }), num(r.minCount, { min: 1, max: 16, step: 1, style: 'width:52px', 'aria-label': 'Minimum hazards' }, (x) => { r.minCount = Math.max(1, Math.round(x)); }),
        h('span', { text: 'of these, including' }), must),
      h('div', { class: 'row' }, h('span', { text: 'within' }), num(r.radiusKm, { min: 0, step: 25, style: 'width:64px', 'aria-label': 'Distance in km' }, (x) => { r.radiusKm = Math.max(0, x); }),
        h('span', { text: 'km and ±' }), num(r.windowH, { min: 0, step: 1, style: 'width:52px', 'aria-label': 'Time window in hours' }, (x) => { r.windowH = Math.max(0, Math.round(x)); }),
        h('span', { text: 'h' })),
      h('label', { class: 'row small nowrap', title: 'Without this, the distance buffer alone can flag cells where no hazard occurs' },
        anchor, h('span', { text: 'only flag cells where one of these hazards occurs' })),
      h('div', { class: 'status', 'data-status': '' }),
    );
    list.append(card);
  });
  $('#rule-add').disabled = S.cfg.rules.length >= 8;
  refreshCardsStatus();
}

function shortName(v) {
  return ({ space_weather: 'Space weather', precip_1h: 'Precip', gust: 'Gust', t2m: 'Temp', hail: 'Hail (MESH)',
    rotation: 'Rotation', flash_ari: 'Rain ARI', frp: 'Fire', hail_swdi: 'Hail (radar)', tvs: 'Tornado sig.' })[v.key] || v.label;
}

function refreshCardsStatus() {
  if (!S.man) return;
  for (const v of S.vars) {
    const el = $(`#var-list [data-key="${v.key}"] [data-status]`);
    if (!el) continue;
    const cnt = S.varCount[v.key];
    el.textContent = cnt ? `now ${cnt[S.t]} cells · event ${S.varTotal[v.key].toLocaleString()} cell-hours` : 'switched off';
  }
  S.cfg.rules.forEach((r, ri) => {
    const el = $(`#rule-list [data-rule="${ri}"] [data-status]`), res = S.rules[ri];
    if (!el || !res) return;
    el.textContent = res.flag ? `now ${res.count[S.t]} cells · event ${res.total.toLocaleString()} cell-hours` : res.note;
  });
}

function buildBgSelect() {
  const sel = $('#bg-select');
  sel.replaceChildren(h('option', { value: '', text: 'none' }), ...S.vars.map((v) => h('option', { value: v.key, text: v.label })));
  sel.value = S.cfg.bg;
}

function buildDataNotes() {
  const box = $('#data-notes');
  box.replaceChildren(
    h('div', { class: 'muted', text: `${S.man.title}: ${S.T} hourly steps from ${timeLabel(0)}. 50 km EPSG:5070 grid. Built ${S.man.built}.` }),
    h('ul', { class: 'notes-missing' }, S.vars.map((v) => h('li', {}, h('b', { text: shortName(v) + ': ' }), document.createTextNode(v.source)))),
  );
  if (S.man.missing.length) {
    box.append(h('div', { text: 'Not on this machine (skipped):' }),
      h('ul', { class: 'notes-missing' }, S.man.missing.map((m) => h('li', { text: `${m.label}: ${m.reason}` }))));
  }
}

// ------------------------------------------------------------------ map geometry

function viewFor(canvas, pad = 8) {
  const { NX, NY } = S, g = S.man.grid;
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  const xmin = g.x0_km - g.dx_km / 2, ymax = g.y0_km + (NY - 0.5) * g.dy_km;
  const W = NX * g.dx_km, H = NY * g.dy_km;
  const s = Math.min((w - 2 * pad) / W, (hgt - 2 * pad) / H);
  return { s, ox: (w - W * s) / 2, oy: (hgt - H * s) / 2, xmin, ymax, cw: g.dx_km * s, ch: g.dy_km * s, w, h: hgt };
}

function prepCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  return ctx;
}

function cellAt(V, px, py) {
  const ix = Math.floor((px - V.ox) / V.cw), row = Math.floor((py - V.oy) / V.ch);
  if (ix < 0 || ix >= S.NX || row < 0 || row >= S.NY) return -1;
  return (S.NY - 1 - row) * S.NX + ix;
}
const cellX = (V, c) => V.ox + (c % S.NX) * V.cw;
const cellY = (V, c) => V.oy + (S.NY - 1 - Math.floor(c / S.NX)) * V.ch;

function fieldImage(key, t) {
  const { NX, NY, N } = S, v = S.data[key], sc = S.scales[key];
  const lut = S.ramps[key] || (S.ramps[key] = makeRamp(S.byKey[key]));
  const nod = rgb(css('--nodata'));
  const img = new ImageData(NX, NY), d = img.data;
  for (let c = 0; c < N; c++) {
    const x = v[t * N + c], iy = (c / NX) | 0, ix = c - iy * NX, o = ((NY - 1 - iy) * NX + ix) * 4;
    if (x !== x) { d[o] = nod[0]; d[o + 1] = nod[1]; d[o + 2] = nod[2]; d[o + 3] = 255; continue; }
    const k = Math.round(sc.f(x) * 255) * 3;
    d[o] = lut[k]; d[o + 1] = lut[k + 1]; d[o + 2] = lut[k + 2]; d[o + 3] = 255;
  }
  const off = S.offscreen || (S.offscreen = document.createElement('canvas'));
  off.width = NX; off.height = NY;
  off.getContext('2d').putImageData(img, 0, 0);
  return off;
}

function drawField(ctx, V, key, t) {
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(fieldImage(key, t), V.ox, V.oy, S.NX * V.cw, S.NY * V.ch);
}

function drawOutsideMask(ctx, V) {
  if (!S.cfg.conusOnly) return;
  ctx.fillStyle = css('--surface');
  ctx.globalAlpha = 0.6;
  ctx.beginPath();
  for (let c = 0; c < S.N; c++) if (!S.inConus[c]) ctx.rect(cellX(V, c), cellY(V, c), V.cw + 0.5, V.ch + 0.5);
  ctx.fill();
  ctx.globalAlpha = 1;
}

function drawStates(ctx, V) {
  ctx.strokeStyle = css('--ink2');
  ctx.globalAlpha = 0.55;
  ctx.lineWidth = 0.7;
  ctx.beginPath();
  for (const line of S.states.lines) {
    line.forEach(([x, y], i) => {
      const px = V.ox + (x - V.xmin) * V.s, py = V.oy + (V.ymax - y) * V.s;
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
}

// Outline the edge of a set of cells (edges between an inside cell and an outside one).
function maskPath(ctx, V, inside) {
  const { NX, NY, N } = S;
  ctx.beginPath();
  for (let c = 0; c < N; c++) {
    if (!inside(c)) continue;
    const iy = (c / NX) | 0, ix = c - iy * NX, x = cellX(V, c), y = cellY(V, c);
    if (iy === NY - 1 || !inside(c + NX)) { ctx.moveTo(x, y); ctx.lineTo(x + V.cw, y); }           // north
    if (iy === 0 || !inside(c - NX)) { ctx.moveTo(x, y + V.ch); ctx.lineTo(x + V.cw, y + V.ch); }  // south
    if (ix === 0 || !inside(c - 1)) { ctx.moveTo(x, y); ctx.lineTo(x, y + V.ch); }                 // west
    if (ix === NX - 1 || !inside(c + 1)) { ctx.moveTo(x + V.cw, y); ctx.lineTo(x + V.cw, y + V.ch); } // east
  }
}

function fillCells(ctx, V, inside) {
  ctx.beginPath();
  for (let c = 0; c < S.N; c++) if (inside(c)) ctx.rect(cellX(V, c), cellY(V, c), V.cw, V.ch);
  ctx.fill();
}

function hatchPattern(ctx) {
  const p = document.createElement('canvas');
  p.width = p.height = 6;
  const g = p.getContext('2d');
  g.strokeStyle = css('--critical');
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(0, 6); g.lineTo(6, 0); g.moveTo(-1, 1); g.lineTo(1, -1); g.moveTo(5, 7); g.lineTo(7, 5); g.stroke();
  return ctx.createPattern(p, 'repeat');
}

function haloStroke(ctx, color, width) {
  ctx.lineCap = 'round';
  ctx.strokeStyle = css('--surface'); ctx.lineWidth = width + 2; ctx.stroke();
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
}

// ------------------------------------------------------------------ rendering

function render() {
  if (!S.man) return;
  $('#t-slider').value = S.t;
  $('#t-label').textContent = timeLabel(S.t);
  if (S.view === 'map') drawMap(); else drawFields();
  drawTimeline();
  renderEpisodes();
  renderCellDetail();
  refreshCardsStatus();
  writeHash();
}

function drawMap() {
  const cv = $('#map'), ctx = prepCanvas(cv), V = viewFor(cv), { t, N } = S, base = t * N;
  S.mapView = V;
  ctx.fillStyle = css('--nodata');
  ctx.fillRect(V.ox, V.oy, S.NX * V.cw, S.NY * V.ch);
  if (S.cfg.bg && S.byKey[S.cfg.bg]) drawField(ctx, V, S.cfg.bg, t);
  else { ctx.fillStyle = css('--surface'); ctx.fillRect(V.ox, V.oy, S.NX * V.cw, S.NY * V.ch); }
  drawOutsideMask(ctx, V);
  drawStates(ctx, V);

  const showFill = $('#show-fill').checked, showOut = $('#show-outlines').checked;
  for (const v of S.vars) {
    const ex = S.exceed[v.key];
    if (!ex || !S.varCount[v.key][t]) continue;
    const inside = (c) => ex[base + c] === 1, col = slotColor(v.color_slot);
    if (showFill) { ctx.fillStyle = col; ctx.globalAlpha = 0.35; fillCells(ctx, V, inside); ctx.globalAlpha = 1; }
    if (showOut) { maskPath(ctx, V, inside); haloStroke(ctx, col, 1.6); }
  }

  if ($('#show-flags').checked) {
    const F = S.flagRules, inside = (c) => F[base + c] !== 0;
    ctx.fillStyle = hatchPattern(ctx);
    fillCells(ctx, V, inside);
    maskPath(ctx, V, inside);
    haloStroke(ctx, css('--ink'), 2.2);
  }
  if (S.selEpisode) {
    const L = S.epLabel, id = S.selEpisode;
    maskPath(ctx, V, (c) => L[base + c] === id);
    ctx.setLineDash([4, 3]); haloStroke(ctx, css('--critical'), 2); ctx.setLineDash([]);
  }
  for (const c of [S.pinned, S.hover?.view === 'map' ? S.hover.c : null]) {
    if (c == null || c < 0) continue;
    ctx.strokeStyle = css('--ink'); ctx.lineWidth = c === S.pinned ? 2 : 1;
    ctx.strokeRect(cellX(V, c) + 0.5, cellY(V, c) + 0.5, V.cw - 1, V.ch - 1);
  }
  renderLegend();
}

function colorbar(key) {
  const sc = S.scales[key], lut = S.ramps[key] || (S.ramps[key] = makeRamp(S.byKey[key]));
  const stops = [];
  for (let i = 0; i <= 10; i++) { const k = Math.round(i / 10 * 255) * 3; stops.push(`rgb(${lut[k]},${lut[k + 1]},${lut[k + 2]}) ${i * 10}%`); }
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((p) => fmt(sc.inv(p)));
  return [h('div', { class: 'bar', style: `background:linear-gradient(90deg,${stops.join(',')})` }),
    h('div', { class: 'ticks' }, ticks.map((x) => h('span', { text: x })))];
}

function renderLegend() {
  const kids = [];
  const bg = S.cfg.bg && S.byKey[S.cfg.bg];
  $('#lg-title').textContent = bg ? `${bg.label} (${bg.units})` : 'Legend';
  $('#lg-title').title = $('#lg-title').textContent;
  if (bg) kids.push(...colorbar(bg.key));
  const keys = S.vars.filter((v) => S.exceed[v.key]).map((v) =>
    h('span', { class: 'key' }, h('span', { class: 'linekey', style: `background:${slotColor(v.color_slot)};height:3px` }), h('span', { text: shortName(v) })));
  keys.push(h('span', { class: 'key' }, h('span', { class: 'flag-key' }), h('span', { text: 'flagged interaction' })));
  kids.push(h('div', { class: 'keys' }, keys));
  $('#lg-body').replaceChildren(...kids);
}

// legend: collapse with the button, drag by its header, double-click the header to reset; remembered per browser
function wireLegend() {
  const box = $('#legend'), wrap = box.parentElement, head = box.querySelector('.lg-head'), btn = $('#lg-toggle');
  const st = store.get('hx:legend') || {};
  const setCollapsed = (c) => {
    box.classList.toggle('collapsed', c);
    btn.textContent = c ? '+' : '–';
    btn.setAttribute('aria-expanded', String(!c));
    btn.setAttribute('aria-label', c ? 'Expand legend' : 'Collapse legend');
  };
  const place = () => {
    if (st.x == null) { box.style.left = box.style.top = ''; box.style.bottom = ''; return; }
    const maxX = Math.max(0, wrap.clientWidth - box.offsetWidth), maxY = Math.max(0, wrap.clientHeight - box.offsetHeight);
    box.style.left = `${Math.min(Math.max(0, st.x), maxX)}px`;
    box.style.top = `${Math.min(Math.max(0, st.y), maxY)}px`;
    box.style.bottom = 'auto';
  };
  setCollapsed(!!st.collapsed);
  place();
  btn.onclick = () => { st.collapsed = !st.collapsed; setCollapsed(st.collapsed); place(); store.set('hx:legend', st); };
  let drag = null;
  head.addEventListener('pointerdown', (ev) => {
    if (ev.target.closest('button')) return;
    drag = { dx: ev.clientX - box.offsetLeft, dy: ev.clientY - box.offsetTop };
    head.setPointerCapture(ev.pointerId); box.classList.add('dragging');
  });
  head.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    st.x = ev.clientX - drag.dx; st.y = ev.clientY - drag.dy; place();
  });
  head.addEventListener('pointerup', () => { if (!drag) return; drag = null; box.classList.remove('dragging'); store.set('hx:legend', st); });
  head.addEventListener('dblclick', (ev) => { if (ev.target.closest('button')) return; delete st.x; delete st.y; place(); store.set('hx:legend', st); });
  new ResizeObserver(place).observe(wrap);
}

function drawFields() {
  const grid = $('#fields-grid');
  const keys = S.vars.filter((v) => S.cfg.vars[v.key].enabled).map((v) => v.key);
  if (grid.dataset.keys !== keys.join(',')) {
    grid.dataset.keys = keys.join(',');
    grid.replaceChildren(...keys.map((k) => {
      const v = S.byKey[k], cv = h('canvas', { 'aria-label': `${v.label} field` });
      attachHover(cv, () => S.fieldViews?.[k], 'field:' + k);
      return h('div', { class: 'field-card', 'data-key': k },
        h('div', { class: 'ttl', text: v.label }), h('div', { class: 'sub' }), h('div', { class: 'cv' }, cv), ...colorbar(k));
    }));
  }
  S.fieldViews = {};
  const { t, N } = S, base = t * N;
  for (const k of keys) {
    const card = grid.querySelector(`[data-key="${k}"]`), cv = card.querySelector('canvas');
    const ctx = prepCanvas(cv), V = viewFor(cv, 4), ex = S.exceed[k], v = S.data[k];
    S.fieldViews[k] = V;
    drawField(ctx, V, k, t);
    drawOutsideMask(ctx, V);
    drawStates(ctx, V);
    if (ex) { maskPath(ctx, V, (c) => ex[base + c] === 1); haloStroke(ctx, css('--ink'), 1.6); }
    let valid = 0, tot = 0;
    for (let c = 0; c < N; c++) { if (S.cfg.conusOnly && !S.inConus[c]) continue; tot++; if (v[base + c] === v[base + c]) valid++; }
    card.querySelector('.sub').textContent =
      `threshold ${thresholdText(k)} | active cells=${S.varCount[k]?.[t] ?? 0} | valid=${Math.round(100 * valid / Math.max(1, tot))}%`;
    if (S.pinned != null) { ctx.strokeStyle = css('--ink'); ctx.lineWidth = 2; ctx.strokeRect(cellX(V, S.pinned), cellY(V, S.pinned), V.cw, V.ch); }
  }
}

// Swimlane timeline: one row per hazard (cells exceeding each hour) and per rule (cells flagged).
function drawTimeline() {
  const cv = $('#timeline'), ctx = prepCanvas(cv), w = cv.clientWidth, hgt = cv.clientHeight;
  const rows = [
    ...S.vars.filter((v) => S.varCount[v.key]).map((v) => ({ label: shortName(v), counts: S.varCount[v.key], color: slotColor(v.color_slot) })),
    ...S.cfg.rules.map((r, ri) => ({ label: r.name || `Rule ${ri + 1}`, counts: S.rules[ri]?.flag ? S.rules[ri].count : null, color: css('--critical'), rule: true })),
  ];
  const left = 120, top = 4, bottom = 18, rh = Math.min(16, (hgt - top - bottom) / Math.max(1, rows.length));
  const cw = (w - left - 8) / S.T;
  S.timelineGeom = { left, cw, top, rh, rows };
  ctx.font = '11px system-ui, -apple-system, sans-serif';
  ctx.textBaseline = 'middle';
  rows.forEach((r, i) => {
    const y = top + i * rh;
    ctx.fillStyle = css('--grid');
    ctx.fillRect(left, y + 1, w - left - 8, rh - 2);
    ctx.fillStyle = r.counts ? css('--ink2') : css('--muted');
    ctx.textAlign = 'right';
    ctx.fillText(r.label.length > 18 ? r.label.slice(0, 17) + '…' : r.label, left - 6, y + rh / 2);
    if (!r.counts) return;
    let mx = 0;
    for (const k of r.counts) if (k > mx) mx = k;
    if (!mx) return;
    ctx.fillStyle = r.color;
    for (let t = 0; t < S.T; t++) {
      if (!r.counts[t]) continue;
      ctx.globalAlpha = 0.25 + 0.75 * Math.sqrt(r.counts[t] / mx);
      ctx.fillRect(left + t * cw, y + 1, Math.max(1, cw), rh - 2);
    }
    ctx.globalAlpha = 1;
  });
  // day ticks
  const yAxis = top + rows.length * rh + 2;
  ctx.fillStyle = css('--muted');
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  const start = timeAt(0), firstMidnight = (24 - start.getUTCHours()) % 24;
  const every = cw * 24 < 34 ? 2 : 1;
  for (let t = firstMidnight, d = 0; t < S.T; t += 24, d++) {
    ctx.fillRect(left + t * cw, top, 1, rows.length * rh);
    if (d % every === 0) ctx.fillText(timeAt(t).toISOString().slice(5, 10), left + t * cw + 2, yAxis);
  }
  // cursor
  ctx.fillStyle = css('--ink');
  ctx.fillRect(left + S.t * cw + cw / 2 - 1, top - 2, 2, rows.length * rh + 4);
}

function renderEpisodes() {
  const list = $('#episode-list'), eps = S.episodes;
  const total = eps.reduce((s, e) => s + e.cellHours, 0);
  $('#episode-summary').textContent = eps.length
    ? `${eps.length} episodes · ${total.toLocaleString()} flagged cell-hours${eps.length > 40 ? ' · showing the 40 largest' : ''}`
    : 'No flagged cells with the current thresholds and rules.';
  list.replaceChildren(...eps.slice(0, 40).map((e) => {
    const hz = S.vars.filter((v, i) => e.haz & (1 << i));
    const rules = S.cfg.rules.filter((r, i) => e.rules & (1 << i)).map((r) => r.name).join(', ');
    return h('li', { class: e.id === S.selEpisode ? 'sel' : '', tabindex: 0,
      onclick: () => selectEpisode(e), onkeydown: (ev) => { if (ev.key === 'Enter') selectEpisode(e); } },
      h('div', { class: 'ep-top' }, h('b', { text: timeLabel(e.peakT) }), h('span', { text: `${e.cellHours} cell-h` })),
      h('div', { class: 'ep-sub', text: `${e.lat.toFixed(1)}°N ${Math.abs(e.lon).toFixed(1)}°W · ${e.t1 - e.t0 + 1} h · peak ${e.peakN} cells` }),
      h('div', { class: 'haz-tags' }, hz.map((v) => h('span', {}, h('span', { class: 'linekey', style: `background:${slotColor(v.color_slot)};height:3px` }), document.createTextNode(shortName(v))))),
      h('div', { class: 'ep-sub', text: rules }));
  }));
}

function selectEpisode(e) {
  S.selEpisode = S.selEpisode === e.id ? null : e.id;
  if (S.selEpisode) {
    S.t = e.peakT;
    // pin the flagged cell nearest the episode's centroid at its peak hour
    let best = -1, bd = Infinity;
    for (let c = 0; c < S.N; c++) {
      if (S.epLabel[e.peakT * S.N + c] !== e.id) continue;
      const [la, lo] = cellLatLon(c), d = (la - e.lat) ** 2 + (lo - e.lon) ** 2;
      if (d < bd) { bd = d; best = c; }
    }
    if (best >= 0) S.pinned = best;
  }
  render();
}

function renderCellDetail() {
  const box = $('#cell-detail'), c = S.pinned;
  $('#cell-hint').hidden = c != null;
  if (c == null) { box.replaceChildren(); return; }
  const [lat, lon] = cellLatLon(c);
  const kids = [h('div', { class: 'small muted', text: `Cell ${c % S.NX}, ${Math.floor(c / S.NX)} · ${lat.toFixed(2)}°N ${Math.abs(lon).toFixed(2)}°W` }),
    h('button', { class: 'small', text: 'Unpin', onclick: () => { S.pinned = null; render(); } })];
  for (const v of S.vars) {
    const cv = h('canvas');
    const val = S.data[v.key][S.t * S.N + c];
    kids.push(h('div', { class: 'spark' },
      h('div', { class: 'sp-head' }, h('span', { text: shortName(v) }), h('span', { class: 'v', text: `${fmt(val)} ${v.units}` })), cv));
    requestAnimationFrame(() => drawSpark(cv, v, c));
  }
  box.replaceChildren(...kids);
}

function drawSpark(cv, v, c) {
  const ctx = prepCanvas(cv), w = cv.clientWidth, hgt = cv.clientHeight, { T, N } = S, d = S.data[v.key];
  const cfg = S.cfg.vars[v.key];
  const thr = cfg.mode === 'pct' ? S.cellThr[v.key]?.[c] : +cfg.value;
  let lo = Infinity, hi = -Infinity;
  for (let t = 0; t < T; t++) { const x = d[t * N + c]; if (x === x) { if (x < lo) lo = x; if (x > hi) hi = x; } }
  if (Number.isFinite(thr) && cfg.enabled) { lo = Math.min(lo, thr); hi = Math.max(hi, thr); }
  if (!Number.isFinite(lo)) { ctx.fillStyle = css('--muted'); ctx.font = '11px system-ui'; ctx.fillText('no data', 4, 14); return; }
  if (hi === lo) hi = lo + 1;
  const X = (t) => (t + 0.5) / T * w, Y = (x) => hgt - 3 - (x - lo) / (hi - lo) * (hgt - 6);
  const ex = S.exceed[v.key];
  if (ex) {  // exceedance hours as a faint band
    ctx.fillStyle = slotColor(v.color_slot); ctx.globalAlpha = 0.18;
    for (let t = 0; t < T; t++) if (ex[t * N + c]) ctx.fillRect(t / T * w, 0, Math.max(1, w / T), hgt);
    ctx.globalAlpha = 1;
  }
  if (Number.isFinite(thr) && cfg.enabled) {
    ctx.strokeStyle = css('--muted'); ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Y(thr)); ctx.lineTo(w, Y(thr)); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.strokeStyle = slotColor(v.color_slot); ctx.lineWidth = 1.5; ctx.beginPath();
  let pen = false;
  for (let t = 0; t < T; t++) {
    const x = d[t * N + c];
    if (x !== x) { pen = false; continue; }
    pen ? ctx.lineTo(X(t), Y(x)) : ctx.moveTo(X(t), Y(x));
    pen = true;
  }
  ctx.stroke();
  ctx.fillStyle = css('--ink');
  ctx.fillRect(X(S.t) - 0.5, 0, 1, hgt);
}

// ------------------------------------------------------------------ hover & tooltip

function showTooltip(ev, c) {
  const tip = $('#tooltip'), { t, N } = S, i = t * N + c, [lat, lon] = cellLatLon(c);
  const rows = [h('div', { class: 'tt-head', text: `${timeLabel(t)} · ${lat.toFixed(2)}°N ${Math.abs(lon).toFixed(2)}°W` })];
  for (const v of S.vars) {
    const x = S.data[v.key][i], over = S.exceed[v.key]?.[i] === 1;
    rows.push(h('div', { class: 'tt-row' },
      h('span', { class: 'linekey', style: `background:${slotColor(v.color_slot)}` }),
      h('b', { text: `${fmt(x)} ${v.units}` }), h('span', { class: 'lbl', text: shortName(v) + (over ? ' · over threshold' : '') })));
  }
  const fr = S.flagRules[i];
  if (fr) {
    const names = S.cfg.rules.filter((r, ri) => fr & (1 << ri)).map((r) => r.name);
    const hz = S.vars.filter((v, k) => S.flagHaz[i] & (1 << k)).map(shortName);
    rows.push(h('div', { class: 'tt-flag', text: `Flagged: ${names.join(', ')}` }), h('div', { class: 'lbl', text: `Hazards nearby: ${hz.join(', ')}` }));
  }
  tip.replaceChildren(...rows);
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = `${Math.max(8, x)}px`; tip.style.top = `${Math.max(8, y)}px`;
}

function attachHover(canvas, getView, viewName) {
  canvas.addEventListener('pointermove', (ev) => {
    const V = getView(); if (!V) return;
    const rect = canvas.getBoundingClientRect(), c = cellAt(V, ev.clientX - rect.left, ev.clientY - rect.top);
    if (c < 0) { $('#tooltip').hidden = true; S.hover = null; return; }
    const changed = S.hover?.c !== c;
    S.hover = { c, view: viewName };
    showTooltip(ev, c);
    if (changed && viewName === 'map') drawMap();
  });
  canvas.addEventListener('pointerleave', () => { $('#tooltip').hidden = true; S.hover = null; if (viewName === 'map') drawMap(); });
  canvas.addEventListener('click', (ev) => {
    const V = getView(); if (!V) return;
    const rect = canvas.getBoundingClientRect(), c = cellAt(V, ev.clientX - rect.left, ev.clientY - rect.top);
    S.pinned = c < 0 || c === S.pinned ? null : c;
    render();
  });
}

// ------------------------------------------------------------------ time

function setT(t) {
  S.t = Math.max(0, Math.min(S.T - 1, t));
  render();
}

function stopPlay() {
  S.playing = false;
  clearInterval(S.playTimer);
  $('#t-play').textContent = 'Play';
}

function togglePlay() {
  if (S.playing) return stopPlay();
  S.playing = true;
  $('#t-play').textContent = 'Pause';
  const tick = () => { if (S.t >= S.T - 1) S.t = -1; setT(S.t + 1); };
  S.playTimer = setInterval(tick, 1000 / +$('#t-speed').value);
}

// ------------------------------------------------------------------ export

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function cellRing(c) {
  const ix = c % S.NX, iy = Math.floor(c / S.NX), P = (y, x) => {
    const k = ((iy + y) * (S.NX + 1) + ix + x) * 2;
    return [+S.corners[k].toFixed(5), +S.corners[k + 1].toFixed(5)];
  };
  return [P(0, 0), P(0, 1), P(1, 1), P(1, 0), P(0, 0)];
}

function exportGeoJSON() {
  const { T, N } = S, feats = [];
  for (let i = 0; i < T * N; i++) {
    const fr = S.flagRules[i];
    if (!fr) continue;
    const t = (i / N) | 0, c = i - t * N;
    const props = {
      start_time: timeAt(t).toISOString(), end_time: timeAt(t + 1).toISOString(), episode: S.epLabel[i],
      rules: S.cfg.rules.filter((r, ri) => fr & (1 << ri)).map((r) => r.name).join('; '),
      hazards_nearby: S.vars.filter((v, k) => S.flagHaz[i] & (1 << k)).map((v) => v.key).join('; '),
    };
    for (const v of S.vars) {
      const x = S.data[v.key][i];
      props[v.key] = Number.isFinite(x) ? +x.toPrecision(5) : null;
      props[`${v.key}_over`] = S.exceed[v.key]?.[i] ? 1 : 0;
    }
    feats.push({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [cellRing(c)] }, properties: props });
  }
  const fc = { type: 'FeatureCollection', name: `${S.man.event_id}_flagged_cells`, settings: S.cfg, features: feats };
  download(`${S.man.event_id}_flagged_cells.geojson`, JSON.stringify(fc), 'application/geo+json');
}

function exportCSV() {
  const q = (s) => `"${String(s).replace(/"/g, '""')}"`;
  const lines = ['episode,start_utc,end_utc,peak_utc,peak_cells,cell_hours,centroid_lat,centroid_lon,hazards,rules'];
  for (const e of S.episodes) {
    lines.push([e.id, timeAt(e.t0).toISOString(), timeAt(e.t1 + 1).toISOString(), timeAt(e.peakT).toISOString(), e.peakN, e.cellHours,
      e.lat.toFixed(3), e.lon.toFixed(3), q(S.vars.filter((v, k) => e.haz & (1 << k)).map((v) => v.key).join('; ')),
      q(S.cfg.rules.filter((r, ri) => e.rules & (1 << ri)).map((r) => r.name).join('; '))].join(','));
  }
  lines.push('', `# thresholds: ${S.vars.filter((v) => S.cfg.vars[v.key].enabled).map((v) => `${v.key} ${thresholdText(v.key)}`).join(' | ')}`);
  download(`${S.man.event_id}_episodes.csv`, lines.join('\n'), 'text/csv');
}

// ------------------------------------------------------------------ wiring

function setView(view) {
  S.view = view;
  $('#map-view').hidden = view !== 'map';
  $('#fields-view').hidden = view !== 'fields';
  $('#view-map').setAttribute('aria-selected', view === 'map');
  $('#view-fields').setAttribute('aria-selected', view === 'fields');
  render();
}

function wireStaticControls() {
  $('#view-map').onclick = () => setView('map');
  $('#view-fields').onclick = () => setView('fields');
  $('#t-slider').addEventListener('input', (e) => setT(+e.target.value));
  $('#t-prev').onclick = () => setT(S.t - 1);
  $('#t-next').onclick = () => setT(S.t + 1);
  $('#t-play').onclick = togglePlay;
  $('#t-speed').onchange = () => { if (S.playing) { stopPlay(); togglePlay(); } };
  $('#bg-select').onchange = (e) => { S.cfg.bg = e.target.value; saveConfig(); render(); };
  for (const id of ['#show-outlines', '#show-fill', '#show-flags']) $(id).onchange = render;
  $('#conus-only').onchange = (e) => { S.cfg.conusOnly = e.target.checked; saveConfig(); recomputeAll(); };
  $('#rule-add').onclick = () => {
    S.cfg.rules.push({ name: `Rule ${S.cfg.rules.length + 1}`, enabled: true, hazards: S.vars.map((v) => v.key), minCount: 2,
      mustInclude: '', radiusKm: 100, windowH: 3, anchor: true });
    buildRuleCards(); recomputeRules();
  };
  $('#cfg-save').onclick = () => download(`hazard_explorer_settings_${S.man.event_id}.json`, JSON.stringify(S.cfg, null, 1), 'application/json');
  $('#cfg-load').onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    try {
      const saved = JSON.parse(await f.text());
      if (saved.event_id !== S.man.event_id && !confirm(`These settings are for ${saved.event_id}. Apply the matching thresholds to ${S.man.event_id} anyway?`)) return;
      S.cfg = mergeConfig(defaultConfig(), { ...saved, event_id: S.man.event_id });
      afterConfigChange();
    } catch (err) { alert(`Could not read settings: ${err.message}`); }
  };
  $('#cfg-reset').onclick = () => { S.cfg = defaultConfig(); afterConfigChange(); };
  $('#export-geojson').onclick = exportGeoJSON;
  $('#export-csv').onclick = exportCSV;
  attachHover($('#map'), () => S.mapView, 'map');
  wireLegend();

  // timeline: click or drag to move in time; hover for counts
  const tl = $('#timeline');
  const tAt = (ev) => {
    const g = S.timelineGeom, rect = tl.getBoundingClientRect();
    return g ? Math.floor((ev.clientX - rect.left - g.left) / g.cw) : -1;
  };
  let dragging = false;
  tl.addEventListener('pointerdown', (ev) => { dragging = true; tl.setPointerCapture(ev.pointerId); const t = tAt(ev); if (t >= 0) setT(t); });
  tl.addEventListener('pointerup', () => { dragging = false; });
  tl.addEventListener('pointermove', (ev) => {
    const t = tAt(ev), g = S.timelineGeom;
    if (dragging && t >= 0) setT(t);
    if (!g || t < 0 || t >= S.T) { $('#tooltip').hidden = true; return; }
    const tip = $('#tooltip');
    tip.replaceChildren(h('div', { class: 'tt-head', text: timeLabel(t) }), ...g.rows.map((r) =>
      h('div', { class: 'tt-row' }, h('span', { class: 'linekey', style: `background:${r.color}` }),
        h('b', { text: r.counts ? String(r.counts[t]) : '–' }), h('span', { class: 'lbl', text: `${r.label} (${r.rule ? 'flagged' : 'exceeding'} cells)` }))));
    tip.hidden = false;
    const rect = tip.getBoundingClientRect();
    tip.style.left = `${Math.min(ev.clientX + 14, innerWidth - rect.width - 8)}px`;
    tip.style.top = `${Math.max(8, ev.clientY - rect.height - 10)}px`;
  });
  tl.addEventListener('pointerleave', () => { $('#tooltip').hidden = true; });

  document.addEventListener('keydown', (ev) => {
    if (ev.target.closest('input, select, textarea, button')) return;
    if (ev.key === 'ArrowLeft') { setT(S.t - 1); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight') { setT(S.t + 1); ev.preventDefault(); }
    else if (ev.key === ' ') { togglePlay(); ev.preventDefault(); }
  });
  new ResizeObserver(debounce(render, 50)).observe($('.center'));
  darkQuery.addEventListener('change', () => { S.ramps = {}; buildVarCards(); buildRuleCards(); refreshCardsStatus(); render(); });
}

function afterConfigChange() {
  buildVarCards(); buildRuleCards(); buildBgSelect();
  $('#conus-only').checked = S.cfg.conusOnly;
  saveConfig();
  recomputeAll();
}

init();
