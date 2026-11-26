// The Mail tab: replies drafted for you. Approving never sends; a moment later
// a draft appears in your own mail account and you send it from there.
(function () {
  const {get, post, esc, badge} = window.CC;
  let el, sig = '';

  const busy = () => [...el.querySelectorAll('textarea')].some(t => t === document.activeElement || t.value !== t.defaultValue);
  const where = m => m.gmail_draft_id ? 'draft made' : 'draft not made yet';

  // Tall enough for the whole draft (about 40 characters a line on a phone), never under five.
  const rowsFor = t => Math.min(30, Math.max(5, String(t || '').split('\n').reduce((n, l) => n + Math.max(1, Math.ceil(l.length / 40)), 0)));

  function row(m) {
    const pending = m.state === 'pending';
    return `<article class="card mail" data-id="${esc(m.id)}">
      <h3>${esc(m.subject || '(no subject)')}</h3>
      <p class="dim">${esc(m.from || '')} <span class="faint">${esc((m.received || '').slice(0, 10))}</span></p>
      ${m.why ? `<p class="faint">${esc(m.why)}</p>` : ''}
      ${pending
        ? `<textarea rows="${rowsFor(m.reply)}" aria-label="Draft reply">${esc(m.reply || '')}</textarea>`
        : `<p class="reply">${esc(m.reply || '')}</p>`}
      <div class="acts">${pending
        ? `<button type="button" class="act primary" data-do="approve">Approve</button>
           <button type="button" class="act" data-do="edit">Save edit</button>
           <button type="button" class="act" data-do="skip">Skip</button>`
        : `<span class="chip quiet faint">${esc(m.state === 'approved' ? 'approved, ' + where(m) : 'skipped')}</span>`}</div>
    </article>`;
  }

  async function refresh() {
    const d = await get('/api/mail');
    if (d.setup === false) {
      badge('mail', 0);
      el.innerHTML = `<p class="empty">${esc(d.detail || 'Mail is not set up for this account.')}</p>`;
      return;
    }
    const drafts = d.drafts || [];
    badge('mail', (d.counts || {}).pending || 0);
    const s = JSON.stringify(drafts);
    if (s === sig || busy()) return;      // do not wipe words being typed
    sig = s;
    el.innerHTML = (drafts.length ? drafts.map(row).join('')
      : '<p class="empty">Nothing waiting. Replies show up here only for mail that wants an answer from you.</p>')
      + '<p class="faint">Approving does not send. It puts a draft in your mail account, in the same thread. You send it from there.</p>';
  }

  async function act(btn) {
    const card = btn.closest('[data-id]'), id = encodeURIComponent(card.dataset.id), what = btn.dataset.do;
    btn.disabled = true;
    try {
      if (what === 'edit' || what === 'approve') {
        const t = card.querySelector('textarea');
        if (t && t.value !== t.defaultValue) await post(`/api/mail/${id}/edit`, {reply: t.value});
      }
      if (what !== 'edit') {
        const state = what === 'skip' ? 'rejected' : 'approved';
        await post(`/api/mail/${id}`, {state});
      }
    } catch (e) {
      card.insertAdjacentHTML('beforeend', `<p class="needs">${esc(e.message)}</p>`);
      btn.disabled = false;
      return;
    }
    sig = '';
    await refresh();
  }

  window.CC.register('mail', {
    mount(section) {
      el = section;
      el.addEventListener('click', e => { const b = e.target.closest('button[data-do]'); if (b) act(b); });
    },
    refresh,
  });
})();
