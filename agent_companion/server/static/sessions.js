// Sessions: one card per live session, and the spend strip in the header.
'use strict';
(function () {
  const esc = CC.esc;
  const identity = window.CCIdentity;
  const openCards = new Set();
  const openHelpers = new Set();
  const ago = s => s == null ? '' : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
  const money = n => '$' + Number(n || 0).toFixed(2);
  const short = m => String(m || '').replace(/^claude-/, '').replace(/-(\d)-(\d)$/, ' $1.$2');
  const ARM_MS = 4000;
  const RESULT_MS = 6000;
  const arms = new Map();
  const results = new Map();
  const askErrors = new Map();
  const locallySeen = new Map();
  let rowsOnPage = [];
  let sessionsEl = null;
  let seenAllError = '';
  let restoreState = null;
  let restoreBusy = false;
  let restoreReports = [];

  const cardKey = r => `${String(r.pid || '')}:${String(r.session_id || '')}:${String(r.speaker || r.name || '')}`;
  const actionKey = (r, action) => `${action}:${cardKey(r)}`;
  const isArmed = (r, action) => (arms.get(actionKey(r, action)) || 0) > Date.now();
  const suggestionLabel = words => {
    const value = String(words || '').replace(/\s+/g, ' ').trim();
    return value.length > 60 ? `${value.slice(0, 59)}…` : value;
  };

  function actionResult(r) {
    const value = results.get(cardKey(r));
    if (!value || value.until <= Date.now()) {
      results.delete(cardKey(r));
      return '';
    }
    return `<p class="action-result" role="status">${esc(value.text)}</p>`;
  }

  function isUnread(r) {
    if (!r.unread) return false;
    const seenAt = locallySeen.get(cardKey(r));
    return seenAt === undefined || seenAt < Number(r.last_reply_at || 0);
  }

  function actionButton(r, action, label) {
    const armed = isArmed(r, action);
    return `<button type="button" class="act" data-card-action="${esc(action)}" data-name="${esc(r.speaker || r.name || '')}" data-pid="${esc(r.pid)}" data-session-id="${esc(r.session_id)}">${esc(armed ? `Tap again to ${action}` : label)}</button>`;
  }

  function suggestionButton(r) {
    const armed = isArmed(r, 'suggest');
    return `<button type="button" class="act" data-card-action="suggest" data-words="${esc(r.suggestion)}">${esc(armed ? 'Tap again to send' : `Send: ${suggestionLabel(r.suggestion)}`)}</button>`;
  }

  function actionRow(r) {
    const idle = r.status === 'idle' && !r.waiting;
    const actions = [
      r.status === 'busy' ? actionButton(r, 'stop', 'Stop') : '',
      idle ? actionButton(r, 'resume', 'Resume') : '',
      idle ? actionButton(r, 'compact', 'Compact') : '',
      idle ? actionButton(r, 'clear', 'Clear') : '',
      actionButton(r, 'end', 'End'),
      idle && r.suggestion ? suggestionButton(r) : '',
    ].filter(Boolean).join('');
    return actions ? `<div class="actions">${actions}</div>` : '';
  }

  function cacheChip(cache) {
    if (!cache || cache.rewarm_usd == null) return '';
    const rewarm = Number(cache.rewarm_usd);
    if (!Number.isFinite(rewarm) || rewarm < 1) return '';
    let text = '';
    if (cache.cold) text = `cold: next turn ~${money(rewarm)}`;
    else if (Number(cache.expires_in_s) > 0 && Number(cache.expires_in_s) <= 600) {
      text = `cold in ${Math.ceil(Number(cache.expires_in_s) / 60)} min: ~${money(rewarm)}`;
    }
    return text ? `<span class="cold">${esc(text)}</span>` : '';
  }

  function backgroundJobs(r) {
    const jobs = Array.isArray(r.bg_job_list) ? r.bg_job_list : [];
    if (!jobs.length) return '';
    const count = jobs.length;
    const label = `${count} job${count === 1 ? '' : 's'} running`;
    const lines = jobs.map(job => `<div class="bg-job"><code>${esc(job.cmd || 'shell job')}</code><time>${esc(ago(job.age_s).replace(' ago', ''))}</time></div>`).join('');
    return `<div class="bg-jobs"><div>${esc(label)}</div>${lines}</div>`;
  }

  function splitHelpers(rows) {
    const byPid = new Map();
    for (const r of rows) if (r.pid != null) byPid.set(r.pid, r);
    const isHelper = r => Boolean(r.child && !r.job && r.root_pid && byPid.has(r.root_pid) && byPid.get(r.root_pid) !== r);
    const helpersByRoot = new Map();
    const agents = [];
    for (const r of rows) {
      if (isHelper(r)) {
        if (!helpersByRoot.has(r.root_pid)) helpersByRoot.set(r.root_pid, []);
        helpersByRoot.get(r.root_pid).push(r);
      } else {
        agents.push(r);
      }
    }
    return {agents, helpersByRoot};
  }

  function ctx(r, helpers) {
    const name = r.speaker || r.name || r.project || 'session';
    const key = cardKey(r);
    const askId = r.ask && r.ask.id ? String(r.ask.id) : '';
    return {key, name, hue: identity.hue(name), open: openCards.has(key), unread: isUnread(r),
      actions: actionRow(r), result: actionResult(r), jobs: backgroundJobs(r), cache: cacheChip(r.cache),
      askError: askId ? (askErrors.get(askId) || '') : '',
      helpers: helpers || [], helpersOpen: openHelpers.has(key),
      ago, money, short};
  }

  const GROUPS = [['needs', 'Needs you'], ['working', 'Working'], ['idle', 'Idle']];

  function groupsHtml() {
    const {agents, helpersByRoot} = splitHelpers(rowsOnPage);
    return GROUPS.map(([id, title]) => {
      const rows = agents.filter(r => CCCard.group(r) === id);
      if (!rows.length) return '';
      return `<h2 class="grp">${esc(title)} <span class="n">${rows.length}</span></h2>${rows.map(r => CCCard.html(r, ctx(r, helpersByRoot.get(r.pid)))).join('')}`;
    }).join('');
  }

  function restorePanel() {
    const missing = restoreState && restoreState.ok ? (restoreState.sessions || []).filter(r => !r.running && !r.gone) : [];
    if (!missing.length && !restoreReports.length) return '';
    const reports = restoreReports.length ? `<ul class="action-result" role="status">${restoreReports.map(r =>
      `<li>${esc(r.tmux)}: ${esc(r.detail || (r.ok ? 'started' : 'not restored'))}</li>`).join('')}</ul>` : '';
    const button = missing.length ? `<p><button type="button" class="act" data-restore-sessions${restoreBusy ? ' disabled' : ''}>${esc(restoreBusy ? 'Restoring sessions...' : `Restore ${missing.length} sessions`)}</button></p>` : '';
    const listed = missing.length ? `<ul>${missing.map(r =>
      `<li>${esc(r.tmux)}${r.model ? ` (${esc(short(r.model))})` : ''}</li>`).join('')}</ul>` : '';
    return `<section class="restore-sessions">${button}${listed}${reports}</section>`;
  }

  function renderCards() {
    if (!sessionsEl) return;
    const seenAll = rowsOnPage.some(isUnread);
    const control = seenAll ? `<p class="sessions-seen-all"><button type="button" class="act" data-seen-all>Seen all</button>${seenAllError ? `<span class="faint" role="status">${esc(seenAllError)}</span>` : ''}</p>` : '';
    const launcher = '<button type="button" class="act primary new-session-button" data-new-session>New session</button>';
    sessionsEl.innerHTML = launcher + restorePanel() + (rowsOnPage.length ? control + groupsHtml() : '<p class="empty">No live sessions.</p>');
  }

  function rememberResult(r, text) {
    const key = cardKey(r);
    results.set(key, {text, until: Date.now() + RESULT_MS});
    setTimeout(() => {
      const value = results.get(key);
      if (value && value.until <= Date.now()) {
        results.delete(key);
        renderCards();
      }
    }, RESULT_MS + 50);
  }

  function flipCard(key, focusSelector, restoreFocus) {
    if (openCards.has(key)) openCards.delete(key); else openCards.add(key);
    renderCards();
    if (restoreFocus) {
      const again = [...sessionsEl.querySelectorAll('.scard')].find(el => el.dataset.cardKey === key);
      if (again) again.querySelector(focusSelector)?.focus();
    }
  }

  function toggleCard(event) {
    const toggle = event.target.closest('.scard-toggle');
    if (toggle && sessionsEl && sessionsEl.contains(toggle)) {
      flipCard(toggle.closest('.scard').dataset.cardKey, '.scard-toggle', event.type === 'keydown');
      return true;
    }
    const main = event.target.closest('.scard-main');
    if (!main || !sessionsEl || !sessionsEl.contains(main)) return false;
    const key = main.closest('.scard').dataset.cardKey;
    const row = rowsOnPage.find(item => cardKey(item) === key);
    if (window.CCChat && row && !row.child) {
      window.CCChat.open(row.speaker || row.name || row.project || 'session');
      return true;
    }
    flipCard(key, '.scard-main', event.type === 'keydown');
    return true;
  }

  async function act(event) {
    if (toggleCard(event)) return;
    const helpersBtn = event.target.closest('[data-helpers-toggle]');
    if (helpersBtn && sessionsEl && sessionsEl.contains(helpersBtn)) {
      const key = helpersBtn.closest('.scard').dataset.cardKey;
      if (openHelpers.has(key)) openHelpers.delete(key); else openHelpers.add(key);
      renderCards();
      return;
    }
    const askAll = event.target.closest('[data-ask-all], [data-ask-answer]');
    if (askAll && sessionsEl && sessionsEl.contains(askAll)) {
      CC.show('asks');
      return;
    }
    const askChat = event.target.closest('[data-ask-chat]');
    if (askChat && sessionsEl && sessionsEl.contains(askChat)) {
      const cardEl = askChat.closest('.scard');
      const row = cardEl && rowsOnPage.find(item => cardKey(item) === cardEl.dataset.cardKey);
      if (window.CCChat && row) window.CCChat.open(row.speaker || row.name || row.project || 'session');
      return;
    }
    const decide = event.target.closest('[data-ask-decide]');
    if (decide && sessionsEl && sessionsEl.contains(decide)) {
      const askEl = decide.closest('[data-ask]');
      const id = askEl ? askEl.dataset.ask : '';
      if (!id) return;
      askErrors.delete(id);
      renderCards();
      try {
        const result = await CC.post(`/api/asks/${encodeURIComponent(id)}/decide`, {decision: decide.dataset.askDecide});
        if (result && result.ok === false) throw new Error(result.detail || 'refused');
        await refresh();
      } catch (error) {
        askErrors.set(id, `Not sent: ${error.message}`);
        renderCards();
      }
      return;
    }
    const restore = event.target.closest('[data-restore-sessions]');
    if (restore && sessionsEl && sessionsEl.contains(restore) && !restoreBusy) {
      restoreBusy = true;
      restoreReports = [];
      renderCards();
      try {
        const result = await CC.post('/api/restore', {});
        if (!result.ok) throw new Error(result.detail || 'refused');
        restoreReports = Array.isArray(result.sessions) ? result.sessions : [];
      } catch (error) {
        restoreReports = [{tmux: 'Restore', ok: false, detail: error.message}];
      }
      restoreBusy = false;
      renderCards();
      setTimeout(() => { refresh().catch(() => {}); }, 600);
      return;
    }
    const launcher = event.target.closest('[data-new-session]');
    if (launcher && sessionsEl && sessionsEl.contains(launcher)) {
      if (window.CCNewSession) window.CCNewSession.open();
      return;
    }
    const seenAll = event.target.closest('[data-seen-all]');
    if (seenAll && sessionsEl && sessionsEl.contains(seenAll)) {
      seenAllError = '';
      try {
        const result = await CC.post('/api/cave/seen-all', {});
        if (!result.ok) throw new Error(result.detail || 'refused');
        for (const row of rowsOnPage) locallySeen.set(cardKey(row), Number(row.last_reply_at || 0));
      } catch (error) {
        seenAllError = `Not marked: ${error.message}`;
      }
      renderCards();
      return;
    }
    const button = event.target.closest('[data-card-action]');
    if (!button || !sessionsEl || !sessionsEl.contains(button)) return;
    const cardEl = button.closest('.scard');
    const row = rowsOnPage.find(item => cardKey(item) === cardEl.dataset.cardKey);
    if (!row) return;
    const action = button.dataset.cardAction;
    if (!isArmed(row, action)) {
      const key = actionKey(row, action);
      arms.set(key, Date.now() + ARM_MS);
      setTimeout(() => {
        if (!isArmed(row, action)) {
          arms.delete(key);
          renderCards();
        }
      }, ARM_MS + 50);
      renderCards();
      return;
    }
    arms.delete(actionKey(row, action));
    const name = String(row.speaker || row.name || '');
    const verbs = {stop: 'stopped', resume: 'resumed', compact: 'compacted', clear: 'cleared', end: 'ended', suggest: 'sent'};
    try {
      let result;
      if (action === 'suggest') {
        result = await CC.post(`/api/cave/suggest/${encodeURIComponent(name)}`,
          {pid: row.pid, session_id: row.session_id, words: row.suggestion});
      } else if (action === 'resume' || action === 'end') {
        result = await CC.post(`/api/cave/${action}/${encodeURIComponent(name)}`,
          {pid: row.pid, session_id: row.session_id});
      } else if (action === 'stop') {
        result = await CC.post(`/api/cave/stop/${encodeURIComponent(name)}`, {});
      } else {
        result = await CC.post('/api/cave/send', {to: name, text: `/${action}`});
      }
      if (!result.ok) throw new Error(result.detail || 'refused');
      rememberResult(row, `${verbs[action][0].toUpperCase()}${verbs[action].slice(1)}.`);
    } catch (error) {
      rememberResult(row, `Not ${verbs[action]}: ${error.message}`);
    }
    renderCards();
    setTimeout(() => { refresh().catch(() => {}); }, 1200);
  }

  function machineStrip(health) {
    if (!health || typeof health !== 'object') return '';
    const cpu = health.cpu || {}, temp = health.temperature || {};
    const root = Array.isArray(health.disks) ? health.disks.find(d => d && d.path === '/') : null;
    const bits = [];
    if (Number.isFinite(cpu.percent)) bits.push(`CPU ${Math.round(cpu.percent)}%`);
    else if (cpu.reason) bits.push(`CPU: ${cpu.reason}`);
    // A missing temperature is explained on the Machine tab, not on every strip.
    if (Number.isFinite(temp.c)) bits.push(`${Math.round(temp.c)}C`);
    if (root && Number.isFinite(root.percent)) bits.push(`disk ${Math.round(root.percent)}%`);
    else if (root && root.reason) bits.push(`disk: ${root.reason}`);
    return bits.join(' · ');
  }

  function strip(u, health) {
    const f = u.five_hour || {}, s = u.seven_day || {}, l = u.limits || {}, c = u.codex || {};
    const bits = [`5h ${money(f.cost_usd)}`, `7d ${money(s.cost_usd)}`];
    if (l.available) {
      const w = (k, label) => l[k] && l[k].utilization != null ? `${label} ${Math.round(l[k].utilization)}% used` : '';
      bits.push(...[w('five_hour', '5h limit'), w('seven_day', '7d limit')].filter(Boolean));
    } else if (l.reason) bits.push(`limits: ${l.reason}`);
    if (c.enabled && c.available) {
      // The plan's long window is at the top level, whatever its length.
      const wm = c.window_minutes || 10080;
      const label = wm === 10080 ? 'wk' : `${Math.round(wm / 1440)}d`;
      const codex = [
        c.five_hour && c.five_hour.utilization != null ? `5h ${Math.round(c.five_hour.utilization)}%` : '',
        c.utilization != null ? `${label} ${Math.round(c.utilization)}%` : '',
      ].filter(Boolean);
      if (codex.length) bits.push(`Codex ${codex.join(' · ')}${c.age_seconds != null ? ` (as of ${ago(c.age_seconds)})` : ''}`);
    } else if (c.enabled && c.why) bits.push(`Codex: ${c.why}`);
    const machine = machineStrip(health);
    if (machine) bits.push(machine);
    // One span per reading. Escaped first, then the figures (money, percent)
    // are set in bold: the words stay exactly what they were.
    document.getElementById('strip').innerHTML = bits.map(b =>
      `<span>${esc(b).replace(/(\$[\d,.]*\d|\d+%)/g, '<b>$1</b>')}</span>`).join('');
  }

  async function refresh() {
    const [rows, usage, restore, health] = await Promise.all([
      CC.get('/api/sessions'), CC.get('/api/usage').catch(() => null), CC.get('/api/restore').catch(() => null),
      CC.get('/api/health').catch(() => null)]);
    if (usage || health) strip(usage || {}, health);
    restoreState = restore;
    const {agents} = splitHelpers(rows);
    CC.badge('sessions', agents.filter(r => r.waiting).length);
    rowsOnPage = rows;
    renderCards();
  }

  window.CCSessions = {render: renderCards};
  CC.register('sessions', {
    mount(el) {
      sessionsEl = el;
      el.innerHTML = '<p class="empty">Loading sessions...</p>';
      // The first start reads all of Claude Code's history: about a minute on
      // 10 GB of transcripts (measured 2026-11-01). Say so rather than spin.
      setTimeout(() => {
        const p = el.querySelector('p.empty');
        if (p && p.textContent === 'Loading sessions...') {
          p.textContent = 'Reading your Claude Code history. The first start can take a minute on a long one.';
        }
      }, 4000);
      el.addEventListener('click', act);
      el.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        if (!event.target.classList || !event.target.classList.contains('scard-main')) return;
        event.preventDefault();
        toggleCard(event);
      });
    },
    refresh,
  });
})();
