// Connections: what is set up, what is not, what is broken, and what to do.
'use strict';
(function () {
  const CC = window.CC;
  const esc = CC.esc;
  const WORD = {ok: 'Connected', off: 'Not set up', broken: 'Broken'};
  let el, note = '';

  function row(r) {
    const state = WORD[r.state] ? r.state : 'broken';
    return `<article class="card conn ${esc(state)}">
      <h3>${esc(r.name)} <span class="chip quiet state">${esc(WORD[state])}</span></h3>
      <p class="dim">${esc(r.detail)}</p>
      ${state !== 'ok' && r.fix ? `<p class="fix">${esc(r.fix)}</p>` : ''}
    </article>`;
  }

  async function recheck() {
    try { await CC.post('/api/connections/recheck', {}); note = ''; } catch (e) { note = e.message; }
    await part.refresh();
  }

  const part = {
    mount(s) {
      el = s;
      el.addEventListener('click', e => { if (e.target.closest('#conn-recheck')) recheck(); });
    },
    async refresh() {
      const rows = await CC.get('/api/connections');
      const bad = rows.filter(r => r.state === 'broken').length;
      CC.badge('connections', bad);
      el.innerHTML = (rows.length ? rows.map(row).join('') : '<p class="empty">Nothing to check yet.</p>') +
        `<p><button type="button" class="act" id="conn-recheck">Recheck</button></p>` +
        (note ? `<p class="faint" role="status">${esc(note)}</p>` : '');
    },
  };
  CC.register('connections', part);
})();
