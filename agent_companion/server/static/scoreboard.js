// Commit rework by Claude Code session. The server owns all git work and marks.
'use strict';
(function () {
  const {get, post, esc} = window.CC;
  const css = document.createElement('style');
  css.textContent = `
    .scoreboard .score-note, .scoreboard .score-meta { color:var(--dim); font-size:.86rem; margin:5px 0; }
    .scoreboard .score-periods { display:flex; gap:7px; margin:10px 0; }
    .scoreboard .score-periods button[aria-pressed="true"] { border-color:var(--fg); color:var(--fg); font-weight:600; }
    .scoreboard .score-figures { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:6px; margin:9px 0; }
    .scoreboard .score-figures span { color:var(--dim); font-size:.74rem; }
    .scoreboard .score-figures b { display:block; color:var(--fg); font-size:.94rem; font-variant-numeric:tabular-nums; }
    .scoreboard .score-worst { border-top:1px solid var(--line); margin-top:9px; padding-top:9px; } .scoreboard .score-worst p { margin:3px 0; }
    .scoreboard .score-commits { list-style:none; padding:0; margin:10px 0 0; } .scoreboard .score-commits li { border-top:1px solid var(--line); padding:8px 0; }
    .scoreboard .score-actions { display:flex; gap:6px; margin-top:6px; }
    .scoreboard .score-mark { margin-left:4px; } .scoreboard ul { margin:7px 0 0; padding-left:20px; }
    @media (max-width:360px) { .scoreboard .score-figures { grid-template-columns:repeat(2, minmax(0, 1fr)); } }`;
  document.head.appendChild(css);
  let el, data, days = '7';

  const number = value => Number.isFinite(value) ? String(value) : '0';
  const percent = value => Number.isFinite(value) ? `${value}%` : 'none';

  function commit(row) {
    const mark = row.mark ? `<span class="chip quiet score-mark">marked ${esc(row.mark)}</span>` : '';
    return `<li><strong>${esc(row.subject || row.sha.slice(0, 12))}</strong>${mark}
      <p class="score-meta">${number(row.lines_added)} added, ${number(row.lines_reworked)} reworked, ${percent(row.rework_percent)}</p>
      <div class="score-actions"><button type="button" class="act" data-mark="good" data-sha="${esc(row.sha)}">Good</button>
      <button type="button" class="act" data-mark="bad" data-sha="${esc(row.sha)}">Bad</button></div></li>`;
  }

  function card(row) {
    const worst = row.worst || {};
    const title = row.name ? `${row.name} (${row.session_id})` : row.label;
    return `<article class="card score-card"><h3>${esc(title)}</h3><div class="score-figures">
      <span><b>${number(row.commits)}</b>commits</span><span><b>${number(row.lines_added)}</b>lines added</span>
      <span><b>${number(row.lines_reworked)}</b>lines reworked</span><span><b>${percent(row.rework_percent)}</b>rework</span></div>
      <p class="score-meta">${number(row.good)} good, ${number(row.bad)} bad</p>
      <div class="score-worst"><p><strong>Most reworked</strong></p><p>${esc(worst.subject || 'No added lines')}</p>
      <p class="score-meta">${number(worst.lines_reworked)} of ${number(worst.lines_added)} lines, ${percent(worst.rework_percent)}</p></div>
      <ul class="score-commits">${(row.rows || []).map(commit).join('')}</ul></article>`;
  }

  function draw() {
    if (!el || !data) return;
    const rows = (data.windows || {})[days] || [];
    const skipped = data.skipped || [];
    el.innerHTML = `<div class="scoreboard"><h2>Scoreboard</h2><p class="score-note">Rework is an added line replaced within ${number(data.window_days)} days. It is a git history count, not a grade.</p>
      <div class="score-periods" role="group" aria-label="Period">${['7', '30'].map(value =>
        `<button type="button" class="act" data-days="${value}" aria-pressed="${value === days}">${value}d</button>`).join('')}</div>
      ${data.pending ? `<p class="score-note">${number(data.pending)} recent commits will be scored on the next refresh.</p>` : ''}
      ${rows.map(card).join('') || '<p class="empty">No recorded commits in this period.</p>'}
      ${skipped.length ? `<section class="card score-skip"><h3>Skipped (${number(data.skipped_count)})</h3><ul>${skipped.map(row =>
        `<li>${esc(row.subject || row.sha.slice(0, 12))}: ${esc(row.reason)}</li>`).join('')}</ul></section>` : ''}</div>`;
  }

  async function refresh() {
    data = await get('/api/scoreboard');
    draw();
  }

  function click(event) {
    const period = event.target.closest('[data-days]');
    if (period) { days = period.dataset.days; draw(); return; }
    const button = event.target.closest('[data-mark]');
    if (!button) return;
    button.disabled = true;
    post('/api/scoreboard/mark', {sha: button.dataset.sha, mark: button.dataset.mark})
      .then(refresh).catch(() => { button.disabled = false; });
  }

  window.CC.register('scoreboard', {mount(section) { el = section; el.addEventListener('click', click); }, refresh});
})();
