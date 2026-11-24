// Pages: list agent-published HTML, then load it through the authenticated
// route into an opaque-origin iframe.  The frame never receives the dashboard
// key, and its CSP blocks connections even when a page contains scripts.
'use strict';
(function () {
  const CC = window.CC, esc = CC.esc;
  const FRAME_POLICY = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms; default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com https://cdn.tailwindcss.com https://code.jquery.com; style-src 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com; font-src https://fonts.gstatic.com data:; img-src data: blob:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'";
  let box, records = [], open = '', deleting = '', pageHtml = null, problem = '';

  const age = stamp => {
    const seconds = Math.max(0, (Date.now() / 1000) - Number(stamp || 0));
    if (seconds < 60) return 'now';
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  };

  function setAddress(page) {
    const url = new URL(location.href);
    if (page) url.searchParams.set('page', page);
    else url.searchParams.delete('page');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  function drawList() {
    if (!records.length) {
      box.innerHTML = '<p class="pg-empty">No pages yet. An agent makes one with <code>page publish</code>.</p>';
      return;
    }
    box.innerHTML = `<div class="pg-list">${records.map(page => {
      const shared = page.household ? '<span class="pg-house">household</span>' : '';
      return `<button type="button" class="pg-card" data-page="${esc(page.id)}"><span class="pg-title">${esc(page.title)}</span><span class="pg-meta">${esc(page.speaker || 'an agent')} · ${esc(page.project || '')} · ${esc(age(page.updated))}${shared}</span></button>`;
    }).join('')}</div>`;
  }

  // An open page is drawn once. The shell refreshes this view every few
  // seconds, and writing the frame again loaded the page again at its top, a
  // few lines into reading it. While the frame on screen is this page's, a
  // refresh or arming Delete changes only the button beside it.
  function drawPage() {
    const page = records.find(record => record.id === open);
    if (!page) { open = ''; pageHtml = null; setAddress(''); drawList(); return; }
    const armed = deleting === open;
    const shown = box.querySelector('.pg-frame');
    if (shown && pageHtml !== null && shown.dataset.shown === open) {
      const remove = box.querySelector('.pg-delete');
      remove.classList.toggle('pg-armed', armed);
      remove.textContent = armed ? 'Delete again' : 'Delete';
      remove.disabled = false;
      return;
    }
    const frame = pageHtml === null ? (problem ? `<p class="pg-empty pg-error">${esc(problem)}</p>` : '<p class="pg-empty">Loading page...</p>') :
      `<iframe class="pg-frame" data-shown="${esc(open)}" title="${esc(page.title)}" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms" referrerpolicy="no-referrer"></iframe>`;
    box.innerHTML = `<div class="pg-open"><div class="pg-actions"><button type="button" class="act pg-back">Back</button><button type="button" class="act pg-delete${armed ? ' pg-armed' : ''}" data-delete="${esc(open)}">${armed ? 'Delete again' : 'Delete'}</button></div><h2 class="pg-heading">${esc(page.title)}</h2>${frame}</div>`;
    const target = box.querySelector('.pg-frame');
    if (target && pageHtml !== null) {
      // Set the property after parsing rather than interpolating untrusted HTML
      // into this dashboard document. The iframe sandbox makes its origin opaque.
      target.srcdoc = `<meta http-equiv="Content-Security-Policy" content="${FRAME_POLICY}">${pageHtml}`;
    }
  }

  function draw() {
    if (!box) return;
    if (open) drawPage();
    else drawList();
  }

  async function loadPage(id) {
    pageHtml = null;
    problem = '';
    draw();
    try {
      const response = await CC.fetchAuthed(`/pages/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error('Could not open this page.');
      const html = await response.text();
      if (open === id) { pageHtml = html; draw(); }
    } catch (error) {
      if (open === id) { problem = error.message || 'Could not open this page.'; draw(); }
    }
  }

  async function refresh() {
    const data = await CC.get('/api/pages');
    records = Array.isArray(data.pages) ? data.pages : [];
    const linked = new URL(location.href).searchParams.get('page') || '';
    if (!open && linked) open = linked;
    if (open && !records.some(page => page.id === open)) {
      open = '';
      pageHtml = null;
      setAddress('');
    }
    draw();
    if (open && pageHtml === null && !problem) loadPage(open);
  }

  function click(event) {
    const card = event.target.closest('[data-page]');
    if (card) {
      open = card.dataset.page || '';
      deleting = '';
      setAddress(open);
      loadPage(open);
      return;
    }
    if (event.target.closest('.pg-back')) {
      open = '';
      deleting = '';
      pageHtml = null;
      problem = '';
      setAddress('');
      draw();
      return;
    }
    const remove = event.target.closest('[data-delete]');
    if (!remove) return;
    const id = remove.dataset.delete || '';
    if (deleting !== id) { deleting = id; draw(); return; }
    remove.disabled = true;
    CC.fetchAuthed(`/api/pages/${encodeURIComponent(id)}`, {
      method: 'DELETE', headers: {'Content-Type': 'application/json', 'x-agent-companion-key': CC.key()}, body: '{}',
    }).then(response => {
      if (!response.ok) throw new Error('Could not delete this page.');
      records = records.filter(page => page.id !== id);
      open = '';
      deleting = '';
      pageHtml = null;
      setAddress('');
      draw();
    }).catch(() => { deleting = ''; draw(); });
  }

  CC.register('pages', {
    mount(el) {
      box = document.createElement('div');
      box.className = 'pages';
      box.addEventListener('click', click);
      el.appendChild(box);
      draw();
    },
    refresh,
  });
})();
