// A session card as an ordered list of parts. Each part is (row, ctx) => html.
// `frame` opens the article and the tappable main area, `more` closes the main
// area and adds the details when the card is open, `close` ends the article.
// A feature that adds to the card adds a part and puts it in ORDER.
//
// ctx, built by sessions.js for each row:
//   {key, name, hue, open, unread, actions, result, jobs, cache, ago, money, short}
// where actions, result, jobs and cache are html sessions.js already builds,
// and ago, money and short are its formatters.
'use strict';
(function () {
  const esc = value => CC.esc(value);
  const WORDS = {waiting: 'waiting', busy: 'working', idle: 'idle'};

  const stateOf = row => row.waiting ? 'waiting' : row.status === 'busy' ? 'busy' : 'idle';
  const pctOf = row => Math.max(0, Math.min(100, Number(row.context_pct) || 0));

  function group(row) {
    if (row.waiting) return 'needs';
    return row.status === 'busy' ? 'working' : 'idle';
  }

  const PARTS = {
    frame(row, ctx) {
      const classes = ['card', 'scard', stateOf(row)];
      if (ctx.open) classes.push('open');
      if (ctx.unread) classes.push('unread');
      if (row.child) classes.push('child');
      const chat = Boolean(window.CCChat) && !row.child;
      const aria = chat
        ? `aria-label="Chat with ${esc(ctx.name)}"`
        : `aria-expanded="${ctx.open ? 'true' : 'false'}"`;
      return `<article class="${classes.join(' ')}" data-pid="${esc(row.pid)}" data-card-key="${esc(ctx.key)}" style="--h:${esc(ctx.hue)}">`
        + `<div class="scard-main" role="button" tabindex="0" ${aria}>`
        + window.CCSprite.html(ctx.name, row);
    },

    top(row, ctx) {
      const flags = (row.waiting ? '<span class="wbadge">needs you</span>' : '')
        + (ctx.unread ? '<span class="newbadge">new</span>' : '')
        + (row.needs_clear ? '<span class="clearbadge">needs /clear</span>' : '')
        + (row.child ? '<span class="faint">spawned</span>' : '');
      return `<div class="top"><span class="dot${row.status === 'busy' ? ' busy' : ''}"></span><h3>${esc(ctx.name)}</h3>${flags}`
        + `<span class="proj">${esc(row.project)}</span></div>`;
    },

    said(row, ctx) {
      if (!row.last_reply) return '';
      const old = Number(row.last_activity) > 900;
      return `<p class="said">${esc(row.last_reply)}${old ? `<span class="when"> ${esc(ctx.ago(row.last_activity))}</span>` : ''}</p>`;
    },

    more(row, ctx) {
      const toggle = `<button type="button" class="scard-toggle" aria-expanded="${ctx.open ? 'true' : 'false'}" aria-label="Details for ${esc(ctx.name)}"></button>`;
      if (!ctx.open) return `</div>${toggle}`;
      const pct = pctOf(row);
      const state = WORDS[stateOf(row)];
      const bits = [
        row.branch ? `<span class="brchip">${esc(row.branch)}</span>` : '',
        `<span class="mdl">${esc(ctx.short(row.model))}</span>`,
        `<span>${esc(ctx.money(row.cost_usd))}</span>`,
        `<span>${esc(state)}${row.last_activity == null ? '' : ` · ${esc(ctx.ago(row.last_activity))}`}</span>`,
        pct >= 70 ? `<span class="ctx">context ${esc(Math.round(pct))}%</span>` : '',
        ctx.cache,
      ].filter(Boolean).join('');
      return `</div>${toggle}<div class="scard-more">`
        + `<div class="meta">${bits}</div>`
        + (row.title ? `<div class="dim">${esc(row.title)}</div>` : '')
        + ctx.jobs
        + `<div class="bar${pct >= 80 ? ' hot' : ''}"><i style="width:${pct}%"></i></div>`
        + ctx.actions + ctx.result
        + '</div>';
    },

    close() { return '</article>'; },
  };

  const ORDER = ['frame', 'top', 'said', 'more', 'close'];

  function html(row, ctx) {
    return ORDER.map(part => PARTS[part](row, ctx)).join('');
  }

  window.CCCard = {PARTS, ORDER, group, html};
})();
