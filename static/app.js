// Shared helpers
function t(s, vars) {
  let out = (window.I18N && window.I18N[s]) || s;
  if (vars) for (const k in vars) out = out.split(`%(${k})s`).join(vars[k]);
  return out;
}

async function api(url, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF': window.CSRF },
    body: JSON.stringify(body)
  };
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch (_e) { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

// Confirmations: <form data-confirm="..."> / <a data-confirm="..."> / <button data-confirm="...">
document.addEventListener('submit', e => {
  const f = e.target;
  const btn = e.submitter;
  const msg = (btn && btn.dataset.confirm) || f.dataset.confirm;
  if (msg && !confirm(msg)) { e.preventDefault(); return; }
  if (f.dataset.prompt) {
    const reason = prompt(f.dataset.prompt, '');
    if (reason === null) { e.preventDefault(); return; }
    f.querySelector('input[name=reason]').value = reason;
  }
});
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-confirm]');
  if (a && !confirm(a.dataset.confirm)) e.preventDefault();
  // clickable table rows
  const tr = e.target.closest('tr[data-href]');
  if (tr && !e.target.closest('a, button, input, select, label')) location = tr.dataset.href;
});

// Credential reveal / copy (switch page + credentials page). Each reveal is written to the audit log.
const credCache = {};
async function getCreds(cid, sw) {
  if (!credCache[cid]) credCache[cid] = await api('/api/creds/' + cid + (sw ? '?switch=' + encodeURIComponent(sw) : ''));
  return credCache[cid];
}
function toggleReveal(btn, scope) {
  const showing = btn.dataset.showing === '1';
  getCreds(btn.dataset.cid, btn.dataset.switch).then(c => {
    scope.querySelectorAll('code.secret').forEach(el => {
      el.textContent = showing ? '••••••••' : (c[el.dataset.k] || '');
      el.classList.toggle('shown', !showing);
    });
    btn.dataset.showing = showing ? '0' : '1';
    btn.textContent = showing ? (btn.id === 'revealAll' ? t('Show passwords') : t('Show')) : t('Hide');
  });
}
document.addEventListener('click', e => {
  const rv = e.target.closest('#revealAll');
  if (rv) toggleReveal(rv, document.querySelector('.creds-card'));
  const rp = e.target.closest('.reveal-profile');
  if (rp) toggleReveal(rp, rp.closest('.card'));
  const cp = e.target.closest('button.copy');
  if (cp) {
    getCreds(cp.dataset.cid, cp.dataset.switch).then(c => {
      const v = c[cp.dataset.k] || '';
      const done = () => { cp.textContent = '✓'; setTimeout(() => cp.textContent = '⧉', 1200); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(v).then(done);
      else { const ta = document.createElement('textarea'); ta.value = v; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done(); }
    });
  }
  const eye = e.target.closest('button.eye');
  if (eye) { const i = eye.previousElementSibling; i.type = i.type === 'password' ? 'text' : 'password'; }
});

// close dropdown menus (<details class="menu">) when clicking elsewhere
document.addEventListener('click', e => {
  document.querySelectorAll('details.menu[open]').forEach(d => { if (!d.contains(e.target)) d.removeAttribute('open'); });
});

// Sortable tables
document.querySelectorAll('table.sortable').forEach(tbl => {
  tbl.querySelectorAll('th').forEach((th, i) => {
    if (th.hasAttribute('data-nosort')) return;
    th.addEventListener('click', () => {
      const tb = tbl.tBodies[0], rows = [...tb.rows];
      const dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      tbl.querySelectorAll('th').forEach(h => delete h.dataset.dir);
      th.dataset.dir = dir;
      const key = r => { const c = r.cells[i]; if (!c) return ''; return c.dataset.v !== undefined ? +c.dataset.v : c.innerText.trim().toLowerCase(); };
      const ipKey = s => /^\d+\.\d+\.\d+\.\d+$/.test(s) ? s.split('.').map(n => n.padStart(3, '0')).join('.') : s;
      rows.sort((a, b) => {
        let x = key(a), y = key(b);
        if (typeof x === 'string') { x = ipKey(x); y = ipKey(y); }
        return (x > y ? 1 : x < y ? -1 : 0) * (dir === 'asc' ? 1 : -1);
      });
      rows.forEach(r => tb.appendChild(r));
    });
  });
});

// Switch picker (credential and group forms)
const swFilter = document.getElementById('swFilter');
if (swFilter) {
  const items = [...document.querySelectorAll('.sw-picker label')];
  swFilter.addEventListener('input', () => {
    const q = swFilter.value.toLowerCase();
    items.forEach(l => l.hidden = q && !l.dataset.t.includes(q));
  });
  const setVis = v => items.filter(l => !l.hidden).forEach(l => l.querySelector('input').checked = v);
  document.getElementById('swAll').onclick = () => setVis(true);
  document.getElementById('swNone').onclick = () => setVis(false);
}

// ---------------------------------------------------------------- session timeout (sign out after inactivity)
(() => {
  const limit = window.SESSION_TIMEOUT || 0;
  if (!limit) return;
  let lastServer = Date.now(), lastActivity = Date.now(), pinging = false, bar = null;
  const ping = async () => {
    if (pinging) return; pinging = true;
    try {
      const r = await fetch('/api/keepalive', { method: 'POST', headers: { 'X-CSRF': window.CSRF } });
      if (r.ok) lastServer = Date.now(); else if (r.status === 401) location.href = window.LOGOUT_URL;
    } catch (_e) { /* offline: try later */ } finally { pinging = false; }
  };
  ['keydown', 'mousedown', 'input', 'touchstart', 'wheel'].forEach(ev => document.addEventListener(ev, () => {
    lastActivity = Date.now();
    if (Date.now() - lastServer > 60000) ping();   // at most one keep-alive per minute
  }, { passive: true, capture: true }));
  setInterval(() => {
    const left = Math.round((lastServer + limit * 1000 - Date.now()) / 1000);
    if (lastActivity > lastServer && left < limit - 60) ping();
    if (left <= 0) { location.href = window.LOGOUT_URL; return; }
    if (left <= 60) {
      if (!bar) {
        bar = document.createElement('div'); bar.className = 'timeout-bar';
        bar.innerHTML = `<span></span> <button class="btn sm primary" type="button">${t('Stay signed in')}</button>`;
        bar.querySelector('button').onclick = () => { ping(); bar.remove(); bar = null; };
        document.body.appendChild(bar);
      }
      bar.querySelector('span').textContent = t('You will be signed out in %(n)s s because of inactivity.', { n: left });
    } else if (bar) { bar.remove(); bar = null; }
  }, 1000);
})();

// ---------------------------------------------------------------- maintenance notifications (badge, bell, pop-up)
(() => {
  if (!window.HAS_TICKETS) return;
  const pop = document.getElementById('tkPop');
  const seen = () => fetch('/api/tickets/seen', { method: 'POST', headers: { 'X-CSRF': window.CSRF } }).catch(() => {});
  if (pop) {
    document.getElementById('tkLater').onclick = () => { pop.remove(); seen(); };
    pop.querySelectorAll('a').forEach(a => a.addEventListener('click', seen));
  }
  const known = new Set();
  async function poll() {
    try {
      const r = await fetch('/api/notifications'); if (!r.ok) return;
      const n = await r.json();
      const badge = document.getElementById('tkBadge'), bell = document.getElementById('tkBell'), bn = document.getElementById('tkBellN');
      if (badge) { badge.textContent = n.todo + n.validate; badge.hidden = !(n.todo + n.validate); }
      if (bell) { bn.textContent = n.todo; bell.hidden = !n.todo; }
      const fresh = (n.new || []).filter(tk => !known.has(tk.id));
      fresh.forEach(tk => known.add(tk.id));
      if (fresh.length && !document.getElementById('tkPop')) {   // a ticket was just assigned: pop-up
        const d = document.createElement('div'); d.className = 'tk-pop'; d.id = 'tkPop';
        const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        d.innerHTML = `<div class="tk-pop-box card"><h2>🔔 ${esc(t('You have maintenance work to do'))}</h2><ul>${fresh.map(tk =>
          `<li><a href="${window.TICKET_URL.replace(/0$/, tk.id)}"><b>#${tk.id} ${esc(tk.title)}</b></a><div class="small muted">${esc(t('planned'))} ${esc(tk.planned_start)} → ${esc(tk.planned_end)}</div></li>`).join('')}</ul>
          <div class="btns"><a class="btn primary" href="${window.TICKET_URL.replace(/0$/, fresh[0].id)}">${esc(t('Open the ticket'))}</a><button class="btn ghost" type="button">${esc(t('Later'))}</button></div></div>`;
        d.querySelector('button').onclick = () => { d.remove(); seen(); };
        document.body.appendChild(d);
      }
    } catch (_e) { /* offline */ }
  }
  document.querySelectorAll('#tkPop li a').forEach(a => { const m = a.href.match(/(\d+)$/); if (m) known.add(+m[1]); });
  setInterval(poll, 60000);
})();

// ---------------------------------------------------------------- password policy hint (live checklist)
document.querySelectorAll('ul.pw-hint').forEach(ul => {
  const form = ul.closest('form');
  const inp = form.querySelector(`input[name="${ul.dataset.for}"]`);
  if (!inp) return;
  const userInput = ul.dataset.user ? form.querySelector(`input[name="${ul.dataset.user}"]`) : null;
  const check = () => {
    const v = inp.value, user = (userInput ? userInput.value : inp.dataset.username || '').toLowerCase();
    let all = true;
    ul.querySelectorAll('li').forEach(li => {
      let ok;
      if (li.dataset.rule === 'len') ok = v.length >= +li.dataset.min;
      else if (li.dataset.rule === 'user') ok = v.length > 0 && (!user || v.toLowerCase() !== user);
      else ok = new RegExp(li.dataset.rx).test(v);
      li.classList.toggle('ok', ok); all = all && ok;
    });
    ul.classList.toggle('empty', !v); ul.classList.toggle('all-ok', all);
    inp.setCustomValidity(v && !all ? t('The password does not respect the policy') : '');
  };
  inp.addEventListener('input', check); if (userInput) userInput.addEventListener('input', check);
  check();
});

// switch status: refresh the badges of the page every minute (Switches page, top bar)
(() => {
  const badges = document.querySelectorAll('[data-st-sid]'), alert = document.getElementById('stAlert');
  if (!badges.length && !alert) return;
  const t = s => (window.I18N && window.I18N[s]) || s;
  async function refresh() {
    try {
      const r = await fetch('/api/status', { headers: { 'X-Requested-With': 'fetch' } });
      if (!r.ok) return;
      const j = await r.json();
      badges.forEach(b => {
        const s = j.switches[b.dataset.stSid];
        if (!s) return;
        b.className = 'st-badge st-' + s.state;
        b.querySelector('.st-l').textContent = s.label;
        const since = document.querySelector(`[data-st-since="${b.dataset.stSid}"]`);
        if (since) since.textContent = (s.since ? t('for %(d)s').replace('%(d)s', s.since) : '') + (s.rtt != null && (s.state === 'online' || s.state === 'slow') ? ' · ' + s.rtt + ' ms' : '');
      });
      if (alert) { alert.hidden = !j.offline; alert.textContent = '● ' + t('%(n)s offline').replace('%(n)s', j.offline); }
    } catch (e) { /* offline browser: try again later */ }
  }
  setInterval(refresh, 60000);
})();
