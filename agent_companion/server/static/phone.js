// Phone driver: a small owner-facing status and emergency-stop view.
'use strict';
(function () {
  const CC = window.CC;
  const esc = CC.esc;
  let el, error = '';

  const text = value => typeof value === 'string' ? value.trim() : '';
  const entries = value => Array.isArray(value) ? value : [];
  const age = value => {
    const seconds = Math.max(0, Date.now() / 1000 - Number(value || 0));
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    return `${Math.round(seconds / 3600)} h ago`;
  };

  function detail(job) {
    if (job.prompt) return `A prompt is waiting${text(job.prompt.what) ? ': ' + text(job.prompt.what) : ''}.`;
    if (job.your_turn) return `Your turn${text(job.your_turn.what) ? ': ' + text(job.your_turn.what) : ''}.`;
    return text(job.detail);
  }

  function live(job) {
    const urgent = job.state === 'paused' || job.state === 'waiting' || job.prompt || job.your_turn;
    return `<article class="phone-live${urgent ? ' phone-alert' : ''}">
      <div><strong>${esc(text(job.agent) || 'An agent')} is driving the ${esc(text(job.device) || 'phone')}</strong>
      <p>${esc(text(job.task) || 'A task')}${detail(job) ? ': ' + esc(detail(job)) : ''}</p></div>
      <button class="act" type="button" data-stop="${esc(job.id)}">Stop</button>
    </article>`;
  }

  function pending(device) {
    const id = text(device.device_id);
    return `<article class="phone-live phone-alert"><div><strong>${esc(text(device.model) || 'A phone')} wants to pair</strong>
      <p>Registered ${esc(age(device.registered))}; id ending ${esc(id.slice(-6))}.</p></div>
      <button class="act" type="button" data-device="${esc(id)}" data-action="approve">Approve</button>
      <button class="act" type="button" data-device="${esc(id)}" data-action="forget">Forget</button>
    </article>`;
  }

  function history(job) {
    return `<li><strong>${esc(text(job.task) || 'Untitled job')}</strong><span>${esc(
      `${text(job.agent) || 'agent'} · ${text(job.device) || 'phone'} · ${text(job.outcome) || 'ended'} · ${age(job.ended || job.created)}`
    )}</span></li>`;
  }

  function draw(data) {
    const liveJobs = entries(data.live);
    const pendingDevices = entries(data.pending);
    const old = entries(data.history).slice(0, 10);
    const need = liveJobs.filter(job => job.prompt || job.your_turn || job.state === 'paused' || job.state === 'waiting').length;
    CC.badge('phone', need);
    el.innerHTML = (liveJobs.length ? liveJobs.map(live).join('') : '<p class="empty">No phone job is running.</p>') +
      pendingDevices.map(pending).join('') +
      (old.length ? `<details class="phone-history"><summary>Recent phone jobs</summary><ol>${old.map(history).join('')}</ol></details>` : '') +
      (error ? `<p class="phone-error" role="alert">${esc(error)}</p>` : '');
  }

  async function action(path) {
    error = '';
    try {
      await CC.post(path, {});
      await part.refresh();
    } catch (err) {
      error = err.message || 'The phone driver did not answer.';
      draw({live: [], pending: [], history: []});
    }
  }

  const part = {
    mount(section) {
      el = section;
      el.addEventListener('click', event => {
        const stop = event.target.closest('[data-stop]');
        if (stop) action('/api/phone/jobs/' + encodeURIComponent(stop.dataset.stop) + '/stop');
        const device = event.target.closest('[data-device]');
        if (device) action('/api/phone/devices/' + encodeURIComponent(device.dataset.device) + '/' + device.dataset.action);
      });
    },
    async refresh() {
      draw(await CC.get('/api/phone/jobs'));
    },
  };
  CC.register('phone', part);
})();
