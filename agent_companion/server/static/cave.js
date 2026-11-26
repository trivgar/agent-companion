// Direct chat with a running session. The page talks only to /api/cave so the
// server has one full contract for session windows, sends and attachments.
'use strict';
(function () {
  const esc = CC.esc;
  const identityArt = window.CCIdentity;
  let el = null, who = '', agents = [], files = [];
  const PICKER_ARM_MS = 4000;
  let armedPicker = null;
  try { who = localStorage.getItem('cc-chat-to') || ''; } catch (e) {}

  function disarmPicker() {
    if (!armedPicker) return;
    clearTimeout(armedPicker.timer);
    const {button, was} = armedPicker;
    armedPicker = null;
    if (button.isConnected) {
      button.removeAttribute('data-armed');
      button.textContent = was;
    }
  }

  function pickerTap(button) {
    const match = /^\/(?:model|effort)\s+(.+)$/.exec(button.dataset.command || '');
    if (!match) return true;
    if (armedPicker && armedPicker.button === button && button.isConnected) {
      disarmPicker();
      return true;
    }
    disarmPicker();
    armedPicker = {button, was: button.textContent, timer: setTimeout(disarmPicker, PICKER_ARM_MS)};
    button.setAttribute('data-armed', '1');
    button.textContent = `Tap again to switch to ${match[1]}`;
    return false;
  }

  document.addEventListener('click', event => {
    if (armedPicker && !armedPicker.button.contains(event.target)) disarmPicker();
  }, true);

  const time = at => new Date(at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const fileNames = m => m.files && m.files.length ? `\nDelivered: ${m.files.map(f => esc(f.name || 'file')).join(', ')}` : '';
  function line(m) {
    if (m.role === 'work') return `<div class="msg work">${esc((m.tools || []).join(', '))}${m.last ? ' · ' + esc(m.last) : ''}</div>`;
    if (m.role === 'asked') {
      const pick = m.answer ? `Answered: ${esc(m.answer)}` : (m.closed ? 'Closed without an answer' : 'Waiting on you in Asks');
      return `<div class="msg asked">Asked: ${esc(m.text)}${m.options && m.options.length ? ' (' + esc(m.options.join(' / ')) + ')' : ''}<span class="by">${pick}</span></div>`;
    }
    const by = m.role === 'me' ? (m.to ? 'to ' + esc(m.to) : 'you') : esc(m.from || who);
    const bad = m.role === 'me' && m.ok === false ? ' bad' : '';
    const note = bad ? `<span class="by">Not sent: ${esc(m.detail || 'refused')}</span>` : '';
    return `<div class="msg ${esc(m.role)}${bad}"><span class="by">${by} · ${esc(time(m.at))}</span>${esc(m.text)}${fileNames(m)}${note}</div>`;
  }

  function shell() {
    el.innerHTML = `
      <div id="chat-title" hidden></div>
      <div id="chat-bar"><label class="dim" for="chat-to">Talk to</label><select id="chat-to"></select>
      <button type="button" class="act" id="chat-stop">Stop it</button>
      <button type="button" class="act" id="chat-resume" hidden>Resume</button>
      <button type="button" class="act" id="chat-end">End</button>
      <button type="button" class="act" id="chat-clear" hidden>Clear</button>
      <button type="button" class="act" id="chat-compact" hidden>Compact</button>
      <label class="dim"><input type="checkbox" id="chat-win"> its whole window</label></div>
      <div id="chat-controls"></div><div id="chat-asks"></div>
      <div id="chat-log" aria-live="polite"></div>
      <form id="chat-send"><input type="file" id="chat-file" multiple hidden
        accept="image/*,application/pdf,text/plain,text/csv,text/markdown,application/json">
        <textarea id="chat-text" rows="2" maxlength="4000" placeholder="Say something" aria-label="Message"></textarea>
        <button type="submit" class="act primary">Send</button><button type="button" class="act" id="chat-attach">Attach</button>
        <span id="chat-files"></span></form><p class="faint" id="chat-err" role="status"></p>`;
    el.querySelector('#chat-to').addEventListener('change', e => {
      who = e.target.value;
      try { localStorage.setItem('cc-chat-to', who); } catch (x) {}
      refresh();
    });
    el.querySelector('#chat-win').addEventListener('change', refresh);
    el.querySelector('#chat-stop').addEventListener('click', async () => {
      const err = el.querySelector('#chat-err');
      if (!who) { err.textContent = 'Pick a session first.'; return; }
      try { const r = await CC.post(`/api/cave/stop/${encodeURIComponent(who)}`, {}); err.textContent = r.ok ? 'Stopped.' : 'Not stopped: ' + r.detail; }
      catch (e) { err.textContent = 'Not stopped: ' + e.message; }
    });
    el.querySelector('#chat-resume').addEventListener('click', () => cardAction('resume'));
    el.querySelector('#chat-end').addEventListener('click', () => cardAction('end'));
    el.querySelector('#chat-clear').addEventListener('click', () => cardAction('clear'));
    el.querySelector('#chat-compact').addEventListener('click', () => cardAction('compact'));
    el.querySelector('#chat-attach').addEventListener('click', () => el.querySelector('#chat-file').click());
    el.querySelector('#chat-file').addEventListener('change', e => {
      files = [...files, ...e.target.files].slice(0, 6);
      e.target.value = '';
      el.querySelector('#chat-files').textContent = files.map(f => f.name).join(', ');
    });
    el.addEventListener('click', actOnChat);
    el.querySelector('#chat-send').addEventListener('submit', send);
  }

  function selectedAgent() {
    return agents.find(agent => agent.name === who) || null;
  }

  function drawTitle() {
    const title = el.querySelector('#chat-title');
    const target = selectedAgent();
    if (!target) { title.hidden = true; title.className = ''; title.textContent = ''; return; }
    const name = target.name || target.project || 'session';
    title.hidden = false;
    if (el.classList.contains('sheet')) {
      const state = target.waiting ? 'waiting' : target.status === 'busy' ? 'working' : 'idle';
      const sub = [state, target.project].filter(Boolean).map(esc).join(' \u00b7 ');
      title.className = 'shead';
      title.innerHTML = `${window.CCSprite.html(name, target)}<div><h2>${esc(name)}</h2><div class="sub">${sub}</div></div>`;
      return;
    }
    title.className = '';
    title.innerHTML = `<h2 class="identity-heading">${identityArt.sprite(name)}<span>Chat with ${esc(name)}</span></h2>`;
  }

  function identity(action) {
    const target = selectedAgent();
    const err = el.querySelector('#chat-err');
    if (!target) {
      err.textContent = `Not ${action}: pick a session first.`;
      return null;
    }
    if (!Number.isInteger(target.pid) || !target.session_id) {
      err.textContent = `Not ${action}: this session's details are unavailable. Refresh the page.`;
      return null;
    }
    return target;
  }

  function setActionVisibility() {
    const target = selectedAgent();
    const idle = Boolean(target && target.status === 'idle' && !target.waiting);
    el.querySelector('#chat-resume').hidden = !idle;
    el.querySelector('#chat-clear').hidden = !idle;
    el.querySelector('#chat-compact').hidden = !idle;
  }

  async function cardAction(action) {
    const labels = {resume: 'resumed', end: 'ended', clear: 'cleared', compact: 'compacted'};
    const target = identity(labels[action]);
    if (!target) return;
    if (action === 'end' && !confirm(`End ${target.name}? Its window closes.`)) return;
    if (action === 'clear' && !confirm(`Clear ${target.name}? This wipes the session's context.`)) return;
    const err = el.querySelector('#chat-err');
    try {
      let result;
      if (action === 'resume' || action === 'end') {
        result = await CC.post(`/api/cave/${action}/${encodeURIComponent(target.name)}`,
          {pid: target.pid, session_id: target.session_id});
      } else {
        result = await CC.post('/api/cave/send', {to: target.name, text: action === 'clear' ? '/clear' : '/compact'});
      }
      err.textContent = result.ok ? labels[action][0].toUpperCase() + labels[action].slice(1) + '.' :
        `Not ${labels[action]}: ${result.detail || 'refused'}`;
    } catch (e) {
      err.textContent = `Not ${labels[action]}: ${e.message}`;
    }
    await refresh();
  }

  function askCards(asks) {
    return (asks || []).map(a => {
      if (a.questions && a.questions.length) return `<div class="card" data-cave-ask="${esc(a.id)}"><h3>Question</h3>${a.questions.map(q =>
        `<label>${esc(q.question)}<select data-question="${esc(q.question)}"><option value="">Choose</option>${(q.options || []).map(o =>
          `<option value="${esc(o.label)}">${esc(o.label)}</option>`).join('')}</select></label>`).join('')}<p><button class="act primary" data-answer>Send answers</button></p></div>`;
      return `<div class="card" data-cave-ask="${esc(a.id)}"><h3>Needs your OK</h3><p><b>${esc(a.tool || '')}</b> <code>${esc(a.what || '')}</code></p><p>
        <button class="act primary" data-decide="allow">Allow</button><button class="act" data-decide="deny">Deny</button></p></div>`;
    }).join('');
  }

  // A numbered choice the terminal drew without a corresponding ask.  The
  // server marks choices this page may safely press; the rest stay terminal-only.
  function cvDialog(dialog) {
    const buttons = dialog.options.filter(option => option.ok).map(option =>
      `<button type="button" data-cvdialog="${option.n}" data-label="${esc(option.label)}">${esc(option.label)}</button>`).join('');
    const terminalOnly = dialog.options.filter(option => !option.ok).map(option => esc(option.label));
    const why = dialog.multi ? 'Several can be selected: answer it in the terminal.'
      : dialog.cut ? 'The top of its screen is cut off, so affirmative choices stay in the terminal.' : '';
    return `<div class="cv-ask cv-term" data-dialog="${esc(dialog.key)}"><div class="cv-askhead">Waiting in its terminal</div>
      <pre class="cv-termq">${esc(dialog.question)}</pre>${buttons ? `<div class="cv-askbtns">${buttons}</div>` : ''}${
      why || terminalOnly.length ? `<span class="cv-meta">${why ? why + ' ' : ''}${terminalOnly.length ? `Terminal only: ${terminalOnly.join(' · ')}` : ''}</span>` : ''}</div>`;
  }

  function controls(data) {
    const suggested = data.suggestion ? `<button type="button" class="act" data-suggestion="${esc(data.suggestion)}">Use suggestion</button>` : '';
    const models = (data.models || []).map(model => `<button type="button" class="act" data-command="/model ${esc(model)}">${esc(model)}</button>`).join('');
    const efforts = (data.efforts || []).map(level => `<button type="button" class="act" data-command="/effort ${esc(level)}">${esc(level)}</button>`).join('');
    return suggested || models || efforts ? `<p class="faint">${suggested}${models}${efforts}</p>` : '';
  }

  async function actOnChat(event) {
    const dialogButton = event.target.closest('[data-cvdialog]');
    if (dialogButton) {
      const card = dialogButton.closest('.cv-term');
      for (const button of card.querySelectorAll('button')) button.disabled = true;
      try {
        const result = await CC.post(`/api/chat/dialog/${encodeURIComponent(who)}`, {
          key: card.dataset.dialog, n: Number(dialogButton.dataset.cvdialog), label: dialogButton.dataset.label});
        if (!result.ok) throw new Error(result.detail || 'the choice was not accepted');
        card.classList.add('done');
      } catch (error) {
        const note = document.createElement('span');
        note.className = 'cv-meta';
        note.textContent = 'Not sent: ' + error.message;
        card.appendChild(note);
        for (const button of card.querySelectorAll('button')) button.disabled = false;
      }
      await refresh();
      return;
    }
    const suggestion = event.target.closest('[data-suggestion]');
    if (suggestion) { el.querySelector('#chat-text').value = suggestion.dataset.suggestion; el.querySelector('#chat-text').focus(); return; }
    const command = event.target.closest('[data-command]');
    if (command) {
      if (!pickerTap(command)) return;
      el.querySelector('#chat-text').value = command.dataset.command;
      await send(new Event('submit'));
      return;
    }
    const card = event.target.closest('[data-cave-ask]');
    if (!card) return;
    const id = card.dataset.caveAsk;
    const decision = event.target.closest('[data-decide]');
    try {
      if (decision) await CC.post(`/api/cave/ask/${encodeURIComponent(id)}`, {decision: decision.dataset.decide});
      const answer = event.target.closest('[data-answer]');
      if (answer) {
        const answers = {};
        for (const select of card.querySelectorAll('select[data-question]')) {
          if (!select.value) throw new Error('Choose an answer for every question.');
          answers[select.dataset.question] = select.value;
        }
        await CC.post(`/api/cave/ask/${encodeURIComponent(id)}/answer`, {answers});
      }
      await refresh();
    } catch (error) { el.querySelector('#chat-err').textContent = 'Not sent: ' + error.message; }
  }

  const base64 = file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(new Error('could not read the file'));
    reader.readAsDataURL(file);
  });
  async function send(event) {
    event.preventDefault();
    const box = el.querySelector('#chat-text'), err = el.querySelector('#chat-err');
    let text = box.value.trim();
    if (!text && !files.length) return;
    err.textContent = '';
    try {
      const paths = [];
      for (const file of files) {
        const upload = await CC.post('/api/cave/upload', {type: file.type, data: await base64(file)});
        paths.push(`[${file.type.startsWith('image/') ? 'photo' : 'file'}: ${upload.path}]`);
      }
      if (paths.length) text = [text || 'Please review these attachments.', ...paths].join(' ');
      const r = await CC.post('/api/cave/send', {to: who, text});
      if (r.ok) { box.value = ''; files = []; el.querySelector('#chat-files').textContent = ''; }
      else err.textContent = 'Not sent: ' + r.detail;
    } catch (e) { err.textContent = 'Not sent: ' + e.message; }
    refresh();
  }

  async function refresh() {
    if (!el) return;
    if (!el.querySelector('#chat-log')) shell();
    const data = await CC.get('/api/cave');
    agents = data.agents || [];
    const names = agents.map(a => a.name);
    if (!names.includes(who)) who = names.length === 1 ? names[0] : '';
    if (who) el.style.setProperty('--h', String(identityArt.hue(who)));
    else el.style.removeProperty('--h');
    const select = el.querySelector('#chat-to');
    if (document.activeElement !== select) select.innerHTML = `<option value="">${names.length ? 'pick a session' : 'no session reachable'}</option>` +
      agents.map(a => `<option value="${esc(a.name)}"${a.name === who ? ' selected' : ''}>${esc(a.name)}${a.waiting ? ' (waiting)' : ''}${a.project ? ' · ' + esc(a.project) : ''}</option>`).join('');
    drawTitle();
    setActionVisibility();
    const log = el.querySelector('#chat-log');
    let messages = data.messages || [];
    if (who && el.querySelector('#chat-win').checked) {
      try {
        const windowData = await CC.get(`/api/cave/chat/${encodeURIComponent(who)}`);
        messages = windowData.messages || [];
        el.querySelector('#chat-asks').innerHTML = askCards(windowData.asks) + (windowData.dialog ? cvDialog(windowData.dialog) : '');
        el.querySelector('#chat-controls').innerHTML = controls(windowData);
        if (windowData.suggestion && !el.querySelector('#chat-text').value) el.querySelector('#chat-text').placeholder = windowData.suggestion;
        await CC.post(`/api/cave/seen/${encodeURIComponent(who)}`, {});
      } catch (e) { messages = []; el.querySelector('#chat-asks').innerHTML = ''; el.querySelector('#chat-controls').innerHTML = ''; }
    }
    const inSheet = el.classList.contains('sheet');
    const atBottom = inSheet
      ? el.clientHeight + el.scrollTop >= el.scrollHeight - 80
      : innerHeight + scrollY >= document.body.scrollHeight - 80;
    log.innerHTML = messages.length ? messages.map(line).join('') : '<p class="empty">Nothing said yet.</p>';
    if (atBottom) {
      if (inSheet) el.scrollTo(0, el.scrollHeight);
      else scrollTo(0, document.body.scrollHeight);
    }
  }

  function open(name) {
    who = String(name == null ? '' : name);
    try { localStorage.setItem('cc-chat-to', who); } catch (x) {}
    if (el) {
      if (!el.querySelector('#chat-log')) shell();
      el.querySelector('#chat-win').checked = true;
    }
    return CC.sheet('chat');
  }

  window.CCChat = {open};
  CC.register('chat', {mount: root => { el = root; shell(); }, refresh});
})();
