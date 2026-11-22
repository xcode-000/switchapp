// SNMP live view: asks the server to read one table at a time, renders it, keeps nothing.
(() => {
  const T = window.SNMP_T, $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = (s, kw) => s.replace(/%\((\w+)\)s/g, (_, k) => kw[k] ?? '');
  const sel = $('sSwitch'), host = $('sHost');
  let data = {}, tab = 'interfaces', sid = null, body = null;
  const human = n => { if (n == null) return ''; const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0; n = Number(n); while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + ' ' + u[i]; };
  const spd = m => !m ? '' : m >= 1000 ? (m / 1000) + ' G' : m + ' M';
  const csel = $('sCommSel');
  const setHost = () => { const o = sel.selectedOptions[0]; host.value = o ? (o.dataset.ip || '') : '';
    if (csel.value !== 'other') csel.value = (o && o.dataset.comm) || ''; };
  csel.addEventListener('change', () => { $('sCommOther').hidden = csel.value !== 'other'; if (csel.value === 'other') $('sComm').focus(); });
  sel.addEventListener('change', setHost);
  if (sel.value) setHost();

  async function load(section) {
    const r = await fetch(`/api/snmp/${sid}/${section}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF': window.CSRF }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({ error: r.status + ' ' + r.statusText }));
    if (!r.ok || j.error) throw new Error(j.error || r.statusText);
    return j;
  }
  $('snmpForm').addEventListener('submit', async e => {
    e.preventDefault();
    if (!sel.value) { $('sState').textContent = T.no_ip; return; }
    sid = sel.value; data = {};
    body = { host: host.value.trim(), community_id: csel.value, community: csel.value === 'other' ? $('sComm').value : '', version: $('sVer').value };
    $('sGo').disabled = true; $('sOut').hidden = true; $('sState').className = 'muted small';
    let total = 0, where = '';
    try {
      $('sState').textContent = fmt(T.reading, { what: 'system' });
      const sys = await load('system'); total += sys.requests; where = sys.host;
      drawSystem(sys.data);
      $('sOut').hidden = false;
      for (const s of ['interfaces', 'vlans', 'neighbors']) {
        $('sState').textContent = fmt(T.reading, { what: T[s] });
        try { const r = await load(s); data[s] = r.data; total += r.requests; } catch (err) { data[s] = { error: err.message }; }
        count(); if (s === tab || s === 'interfaces') draw();
      }
      $('sState').textContent = fmt(T.done, { n: total, host: where });
    } catch (err) {
      $('sState').textContent = '⚠ ' + err.message; $('sState').className = 'err small';
    } finally { $('sGo').disabled = false; }
  });

  function count() {
    [['interfaces', 'cntInterfaces'], ['vlans', 'cntVlans'], ['neighbors', 'cntNeighbors'], ['macs', 'cntMacs']].forEach(([k, id]) => {
      $(id).textContent = Array.isArray(data[k]) ? data[k].length : '';
    });
  }
  function drawSystem(s) {
    $('sSystem').innerHTML = `<div class="card-head"><h2>${esc(s.name || '?')}</h2><span class="tag approved">${esc(T.uptime)} ${esc(s.uptime)}</span></div>
      <dl class="dl tight snmp-dl">
        <dt>${T.descr}</dt><dd>${esc(s.descr)}</dd><dt>${T.location}</dt><dd>${esc(s.location) || '—'}</dd>
        <dt>${T.contact}</dt><dd>${esc(s.contact) || '—'}</dd><dt>${T.object_id}</dt><dd><code>${esc(s.object_id)}</code></dd></dl>`;
  }
  document.querySelectorAll('#sTabs a').forEach(a => a.addEventListener('click', async e => {
    e.preventDefault(); tab = a.dataset.t;
    document.querySelectorAll('#sTabs a').forEach(x => x.classList.toggle('on', x === a));
    if (tab === 'macs' && !data.macs) {
      $('sTable').innerHTML = `<p class="pad muted">${esc(fmt(T.reading, { what: T.macs }))}</p>`;
      try { const r = await load('macs'); data.macs = r.data; } catch (err) { data.macs = { error: err.message }; }
      count();
    }
    draw();
  }));
  $('sFilter').addEventListener('input', draw);
  $('sWarn').addEventListener('change', draw);

  function table(head, rows) {
    return `<table class="tbl compact snmp-tbl"><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
  }
  function draw() {
    const d = data[tab], q = $('sFilter').value.toLowerCase();
    $('sOnlyWarn').hidden = tab !== 'interfaces';
    if (!d) { $('sTable').innerHTML = ''; return; }
    if (d.error) { $('sTable').innerHTML = `<p class="pad err">⚠ ${esc(d.error)}</p>`; return; }
    if (!d.length) { $('sTable').innerHTML = `<p class="pad muted">${esc(T.none)}</p>`; return; }
    const match = o => !q || JSON.stringify(o).toLowerCase().includes(q);
    let html = '';
    if (tab === 'interfaces') {
      const only = $('sWarn').checked;
      const rows = d.filter(match).filter(i => !only || (i.doc && i.doc.warn.length)).map(i => {
        const doc = i.doc, warn = doc ? doc.warn : [];
        const docTxt = doc ? `<a href="${window.SNMP_SWITCH_URL.replace(/0$/, sid)}#port-${doc.id}"><b>${esc(doc.name)}</b></a>` +
          (doc.status === 'disabled' ? ` <span class="tag off">${esc(T.disabled)}</span>` : '') +
          ` <span class="muted">${esc([doc.vlan && 'VLAN ' + doc.vlan, doc.equipment, doc.equipment2 && '↳ ' + doc.equipment2, doc.remote_port && ('↔ ' + doc.remote_port)].filter(Boolean).join(' · '))}</span>` : '<span class="muted">—</span>';
        return `<tr class="${i.oper === 'up' ? 'up' : 'down'}${warn.length ? ' warnrow' : ''}">
          <td><span class="dot ${i.oper === 'up' ? 'active' : 'disabled'}"></span><b>${esc(i.name)}</b><div class="small muted">${esc(i.descr !== i.name ? i.descr : '')}</div></td>
          <td>${esc(i.alias)}</td><td>${esc(T[i.admin] || i.admin)}</td><td>${esc(T[i.oper] || i.oper)}</td>
          <td>${i.speed_mbps >= 1000 ? `<b>${spd(i.speed_mbps)}</b>` : spd(i.speed_mbps)}</td>
          <td>${esc(i.vlan ?? '')}${i.trunk ? ` <span class="tag">${esc(T.trunk)}</span>` : ''}</td>
          <td class="small nowrap">${human(i.in_octets)} / ${human(i.out_octets)}</td>
          <td class="small">${esc(i.in_errors ?? '')} / ${esc(i.out_errors ?? '')}</td>
          <td class="small">${docTxt}</td>
          <td class="small">${warn.length ? warn.map(w => `<span class="tag warn">${esc(w)}</span>`).join(' ') : (doc ? '<span class="tag approved">✓</span>' : '')}</td></tr>`;
      });
      html = table([T.iface, T.alias, T.admin, T.oper, T.speed, T.vlan, T.traffic, T.errors, T.documented, T.check], rows);
    } else if (tab === 'vlans') {
      html = table([T.vid, T.vname, T.vdoc, T.ports, T.untagged], d.filter(match).map(v =>
        `<tr><td><b>${esc(v.id)}</b></td><td>${esc(v.name)}</td><td>${v.documented != null ? esc(v.documented) : '<span class="tag warn">—</span>'}</td><td>${esc(v.ports ?? '')}</td><td>${esc(v.untagged ?? '')}</td></tr>`));
    } else if (tab === 'neighbors') {
      html = table([T.proto, T.local, T.device, T.rport, T.platform], d.filter(match).map(n =>
        `<tr><td><span class="tag">${esc(n.proto)}</span></td><td><b>${esc(n.local)}</b></td><td>${esc(n.device)}${n.address ? ` <code class="small">${esc(n.address)}</code>` : ''}</td><td>${esc(n.port)}</td><td class="small">${esc(n.platform)}</td></tr>`));
    } else {
      const rows = d.filter(match);
      html = table([T.mac, T.vid, T.local], rows.slice(0, 3000).map(m => `<tr><td><code>${esc(m.mac)}</code></td><td>${esc(m.vlan)}</td><td><b>${esc(m.port)}</b></td></tr>`));
    }
    $('sTable').innerHTML = html;
  }
})();
