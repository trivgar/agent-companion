// The Fitness tab: sleep first (night by night and running), work nights against
// nights off when configured, resting heart rate, steps and distance, weight.
// The server does every sum (agent_companion/fitness/summary.py); this says them.
// A night nothing was recorded for is "not recorded", a gap, never zero, and the
// page says how many nights were recorded.
'use strict';
(function () {
  const {get, esc} = window.CC;
  const css = document.createElement('style');
  css.textContent = `
    .fit .fbig { font-size:1.6rem; font-weight:700; font-variant-numeric:tabular-nums; margin:0; }
    .fit .fshort.bad { color:var(--fg); font-weight:600; } .fit .fshort.good { color:var(--busy); }
    .fit .fline { margin:6px 0; } .fit .fnote, .fit .fd { color:var(--faint); font-size:.8rem; margin:6px 0 0; }
    .fit .fd.up { color:var(--busy); } .fit .fd.down { color:var(--dim); }
    .fit .favgs { display:flex; gap:12px; flex-wrap:wrap; }
    .fit .favgs > div { flex:1 1 90px; min-width:0; }
    .fit .fk { display:block; color:var(--dim); font-size:.8rem; } .fit .fv { display:block; font-weight:700; font-variant-numeric:tabular-nums; }
    .fit dl.fstats { display:flex; flex-wrap:wrap; gap:6px 18px; margin:8px 0 0; }
    .fit dl.fstats dt { color:var(--faint); font-size:.75rem; } .fit dl.fstats dd { margin:0; font-weight:600; }
    .fit .demo { font-weight:700; letter-spacing:.04em; margin-bottom:8px; }
    .fit svg.fchart { display:block; width:100%; height:auto; margin-top:8px; }
    .fit .fbar { fill:var(--dim); } .fit .fok { fill:var(--busy); } .fit .fgap { fill:var(--line-strong); }
    .fit .fgoal { stroke:var(--faint); stroke-dasharray:3 3; stroke-width:1; }
    .fit .favg, .fit .fline-p { fill:none; stroke:var(--fg); stroke-width:1.6; }
    .fit .fax { fill:var(--faint); font-size:9px; }`;
  document.head.appendChild(css);
  let el;

  const DAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const N = n => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const HM = m => {
    if (m == null) return '';
    const t = Math.round(Math.abs(m)), h = Math.floor(t / 60), mm = t % 60;
    return h ? (mm ? `${h}h ${mm}m` : `${h}h`) : `${mm}m`;
  };
  const date = (iso, day = true) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    if (!m) return '';
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return `${day ? DAY[d.getDay()] + ' ' : ''}${MON[d.getMonth()]} ${d.getDate()}`;
  };
  const versus = (m, goal) => m == null ? '' : m < 0 ? `${HM(m)} short of ${HM(goal)}` : m > 0 ? `${HM(m)} over ${HM(goal)}` : `right on ${HM(goal)}`;
  const debt = m => m == null ? 'no nights recorded' : m < 0 ? `${HM(m)} short` : m > 0 ? `${HM(m)} ahead` : 'even';
  const change = (cur, prev, n) => {
    if (cur == null || prev == null) return '<span class="fd">nothing to compare yet</span>';
    const d = cur - prev;
    if (Math.abs(d) < 1) return `<span class="fd">same as the ${n} before</span>`;
    return `<span class="fd ${d > 0 ? 'up' : 'down'}">${d > 0 ? 'up' : 'down'} ${HM(d)} on the ${n} before</span>`;
  };

  const W = 360;
  function sleepChart(chart, goal) {
    const H = 118, top = 6, bottom = 14, n = chart.length || 1, max = 600;
    const y = m => top + (H - top - bottom) * (1 - Math.min(m, max) / max);
    const bw = W / n;
    const bars = chart.map((c, i) => {
      const x = (i * bw).toFixed(2), w = Math.max(bw * .7, .8).toFixed(2);
      if (c.asleep == null) return `<rect class="fgap" x="${x}" y="${H - bottom - 1.5}" width="${w}" height="1.5"/>`;
      const t = y(c.asleep);
      return `<rect class="${c.asleep >= goal ? 'fok' : 'fbar'}" x="${x}" y="${t.toFixed(1)}" width="${w}" height="${(H - bottom - t).toFixed(1)}"/>`;
    }).join('');
    const lines = [];
    let run = [];
    for (const p of chart.map((c, i) => c.avg7 == null ? null : `${(i * bw + bw * .35).toFixed(1)},${y(c.avg7).toFixed(1)}`).concat([null])) {
      if (p) run.push(p);
      else if (run.length) { lines.push(`<polyline class="favg" points="${run.join(' ')}"/>`); run = []; }
    }
    const gy = y(goal).toFixed(1), g = Math.round(goal / 60 * 10) / 10;
    return `<svg class="fchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Time asleep, night by night, with the goal and the 7-night average">
      ${bars}<line class="fgoal" x1="0" x2="${W}" y1="${gy}" y2="${gy}"/>${lines.join('')}
      <text class="fax" x="${W - 2}" y="${(+gy - 3).toFixed(1)}" text-anchor="end">${g}h</text>
      <text class="fax" x="0" y="${H - 2}">${esc(date(chart[0] && chart[0].date, false))}</text>
      <text class="fax" x="${W}" y="${H - 2}" text-anchor="end">${esc(date(chart[chart.length - 1] && chart[chart.length - 1].date, false))}</text></svg>`;
  }

  function lineChart(points, label, digits) {
    const H = 92, top = 10, bottom = 14;
    const vals = points.map(p => p.v).filter(v => v != null);
    if (!vals.length) return '<p class="fnote">Nothing recorded in this window.</p>';
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 2) { lo -= 1; hi += 1; }
    const y = v => top + (H - top - bottom) * (1 - (v - lo) / (hi - lo));
    const step = W / Math.max(points.length - 1, 1);
    const lines = [], dots = [];
    let run = [];
    points.forEach((p, i) => {
      if (p.v != null) run.push([(i * step).toFixed(1), y(p.v).toFixed(1)]);
      if ((p.v == null || i === points.length - 1) && run.length) {
        if (run.length > 1) lines.push(`<polyline class="fline-p" points="${run.map(r => r.join(',')).join(' ')}"/>`);
        else dots.push(`<circle cx="${run[0][0]}" cy="${run[0][1]}" r="1.8" fill="var(--fg)"/>`);
        run = [];
      }
    });
    return `<svg class="fchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
      ${lines.join('')}${dots.join('')}
      <text class="fax" x="0" y="${top - 2}">${esc(hi.toFixed(digits))}</text>
      <text class="fax" x="0" y="${H - bottom + 10}">${esc(lo.toFixed(digits))}</text>
      <text class="fax" x="${W}" y="${H - 2}" text-anchor="end">${esc(date(points[points.length - 1].date, false))}</text></svg>`;
  }

  function stepsChart(days) {
    const H = 84, top = 4, bottom = 14;
    const max = Math.max(1, ...days.map(d => d.steps || 0));
    const bw = W / Math.max(days.length, 1);
    return `<svg class="fchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Steps, the last 7 days">
      ${days.map((d, i) => {
        const h = (H - top - bottom) * ((d.steps || 0) / max);
        const x = i * bw + bw * .18;
        return `<rect class="${d.steps == null ? 'fgap' : 'fbar'}" x="${x.toFixed(1)}" y="${(H - bottom - Math.max(h, 1.5)).toFixed(1)}" width="${(bw * .64).toFixed(1)}" height="${Math.max(h, 1.5).toFixed(1)}"/>
          <text class="fax" x="${(x + bw * .32).toFixed(1)}" y="${H - 2}" text-anchor="middle">${esc(date(d.date).slice(0, 1))}</text>`;
      }).join('')}</svg>`;
  }

  function night(s) {
    const goal = s.goal_min, last = s.last, prior = s.last_recorded;
    if (!last || !last.recorded) {
      return `<section class="card fsec"><h3>Last night</h3><p class="fbig">Not recorded</p>
        <p class="fshort">${prior ? esc(`Nothing was recorded for the latest night. The last recorded night, ${date(prior.date)}: ${HM(prior.asleep)} asleep.`)
          : 'No night has been recorded yet.'}</p></section>`;
    }
    return `<section class="card fsec"><h3>Last night</h3>
      <p class="fbig">${esc(HM(last.asleep))} asleep</p>
      <p class="fshort ${last.short < 0 ? 'bad' : 'good'}">${esc(versus(last.short, goal))}</p>
      <p class="fnote">The morning of ${esc(date(last.date))}.</p></section>`;
  }

  function running(s) {
    const a = s.avg;
    const avg = n => `<div><span class="fk">${n} nights</span><span class="fv">${esc(a[n].min == null ? 'none recorded' : HM(a[n].min))}</span>
      ${change(a[n].min, a[n].prev, n)}</div>`;
    return `<section class="card fsec"><h3>Running</h3>
      <div class="favgs">${avg('7')}${avg('30')}${avg('90')}</div>
      <p class="fline">Against ${esc(HM(s.goal_min))}: <b>${esc(debt(s.debt['7'].min))}</b> over the last 7 nights,
        <b>${esc(debt(s.debt['30'].min))}</b> over the last 30.</p>
      <p class="fline">On target: <b>${esc(s.on_target['7'].hit)} of ${esc(s.on_target['7'].nights)}</b> recorded nights in the last 7,
        <b>${esc(s.on_target['30'].hit)} of ${esc(s.on_target['30'].nights)}</b> in the last 30.</p>
      ${sleepChart(s.chart, s.goal_min)}
      <p class="fnote">${esc(`${s.recorded.nights} of ${s.recorded.of} nights recorded. A night with nothing recorded is left out, never counted as zero. The line is the 7-night average.`)}</p></section>`;
  }

  function work(w) {
    if (!w) return '';
    const diff = w.work.nights && w.off.nights ? w.work.min - w.off.min : null;
    const nn = x => `${x} night${x === 1 ? '' : 's'}`;
    return `<section class="card fsec"><h3>Work nights and nights off</h3>
      <div class="favgs"><div><span class="fk">Before work</span><span class="fv">${esc(w.work.min == null ? 'none yet' : HM(w.work.min))}</span>
        <span class="fd">${esc(nn(w.work.nights))}</span></div>
      <div><span class="fk">Nights off</span><span class="fv">${esc(w.off.min == null ? 'none yet' : HM(w.off.min))}</span>
        <span class="fd">${esc(nn(w.off.nights))}</span></div></div>
      ${diff != null && Math.abs(diff) >= 1 ? `<p class="fline"><b>${esc(HM(diff))} ${diff < 0 ? 'less' : 'more'}</b> before work.</p>` : ''}</section>`;
  }

  function heart(h) {
    const now = h.latest == null ? '<b>not recorded</b>'
      : `<b>${esc(`${Math.round(h.latest)} bpm`)}</b> on ${esc(date(h.date))}`;
    return `<section class="card fsec"><h3>Resting heart rate</h3>
      <p class="fline">${now}${h.avg30 != null ? `, <b>${esc(h.avg30.toFixed(1))}</b> average over 30 days` : ''}.</p>
      ${lineChart(h.chart, 'Resting heart rate', 0)}</section>`;
  }

  function steps(st, u) {
    return `<section class="card fsec"><h3>Steps and distance</h3>
      <p class="fline"><b>${esc(st.today == null ? 'not recorded' : N(st.today))}</b> steps today${
        st.km_today != null ? `, <b>${esc(st.km_today.toFixed(1))} ${esc(u.distance)}</b>` : ''}.</p>
      <p class="fline"><b>${esc(st.week == null ? 'not recorded' : N(st.week))}</b> steps in the last 7 days${
        st.km_week != null ? `, <b>${esc(st.km_week.toFixed(1))} ${esc(u.distance)}</b>` : ''}.</p>
      ${stepsChart(st.chart)}</section>`;
  }

  function weight(w, u) {
    const ch = w.change30 == null ? '' : Math.abs(w.change30) < .1 ? 'no change over 30 days'
      : `${w.change30 < 0 ? 'down' : 'up'} ${Math.abs(w.change30).toFixed(1)} ${u.weight} over 30 days`;
    const when = w.date ? `, last recorded ${date(w.date)}${ch ? `, ${ch}` : ''}` : '';
    return `<section class="card fsec"><h3>Weight</h3>
      <p class="fline"><b>${esc(w.latest == null ? 'not recorded' : `${w.latest.toFixed(1)} ${u.weight}`)}</b>${esc(when)}.</p>
      ${w.flat ? `<p class="fnote">The value has not moved in ${esc(w.window)} days.</p>` : ''}
      ${lineChart(w.chart, 'Weight', 0)}</section>`;
  }

  async function refresh() {
    let s;
    try { s = await get('/api/fitness'); }
    catch (e) { s = {ok: false, detail: 'Could not reach the dashboard.'}; }
    if (!s || s.ok !== true) {
      el.innerHTML = `<div class="fit"><p class="empty">${esc((s && s.detail) || 'The fitness source could not be read.')}${
        s && s.docs ? ` See ${esc(s.docs)} in the repo to set one up.` : ''}</p></div>`;
      return;
    }
    el.innerHTML = `<div class="fit">${s.demo ? '<span class="chip demo">DEMO</span>' : ''}${
      night(s)}${running(s)}${work(s.work_split)}${heart(s.heart)}${steps(s.steps, s.units)}${weight(s.weight, s.units)}
      <p class="fnote">${esc(`${s.source || ''}${s.as_of ? `, data to ${date(String(s.as_of).slice(0, 10))}` : ''}.`)}</p></div>`;
  }

  window.CC.register('fitness', {mount(section) { el = section; }, refresh});
})();
