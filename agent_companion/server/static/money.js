// The Money tab: read only. It shows what a money source reports and does no
// arithmetic of its own. A figure the source does not have reads as a dash,
// never $0.00, and nothing here can move money.
'use strict';
(function () {
  const {get, esc} = window.CC;
  const css = document.createElement('style');
  css.textContent = `
    .mfresh { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:center; color:var(--dim); font-size:.8rem; margin:0 0 10px; }
    .mdemo { font-weight:700; }
    .mnw .big { font-size:1.9rem; font-weight:700; line-height:1.2; font-variant-numeric:tabular-nums; }
    .mnw svg, .mflow svg { display:block; width:100%; }
    .mnw svg { height:56px; color:var(--dim); }
    .munk { color:var(--faint); }
    .mflow .inc { fill:var(--busy); } .mflow .spd { fill:var(--dim); }
    .mflow text { fill:var(--faint); font-size:10px; text-anchor:middle; }
    .mkey { display:flex; gap:14px; color:var(--dim); font-size:.8rem; }
    .mkey i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; }
    .mcat { padding:6px 0; border-top:1px solid var(--line); }
    .mcat:first-of-type { border-top:0; }
    .mbar { margin-top:4px; }`;
  document.head.appendChild(css);

  const DASH = '–';
  let el, sig = '';
  const num = v => typeof v === 'number' && Number.isFinite(v);
  const unk = () => `<span class="munk" aria-label="unknown">${DASH}</span>`;
  function money(v, cur) {
    if (!num(v)) return unk();
    let s;
    try { s = new Intl.NumberFormat('en-US', cur ? {style: 'currency', currency: cur, maximumFractionDigits: 2} : {maximumFractionDigits: 2}).format(v); }
    catch (e) { s = String(v); }
    return esc(s);
  }
  const text = v => (v == null || v === '') ? unk() : esc(v);
  const day = v => (typeof v === 'string' && v) ? esc(v.slice(0, 10)) : unk();

  function fresh(f) {
    if (!f.configured) return '';
    if (f.error) return `<p class="mfresh"><span class="needs">Source not answering: ${esc(f.error)}</span></p>`;
    return `<p class="mfresh">${f.demo ? '<span class="chip mdemo">DEMO</span> <span>Demo data, made up</span>'
      : `<span>${esc(f.source || 'source')}</span>`}
      <span>${f.as_of ? 'as of ' + esc(String(f.as_of).slice(0, 16).replace('T', ' ')) : 'age unknown'}</span></p>`;
  }

  function spark(hist) {
    const pts = (hist || []).filter(p => num(p.total));
    if (pts.length < 2) return `<p class="faint">History: ${unk()}</p>`;
    const lo = Math.min(...pts.map(p => p.total)), hi = Math.max(...pts.map(p => p.total));
    const span = hi - lo || 1;
    const xy = pts.map((p, i) => `${(i / (pts.length - 1) * 300).toFixed(1)},${(46 - (p.total - lo) / span * 40 + 3).toFixed(1)}`);
    return `<svg viewBox="0 0 300 52" preserveAspectRatio="none" role="img" aria-label="Net worth over time">
      <polyline points="${xy.join(' ')}" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
      <p class="faint">${day(pts[0].date)} to ${day(pts[pts.length - 1].date)}</p>`;
  }

  const KINDS = ['checking', 'savings', 'credit', 'loan', 'investment'];
  function accounts(list) {
    if (!list || !list.length) return '<p class="faint">No accounts.</p>';
    return KINDS.concat([...new Set(list.map(a => a.kind))].filter(k => !KINDS.includes(k))).map(k => {
      const rows = list.filter(a => a.kind === k);
      if (!rows.length) return '';
      const owed = k === 'credit' || k === 'loan';
      return `<div class="sec-head mkind">${esc(k)}${owed ? ' (owed)' : ''}</div>` + rows.map(a =>
        `<div class="row mrow"><span class="body nm">${text(a.name)}</span><span class="trail amt">${money(a.balance, a.currency)}</span></div>`).join('');
    }).join('');
  }

  function flow(rows) {
    const ok = (rows || []).filter(r => r && r.month);
    if (!ok.length) return `<p class="faint">${unk()}</p>`;
    const hi = Math.max(1, ...ok.flatMap(r => [r.income, r.spending]).filter(num));
    const W = 360, H = 120, base = 100, step = W / ok.length, bw = Math.min(16, step / 2 - 3);
    const bar = (v, x, cls) => num(v)
      ? `<rect class="${cls}" x="${x.toFixed(1)}" y="${(base - v / hi * 90).toFixed(1)}" width="${bw}" height="${Math.max(1, v / hi * 90).toFixed(1)}" rx="2"/>`
      : `<text x="${(x + bw / 2).toFixed(1)}" y="${base - 4}">${DASH}</text>`;
    const g = ok.map((r, i) => {
      const cx = step * i + step / 2;
      return bar(r.income, cx - bw - 1, 'inc') + bar(r.spending, cx + 1, 'spd')
        + `<text x="${cx.toFixed(1)}" y="${H - 6}">${esc(String(r.month).slice(2))}</text>`;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Income and spending by month">${g}</svg>
      <div class="mkey"><span><i style="background:var(--busy)"></i>Income</span><span><i style="background:var(--dim)"></i>Spending</span></div>`;
  }

  function cats(rows) {
    if (!rows || !rows.length) return '<p class="faint">Nothing spent yet this month.</p>';
    const hi = Math.max(1, ...rows.map(r => r.amount).filter(num));
    return rows.map(r => `<div class="mcat"><div class="row mrow" style="border:0;padding:0"><span class="body nm">${text(r.category)}</span>
      <span class="trail amt">${money(r.amount, 'USD')}</span></div>
      <div class="meter mbar"><i style="width:${num(r.amount) ? Math.round(r.amount / hi * 100) : 0}%"></i></div></div>`).join('');
  }

  const every = rows => rows.map(r => `<div class="row mrow"><span class="body nm">${text(r.name)}
      <span class="faint">${r.every ? esc(r.every) + ', next ' : ''}${day(r.next_date || r.date)}</span></span>
      <span class="trail amt">${money(r.amount, 'USD')}</span></div>`).join('');
  const list = (rows, none) => rows && rows.length ? every(rows) : `<p class="faint">${none}</p>`;

  const card = (title, body) => `<article class="card"><h3>${title}</h3>${body}</article>`;

  async function refresh() {
    const f = await get('/api/money/freshness');
    let html;
    if (!f.configured) {
      html = `<p class="empty">No money source set up. Set <code>money_source</code> in your config
        (<code>demo</code> shows made-up figures), and see <code>docs/money-sources.md</code> for how to connect a real one.</p>`;
    } else {
      const [a, n, c, k, r, u] = await Promise.all(['accounts', 'net_worth', 'cash_flow', 'categories', 'recurring', 'upcoming']
        .map(p => get('/api/money/' + p).catch(() => ({}))));
      const nw = n.net_worth || {};
      html = fresh(f)
        + card('Net worth', `<div class="mnw"><div class="big">${money(nw.total, 'USD')}</div>${spark(nw.history)}</div>`)
        + card('Accounts', accounts(a.accounts))
        + card('Cash flow', `<div class="mflow">${flow(c.cash_flow)}</div>`)
        + card('Spending this month', cats(k.categories))
        + card('Recurring', list(r.recurring, 'None found.'))
        + card('Coming up', list(u.upcoming, 'Nothing due soon.'))
        + '<p class="faint">Read only. Nothing here moves money.</p>';
    }
    if (html === sig) return;
    sig = html;
    el.innerHTML = html;
  }

  window.CC.register('money', {mount(section) { el = section; }, refresh});
})();
