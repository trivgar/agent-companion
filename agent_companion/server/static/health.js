// Local machine facts. Values not available on this system say why instead of
// borrowing a zero from an unrelated counter.
'use strict';
(function () {
  const {get, esc} = window.CC;
  const css = document.createElement('style');
  css.textContent = `
    .machine .machine-note, .machine .machine-missing { color:var(--dim); font-size:.86rem; }
    .machine .machine-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px; margin:10px 0; }
    .machine .machine-reading { min-width:0; margin:0; }
    .machine .machine-disk, .machine .machine-gpu, .machine .machine-processes { min-width:0; }
    .machine .machine-reading.warn, .machine .machine-disk.warn, .machine .machine-gpu.warn { border-color:var(--alert); }
    .machine .machine-label { display:block; color:var(--dim); font-size:.8rem; }
    .machine .machine-value { display:block; font-size:1.08rem; font-weight:650; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
    .machine .machine-missing { display:block; margin-top:4px; overflow-wrap:anywhere; }
    .machine .machine-detail { color:var(--dim); font-size:.84rem; margin:4px 0 0; overflow-wrap:anywhere; }
    .machine .machine-processes ol { margin:4px 0 0; padding:0; list-style:none; }
    @media (max-width:340px) { .machine .machine-grid { grid-template-columns:1fr; } }
  `;
  document.head.appendChild(css);

  let el;
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const pct = value => `${Math.round(value)}%`;
  const celsius = value => `${Math.round(value)}C`;
  const bytes = value => value >= 1024 ** 4 ? `${(value / 1024 ** 4).toFixed(1)} TiB`
    : value >= 1024 ** 3 ? `${Math.round(value / 1024 ** 3)} GiB` : `${Math.round(value / 1024 ** 2)} MiB`;
  const mib = value => value >= 1024 ? `${(value / 1024).toFixed(value >= 10240 ? 0 : 1)} GiB` : `${Math.round(value)} MiB`;

  function reading(label, row, value, warning) {
    const usable = value !== null && value !== undefined && value !== '';  // value arrives formatted, e.g. "34%"
    return `<article class="card machine-reading${warning ? ' warn' : ''}"><span class="machine-label">${esc(label)}</span>` +
      (usable ? `<span class="machine-value">${esc(value)}</span>` :
        `<span class="machine-missing">${esc((row && row.reason) || 'This reading is not available.')}</span>`) +
      `</article>`;
  }

  function disk(row) {
    const usable = number(row.percent);
    return `<article class="card machine-disk${row.warning ? ' warn' : ''}"><h3>${esc(row.path || 'Disk')}</h3>` +
      (usable ? `<span class="machine-value">${esc(pct(row.percent))}</span>` :
        `<span class="machine-missing">${esc(row.reason || 'This disk reading is not available.')}</span>`) +
      (number(row.used_bytes) && number(row.total_bytes) ?
        `<p class="machine-detail">${esc(bytes(row.used_bytes))} used of ${esc(bytes(row.total_bytes))}</p>` : '') +
      `</article>`;
  }

  function gpu(data) {
    if (!data || !Array.isArray(data.gpus) || !data.gpus.length) {
      return `<section class="machine-gpus"><article class="card machine-gpu"><h3>GPU</h3><span class="machine-missing">${esc((data && data.reason) || 'No usable GPU reading was reported.')}</span></article></section>`;
    }
    return `<section class="machine-gpus">${data.gpus.map((row, index) => {
      const bits = [];
      if (number(row.utilization_pct)) bits.push(`GPU ${pct(row.utilization_pct)}`);
      if (number(row.temperature_c)) bits.push(celsius(row.temperature_c));
      if (number(row.memory_used_mib) && number(row.memory_total_mib)) bits.push(`${mib(row.memory_used_mib)} / ${mib(row.memory_total_mib)}`);
      return `<article class="card machine-gpu${row.warning ? ' warn' : ''}"><h3>${esc((data.vendor || 'GPU') + (data.gpus.length > 1 ? ` ${index + 1}` : ''))}</h3>` +
        (bits.length ? `<span class="machine-value">${esc(bits.join(' · '))}</span>` : '<span class="machine-missing">GPU details were not reported.</span>') +
        `</article>`;
    }).join('')}</section>`;
  }

  function processes(data) {
    if (!Array.isArray(data)) return `<span class="machine-missing">${esc((data && data.reason) || 'Process CPU use is not available.')}</span>`;
    if (!data.length) return '<span class="machine-missing">No process used measurable CPU in this sample.</span>';
    return `<ol>${data.map(row => `<li class="row"><code class="body">${esc(row.command || 'unknown command')}</code><span class="trail">${esc(number(row.percent) ? pct(row.percent) : 'not available')}</span></li>`).join('')}</ol>`;
  }

  function render(data) {
    if (!el) return;
    const cpu = data.cpu || {}, load = data.load || {}, memory = data.memory || {}, swap = data.swap || {};
    const loadValue = number(load.one) && number(load.five) && number(load.fifteen) ?
      `${load.one.toFixed(2)} / ${load.five.toFixed(2)} / ${load.fifteen.toFixed(2)}` : null;
    el.innerHTML = `<div class="machine"><h2>Machine health</h2><p class="machine-note">Read locally from the machine running these sessions.</p>
      <section class="machine-grid">
        ${reading('CPU use', cpu, number(cpu.percent) ? pct(cpu.percent) : null, false)}
        ${reading('CPU temperature', data.temperature || {}, number((data.temperature || {}).c) ? celsius(data.temperature.c) : null, (data.temperature || {}).warning)}
        ${reading('Load average', load, loadValue, false)}
        ${reading('Memory', memory, number(memory.percent) ? pct(memory.percent) : null, memory.warning)}
        ${reading('Swap', swap, number(swap.percent) ? pct(swap.percent) : null, false)}
      </section>
      <h3 class="sec-head">Disks</h3><section class="machine-disks">${(Array.isArray(data.disks) ? data.disks : []).map(disk).join('') || '<p class="machine-missing">No disk readings were reported.</p>'}</section>
      <h3 class="sec-head">Graphics</h3>${gpu(data.gpu)}
      <section class="card machine-processes"><h3>Heaviest processes by CPU</h3>${processes(data.processes)}</section>
    </div>`;
  }

  async function refresh() {
    render(await get('/api/health'));
  }

  window.CC.register('health', {mount(section) { el = section; el.innerHTML = '<p class="empty">Reading this machine...</p>'; }, refresh});
})();
