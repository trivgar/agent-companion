// The Deferred tab: a shared Markdown backlog.  The server owns validation,
// locking and writes; this page only shows grouped entries and submits a form.
'use strict';
(function () {
  const {get, post, esc} = window.CC;
  const css = document.createElement('style');
  css.textContent = `
    .deferred h2 { margin-top:0; }
    .deferred label { display:block; color:var(--dim); font-size:.82rem; margin:8px 0 3px; }
    .deferred input, .deferred textarea { display:block; margin-top:3px; }
    .deferred textarea { min-height:58px; resize:vertical; } .deferred .drow { display:flex; gap:8px; } .deferred .drow > label { flex:1 1 0; }
    .deferred button { margin-top:10px; } .deferred .dmsg { color:var(--alert); min-height:1.2em; margin:7px 0 0; font-size:.86rem; }
    .deferred ul { list-style:none; padding:0; margin:0; } .deferred li { border-top:1px solid var(--line); padding:9px 0; } .deferred li:first-child { border-top:0; padding-top:0; }
    .deferred .dhead { font-weight:650; margin:0 0 4px; } .deferred .dmeta, .deferred .danswer { color:var(--dim); font-size:.85rem; margin:3px 0 0; }
    .deferred .dstate { color:var(--busy); font-weight:650; }`;
  document.head.appendChild(css);
  let el;

  function item(entry) {
    const details = [entry.branch && `branch ${entry.branch}`, entry.by && `parked by ${entry.by}`].filter(Boolean).join(', ');
    const target = entry.for ? `For ${entry.for}.` : '';
    const when = entry.when ? `Pick up when: ${entry.when}.` : '';
    const answer = entry.state === 'answered' ? `<p class="danswer"><span class="dstate">Answered.</span> ${esc(entry.answer || '')}</p>` : '';
    return `<li><p class="dhead">${esc(entry.header)}</p><p>${esc(entry.ask || '')}</p>
      ${target || when ? `<p class="dmeta">${esc([target, when].filter(Boolean).join(' '))}</p>` : ''}
      ${details ? `<p class="dmeta">${esc(details)}</p>` : ''}${answer}</li>`;
  }

  function draw(entries) {
    const groups = new Map();
    entries.forEach(entry => {
      const project = entry.project || 'Other project';
      if (!groups.has(project)) groups.set(project, []);
      groups.get(project).push(entry);
    });
    const rows = [...groups.entries()].map(([project, group]) => `<section class="card dgroup"><h3>${esc(project)}</h3><ul>${group.map(item).join('')}</ul></section>`).join('');
    el.innerHTML = `<div class="deferred"><section class="card dadd"><h2>Park work for later</h2>
      <label>What needs doing<textarea id="deferred-text" maxlength="1500" required></textarea></label>
      <div class="drow"><label>For project (optional)<input type="text" id="deferred-for" maxlength="120"></label>
      <label>Pick up when (optional)<input type="text" id="deferred-when" maxlength="200"></label></div>
      <button id="deferred-add" class="act primary" type="button">Add</button><p id="deferred-msg" class="dmsg" role="status"></p></section>
      ${rows || '<p class="empty">Nothing is parked yet.</p>'}</div>`;
    el.querySelector('#deferred-add').addEventListener('click', submit);
  }

  async function submit() {
    const text = el.querySelector('#deferred-text').value;
    const forProject = el.querySelector('#deferred-for').value;
    const when = el.querySelector('#deferred-when').value;
    const message = el.querySelector('#deferred-msg');
    message.textContent = '';
    try {
      await post('/api/deferred', {text, for: forProject, when});
      await refresh();
    } catch (err) { message.textContent = err.message || 'Could not add that entry.'; }
  }

  async function refresh() {
    try { draw((await get('/api/deferred')).entries || []); }
    catch (err) { el.innerHTML = `<p class="empty">${esc(err.message || 'Could not read deferred work.')}</p>`; }
  }

  window.CC.register('deferred', {mount(section) { el = section; }, refresh});
})();
