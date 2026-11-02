// New Session: a small sheet owned by the Sessions view.
'use strict';
(function () {
  const esc = CC.esc;
  let sheet = null;
  const css = document.createElement('style');
  css.textContent = `
    .new-session-backdrop { position:fixed; inset:0; z-index:20; display:flex; align-items:end; background:color-mix(in srgb, var(--fg) 50%, transparent); }
    .new-session-sheet { width:100%; max-width:760px; margin:0 auto; padding:16px; border-radius:14px 14px 0 0; background:var(--raised); box-shadow:0 -8px 28px color-mix(in srgb, var(--fg) 25%, transparent); }
    .new-session-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
    .new-session-head h2 { margin:0; font-size:1.1rem; }
    .new-session-form label { display:block; margin:12px 0 0; font-weight:600; }
    .new-session-form select { width:100%; min-height:44px; margin-top:4px; padding:8px; font:inherit; color:var(--fg); background:var(--sunk); border:1px solid var(--line); border-radius:8px; }
    .new-session-form textarea { display:block; margin-top:4px; }
    .new-session-result { min-height:1.45em; margin:12px 0; color:var(--dim); overflow-wrap:anywhere; }`;
  document.head.appendChild(css);

  function close() {
    if (sheet) sheet.remove();
    sheet = null;
  }

  function render(data, message) {
    const projects = Array.isArray(data.projects) ? data.projects : [];
    const options = projects.map(project => `<option value="${esc(project.key)}"${project.available ? '' : ' disabled'}>${esc(project.label)}${project.available ? '' : ` (${esc(project.reason || 'Unavailable')})`}</option>`).join('');
    sheet.innerHTML = `<div class="new-session-sheet" role="dialog" aria-modal="true" aria-labelledby="new-session-title">
      <form class="new-session-form"><div class="new-session-head"><h2 id="new-session-title">New session</h2><button type="button" class="act" data-new-session-close>Close</button></div>
      <label>Project<select name="project" required>${options}</select></label>
      <p class="faint" data-new-session-note></p>
      ${data.default_model ? `<p class="faint">Model: ${esc(data.default_model)}</p>` : ''}
      <label>First message <span class="faint">(optional)</span><textarea name="message" rows="4" maxlength="4000"></textarea></label>
      <p class="new-session-result" role="status">${esc(message || '')}</p>
      <button class="act primary" type="submit" ${projects.some(p => p.available) ? '' : ' disabled'}>Start Claude</button></form></div>`;
    const form = sheet.querySelector('form');
    // Who already works in the chosen project: a second session is allowed.
    const note = () => {
      const chosen = projects.find(project => project.key === form.project.value);
      sheet.querySelector('[data-new-session-note]').textContent = chosen ? chosen.note : '';
    };
    form.project.addEventListener('change', note);
    note();
    form.addEventListener('submit', submit);
    sheet.querySelector('[data-new-session-close]').addEventListener('click', close);
  }

  async function submit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const result = await CC.post('/api/newsession', {project: form.project.value, message: form.message.value});
      render({projects: [], default_model: ''}, `Started ${result.tmux}.`);
    } catch (error) {
      button.disabled = false;
      form.querySelector('.new-session-result').textContent = `Not started: ${error.message}`;
    }
  }

  async function open() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.className = 'new-session-backdrop';
    document.body.appendChild(sheet);
    sheet.addEventListener('click', event => { if (event.target === sheet) close(); });
    try {
      render(await CC.get('/api/newsession/projects'), '');
      sheet.querySelector('select:not([disabled])')?.focus();
    } catch (error) {
      render({projects: [], default_model: ''}, `Projects are unavailable: ${error.message}`);
    }
  }

  window.CCNewSession = {open};
})();
