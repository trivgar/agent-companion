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
  // The six a person reaches for first, in this order. A feature not listed
  // fills a free place in the server's order; past six, it goes under "more".
  const PRIMARY = ['sessions', 'asks', 'chat', 'mail', 'comms', 'files'];
  const NAV_MAX = 6;
  function splitNav(feats) {
    const by = {};
    for (const f of feats) by[f.id] = f;
    const first = PRIMARY.filter(id => by[id]).map(id => by[id]);
    const rest = feats.filter(f => !PRIMARY.includes(f.id));
    while (first.length < NAV_MAX && rest.length) first.push(rest.shift());
    return {first, rest};
  }
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
  // A feature sets its nav badge: a number that needs the owner, or 0. Counts
  // on items in the menu add up on "more", so a hidden item is never silent.
  function badge(id, n) {
    const b = document.querySelector(`.navwrap button[data-id="${id}"] .n`);
    if (b) b.textContent = n ? String(n) : '';
    let hidden = 0;
    for (const m of document.querySelectorAll('#morebar button[data-id] .n')) hidden += Number(m.textContent) || 0;
    const more = document.querySelector('#morelink .n');
    if (more) more.textContent = hidden ? String(hidden) : '';
  }
  function openMore(open) {
    const link = document.getElementById('morelink'), bar = document.getElementById('morebar');
    bar.hidden = !open;
    link.setAttribute('aria-expanded', String(!!open));
  }
  // The underline under the current nav item. It is placed, not animated, the
  // first time, so a page never opens with a line sliding in from the edge.
  function moveInk() {
    const nav = document.getElementById('nav');
    let ink = nav.querySelector('.ink');
    if (!ink) {
      ink = document.createElement('i');
      ink.className = 'ink';
      nav.appendChild(ink);
      nav.classList.add('inked');
    }
    const cur = nav.querySelector('button[aria-current="true"]');
    if (!cur) { ink.classList.remove('on', 'moves'); return; }
    if (ink.classList.contains('on')) ink.classList.add('moves');
    ink.style.transform = `translateX(${cur.offsetLeft}px) scaleX(${cur.offsetWidth / 100})`;
    ink.classList.add('on');
    cur.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
  function closeSheet() {
    const cur = current();
    delete document.documentElement.dataset.sheet;
    for (const b of document.querySelectorAll('.sheet-back')) b.remove();
    for (const s of document.querySelectorAll('#views > section.sheet')) {
      s.classList.remove('sheet');
      for (const c of s.querySelectorAll('.sheet-close')) c.remove();
      s.hidden = s.dataset.id !== cur;
    }
  }
  function sheet(id) {
    const sec = document.querySelector(`#views > section[data-id="${id}"]`);
    if (!sec) return;
    closeSheet();
    const back = document.createElement('div');
    back.className = 'sheet-back';
    back.addEventListener('click', closeSheet);
    document.getElementById('views').appendChild(back);
    sec.classList.add('sheet');
    sec.hidden = false;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'sheet-close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '\u00d7';
    close.addEventListener('click', e => { e.stopPropagation(); closeSheet(); });
    sec.prepend(close);
    document.documentElement.dataset.sheet = id;
    refresh(id);
  }
  function show(id) {
    closeSheet();
    for (const s of document.querySelectorAll('#views > section')) s.hidden = s.dataset.id !== id;
    for (const b of document.querySelectorAll('.navwrap button[data-id]')) b.setAttribute('aria-current', String(b.dataset.id === id));
    document.getElementById('morelink').classList.toggle('on',
      !!document.querySelector(`#morebar button[data-id="${id}"]`));
    openMore(false);
    moveInk();
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
    const s = document.querySelector('#views > section:not([hidden]):not(.sheet)');
    return s ? s.dataset.id : '';
  }
  function refreshVisible() {
    if (document.hidden) return;
    const cur = current();
    if (cur) refresh(cur);
    const open = document.documentElement.dataset.sheet;
    if (open && open !== cur) refresh(open);
  }
  function register(id, part) {
    parts[id] = part;
    const sec = document.querySelector(`#views > section[data-id="${id}"]`);
    if (sec && part.mount) part.mount(sec);
    refresh(id);
  }
  window.CC = {register, get, post, fetchAuthed, fetchWrite, esc, badge, show, sheet, closeSheet, key: () => key};

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
    const more = document.getElementById('morelink'), bar = document.getElementById('morebar');
    const item = f => `<button type="button" data-id="${esc(f.id)}">${esc(f.title)}<span class="n"></span></button>`;
    const {first, rest} = splitNav(feats);
    for (const f of first.concat(rest)) {
      order.push(f.id);
      views.insertAdjacentHTML('beforeend', `<section data-id="${esc(f.id)}" hidden></section>`);
    }
    nav.innerHTML = first.map(item).join('');
    // The menu always holds the animations switch, so "more" is always there.
    bar.innerHTML = rest.map(item).join('') + (rest.length ? '<hr>' : '') +
      '<button type="button" id="still-toggle" role="menuitemcheckbox"></button>';
    const still = document.getElementById('still-toggle');
    const drawStill = () => {
      const off = window.CCStill.get();
      still.textContent = `animations: ${off ? 'off' : 'on'}`;
      still.setAttribute('aria-checked', String(!off));
    };
    drawStill();
    still.addEventListener('click', e => { e.stopPropagation(); window.CCStill.set(!window.CCStill.get()); drawStill(); });
    more.hidden = false;
    const pick = e => { const b = e.target.closest('button[data-id]'); if (b) show(b.dataset.id); };
    nav.addEventListener('click', pick);
    bar.addEventListener('click', pick);
    more.addEventListener('click', e => { e.stopPropagation(); openMore(bar.hidden); });
    document.addEventListener('click', e => { if (!bar.hidden && !e.target.closest('#morebar')) openMore(false); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') { openMore(false); closeSheet(); } });
    window.addEventListener('resize', moveInk);
    const want = new URL(location.href).searchParams.get('view');
    show(order.includes(want) ? want : order[0] || '');
    for (const f of feats) {
      const s = document.createElement('script');
      s.src = `/static/${encodeURIComponent(f.id)}.js`;
      document.body.appendChild(s);
    }
    setInterval(refreshVisible, 5000);
    document.addEventListener('visibilitychange', refreshVisible);
  }
  boot();
})();
