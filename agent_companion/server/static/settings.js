// Settings: the saved context line, comms (hold, volume, a test clip per
// voice), paired devices, and read-only views of what config.toml sets.
'use strict';
(function () {
  const CC = window.CC, esc = CC.esc;
  const css = document.createElement('style');
  css.textContent = `
    .set .row { align-items:center; justify-content:space-between; min-height:44px; flex-wrap:wrap; }
    .set .row .nm { flex:1 1 8rem; }
    .set code { font:.8rem ui-monospace, Menlo, monospace; background:var(--sunk); padding:2px 6px; border-radius:6px; overflow-wrap:anywhere; }
    .set .on { color:var(--busy); font-weight:600; } .set .off { color:var(--faint); }
    .set input[type=range] { flex:1 1 10rem; min-height:44px; }
    .set input[type=number] { width:10rem; min-height:44px; font:inherit; color:var(--fg); background:var(--bg); border:1px solid var(--line-strong); border-radius:10px; padding:0 12px; font-variant-numeric:tabular-nums; }
    .set .note { color:var(--dim); font-size:.85rem; margin:6px 0 0; }
    .set .msg { color:var(--dim); font-size:.85rem; min-height:1.2em; margin:6px 0 0; }
    .set .msg.bad { color:var(--alert); }
    .set a { color:var(--fg); }`;
  document.head.appendChild(css);

  let box, data = null, clearLine = null, msg = '', msgBad = false, timer = 0, busy = false;
  const when = t => t ? new Date(t * 1000).toLocaleDateString() : 'unknown date';
  const val = v => (v == null || v === '') ? '<span class="off">unset</span>' : `<code>${esc(v)}</code>`;
  const safeRepo = r => /^[\w.-]+\/[\w.-]+$/.test(r || '') ? r : '';

  function comms(c) {
    const names = Object.keys(c.cast || {});
    const pct = Math.round((Number(c.volume) || 0) * 100);
    const hold = c.held
      ? (c.hold_until ? 'On hold until ' + new Date(c.hold_until * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : 'On hold until you resume')
      : 'Clips play as they arrive';
    return `<h2 class="sec-head">Comms</h2><div class="card">
      <div class="row"><span class="body nm"><b>Hold</b><br><span class="dim">${esc(hold)}</span></span>
        <button type="button" class="act" id="s-hold" aria-pressed="${!!c.held}">${c.held ? 'Resume' : 'Hold'}</button></div>
      <div class="row"><label class="body nm" for="s-vol"><b>Volume</b> <output id="s-volo">${pct}%</output></label>
        <input type="range" id="s-vol" min="0" max="100" step="5" value="${pct}"></div>
      ${c.installed ? (names.length ? names.map(n => `<div class="row"><span class="body nm"><b>${esc(n)}</b><br>
        <span class="faint">${esc(c.cast[n])}</span></span>
        <button type="button" class="act" data-test="${esc(n)}">Test</button></div>`).join('')
        : '<p class="note">No voices in the cast. comms/README.md says how to add one.</p>')
        : '<p class="note">Comms is not installed yet: INSTALL.md step 10.</p>'}
      </div>`;
  }
  function contextLine() {
    const tokens = Number.isInteger(clearLine) ? clearLine : '';
    return `<h2 class="sec-head">Sessions</h2><div class="card"><div class="row">
      <label class="body nm" for="s-clear-line"><b>Context line (tokens)</b><br>
        <span class="faint">Top-level sessions at this line say needs /clear.</span></label>
      <input type="number" id="s-clear-line" min="50000" max="2000000" step="1000" value="${esc(tokens)}">
      <button type="button" class="act" id="s-clear-line-save">Save</button></div></div>`;
  }
  function devices(ds) {
    return `<h2 class="sec-head">Devices</h2><div class="card">` + (ds.length ? ds.map(d =>
      `<div class="row"><span class="body nm"><b>${esc(d.name)}</b>${d.this_device ? ' <span class="dim">(this device)</span>' : ''}<br>
        <span class="faint">paired ${esc(when(d.created))}</span></span>
        <button type="button" class="act" data-unpair="${esc(d.name)}" data-self="${d.this_device ? 1 : 0}">Unpair</button></div>`).join('')
      : '<p class="note">No devices paired.</p>') +
      '<p class="note">To add one, run <code>python -m agent_companion.server pair</code> on the computer.</p></div>';
  }
  function features(fs) {
    return `<h2 class="sec-head">Features</h2><div class="card">` + fs.map(f =>
      `<div class="row"><span class="body nm"><b>${esc(f.id)}</b> <span class="chip quiet ${f.enabled ? 'on' : 'off'}">${f.enabled ? 'on' : 'off'}</span><br>
        <span class="faint">to ${f.enabled ? 'turn off' : 'turn on'}, in config.toml:</span><br>${val(f.line)}</span></div>`).join('') +
      '<p class="note">These stay in the file. Edit it by hand or ask your Claude, then restart the server.</p></div>';
  }
  function collab(c) {
    return `<h2 class="sec-head">Collab</h2><div class="card">` + [['label', 'collab_label'], ['from', 'collab_from'], ['repo', 'collab_repo']].map(([k, key]) =>
      `<div class="row"><span class="body nm"><b>${esc(k)}</b><br><span class="faint">${esc(key)}</span></span><span>${val(c[k])}</span></div>`).join('') +
      '<p class="note">Set in config.toml. COLLAB.md explains them.</p></div>';
  }
  function about(d) {
    const repo = safeRepo(d.collab && d.collab.repo);
    const links = repo ? Object.keys(d.docs || {}).map(n =>
      `<a href="https://github.com/${esc(repo)}/blob/main/${esc(n)}" target="_blank" rel="noopener">${esc(n)}</a>`).join(' &middot; ') : '';
    return `<h2 class="sec-head">About</h2><div class="card">
      <div class="row"><span class="body nm"><b>Version</b></span><span>${esc(d.version)}</span></div>
      <div class="row"><span class="body nm"><b>Config file</b><br>${val(d.config_path)}</span></div>
      ${links ? `<div class="row"><span class="body nm">${links}</span></div>` : ''}</div>`;
  }

  function draw() {
    if (!data) return;
    box.innerHTML = `<div class="set">${contextLine()}${comms(data.comms)}${devices(data.devices)}${features(data.features)}` +
      `${collab(data.collab)}${about(data)}<p class="msg${msgBad ? ' bad' : ''}" role="status" id="s-msg">${esc(msg)}</p></div>`;
  }
  const say = (t, bad = false) => {
    msg = t; msgBad = bad;
    const m = document.getElementById('s-msg');
    if (m) { m.textContent = t; m.classList.toggle('bad', bad); }
  };
  async function load() {
    const results = await Promise.all([CC.get('/api/settings'), CC.get('/api/clearline')]);
    data = results[0];
    clearLine = results[1].tokens;
  }
  async function act(fn, ok, failed = '') {
    busy = true;
    try { await fn(); say(ok || '', false); } catch (e) { say(failed ? failed + ': ' + (e.message || 'try again.') : e.message, true); }
    busy = false;
    try { await load(); draw(); } catch (e) {}
  }

  function wire() {
    box.addEventListener('click', e => {
      const t = e.target.closest('button');
      if (!t) return;
      if (t.id === 's-hold') {
        const held = t.getAttribute('aria-pressed') === 'true';
        act(() => CC.post(held ? '/api/settings/comms/resume' : '/api/settings/comms/hold', {}), held ? 'Resumed.' : 'Held.');
      } else if (t.dataset.test) {
        act(() => CC.post('/api/settings/comms/test', {name: t.dataset.test}), 'Test clip sent as ' + t.dataset.test + '.');
      } else if (t.dataset.unpair) {
        const self = t.dataset.self === '1', name = t.dataset.unpair;
        const q = self ? `Unpair ${name}? This is the device you are using; it will need a new pairing link.` : `Unpair ${name}?`;
        if (!confirm(q)) return;
        act(() => CC.post('/api/settings/devices/unpair', {name, confirm: self}), name + ' unpaired.');
      } else if (t.id === 's-clear-line-save') {
        const tokens = Number(document.getElementById('s-clear-line').value);
        if (!Number.isInteger(tokens)) return say('Enter a whole number of tokens.', true);
        act(() => CC.post('/api/clearline', {tokens}), 'Context line saved.', 'Could not save context line');
      }
    });
    box.addEventListener('input', e => {
      if (e.target.id !== 's-vol') return;
      const v = Number(e.target.value);
      document.getElementById('s-volo').textContent = v + '%';
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = 0;
        act(() => CC.post('/api/settings/comms/volume', {volume: v / 100}), 'Volume saved.');
      }, 400);
    });
  }

  CC.register('settings', {
    mount(el) { box = document.createElement('div'); el.appendChild(box); wire(); },
    async refresh() {
      // Leave the page alone while a slider is being dragged or a write is in flight.
      if (timer || busy) return;
      await load();
      draw();
    },
  });
})();
