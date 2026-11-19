// The page's shell. Each feature is /static/<id>.js and calls CC.register(id,
// {mount(el), refresh()}); the shell gives it a section and a nav button,
// refreshes the visible one every few seconds, and owns the key.
'use strict';
(function () {
  // The owner key lives in this browser only and travels as a header, never in
  // a URL. A device gets one by pairing: see /pair and INSTALL.md.
  const url = new URL(location.href);
  if (url.searchParams.has('k')) {          // an old link: drop the stale key from the address bar
    url.searchParams.delete('k');
    history.replaceState(null, '', url.pathname + (url.search || '') + url.hash);
  }
  let key = '';
  try { key = localStorage.getItem('cc-owner-key') || ''; } catch (e) {}

  const parts = {};
  const order = [];
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const KEY_HEADER = 'x-agent-companion-key';
  const authHeaders = extra => Object.assign({[KEY_HEADER]: key}, extra || {});

  function showPair(why) {
    document.getElementById('views').innerHTML =
      `<section class="pair"><h2>Pair this device</h2>${why ? `<p class="empty">${esc(why)}</p>` : ''}` +
      '<p>Run <code>python -m agent_companion.server pair</code> on the computer and open the link it prints.</p></section>';
  }
  async function get(path) {
    const r = await fetch(path, {cache: 'no-store', headers: authHeaders()});
    if (r.status === 401) { showPair('This device is not paired, or its key was removed.'); throw new Error(`${path}: 401`); }
    if (!r.ok) {
      // The server's reason ("comms is not set up: set tts_dir ...") beats a status code.
      const out = await r.json().catch(() => ({}));
      throw new Error(typeof out.detail === 'string' && out.detail ? out.detail : `${path}: ${r.status}`);
    }
    return r.json();
  }
  // Raw bytes with the key; the caller makes what it needs of the response.
  // A feature may pass a method and body (Pages uses this for DELETE), but it
  // cannot replace the authenticated header.
  async function fetchAuthed(path, options) {
    const init = Object.assign({}, options || {});
    init.headers = Object.assign({}, (options || {}).headers || {}, authHeaders());
    return fetch(path, init);
  }
  async function post(path, body) {
    const r = await fetch(path, {method: 'POST', headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify(body || {})});
    const out = await r.json().catch(() => ({}));
    if (r.status === 401) showPair('This device is not paired, or its key was removed.');
    if (!r.ok) throw new Error(out.detail || `${path}: ${r.status}`);
    return out;
  }
  // A write that returns bytes, used by the voice-lab test renderer.
  async function fetchWrite(path, body) {
    const r = await fetch(path, {method: 'POST', headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify(body || {})});
    if (r.status === 401) showPair('This device is not paired, or its key was removed.');
    return r;
  }
  // A feature sets its nav badge: a number that needs the owner, or 0.
  function badge(id, n) {
    const b = document.querySelector(`#nav button[data-id="${id}"] .n`);
    if (b) b.textContent = n ? String(n) : '';
  }
  function show(id) {
    for (const s of document.querySelectorAll('#views > section')) s.hidden = s.dataset.id !== id;
    for (const b of document.querySelectorAll('#nav button')) b.setAttribute('aria-current', String(b.dataset.id === id));
    const u = new URL(location.href);
    u.searchParams.set('view', id);
    history.replaceState(null, '', u.pathname + u.search);
    refresh(id);
  }
  async function refresh(id) {
    const p = parts[id];
    if (p && p.refresh) { try { await p.refresh(); } catch (e) { console.warn(id, e); } }
  }
  function current() {
    const s = document.querySelector('#views > section:not([hidden])');
    return s ? s.dataset.id : '';
  }
  function register(id, part) {
    parts[id] = part;
    const sec = document.querySelector(`#views > section[data-id="${id}"]`);
    if (sec && part.mount) part.mount(sec);
    refresh(id);
  }
  window.CC = {register, get, post, fetchAuthed, fetchWrite, esc, badge, show, key: () => key};

  async function restartNotice() {
    try {
      const state = await get('/api/needs-restart');
      const notice = document.getElementById('needs-restart');
      notice.hidden = !state.needed;
      if (state.needed) notice.querySelector('code').textContent = state.command || '';
    } catch (e) {}
  }

  async function boot() {
    if (!key) { showPair(''); return; }
    let feats = [];
    try { feats = await get('/api/features'); } catch (e) {
      document.getElementById('views').innerHTML = `<p class="empty">Server not answering: ${esc(e.message)}</p>`;
      return;
    }
    await restartNotice();
    const nav = document.getElementById('nav'), views = document.getElementById('views');
    for (const f of feats) {
      order.push(f.id);
      nav.insertAdjacentHTML('beforeend',
        `<button type="button" data-id="${esc(f.id)}">${esc(f.title)}<span class="n"></span></button>`);
      views.insertAdjacentHTML('beforeend', `<section data-id="${esc(f.id)}" hidden></section>`);
    }
    nav.addEventListener('click', e => { const b = e.target.closest('button[data-id]'); if (b) show(b.dataset.id); });
    const want = new URL(location.href).searchParams.get('view');
    show(order.includes(want) ? want : order[0] || '');
    for (const f of feats) {
      const s = document.createElement('script');
      s.src = `/static/${encodeURIComponent(f.id)}.js`;
      document.body.appendChild(s);
    }
    setInterval(() => { if (!document.hidden) refresh(current()); }, 5000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(current()); });
  }
  boot();
})();
