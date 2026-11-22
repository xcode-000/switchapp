// Port table: read-only by default; "Edit ports" -> change anything -> "Save" sends all changes at once
// (applied directly, or as change requests for users who need approval).
// Equipment type -> Connected equipment (assets of that type) -> optional device behind it (e.g. PC behind a phone).
// Equipment type "Switch" = switch-to-switch link: Remote port becomes a drop-down of that switch's ports.
(() => {
  const table = document.getElementById('ports');
  if (!table) return;
  const state = document.getElementById('saveState');
  const rows = [...table.tBodies[0].rows];
  const canEdit = table.dataset.editable === '1';
  const editBtn = document.getElementById('editBtn'), saveBtn = document.getElementById('saveBtn'), cancelBtn = document.getElementById('cancelBtn');
  const SW = window.SWITCH_TYPE || 'Switch';
  const LP = window.LINK_PORTS || {};                       // {switch: [[port, info, its remote switch, its remote port], ...]}
  const ASSETS = window.ASSETS || {};                       // {type: [[name, ip], ...]}
  const TYPES = window.ASSET_TYPES || [];
  const TYPE_COLOR = Object.fromEntries(TYPES.map(x => [x.name, x.color]));
  let editing = false;

  const fields = () => table.querySelectorAll('[data-f]');
  const F = (tr, f) => tr.querySelector(`[data-f="${f}"]`);
  const setState = (txt, cls) => { state.textContent = txt; state.className = 'save-state ' + (cls || ''); };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const opt = (v, txt, extra = {}) => { const o = document.createElement('option'); o.value = v; o.textContent = txt; Object.assign(o.dataset, extra); return o; };
  const fire = el => el.dispatchEvent(new Event('change', { bubbles: true }));
  function setVal(el, v) {   // select: make sure the value exists as an option
    v = v ?? '';
    if (el.tagName === 'SELECT' && v !== '' && ![...el.options].some(o => o.value === v)) el.appendChild(opt(v, v));
    el.value = v;
  }
  fields().forEach(el => el.dataset.orig = el.value);

  // ---------------------------------------------------------------- asset drop-downs (option lists cached per type)
  const cache = {};
  const known = name => {   // type of an existing asset / switch name (names are unique), or null
    const n = name.toLowerCase();
    for (const [ty, list] of Object.entries(ASSETS)) if (list.some(a => a[0].toLowerCase() === n)) return ty;
    if (Object.keys(LP).some(s => s.toLowerCase() === n) || n === (window.CUR_SWITCH || '').toLowerCase()) return SW;
    return null;
  };
  function optionsHtml(type) {
    if (!type) return `<option value="">${esc(t('— choose the equipment type first —'))}</option>`;
    if (cache[type]) return cache[type];
    let h = `<option value="">${esc(t('— none —'))}</option>`;
    (ASSETS[type] || []).forEach(([n, ip]) => {
      let label = n;
      if (type === SW) { const used = (LP[n] || []).length; label = n; if (!used) label += ' (' + t('no ports') + ')'; }
      else if (ip) label += '  · ' + ip;
      h += `<option value="${esc(n)}">${esc(label)}</option>`;
    });
    if (type !== SW && window.CAN_NEW_ASSET) h += `<option value="__new">${esc(t('＋ New %(type)s…', { type }))}</option>`;
    return (cache[type] = h);
  }
  function fillEq(sel, type) {
    const cur = sel.value;
    sel.innerHTML = optionsHtml(type);
    if (cur && ![...sel.options].some(o => o.value === cur))
      sel.insertBefore(opt(cur, cur + ' (' + t(type === SW ? 'not a documented switch' : 'not found') + ')'), sel.options[1] || null);
    sel.value = cur;
    markUsed(sel, type);
  }
  // grey out the assets already connected elsewhere (types allowing one port only) and the defective ones
  const pageIds = new Set(rows.map(tr => String(tr.dataset.id)));
  function markUsed(sel, type) {
    if (!type || type === SW || !ASSETS[type]) return;
    const multi = (TYPES.find(x => x.name === type) || {}).multi;
    const me = sel.closest('tr'), cur = sel.value.toLowerCase();
    const onPage = {};
    rows.forEach(tr => { if (tr === me) return; ['equipment', 'equipment2'].forEach(f => { const v = F(tr, f).value; if (v) onPage[v.toLowerCase()] = `${window.CUR_SWITCH} ${F(tr, 'name').value}`; }); });
    const info = {}; ASSETS[type].forEach(a => info[a[0].toLowerCase()] = a);
    [...sel.options].forEach(o => {
      if (!o.value || o.value === '__new') return;
      const k = o.value.toLowerCase(), a = info[k];
      o.textContent = o.textContent.replace(/  — .*$/, '');
      o.disabled = false; o.classList.remove('used');
      if (!a || k === cur) return;
      const where = onPage[k] || ((a[2] || []).find(u => !pageIds.has(String(u[0]))) || [])[1];
      if (where && !multi) { o.disabled = true; o.classList.add('used'); o.textContent += '  — ' + t('in use: %(where)s', { where }); }
      else if (!where && a[3] === 'defective') { o.disabled = true; o.classList.add('used'); o.textContent += '  — ' + t('Defective'); }
    });
  }
  function newAsset(sel, type) {
    const name = (prompt(t('Name of the new %(type)s (asset names are unique):', { type }), '') || '').trim();
    if (!name) { sel.value = sel.dataset.prev || ''; return; }
    const other = known(name);
    if (other && other !== type) { alert(t('%(name)s already exists as %(t)s - asset names are unique.', { name, t: other })); sel.value = sel.dataset.prev || ''; return; }
    if (other === type) {
      const a = ASSETS[type].find(x => x[0].toLowerCase() === name.toLowerCase());
      fillEq(sel, type);
      if ([...sel.options].find(o => o.value === a[0])?.disabled) { alert(t('%(name)s is already in use or defective - choose another asset.', { name })); sel.value = sel.dataset.prev || ''; return; }
    }
    if (!other) { (ASSETS[type] ||= []).push([name, '', [], 'stock']); ASSETS[type].sort((a, b) => a[0].localeCompare(b[0])); delete cache[type]; }
    fillEq(sel, type); setVal(sel, (ASSETS[type].find(a => a[0].toLowerCase() === name.toLowerCase()) || [name])[0]);
    setState(t('%(name)s will be added to the assets when you save', { name }), 'busy');
  }

  // ---------------------------------------------------------------- remote port picker (equipment type Switch)
  function fillPortSelect(tr) {
    const rpIn = F(tr, 'remote_port');
    let rpSel = tr.querySelector('.rp-sel');
    const sw = F(tr, 'eq_type').value === SW ? F(tr, 'equipment').value : '';
    const me = F(tr, 'name').value;
    if (!editing || !LP[sw]) { if (rpSel) rpSel.hidden = true; rpIn.hidden = false; return; }
    if (!rpSel) {
      rpSel = document.createElement('select'); rpSel.className = 'rp-sel'; rpIn.after(rpSel);
      rpSel.addEventListener('focus', () => fillPortSelect(tr));
      rpSel.addEventListener('change', () => {
        rpIn.value = rpSel.value; refresh(tr);
        const busy = rpSel.selectedOptions[0]?.dataset.busy;
        setState(busy ? t('%(sw)s port %(p)s is currently linked to %(x)s - saving will re-link it to this switch',
                          { sw: F(tr, 'equipment').value, p: rpSel.value, x: busy })
                      : t('Editing - click Save when you are done'), 'busy');
      });
    }
    rpSel.hidden = false; rpIn.hidden = true;
    rpSel.innerHTML = '';
    rpSel.appendChild(opt('', t('— port —')));
    let found = false;
    // ports of that switch already chosen by other rows of this page
    const taken = new Set(rows.filter(r => r !== tr && F(r, 'eq_type').value === SW && F(r, 'equipment').value === sw)
                              .map(r => F(r, 'remote_port').value).filter(Boolean));
    LP[sw].forEach(([n, info, rs, rp, occupied]) => {
      const mine = rs === window.CUR_SWITCH && rp === me;
      const busy = (rs && !mine) || occupied || taken.has(n);
      const o = opt(n, n + (mine ? '  ✓' : taken.has(n) ? '  — ' + t('chosen on another port') : info ? '  — ' + info : '  — ' + t('free')));
      if (busy && n !== rpIn.value) { o.className = 'busy'; o.disabled = true; }
      rpSel.appendChild(o);
      if (n === rpIn.value) found = true;
    });
    if (rpIn.value && !found) rpSel.appendChild(opt(rpIn.value, rpIn.value + ' (' + t('not found') + ')'));
    rpSel.value = rpIn.value;
  }

  // ---------------------------------------------------------------- display helpers
  function subVisible(tr) {
    const sub = tr.querySelector('.eq-sub'), add = tr.querySelector('.add-sub');
    const ty = F(tr, 'eq_type').value, has = !!(F(tr, 'equipment2').value || F(tr, 'eq2_type').value);
    const allowed = ty && ty !== SW;
    sub.hidden = !(has || (editing && sub.dataset.open === '1' && allowed));
    add.hidden = !(editing && allowed && sub.hidden);
    tr.classList.toggle('has-sub', !sub.hidden);
  }
  function paintRow(tr) {
    const ty = F(tr, 'eq_type').value, eq = F(tr, 'equipment').value;
    const isSw = ty === SW && eq;
    const knownSw = isSw && (window.SWITCH_IDS[eq] || LP[eq]);
    tr.classList.toggle('s2s', !!knownSw);
    tr.classList.toggle('ext', !!(isSw && !knownSw));
    const st = F(tr, 'status').value, pt = F(tr, 'ptype').value;
    tr.dataset.status = st; tr.dataset.type = pt; tr.dataset.speed = F(tr, 'speed').value;
    tr.querySelector('.dot').className = 'dot ' + st;
    const sw = tr.querySelector('td:not(.eqt) .tsw'); if (sw) sw.style.background = window.TYPE_COLORS[pt] || '#64748b';
    const et = tr.querySelector('.eqt img.ticon');
    if (et) { const ic = (TYPES.find(x => x.name === ty) || {}).icon; et.hidden = !ty; if (ty) et.src = window.ICON_URL + (ic || 'other') + '.svg'; }
    const go = tr.querySelector('a.go');
    if (go && !editing) {
      const sid = ty === SW && window.SWITCH_IDS[eq];
      if (sid) { go.href = window.SWITCH_URL.replace(/0$/, sid); go.hidden = false; }
      else if (eq && window.ASSETS_URL) { go.href = window.ASSETS_URL + '?q=' + encodeURIComponent(eq); go.hidden = false; }
      else go.hidden = true;
    }
    if (editing) (window.CLEARED_WHEN_DISABLED || []).forEach(f => { const el = F(tr, f); if (el) el.disabled = st === 'disabled'; });
    const tile = document.getElementById('tile-' + tr.dataset.id);
    if (tile) { tile.dataset.status = st; tile.dataset.type = pt; tile.style.setProperty('--pc', window.TYPE_COLORS[pt] || '#64748b'); }
    subVisible(tr);
  }
  function clearDisabled(tr) {   // a disabled port has nothing documented on it
    (window.CLEARED_WHEN_DISABLED || []).forEach(f => { const el = F(tr, f); if (el) setVal(el, ''); });
    tr.querySelector('.eq-sub').dataset.open = '';
    if (editing) { fillEq(F(tr, 'equipment'), ''); fillEq(F(tr, 'equipment2'), ''); }
    fillPortSelect(tr);
  }

  function applyPort(p) {
    const tr = document.getElementById('port-' + p.id);
    const tile = document.getElementById('tile-' + p.id);
    const pend = p.pending || {};
    tr.querySelectorAll('[data-f]').forEach(el => {
      const f = el.dataset.f, td = el.closest('td');
      setVal(el, p[f]); el.dataset.orig = el.value; td.classList.remove('dirty');
      const box = f === 'equipment2' || f === 'eq2_type' ? tr.querySelector('.eq-sub') : td;
      if (pend[f]) {
        const v = f === 'status' ? (window.STATUS_LABELS[pend[f].value] || pend[f].value) : (pend[f].value || t('(empty)'));
        box.classList.add('pend'); box.dataset.proposed = `→ ${v} · ${pend[f].by}`;
      } else if (box === td || !pend.equipment2) { box.classList.remove('pend'); delete box.dataset.proposed; }
    });
    paintRow(tr);
    if (tile) {
      tile.classList.toggle('has-pending', Object.keys(pend).length > 0);
      tile.title = `${t('Port')} ${p.name} · ${p.status === 'active' ? t('Active') : t('Disabled')} · ${p.ptype}` +
        (p.vlan ? ` · VLAN ${p.vlan}` : '') + (p.equipment ? ` · ${p.eq_type ? p.eq_type + ' ' : ''}${p.equipment}` : '') +
        (p.equipment2 ? ` ↳ ${p.equipment2}` : '') + (Object.keys(pend).length ? ' · ' + t('change pending approval') : '');
    }
  }

  // ---------------------------------------------------------------- edit mode
  function collectChanges() {
    const out = {};
    fields().forEach(el => {
      if (el.value !== el.dataset.orig) (out[el.closest('tr').dataset.id] ||= {})[el.dataset.f] = el.value;
    });
    return out;
  }
  function countChanges() { return Object.values(collectChanges()).reduce((n, o) => n + Object.keys(o).length, 0); }
  function refreshDirty() {
    fields().forEach(el => el.closest('td').classList.toggle('dirty', el.value !== el.dataset.orig));
    const n = countChanges();
    if (saveBtn) { saveBtn.querySelector('span').textContent = window.T_SAVE_N.replace('%(n)s', n); saveBtn.disabled = n === 0; }
  }
  function refresh(tr) { paintRow(tr); refreshDirty(); }
  function prepareRow(tr) {
    fillEq(F(tr, 'equipment'), F(tr, 'eq_type').value);
    fillEq(F(tr, 'equipment2'), F(tr, 'eq2_type').value);
    fillPortSelect(tr);
  }
  function setEditing(on) {
    editing = on;
    table.classList.toggle('editing', on); table.classList.toggle('readonly', !on);
    fields().forEach(el => el.disabled = !on);
    if (on) rows.forEach(prepareRow);
    else {
      table.querySelectorAll('.rp-sel').forEach(el => { el.hidden = true; el.previousElementSibling.hidden = false; });
      // read-only: an empty equipment shows nothing (not the "choose the type first" hint)
      table.querySelectorAll('select[data-f=equipment], select[data-f=equipment2]').forEach(el => {
        if (!el.value) el.innerHTML = '<option value=""></option>';
      });
    }
    if (editBtn) { editBtn.hidden = on; saveBtn.hidden = !on; cancelBtn.hidden = !on; }
    if (!on) { rows.forEach(tr => { const c = tr.querySelector('.sel'); if (c) c.checked = false; tr.querySelector('.eq-sub').dataset.open = ''; }); updBulk(); }
    rows.forEach(paintRow);
    refreshDirty();
  }

  if (canEdit && editBtn) {
    editBtn.onclick = () => { setEditing(true); setState(t('Editing - click Save when you are done'), 'busy'); };
    cancelBtn.onclick = () => {
      if (countChanges() && !confirm(t('Discard your changes?'))) return;
      fields().forEach(el => setVal(el, el.dataset.orig));
      setEditing(false); setState('');
    };
    saveBtn.onclick = async () => {
      const changes = collectChanges();
      if (!Object.keys(changes).length) { setEditing(false); setState(''); return; }
      saveBtn.disabled = true; setState(t('Saving…'), 'busy');
      try {
        const res = await api('/api/ports/save', { changes, ticket: table.dataset.ticket || null });
        const held = res.mode === 'pending' || res.mode === 'ticket';
        setEditing(false);
        res.ports.forEach(applyPort);
        res.ports.forEach(p => {   // keep the picker data of the other switches up to date
          Object.values(LP).forEach(list => list.forEach(e => { if (e[2] === window.CUR_SWITCH && e[3] === p.name && p.remote_switch === '') { e[1] = ''; e[2] = ''; e[3] = ''; } }));
          const far = LP[p.remote_switch]?.find(e => e[0] === p.remote_port);
          if (far && !held) { far[1] = `↔ ${window.CUR_SWITCH} ${p.name}`; far[2] = window.CUR_SWITCH; far[3] = p.name; }
        });
        let msg = res.mode === 'ticket' ? t('%(n)s port changes held in the maintenance ticket until it is validated', { n: res.ports.length })
          : res.mode === 'pending' ? t('%(n)s change requests sent for approval', { n: res.ports.length })
          : t('Updated %(n)s ports', { n: res.ports.length });
        if (res.linked && res.linked.length) msg += ' · ' + t('also updated: %(list)s', { list: res.linked.join(', ') });
        setState(msg, held ? 'busy' : 'ok');
        res.ports.forEach(p => { const tr = document.getElementById('port-' + p.id); tr.classList.add(held ? 'flash-pend' : 'flash-ok'); setTimeout(() => tr.classList.remove('flash-ok', 'flash-pend'), 900); });
      } catch (e) { saveBtn.disabled = false; setState(t('Save failed:') + ' ' + e.message, 'err'); }
    };

    table.addEventListener('focusin', e => {
      if (!e.target.matches('select[data-f]')) return;
      e.target.dataset.prev = e.target.value;
      const f = e.target.dataset.f;
      if (f === 'equipment' || f === 'equipment2') markUsed(e.target, F(e.target.closest('tr'), f === 'equipment' ? 'eq_type' : 'eq2_type').value);
    });
    table.addEventListener('change', e => {
      const el = e.target;
      if (!el.matches('[data-f]')) return;
      const tr = el.closest('tr'), f = el.dataset.f;
      if (f === 'status' && el.value === 'disabled') {
        clearDisabled(tr);
        setState(t('Port disabled: patch panel, equipment, user and link were emptied'), 'busy');
      }
      if (f === 'eq_type') {
        const was = el.dataset.prev;
        const eq = F(tr, 'equipment'); eq.value = ''; fillEq(eq, el.value);
        if (was === SW || el.value === SW) F(tr, 'remote_port').value = '';
        if (el.value === SW || !el.value) { setVal(F(tr, 'eq2_type'), ''); setVal(F(tr, 'equipment2'), ''); tr.querySelector('.eq-sub').dataset.open = ''; }
        el.dataset.prev = el.value;
        fillPortSelect(tr);
        if (el.value) eq.focus();
      }
      if (f === 'eq2_type') { const e2 = F(tr, 'equipment2'); e2.value = ''; fillEq(e2, el.value); if (el.value) e2.focus(); }
      if ((f === 'equipment' || f === 'equipment2') && el.value === '__new') {
        newAsset(el, F(tr, f === 'equipment' ? 'eq_type' : 'eq2_type').value);
      }
      if (f === 'equipment') {
        if (F(tr, 'eq_type').value === SW && el.dataset.prev !== el.value) F(tr, 'remote_port').value = '';
        el.dataset.prev = el.value;
        fillPortSelect(tr);
        if (F(tr, 'eq_type').value === SW && LP[el.value]) tr.querySelector('.rp-sel')?.focus();
      }
      refresh(tr);
    });
    table.addEventListener('input', e => { if (e.target.matches('input[data-f]')) refresh(e.target.closest('tr')); });
    table.addEventListener('click', e => {
      const tr = e.target.closest('tr'); if (!tr) return;
      if (e.target.closest('.add-sub')) {
        const sub = tr.querySelector('.eq-sub'); sub.dataset.open = '1';
        const ty = F(tr, 'eq2_type');
        if (!ty.value) { const pc = TYPES.find(x => x.kind === 'pc'); if (pc) { ty.value = pc.name; fillEq(F(tr, 'equipment2'), pc.name); } }
        refresh(tr); F(tr, 'equipment2').focus();
      }
      if (e.target.closest('.sub-del') && editing) {
        setVal(F(tr, 'eq2_type'), ''); setVal(F(tr, 'equipment2'), ''); fillEq(F(tr, 'equipment2'), '');
        tr.querySelector('.eq-sub').dataset.open = ''; refresh(tr);
      }
    });
    table.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.matches('input[data-f]')) {
        e.preventDefault();
        const td = e.target.closest('td'), tr = td.parentElement, next = tr.nextElementSibling;
        if (next) next.cells[td.cellIndex].querySelector('input,select')?.focus();
      }
      if (e.key === 's' && (e.ctrlKey || e.metaKey) && editing) { e.preventDefault(); saveBtn.click(); }
      if (e.key === 'Escape' && editing) cancelBtn.click();
    });
    window.addEventListener('beforeunload', e => { if (editing && countChanges()) { e.preventDefault(); e.returnValue = window.T_LEAVE; } });
    // links inside the page (e.g. ↗ remote switch) also warn
    document.addEventListener('click', e => {
      const a = e.target.closest('a[href]');
      if (a && editing && countChanges() && !a.target && !a.classList.contains('exp-link') && !confirm(window.T_LEAVE + ' ' + t('Discard your changes?'))) e.preventDefault();
    }, true);
  }
  rows.forEach(paintRow);

  // ---------------------------------------------------------------- front panel
  document.querySelectorAll('.portgrid').forEach(grid => grid.addEventListener('click', e => {
    const b = e.target.closest('.port'); if (!b) return;
    const tr = document.getElementById('port-' + b.dataset.id);
    tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
    tr.classList.add('focus'); setTimeout(() => tr.classList.remove('focus'), 1600);
    if (editing) F(tr, 'eq_type').focus({ preventScroll: true });
  }));
  if (location.hash.startsWith('#port-')) {
    const tr = document.querySelector(location.hash);
    if (tr) { setTimeout(() => tr.scrollIntoView({ block: 'center' }), 50); tr.classList.add('focus'); setTimeout(() => tr.classList.remove('focus'), 2500); }
  }

  // ---------------------------------------------------------------- filters (+ export only what is shown)
  const fText = document.getElementById('portFilter'), fStatus = document.getElementById('statusFilter'), fType = document.getElementById('typeFilter');
  const expLinks = [...document.querySelectorAll('a.exp-link')];
  expLinks.forEach(a => a.dataset.base = a.getAttribute('href'));
  function filter() {
    const q = fText.value.toLowerCase();
    rows.forEach(tr => {
      const txt = [...tr.querySelectorAll('[data-f]')].map(i => i.value).join(' ').toLowerCase();
      tr.hidden = (q && !txt.includes(q)) || (fStatus.value && tr.dataset.status !== fStatus.value) || (fType.value && tr.dataset.type !== fType.value);
    });
    const filtered = rows.some(tr => tr.hidden);
    const ids = rows.filter(tr => !tr.hidden).map(tr => tr.dataset.id).join(',');
    expLinks.forEach(a => {
      const base = a.dataset.base;
      a.href = filtered ? base + (base.includes('?') ? '&' : '?') + 'ports=' + ids : base;
    });
    const note = document.getElementById('expNote'); if (note) note.hidden = !filtered;
  }
  [fText, fStatus, fType].forEach(el => el.addEventListener('input', filter));

  // ---------------------------------------------------------------- selection + bulk edit (edit mode only, saved with Save)
  const bulk = document.getElementById('bulk');
  const count = document.getElementById('bulkCount'), selAll = document.getElementById('selAll');
  const selected = () => rows.filter(tr => tr.querySelector('.sel')?.checked);
  function updBulk() {
    if (!bulk) return;
    const n = selected().length; bulk.hidden = n === 0 || !editing; count.textContent = n;
    rows.forEach(tr => tr.classList.toggle('picked', !!tr.querySelector('.sel')?.checked));
  }
  if (!bulk) return;
  table.addEventListener('change', e => { if (e.target.classList.contains('sel')) updBulk(); });
  selAll.addEventListener('change', () => { rows.forEach(tr => { if (!tr.hidden) tr.querySelector('.sel').checked = selAll.checked; }); updBulk(); });
  document.getElementById('bulkClear').onclick = () => { rows.forEach(tr => tr.querySelector('.sel').checked = false); selAll.checked = false; updBulk(); };
  document.getElementById('bulkApply').onclick = () => {
    const st = document.getElementById('bulkStatus').value, ty = document.getElementById('bulkType').value, vl = document.getElementById('bulkVlan').value;
    const sp = document.getElementById('bulkSpeed').value;
    if (!st && !ty && !vl && !sp) { setState(t('Pick a status, type or VLAN to apply'), 'err'); return; }
    selected().forEach(tr => {
      if (st) { F(tr, 'status').value = st; if (st === 'disabled') clearDisabled(tr); }
      if (ty) F(tr, 'ptype').value = ty;
      if (vl) setVal(F(tr, 'vlan'), vl);
      if (sp) F(tr, 'speed').value = sp;
      paintRow(tr);
    });
    refreshDirty(); filter();
    setState(t('Applied to the selected ports - click Save to keep the changes'), 'busy');
  };
})();
