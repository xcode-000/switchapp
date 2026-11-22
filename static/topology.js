// Topology diagram: plain SVG, no external libraries (works offline on the LAN)
(() => {
  const D = window.TOPO, SAVED = window.TOPO_SAVED || {}, TYPES = window.TOPO_TYPES || {};
  const GROUP_COLORS = Object.fromEntries((window.TOPO_GROUPS || []).map(g => [g.name, g.color]));
  const NS = 'http://www.w3.org/2000/svg';
  const W = 132, H = 44, SX = 162, SY = 112, PER_ROW = 10;
  const PALETTE = ['#0f766e', '#2563eb', '#9333ea', '#db2777', '#ea580c', '#65a30d', '#0891b2', '#ca8a04',
    '#dc2626', '#4f46e5', '#059669', '#7c3aed', '#be185d', '#0369a1', '#a16207', '#15803d'];
  const LAYER_COLORS = { L1: '#64748b', L2: '#2563eb', L3: '#9333ea' };
  // connected equipment (expand a switch with its + button)
  const EQ = D.equipment || {};
  const EW = 112, EH = 64, EQ_COLS = 4, EQ_GAP_X = 10, EQ_GAP_Y = 12;
  const ICON_URL = window.ICON_URL || '/static/icons/';
  const KINDS = {
    pc:       { tag: 'PC',  fill: '#e0f2fe', stroke: '#0284c7', label: 'PC / workstation' },
    server:   { tag: 'SRV', fill: '#e2e8f0', stroke: '#334155', label: 'Server / NAS' },
    ap:       { tag: 'AP',  fill: '#dcfce7', stroke: '#16a34a', label: 'WiFi access point' },
    camera:   { tag: 'CAM', fill: '#ffedd5', stroke: '#ea580c', label: 'Camera' },
    printer:  { tag: 'PRN', fill: '#f3e8ff', stroke: '#9333ea', label: 'Printer / copier' },
    barcode:  { tag: 'BAR', fill: '#fae8ff', stroke: '#c026d3', label: 'Barcode / label printer' },
    hub:      { tag: 'HUB', fill: '#ccfbf1', stroke: '#0f766e', label: 'Unmanaged switch / hub' },
    phone:    { tag: 'TEL', fill: '#ccfbf1', stroke: '#0d9488', label: 'Phone' },
    firewall: { tag: 'FW',  fill: '#fee2e2', stroke: '#dc2626', label: 'Firewall' },
    router:   { tag: 'RTR', fill: '#fef3c7', stroke: '#b45309', label: 'Router / internet link' },
    switch:   { tag: 'SW',  fill: '#e0e7ff', stroke: '#4f46e5', label: 'Switch (not documented)' },
    other:    { tag: '?',   fill: '#f8fafc', stroke: '#94a3b8', label: 'Other device' },
  };
  let expanded = new Set(), eqOff = {}, eqEls = [];
  const eqKey = (sw, i) => `${sw}#${i}`;
  const eqCount = id => (EQ[id] || []).length;
  const rackMode = () => document.getElementById('tLayout').value === 'rack';
  const isOpen = id => !rackMode() && expanded.has(id) && eqCount(id) > 0;
  const gridSize = n => { const cols = Math.min(EQ_COLS, n); return { cols, rows: Math.ceil(n / cols) }; };
  const treeWidth = id => isOpen(id) ? Math.max(SX, gridSize(eqCount(id)).cols * (EW + EQ_GAP_X) + 24) : SX;
  const treeExtraH = id => isOpen(id) ? gridSize(eqCount(id)).rows * (EH + EQ_GAP_Y) + 40 : 0;

  const $ = id => document.getElementById(id);
  const svg = $('topo'), vp = $('vp'), gE = $('edges'), gN = $('nodes'), side = $('tSide');
  const ui = { layout: $('tLayout'), color: $('tColor'), group: $('tGroup'), area: $('tArea'), find: $('tFind'),
    ext: $('tExt'), labels: $('tLabels'), alone: $('tAlone') };

  const nodes = new Map(D.nodes.map(n => [n.id, n]));
  const adj = new Map([...nodes.keys()].map(id => [id, new Set()]));
  D.edges.forEach(e => { adj.get(e.a)?.add(e.b); adj.get(e.b)?.add(e.a); });
  const areas = [...new Set(D.nodes.map(n => n.area).filter(Boolean))].sort();
  const AREA_COLORS = Object.fromEntries(areas.map((a, i) => [a, PALETTE[i % PALETTE.length]]));

  let pos = {}, view = { x: 0, y: 0, k: 1 }, shown = new Set(), ghost = new Set(), selected = null;
  let nodeEls = new Map(), edgeEls = [];

  // remember UI choices for this browser
  const store = {
    get() { try { return JSON.parse(localStorage.getItem('switchdoc.topo') || '{}'); } catch { return {}; } },
    set(v) { try { localStorage.setItem('switchdoc.topo', JSON.stringify(v)); } catch { /* ignore */ } },
  };
  const pref = store.get();
  (pref.expanded || []).forEach(id => { if (EQ[id]) expanded.add(id); });
  if (Object.keys(SAVED).length && !pref.layout) ui.layout.value = 'saved';
  ['layout', 'color', 'group', 'area'].forEach(k => { if (pref[k] !== undefined && [...ui[k].options].some(o => o.value === pref[k])) ui[k].value = pref[k]; });
  ['ext', 'labels', 'alone'].forEach(k => { if (pref[k] !== undefined) ui[k].checked = pref[k]; });
  const savePref = () => store.set({ layout: ui.layout.value, color: ui.color.value, group: ui.group.value, area: ui.area.value,
    ext: ui.ext.checked, labels: ui.labels.checked, alone: ui.alone.checked, expanded: [...expanded] });

  function nodeColor(n) {
    if (n.external) return '#ffffff';
    if (ui.color.value === 'group') return GROUP_COLORS[n.groups[0]] || '#475569';
    if (ui.color.value === 'layer') return LAYER_COLORS[n.layer] || '#475569';
    return AREA_COLORS[n.area] || '#475569';
  }

  // ---------------------------------------------------------------- which nodes are shown
  function computeShown() {
    const g = ui.group.value, a = ui.area.value;
    const core = new Set([...nodes.values()].filter(n => !n.external && (!g || n.groups.includes(g)) && (!a || n.area === a)).map(n => n.id));
    shown = new Set(core); ghost = new Set();
    const filtered = g || a;
    for (const id of core) for (const nb of adj.get(id)) {
      if (core.has(nb)) continue;
      const n = nodes.get(nb);
      if (n.external) { if (ui.ext.checked) shown.add(nb); continue; }
      if (filtered) { shown.add(nb); ghost.add(nb); }
    }
    if (!filtered && ui.ext.checked) nodes.forEach(n => { if (n.external) shown.add(n.id); });
    if (!ui.alone.checked) for (const id of [...shown]) if (![...adj.get(id)].some(nb => shown.has(nb))) shown.delete(id);
  }
  const degree = id => [...adj.get(id)].filter(nb => shown.has(nb)).length;

  // ---------------------------------------------------------------- layouts
  function treeLayout() {
    const p = {};
    const ids = [...shown].sort((x, y) => {
      const cx = /core/i.test(x) ? 1 : 0, cy = /core/i.test(y) ? 1 : 0;
      return (cy - cx) || (degree(y) - degree(x)) || x.localeCompare(y);
    });
    const seen = new Set(), singles = [];
    let cursor = 0, maxY = 0, rowTop = 0, rowH = 0;
    const ROW_W = PER_ROW * SX * 1.15;
    for (const root of ids) {
      if (seen.has(root)) continue;
      if (degree(root) === 0) { seen.add(root); singles.push(root); continue; }
      // BFS levels
      const levels = [[root]]; seen.add(root);
      for (;;) {
        const next = [];
        for (const id of levels[levels.length - 1]) for (const nb of [...adj.get(id)].sort())
          if (shown.has(nb) && !seen.has(nb)) { seen.add(nb); next.push(nb); }
        if (!next.length) break;
        levels.push(next);
      }
      // order each level by the average position of its parents (fewer crossings)
      const idx = {};
      levels.forEach((lv, li) => {
        if (li > 0) {
          const bary = id => { const ps = [...adj.get(id)].filter(nb => idx[nb] !== undefined); return ps.length ? ps.reduce((s, nb) => s + idx[nb], 0) / ps.length : 1e9; };
          lv.sort((a, b) => bary(a) - bary(b) || (nodes.get(a).external - nodes.get(b).external) || a.localeCompare(b));
        }
        lv.forEach((id, i) => idx[id] = i / Math.max(1, lv.length - 1));
      });
      const rowsOf = [];
      levels.forEach(lv => { for (let r = 0; r < lv.length; r += PER_ROW) rowsOf.push({ ids: lv.slice(r, r + PER_ROW), last: r + PER_ROW >= lv.length, cont: r > 0 }); });
      const rowW = row => row.ids.reduce((s, id) => s + treeWidth(id), 0);
      const width = Math.max(...rowsOf.map(rowW));
      if (cursor > 0 && cursor + width > ROW_W) { cursor = 0; rowTop += rowH + SY * 0.8; rowH = 0; }
      let y = rowTop;
      rowsOf.forEach(row => {
        let x = cursor + (width - rowW(row)) / 2 + (row.cont ? SX / 4 : 0);
        row.ids.forEach(id => { const w = treeWidth(id); p[id] = { x: x + w / 2, y }; x += w; });
        y += (row.last ? SY : SY * 0.62) + Math.max(0, ...row.ids.map(treeExtraH));
      });
      maxY = Math.max(maxY, y);
      rowH = Math.max(rowH, y - rowTop);
      cursor += width + SX;
    }
    let sx = 0, sy = maxY + SY * 0.6, lineH = 0;
    singles.sort().forEach(id => {
      const w = treeWidth(id);
      if (sx > 0 && sx + w > ROW_W) { sx = 0; sy += SY * 0.6 + lineH; lineH = 0; }
      p[id] = { x: sx + w / 2, y: sy }; sx += w; lineH = Math.max(lineH, treeExtraH(id));
    });
    return p;
  }

  function forceLayout() {
    const p = treeLayout();
    const ids = [...shown], n = ids.length;
    if (n < 2) return p;
    const k = 190, E = [];
    D.edges.forEach(e => { if (shown.has(e.a) && shown.has(e.b)) E.push([e.a, e.b]); });
    let cx = 0, cy = 0; ids.forEach(id => { cx += p[id].x; cy += p[id].y; }); cx /= n; cy /= n;
    let temp = 260;
    for (let it = 0; it < 350; it++) {
      const d = {}; ids.forEach(id => d[id] = { x: 0, y: 0 });
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const a = p[ids[i]], b = p[ids[j]];
        let dx = a.x - b.x, dy = a.y - b.y, dist = Math.hypot(dx, dy) || 0.01;
        const f = (k * k) / dist;
        dx /= dist; dy /= dist;
        d[ids[i]].x += dx * f; d[ids[i]].y += dy * f; d[ids[j]].x -= dx * f; d[ids[j]].y -= dy * f;
      }
      for (const [a, b] of E) {
        const A = p[a], B = p[b];
        let dx = A.x - B.x, dy = A.y - B.y, dist = Math.hypot(dx, dy) || 0.01;
        const f = (dist * dist) / k;
        dx /= dist; dy /= dist;
        d[a].x -= dx * f; d[a].y -= dy * f; d[b].x += dx * f; d[b].y += dy * f;
      }
      ids.forEach(id => {
        const P = p[id];
        d[id].x += (cx - P.x) * 0.03 * k / 10; d[id].y += (cy - P.y) * 0.03 * k / 10;
        const len = Math.hypot(d[id].x, d[id].y) || 1;
        P.x += d[id].x / len * Math.min(len, temp); P.y += d[id].y / len * Math.min(len, temp);
      });
      temp *= 0.985;
    }
    return p;
  }

  // racks: every rack is a cabinet with its switches stacked inside; links are drawn between the switches
  const RACK_PAD = 18, RACK_HEAD = 40, RACK_PITCH = H + 14, RACK_GAP_X = 150, RACK_GAP_Y = 120;
  let rackBoxes = [];
  function rackLayout() {
    const p = {}, groups = new Map();
    const noRack = t('No rack');
    [...shown].forEach(id => {
      const n = nodes.get(id);
      const key = n.external ? '\u0000ext' : `${n.area || ''}\u0001${n.location || ''}\u0001${n.rack || ''}`;
      if (!groups.has(key)) groups.set(key, { key, ext: !!n.external, area: n.area || '', location: n.location || '', rack: n.rack || '', ids: [] });
      groups.get(key).ids.push(id);
    });
    const list = [...groups.values()].sort((a, b) => (b.ext - a.ext) || a.area.localeCompare(b.area) ||
      a.location.localeCompare(b.location) || (!a.rack - !b.rack) || a.rack.localeCompare(b.rack, undefined, { numeric: true }));
    // inside a rack: core / most connected switches on top
    list.forEach(g => g.ids.sort((x, y) => ((/core/i.test(y) ? 1 : 0) - (/core/i.test(x) ? 1 : 0)) || degree(y) - degree(x) || x.localeCompare(y)));
    const perRow = Math.max(4, Math.ceil(Math.sqrt(list.length * 1.8)));
    rackBoxes = [];
    let x = 0, y = 0, rowH = 0, col = 0, lastArea = null;
    list.forEach(g => {
      const cols = g.ext ? Math.min(6, g.ids.length) : 1;
      const w = cols * (W + 24) - 24 + RACK_PAD * 2;
      const h = RACK_HEAD + Math.ceil(g.ids.length / cols) * RACK_PITCH + RACK_PAD;
      const newArea = !g.ext && lastArea !== null && g.area !== lastArea;
      if (col >= perRow || (g.ext && col > 0) || (newArea && col > 0 && col >= perRow / 2) || (col > 0 && rackBoxes[rackBoxes.length - 1]?.ext)) {
        x = 0; y += rowH + RACK_GAP_Y; rowH = 0; col = 0;
      }
      if (!g.ext) lastArea = g.area;
      rackBoxes.push({ x, y, w, h, ext: g.ext, label: g.ext ? t('Other devices') : (g.rack || noRack),
        sub: g.ext ? '' : [g.location, g.area].filter(Boolean).join(' · '), n: g.ids.length, empty: !g.rack && !g.ext });
      g.ids.forEach((id, i) => {
        const c = i % cols, r = Math.floor(i / cols);
        p[id] = { x: x + RACK_PAD + c * (W + 24) + W / 2, y: y + RACK_HEAD + r * RACK_PITCH + H / 2 };
      });
      x += w + RACK_GAP_X; rowH = Math.max(rowH, h); col++;
    });
    return p;
  }
  function drawRacks() {
    gR.textContent = '';
    if (!rackMode()) return;
    rackBoxes.forEach(b => {
      const g = el('g', { class: 'rack' + (b.ext ? ' ext' : '') + (b.empty ? ' norack' : '') }, gR);
      el('rect', { class: 'rb', x: b.x, y: b.y, width: b.w, height: b.h, rx: 6 }, g);
      el('rect', { class: 'rh', x: b.x, y: b.y, width: b.w, height: 30, rx: 6 }, g);
      el('rect', { class: 'rh', x: b.x, y: b.y + 20, width: b.w, height: 10 }, g);
      if (!b.ext) [b.x + 7, b.x + b.w - 7].forEach(rx => el('line', { class: 'rail', x1: rx, y1: b.y + 34, x2: rx, y2: b.y + b.h - 6 }, g));
      el('text', { class: 'rt', x: b.x + 10, y: b.y + 19 }, g).textContent = short(b.label, Math.floor(b.w / 7.5));
      el('text', { class: 'rn', x: b.x + b.w - 10, y: b.y + 19, 'text-anchor': 'end' }, g).textContent = b.n;
      if (b.sub) el('text', { class: 'rs', x: b.x, y: b.y + b.h + 15 }, g).textContent = short(b.sub, Math.floor(b.w / 5.5));
    });
  }

  function layout() {
    const tree = treeLayout();
    if (ui.layout.value === 'rack') pos = rackLayout();
    else if (ui.layout.value === 'force') pos = forceLayout();
    else if (ui.layout.value === 'saved') { pos = {}; shown.forEach(id => pos[id] = SAVED[id] ? { x: SAVED[id][0], y: SAVED[id][1] } : tree[id]); }
    else pos = tree;
    eqOff = {};
    shown.forEach(id => { if (isOpen(id)) placeEquipment(id); });
    if (ui.layout.value === 'force') makeRoom();
  }

  // offsets of the devices around / under their switch
  const RING_CAP = k => 8 + 5 * k, RING_R = k => 175 + 85 * k;
  function ringRadius(n) { let r = 0, left = n, k = 0; while (left > 0) { left -= RING_CAP(k); r = RING_R(k); k++; } return r; }
  function placeEquipment(id) {
    const list = EQ[id] || [], n = list.length;
    if (ui.layout.value === 'tree') {
      const { cols } = gridSize(n);
      list.forEach((_, i) => {
        const c = i % cols, r = Math.floor(i / cols);
        eqOff[eqKey(id, i)] = { dx: (c - (cols - 1) / 2) * (EW + EQ_GAP_X), dy: H / 2 + 34 + EH / 2 + r * (EH + EQ_GAP_Y) };
      });
    } else {
      let i = 0, k = 0;
      while (i < n) {
        const cap = RING_CAP(k), R = RING_R(k), cnt = Math.min(cap, n - i);
        for (let j = 0; j < cnt; j++, i++) {
          const a = -Math.PI / 2 + (2 * Math.PI * j) / cnt + k * 0.35;
          eqOff[eqKey(id, i)] = { dx: Math.cos(a) * R * 1.35, dy: Math.sin(a) * R * 0.8 };
        }
        k++;
      }
      // the rings are ellipses: push apart any devices whose boxes still touch
      const offs = list.map((_, j) => eqOff[eqKey(id, j)]), gx = EW + 10, gy = EH + 10;
      for (let it = 0; it < 120; it++) {
        let moved = false;
        for (let a = 0; a < offs.length; a++) for (let b = a + 1; b < offs.length; b++) {
          const A = offs[a], B = offs[b], dx = B.dx - A.dx, dy = B.dy - A.dy;
          const ox = gx - Math.abs(dx), oy = gy - Math.abs(dy);
          if (ox <= 0 || oy <= 0) continue;
          moved = true;
          if (ox / gx < oy / gy) { const s = (dx >= 0 ? 1 : -1) * (ox / 2 + 0.5); A.dx -= s; B.dx += s; }
          else { const s = (dy >= 0 ? 1 : -1) * (oy / 2 + 0.5); A.dy -= s; B.dy += s; }
        }
        if (!moved) break;
      }
    }
  }
  // distance from the switch to its farthest device (organic layout)
  function eqExtent(id) {
    let r = 0;
    (EQ[id] || []).forEach((_, i) => { const o = eqOff[eqKey(id, i)]; if (o) r = Math.max(r, Math.hypot(o.dx, o.dy)); });
    return r || ringRadius(eqCount(id)) * 1.35;
  }
  // organic layout: push switches away from expanded switches so the device rings stay readable
  function makeRoom() {
    const ids = [...shown];
    const rad = id => isOpen(id) ? eqExtent(id) + EW / 2 + 20 : 85;
    for (let it = 0; it < 80; it++) {
      let moved = false;
      for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
        const a = ids[i], b = ids[j];
        if (!isOpen(a) && !isOpen(b)) continue;
        const A = pos[a], B = pos[b];
        let dx = B.x - A.x, dy = B.y - A.y, d = Math.hypot(dx, dy) || 0.1;
        const need = rad(a) + rad(b);
        if (d < need) {
          const push = (need - d) / 2; dx /= d; dy /= d; moved = true;
          if (!isOpen(a) || isOpen(b)) { A.x -= dx * push; A.y -= dy * push; }
          if (!isOpen(b) || isOpen(a)) { B.x += dx * push; B.y += dy * push; }
        }
      }
      if (!moved) break;
    }
  }
  const eqPos = (id, i) => { const o = eqOff[eqKey(id, i)], p = pos[id]; return o && p ? { x: p.x + o.dx, y: p.y + o.dy } : null; };

  function toggle(id, on) {
    if (!eqCount(id)) return;
    if (on === undefined) on = !expanded.has(id);
    if (on) expanded.add(id); else expanded.delete(id);
    if (ui.layout.value === 'tree') { layout(); }
    else { eqOff = Object.fromEntries(Object.entries(eqOff).filter(([k]) => !k.startsWith(id + '#'))); if (on) placeEquipment(id); if (ui.layout.value === 'force' && on) makeRoom(); }
    draw(); savePref();
  }

  // ---------------------------------------------------------------- drawing
  const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
  const short = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;

  function edgeStyle(e) {
    const counts = {};
    e.links.forEach(l => counts[l.type] = (counts[l.type] || 0) + 1);
    const type = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
    return {
      color: TYPES[type] || '#94a3b8',
      width: Math.min(8, 2 + (e.links.length - 1) * 1.6),
      warn: e.links.some(l => l.check !== 'ok'),
      off: e.links.every(l => l.status === 'disabled'),
      label: e.links.length === 1 ? `${e.links[0].a_port} ↔ ${e.links[0].b_port}` : `${e.links.length} ${t('links')}`,
    };
  }

  let gX = document.getElementById('equip');
  if (!gX) { gX = document.createElementNS(NS, 'g'); gX.id = 'equip'; vp.insertBefore(gX, gN); }
  let gR = document.getElementById('racks');
  if (!gR) { gR = document.createElementNS(NS, 'g'); gR.id = 'racks'; vp.insertBefore(gR, vp.firstChild); }

  function eqShape(g, kind) {
    const k = KINDS[kind] || KINDS.other, w = EW, h = EH, x = -w / 2, y = -h / 2;
    const st = { fill: k.fill, stroke: k.stroke, 'stroke-width': 1.5, class: 'qb' };
    const poly = pts => el('polygon', { ...st, points: pts.map(p => p.join(',')).join(' ') }, g);
    switch (kind) {
      case 'ap': el('rect', { ...st, x, y, width: w, height: h, rx: h / 2 }, g); break;
      case 'phone': el('ellipse', { ...st, cx: 0, cy: 0, rx: w / 2, ry: h / 2 }, g); break;
      case 'camera': poly([[x + 10, y], [x + w - 10, y], [x + w, 0], [x + w - 10, y + h], [x + 10, y + h], [x, 0]]); break;
      case 'firewall': poly([[x + 7, y], [x + w - 7, y], [x + w, y + 7], [x + w, y + h - 7], [x + w - 7, y + h], [x + 7, y + h], [x, y + h - 7], [x, y + 7]]); break;
      case 'router': poly([[x + 14, y], [x + w - 14, y], [x + w, 0], [x + w - 14, y + h], [x + 14, y + h], [x, 0]]);
        el('line', { x1: x + 8, y1: 0, x2: x + w - 8, y2: 0, stroke: k.stroke, 'stroke-width': 0.6, opacity: 0.35 }, g); break;
      case 'printer': case 'barcode': el('path', { ...st, d: `M${x} ${y + 6} h${w} v${h - 6} h${-w} z M${x + 14} ${y + 6} v-6 h${w - 28} v6` }, g); break;
      case 'server': el('rect', { ...st, x, y, width: w, height: h, rx: 2 }, g);
        [-6, 0, 6].forEach(d => el('line', { x1: x + 4, y1: d, x2: x + 12, y2: d, stroke: k.stroke, 'stroke-width': 2 }, g)); break;
      case 'switch': case 'hub': el('rect', { ...st, x, y, width: w, height: h, rx: 3 }, g);
        el('rect', { x: x + 3, y: y + 3, width: w - 6, height: h - 6, rx: 2, fill: 'none', stroke: k.stroke, 'stroke-width': 0.8 }, g); break;
      case 'other': el('rect', { ...st, x, y, width: w, height: h, rx: 4, 'stroke-dasharray': '4 3' }, g); break;
      default: el('rect', { ...st, x, y, width: w, height: h, rx: 4 }, g);
    }
    el('text', { class: 'qt', x: x + (kind === 'server' ? 16 : 9), y: 4, fill: k.stroke }, g).textContent = k.tag;
  }

  function drawEquipment() {
    gX.textContent = ''; eqEls = [];
    shown.forEach(id => {
      if (!isOpen(id)) return;
      (EQ[id] || []).forEach((q, i) => {
        const key = eqKey(id, i);
        const ln = el('line', { class: 'ql' + (q.status === 'disabled' ? ' off' : '') }, gX);
        let pl = null;
        if (ui.labels.checked) {
          pl = el('g', { class: 'qpl' }, gX);
          const lab = String(q.port);
          el('rect', { x: -(lab.length * 3.3 + 5), y: -7, width: lab.length * 6.6 + 10, height: 14, rx: 7 }, pl);
          el('text', { 'text-anchor': 'middle', y: 3.5 }, pl).textContent = lab;
        }
        const g = el('g', { class: 'q k-' + q.kind + (q.status === 'disabled' ? ' off' : ''), 'data-sw': id, 'data-i': i }, gX);
        // logo with the equipment name below it
        el('rect', { class: 'qb', x: -EW / 2, y: -EH / 2, width: EW, height: EH, rx: 9 }, g);
        const img = el('image', { x: -17, y: -EH / 2 + 3, width: 34, height: 34 }, g);
        img.setAttribute('href', ICON_URL + (q.icon || 'other') + '.svg');
        el('text', { class: 'qn', x: 0, y: EH / 2 - (q.behind ? 15 : 8), 'text-anchor': 'middle' }, g).textContent = short(q.name, 17);
        if (q.behind) {
          el('text', { class: 'qn qsub', x: 0, y: EH / 2 - 4, 'text-anchor': 'middle' }, g).textContent = '↳ ' + short(q.behind, 15);
          const bi = el('image', { x: EW / 2 - 24, y: -EH / 2 + 3, width: 20, height: 20 }, g);
          bi.setAttribute('href', ICON_URL + (q.behind_icon || 'other') + '.svg');
        }
        el('title', {}, g).textContent = `${q.name}${q.behind ? ' ↳ ' + q.behind : ''}\n${q.atype || t((KINDS[q.kind] || KINDS.other).label)} · ${id} ${t('port')} ${q.port}` +
          (q.vlan ? ` · VLAN ${q.vlan}` : '') + (q.status === 'disabled' ? ' ' + t('(disabled)') : '');
        eqEls.push({ id, i, key, g, ln, pl });
      });
    });
  }

  function draw() {
    gE.textContent = ''; gN.textContent = ''; nodeEls = new Map(); edgeEls = [];
    drawRacks();
    drawEquipment();
    D.edges.forEach(e => {
      if (!shown.has(e.a) || !shown.has(e.b)) return;
      const st = edgeStyle(e);
      const down = D.status && [e.a, e.b].some(x => (D.status[x] || {}).state === 'offline');
      const g = el('g', { class: 'e' + (st.warn ? ' warn' : '') + (st.off ? ' off' : '') + (down ? ' st-down' : ''), 'data-a': e.a, 'data-b': e.b }, gE);
      const hit = el('path', { stroke: 'transparent', 'stroke-width': 14, fill: 'none' }, g);
      const line = el('path', { class: 'v', stroke: st.color, 'stroke-width': st.width, fill: 'none' }, g);
      let lbl = null;
      if (ui.labels.checked) {
        lbl = el('g', { class: 'lbl' }, g);
        const tw = st.label.length * 5.6 + 10;
        el('rect', { x: -tw / 2, y: -8, width: tw, height: 16, rx: 8 }, lbl);
        el('text', { 'text-anchor': 'middle', y: 3.5 }, lbl).textContent = st.label;
      }
      el('title', {}, g).textContent = `${e.a} ↔ ${e.b}\n` + e.links.map(l => `${e.a}:${l.a_port} ↔ ${e.b}:${l.b_port}  ${l.type}${l.status === 'disabled' ? ' ' + t('(disabled)') : ''}${l.check !== 'ok' ? '  ⚠ ' + l.check : ''}`).join('\n');
      g.addEventListener('click', ev => { ev.stopPropagation(); selectEdge(e); });
      edgeEls.push({ e, lines: [hit, line], lbl });
    });
    shown.forEach(id => {
      const n = nodes.get(id);
      const ms = D.status && D.status[id];
      const g = el('g', { class: 'n' + (n.external ? ' ext' : '') + (ghost.has(id) ? ' ghost' : '') + (ms ? ' st-' + ms.state : ''), 'data-id': id }, gN);
      g.style.cursor = 'pointer';
      el('rect', { class: 'box', width: W, height: H, rx: 8, fill: nodeColor(n) }, g);
      if (ms && ms.state !== 'off') {
        el('circle', { class: 'stdot', cx: W - 9, cy: H - 9, r: 5 }, g);
        el('title', {}, g).textContent = id + ' · ' + ms.label + (ms.since ? ' · ' + t('for %(d)s').replace('%(d)s', ms.since) : '');
      }
      if (n.external) {
        const k = KINDS[n.kind];
        if (n.icon) {
          const im = el('image', { x: 6, y: 6, width: 32, height: 32 }, g); im.setAttribute('href', ICON_URL + n.icon + '.svg');
          el('text', { class: 'nm', x: W / 2 + 16, y: H / 2 + 4, 'text-anchor': 'middle' }, g).textContent = short(id, 15);
        } else if (k) {
          el('text', { class: 'ek', x: 10, y: H / 2 + 4, style: 'fill:' + k.stroke }, g).textContent = k.tag;
          el('text', { class: 'nm', x: W / 2 + 10, y: H / 2 + 4, 'text-anchor': 'middle' }, g).textContent = short(id, 17);
        } else el('text', { class: 'nm', x: W / 2, y: H / 2 + 4, 'text-anchor': 'middle' }, g).textContent = short(id, 20);
      } else {
        el('text', { class: 'nm', x: 10, y: 19 }, g).textContent = short(id, n.layer ? 13 : 17);
        el('text', { class: 'ip', x: 10, y: 34 }, g).textContent = short(n.ip || n.designation || '', 20);
        if (n.layer) { el('rect', { class: 'badge', x: W - 30, y: 6, width: 24, height: 15, rx: 4 }, g); el('text', { class: 'bt', x: W - 18, y: 17, 'text-anchor': 'middle' }, g).textContent = n.layer; }
      }
      const cnt = eqCount(id);
      if (cnt && !n.external && !rackMode()) {
        const open = isOpen(id);
        const tg = el('g', { class: 'tog' + (open ? ' open' : ''), transform: `translate(${W / 2},${H})` }, g);
        el('title', {}, tg).textContent = open ? t('Hide connected equipment') : t('Show connected equipment') + ` (${cnt})`;
        el('rect', { x: -22, y: -9, width: 44, height: 18, rx: 9 }, tg);
        el('text', { x: -11, y: 4.5, 'text-anchor': 'middle', class: 'tsign' }, tg).textContent = open ? '−' : '+';
        el('text', { x: 7, y: 3.5, 'text-anchor': 'middle', class: 'tcnt' }, tg).textContent = cnt;
      }
      el('title', {}, g).textContent = n.external ? `${id} ${n.kind ? '(' + t((KINDS[n.kind] || KINDS.other).label) + ')' : t('(not a documented switch)')}` :
        `${id}${n.designation ? ' — ' + n.designation : ''}\n${n.ip || ''}  ${n.model || ''}\n${[n.area, n.location].filter(Boolean).join(' · ')}`;
      nodeEls.set(id, g);
    });
    place();
    drawLegend();
    if (selected && selected.kind === 'node') selectNode(selected.id, false);
    else if (selected && selected.kind === 'eq') { if (isOpen(selected.id)) selectEq(selected.id, selected.i); else selected = null; }
  }

  function place() {
    nodeEls.forEach((g, id) => { const p = pos[id]; if (p) g.setAttribute('transform', `translate(${p.x - W / 2},${p.y - H / 2})`); });
    eqEls.forEach(({ id, i, g, ln, pl }) => {
      const p = pos[id], q = eqPos(id, i); if (!p || !q) return;
      g.setAttribute('transform', `translate(${q.x},${q.y})`);
      const sy = p.y + H / 2;
      ln.setAttribute('x1', p.x); ln.setAttribute('y1', ui.layout.value === 'tree' ? sy : p.y); ln.setAttribute('x2', q.x); ln.setAttribute('y2', q.y);
      if (pl) {   // port number just outside the device box, on its line
        const y0 = ui.layout.value === 'tree' ? sy : p.y;
        let dx = p.x - q.x, dy = y0 - q.y; const d = Math.hypot(dx, dy) || 1; dx /= d; dy /= d;
        const tx = Math.abs(dx) > 1e-3 ? (EW / 2) / Math.abs(dx) : 1e9, ty = Math.abs(dy) > 1e-3 ? (EH / 2) / Math.abs(dy) : 1e9;
        const off = Math.min(tx, ty) + 11;
        pl.setAttribute('transform', `translate(${q.x + dx * off},${q.y + dy * off})`);
      }
    });
    edgeEls.forEach(({ e, lines, lbl }) => {
      const a = pos[e.a], b = pos[e.b]; if (!a || !b) return;
      let d = `M${a.x},${a.y} L${b.x},${b.y}`, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      if (rackMode() && Math.abs(a.x - b.x) < 1) {   // two switches of the same rack: loop out on the right side
        const x0 = a.x + W / 2, bulge = 34 + Math.min(70, Math.abs(a.y - b.y) / 4);
        d = `M${x0},${a.y} C${x0 + bulge},${a.y} ${x0 + bulge},${b.y} ${x0},${b.y}`;
        mx = x0 + bulge * 0.75; my = (a.y + b.y) / 2;
      }
      lines.forEach(l => l.setAttribute('d', d));
      if (lbl) lbl.setAttribute('transform', `translate(${mx},${my})`);
    });
  }
  const applyView = () => vp.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);

  function bbox(ids = [...shown]) {
    const ps = ids.map(id => pos[id]).filter(Boolean);
    if (rackMode() && ids.length === shown.size) rackBoxes.forEach(b => { ps.push({ x: b.x + W / 2, y: b.y + H / 2 }); ps.push({ x: b.x + b.w - W / 2, y: b.y + b.h + 10 }); });
    ids.forEach(id => { if (isOpen(id)) (EQ[id] || []).forEach((_, i) => { const q = eqPos(id, i); if (q) ps.push(q); }); });
    if (!ps.length) return { x: 0, y: 0, w: 100, h: 100 };
    const xs = ps.map(p => p.x), ys = ps.map(p => p.y);
    const x0 = Math.min(...xs) - W, y0 = Math.min(...ys) - H, x1 = Math.max(...xs) + W, y1 = Math.max(...ys) + H;
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }
  function fit(ids) {
    const b = bbox(ids), r = svg.getBoundingClientRect();
    view.k = Math.min(r.width / b.w, r.height / b.h, 1.4);
    view.x = r.width / 2 - (b.x + b.w / 2) * view.k;
    view.y = r.height / 2 - (b.y + b.h / 2) * view.k;
    applyView();
  }

  function eqLegend() {
    const m = new Map();
    Object.values(EQ).forEach(list => list.forEach(q => { const n = q.atype || t((KINDS[q.kind] || KINDS.other).label); if (!m.has(n)) m.set(n, q.icon || 'other'); }));
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }
  function drawLegend() {
    const lg = $('tLegend');
    if (!lg) return;
    let entries = [];
    if (ui.color.value === 'area') entries = areas.map(a => [AREA_COLORS[a], a]);
    else if (ui.color.value === 'group') entries = Object.entries(GROUP_COLORS).map(([n, c]) => [c, n]);
    else entries = [[LAYER_COLORS.L1, t('Layer 1')], [LAYER_COLORS.L2, t('Layer 2')], [LAYER_COLORS.L3, t('Layer 3')], ['#475569', t('Layer not set')]];
    const lines = Object.entries(TYPES).map(([ty, c]) => `<span><i class="ln" style="background:${c}"></i>${esc(ty)} ${t('link')}</span>`).join('');
    lg.innerHTML = `<b>${t('Switches')}</b>${entries.map(([c, n]) => `<span><i class="lg" style="background:${c}"></i>${esc(n)}</span>`).join('')}
      <span><i class="lg" style="background:#fff;border:1px dashed #64748b"></i>${t('Other device (not a switch)')}</span>
      ${Object.keys(EQ).length ? `<b>${t('Equipment')}</b>` + eqLegend().map(([name, icon]) =>
        `<span><img src="${ICON_URL + icon}.svg" width="22" height="22" alt="" style="vertical-align:middle;margin-right:6px">${esc(name)}</span>`).join('') : ''}
      <b>${t('Connections')}</b>${lines}<span><i class="ln ln-dash"></i>${t('All ports disabled')}</span><span><i class="ln ln-warn"></i>${t('Documented on one side only / conflict')}</span>`;
  }
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ---------------------------------------------------------------- selection panel
  function highlight(ids, eqKeep) {
    nodeEls.forEach((g, id) => g.classList.toggle('dim', !!ids && !ids.has(id)));
    eqEls.forEach(({ id, key, g, ln, pl }) => {
      const off = !!ids && !(eqKeep ? eqKeep.has(key) : ids.has(id));
      [g, ln, pl].forEach(x => x && x.classList.toggle('dim', off));
      g.classList.toggle('sel', !!eqKeep && eqKeep.has(key) && eqKeep.size === 1);
    });
    edgeEls.forEach(({ e, lines }) => lines[1].parentNode.classList.toggle('dim', !!ids && !(ids.has(e.a) && ids.has(e.b))));
  }
  function selectNode(id, center = true) {
    const n = nodes.get(id); if (!n) return;
    selected = { kind: 'node', id };
    nodeEls.forEach((g, k) => g.classList.toggle('sel', k === id));
    highlight(new Set([id, ...[...adj.get(id)].filter(nb => shown.has(nb))]));
    const links = [];
    D.edges.forEach(e => {
      if (e.a !== id && e.b !== id) return;
      const other = e.a === id ? e.b : e.a;
      e.links.forEach(l => links.push({ mine: e.a === id ? l.a_port : l.b_port, other, theirs: e.a === id ? l.b_port : l.a_port, l }));
    });
    links.sort((x, y) => (parseInt(x.mine.replace(/\D+/g, '')) || 0) - (parseInt(y.mine.replace(/\D+/g, '')) || 0));
    const url = n.sid ? window.SWITCH_URL.replace(/0$/, n.sid) : null;
    side.innerHTML = `
      <div class="card-head"><h2>${esc(id)}</h2>${url ? `<a class="btn sm" href="${url}">${t('Open switch')}</a>` : ''}</div>
      ${n.external ? `<p class="muted">${n.kind ? esc(t((KINDS[n.kind] || KINDS.other).label)) : t('Not a documented switch - a port has the equipment type Switch with this name (typo, or a switch that is not documented yet).')}</p>` : `
      <dl class="dl tight">
        ${n.designation ? `<dt>${t('Office')}</dt><dd>${esc(n.designation)}</dd>` : ''}
        <dt>${t('IP')}</dt><dd><code>${esc(n.ip || '—')}</code></dd>
        <dt>${t('Model')}</dt><dd>${esc(n.model || '—')}${n.layer ? ` <span class="layer l-${n.layer.toLowerCase()}">${n.layer}</span>` : ''}</dd>
        <dt>${t('Area')}</dt><dd>${esc([n.area, n.location].filter(Boolean).join(' · ') || '—')}</dd>
        <dt>${t('Groups')}</dt><dd>${n.groups.map(g => `<span class="gchip" style="--gc:${GROUP_COLORS[g] || '#64748b'}">${esc(g)}</span>`).join(' ') || '—'}</dd>
      </dl>`}
      ${eqCount(id) ? `<p><button type="button" class="btn sm" id="tTog">${isOpen(id) ? '− ' + t('Hide connected equipment') : '+ ' + t('Show connected equipment')} (${eqCount(id)})</button></p>` : ''}
      <h3>${links.length} ${links.length === 1 ? t('link') : t('links')}</h3>
      <table class="tbl compact mini"><thead><tr><th>${t('Port')}</th><th>${t('To')}</th><th>${t('Port')}</th><th></th></tr></thead>
      ${links.map(x => `<tr class="${x.l.status === 'disabled' ? 'muted' : ''}"><td><b>${esc(x.mine)}</b></td>
        <td><a href="#" class="tgo" data-n="${esc(x.other)}">${esc(x.other)}</a></td><td>${esc(x.theirs)}</td>
        <td><i class="tsw" style="background:${TYPES[x.l.type] || '#94a3b8'}" title="${esc(x.l.type)}"></i>${x.l.check !== 'ok' ? `<span class="tag warn" title="${x.l.check}">!</span>` : ''}</td></tr>`).join('')}
      </table>`;
    if (center && pos[id]) { const r = svg.getBoundingClientRect(); view.x = r.width / 2 - pos[id].x * view.k; view.y = r.height / 2 - pos[id].y * view.k; applyView(); }
    const tb = document.getElementById('tTog'); if (tb) tb.onclick = () => { toggle(id); selectNode(id, false); };
  }
  function selectEq(id, i) {
    const q = (EQ[id] || [])[i]; if (!q) return;
    selected = { kind: 'eq', id, i };
    nodeEls.forEach(g => g.classList.remove('sel'));
    highlight(new Set([id]), new Set([eqKey(id, i)]));
    const k = KINDS[q.kind] || KINDS.other, n = nodes.get(id);
    const url = n && n.sid ? window.SWITCH_URL.replace(/0$/, n.sid) + '#port-' + q.pid : null;
    side.innerHTML = `
      <div class="card-head"><h2>${esc(q.name)}</h2>${url ? `<a class="btn sm" href="${url}">${t('Open port')}</a>` : ''}</div>
      <p class="eq-head"><img src="${ICON_URL + (q.icon || 'other')}.svg" width="48" height="48" alt=""> ${esc(q.atype || t(k.label))}</p>
      <dl class="dl tight">
        <dt>${t('Switch')}</dt><dd><a href="#" class="tgo" data-n="${esc(id)}">${esc(id)}</a></dd>
        <dt>${t('Port')}</dt><dd><b>${esc(q.port)}</b></dd>
        ${q.atype ? `<dt>${t('Equipment type')}</dt><dd>${esc(q.atype)}</dd>` : ''}
        ${q.behind ? `<dt>${t('Device behind')}</dt><dd>↳ <b>${esc(q.behind)}</b> ${q.behind_type ? '(' + esc(q.behind_type) + ')' : ''}</dd>` : ''}
        <dt>${t('Status')}</dt><dd>${q.status === 'disabled' ? t('Disabled') : t('Active')}</dd>
        <dt>${t('Type')}</dt><dd>${esc(q.type)}</dd>
        <dt>${t('VLAN')}</dt><dd>${esc(q.vlan || '—')}</dd>
        ${q.user ? `<dt>${t('User')}</dt><dd>${esc(q.user)}</dd>` : ''}
      </dl>`;
  }
  function selectEdge(e) {
    selected = { kind: 'edge' };
    nodeEls.forEach(g => g.classList.remove('sel'));
    highlight(new Set([e.a, e.b]));
    side.innerHTML = `<div class="card-head"><h2>${esc(e.a)} ↔ ${esc(e.b)}</h2></div>
      <table class="tbl compact mini"><thead><tr><th>${esc(e.a)}</th><th>${esc(e.b)}</th><th>${t('Type')}</th><th>${t('Patch')}</th><th>${t('Check')}</th></tr></thead>
      ${e.links.map(l => `<tr><td><b>${esc(l.a_port)}</b></td><td><b>${esc(l.b_port)}</b></td><td><span class="speed-tag">${esc(l.type)}</span>${l.speed === 'fiber' ? ` <span class="speed-tag">${t('Fiber')}</span>` : ''}${l.status === 'disabled' ? ` <span class="tag off">${t('off')}</span>` : ''}</td>
        <td class="small">${esc(l.patch)}</td><td>${l.check === 'ok' ? '✓' : `<span class="tag warn">${l.check}</span>`}</td></tr>`).join('')}</table>
      <p class="small"><a href="#" class="tgo" data-n="${esc(e.a)}">${esc(e.a)}</a> · <a href="#" class="tgo" data-n="${esc(e.b)}">${esc(e.b)}</a></p>`;
  }
  function clearSelection() {
    selected = null; highlight(null); nodeEls.forEach(g => g.classList.remove('sel'));
    side.innerHTML = `<p class="muted">${t('Click a switch or a connection to see details. Drag to move a switch, drag the background to pan, scroll to zoom.')}</p><div class="legend col" id="tLegend"></div>`;
    drawLegend();
  }
  function centerOn(p, minK = 1) {
    const r = svg.getBoundingClientRect();
    view.k = Math.max(view.k, minK);
    view.x = r.width / 2 - p.x * view.k; view.y = r.height / 2 - p.y * view.k; applyView();
  }
  function goTo(name) {
    if (!nodes.has(name)) return;
    if (!shown.has(name)) { ui.group.value = ''; ui.area.value = ''; ui.alone.checked = true; if (nodes.get(name).external) ui.ext.checked = true; rebuild(false); }
    selectNode(name);
    if (pos[name]) centerOn(pos[name], 0.9);
    const g = nodeEls.get(name); if (g) { g.classList.add('hit'); setTimeout(() => g.classList.remove('hit'), 1500); }
  }
  document.addEventListener('click', ev => { const a = ev.target.closest('.tgo'); if (a) { ev.preventDefault(); goTo(a.dataset.n); svg.scrollIntoView({ behavior: 'smooth', block: 'center' }); } });

  // ---------------------------------------------------------------- pan / zoom / drag
  let drag = null;
  const world = ev => { const r = svg.getBoundingClientRect(); return { x: (ev.clientX - r.left - view.x) / view.k, y: (ev.clientY - r.top - view.y) / view.k }; };
  svg.addEventListener('pointerdown', ev => {
    if (ev.button !== 0) return;
    const tg = ev.target.closest('.tog');
    if (tg) { ev.stopPropagation(); toggle(tg.closest('.n').dataset.id); return; }
    const qg = ev.target.closest('.q');
    if (qg) {
      svg.setPointerCapture(ev.pointerId);
      const id = qg.dataset.sw, i = +qg.dataset.i, w = world(ev), q = eqPos(id, i);
      drag = { kind: 'eq', id, i, dx: w.x - q.x, dy: w.y - q.y, sx: ev.clientX, sy: ev.clientY, moved: false };
      return;
    }
    const ng = ev.target.closest('.n');
    if (ev.target.closest('.e') && !ng) return;
    svg.setPointerCapture(ev.pointerId);
    if (ng) { const id = ng.dataset.id, w = world(ev); drag = { kind: 'node', id, dx: w.x - pos[id].x, dy: w.y - pos[id].y, sx: ev.clientX, sy: ev.clientY, moved: false }; }
    else drag = { kind: 'pan', sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y, moved: false };
  });
  svg.addEventListener('pointermove', ev => {
    if (!drag) return;
    if (Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) > 3) drag.moved = true;
    if (drag.kind === 'node') { const w = world(ev); pos[drag.id] = { x: w.x - drag.dx, y: w.y - drag.dy }; place(); }
    else if (drag.kind === 'eq') { const w = world(ev), p = pos[drag.id]; eqOff[eqKey(drag.id, drag.i)] = { dx: w.x - drag.dx - p.x, dy: w.y - drag.dy - p.y }; place(); }
    else { view.x = drag.vx + ev.clientX - drag.sx; view.y = drag.vy + ev.clientY - drag.sy; applyView(); }
  });
  svg.addEventListener('pointerup', () => {
    if (drag && !drag.moved) { if (drag.kind === 'node') selectNode(drag.id, false); else if (drag.kind === 'eq') selectEq(drag.id, drag.i); else clearSelection(); }
    drag = null;
  });
  svg.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = svg.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const k = Math.max(0.1, Math.min(3, view.k * Math.pow(1.0015, -ev.deltaY)));
    view.x = mx - (mx - view.x) * k / view.k; view.y = my - (my - view.y) * k / view.k; view.k = k; applyView();
  }, { passive: false });

  // ---------------------------------------------------------------- toolbar
  function rebuild(refit = true) { computeShown(); layout(); draw(); if (refit) fit(); savePref(); }
  ['layout', 'group', 'area', 'ext', 'alone'].forEach(k => ui[k].addEventListener('change', () => { selected = null; rebuild(); }));
  ui.color.addEventListener('change', () => { draw(); savePref(); });
  ui.labels.addEventListener('change', () => { draw(); savePref(); });
  ui.find.addEventListener('change', () => goTo(ui.find.value.trim()));
  function findEquipment(q) {
    for (const [sw, list] of Object.entries(EQ)) {
      const i = list.findIndex(e => e.name.toLowerCase() === q);
      if (i >= 0) return [sw, i];
    }
    for (const [sw, list] of Object.entries(EQ)) {
      const i = list.findIndex(e => e.name.toLowerCase().includes(q));
      if (i >= 0) return [sw, i];
    }
    return null;
  }
  ui.find.addEventListener('keydown', ev => {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    const q = ui.find.value.trim().toLowerCase(); if (!q) return;
    const hit = [...nodes.keys()].find(n => n.toLowerCase() === q) || [...nodes.keys()].find(n => n.toLowerCase().includes(q));
    if (hit) { goTo(hit); return; }
    const eq = findEquipment(q);
    if (eq) {
      const [sw, i] = eq;
      goTo(sw); if (!isOpen(sw)) toggle(sw, true);
      selectEq(sw, i);
      const p = eqPos(sw, i); if (p) centerOn(p, 1.1);
      const e = eqEls.find(x => x.id === sw && x.i === i); if (e) { e.g.classList.add('hit'); setTimeout(() => e.g.classList.remove('hit'), 1800); }
    }
  });
  const expAll = $('tExpandAll'), colAll = $('tCollapseAll');
  if (expAll) expAll.addEventListener('click', () => { shown.forEach(id => { if (eqCount(id)) expanded.add(id); }); layout(); draw(); fit(); savePref(); });
  if (colAll) colAll.addEventListener('click', () => { expanded.clear(); layout(); draw(); fit(); savePref(); });
  $('tFit').addEventListener('click', () => fit());
  const saveBtn = $('tSave');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const positions = {}; shown.forEach(id => { if (pos[id]) positions[id] = [Math.round(pos[id].x), Math.round(pos[id].y)]; });
    try {
      await api('/api/topology/layout', { positions });
      Object.assign(SAVED, positions);
      if (![...ui.layout.options].some(o => o.value === 'saved')) ui.layout.add(new Option(t('Saved layout'), 'saved'));
      ui.layout.value = 'saved'; savePref();
      saveBtn.textContent = t('Saved ✓'); setTimeout(() => saveBtn.textContent = t('Save layout'), 1500);
    } catch (e) { saveBtn.textContent = t('Save failed'); }
  });
  $('tSvg').addEventListener('click', () => {
    const b = bbox(), clone = svg.cloneNode(true);
    clone.querySelector('#vp').removeAttribute('transform');
    clone.setAttribute('viewBox', `${b.x} ${b.y} ${b.w} ${b.h}`);
    clone.setAttribute('width', Math.round(b.w)); clone.setAttribute('height', Math.round(b.h));
    const bg = document.createElementNS(NS, 'rect');
    Object.entries({ x: b.x, y: b.y, width: b.w, height: b.h, fill: '#ffffff' }).forEach(([k, v]) => bg.setAttribute(k, v));
    clone.insertBefore(bg, clone.querySelector('#vp'));
    clone.querySelectorAll('.dim').forEach(e => e.classList.remove('dim'));
    const blob = new Blob(['<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'switch-topology.svg'; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  rebuild();
  if (window.TOPO_FOCUS) goTo(window.TOPO_FOCUS);
  window.addEventListener('resize', () => fit());
})();
