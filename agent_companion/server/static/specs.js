// Read-only specs and plans from every checkout, by id (project/kind/name).
'use strict';
(function () {
  const esc = CC.esc;
  const css = document.createElement('style');
  css.textContent = `
    .specs-filters { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 10px; }
    .specs-filters button { min-height:36px; padding:0 10px; border:1px solid var(--line); border-radius:16px; background:var(--raised); color:var(--fg); font:inherit; }
    .specs-filters button[aria-pressed="true"] { border-color:var(--fg); font-weight:600; }
    .spec-row { display:grid; width:100%; grid-template-columns:minmax(0, 1fr) auto; gap:4px 12px; margin:0 0 8px; padding:10px; text-align:left; border:1px solid var(--line); border-radius:8px; background:var(--raised); color:var(--fg); font:inherit; }
    .spec-row strong { overflow-wrap:anywhere; }
    .spec-row .meta { grid-column:1; color:var(--dim); font-size:.8rem; overflow-wrap:anywhere; }
    .spec-row time { grid-column:2; grid-row:1 / span 2; align-self:center; color:var(--dim); font-size:.8rem; }
    .spec-back { margin:0 0 10px; }
    .spec-document { margin:0; padding:12px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; border:1px solid var(--line); border-radius:8px; background:var(--raised); color:var(--fg); font:13px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .spec-detail h2 { margin:0 0 4px; font-size:1.1rem; }
    .spec-detail .dim { margin:0 0 10px; }`;
  document.head.appendChild(css);

  let docs = null;
  let project = '';
  let kind = '';
  let open = null;
  let node = null;

  const selected = () => (docs || []).filter(doc => (!project || doc.project === project) && (!kind || doc.kind === kind));
  const filters = (label, value, current, values, attr) => `<div class="specs-filters" role="group" aria-label="${label}">
    <button type="button" ${attr}="" aria-pressed="${!current}">All ${esc(label.toLowerCase())}</button>
    ${values.map(item => `<button type="button" ${attr}="${esc(item)}" aria-pressed="${current === item}">${esc(item)}</button>`).join('')}
  </div>`;

  function drawList() {
    if (!node) return;
    if (docs === null) { node.innerHTML = '<p class="empty">Loading specs and plans...</p>'; return; }
    const projects = [...new Set(docs.map(doc => doc.project))];
    const kinds = [...new Set(docs.map(doc => doc.kind))];
    if (project && !projects.includes(project)) project = '';
    if (kind && !kinds.includes(kind)) kind = '';
    const rows = selected();
    node.innerHTML = `<h2>Specs and plans</h2>
      ${filters('Projects', project, project, projects, 'data-spec-project')}
      ${filters('Kinds', kind, kind, kinds, 'data-spec-kind')}
      <div class="spec-list">${rows.length ? rows.map(doc => `<button type="button" class="spec-row" data-spec-open data-id="${esc(doc.id)}">
        <strong>${esc(doc.title)}</strong><span class="meta">${esc(doc.project)} · ${esc(doc.kind)}</span><time>${esc(doc.date)}</time>
      </button>`).join('') : '<p class="empty">No specs or plans found.</p>'}</div>`;
  }

  function drawDocument() {
    if (!node || !open) return;
    if (open.error) {
      node.innerHTML = `<button type="button" class="act spec-back" data-spec-back>Back to specs</button><p class="empty">${esc(open.error)}</p>`;
      return;
    }
    if (!open.doc) {
      node.innerHTML = '<p class="empty">Loading document...</p>';
      return;
    }
    const doc = open.doc;
    node.innerHTML = `<button type="button" class="act spec-back" data-spec-back>Back to specs</button>
      <article class="spec-detail"><h2>${esc(doc.title)}</h2><p class="dim">${esc(doc.project)} · ${esc(doc.kind)} · ${esc(doc.name)}</p>
      <pre class="spec-document">${esc(doc.text)}</pre>${doc.cut ? '<p class="dim">Only the start of this document is shown.</p>' : ''}</article>`;
  }

  function draw() {
    if (open) drawDocument();
    else drawList();
  }

  async function showDocument(id) {
    open = {doc: null, error: ''};
    draw();
    try {
      const doc = await CC.get(`/api/specs/doc?${new URLSearchParams({id})}`);
      if (doc.ok) open.doc = doc;
      else open.error = doc.detail || 'Could not read that document.';
    } catch (error) {
      open.error = error.message || 'Could not read that document.';
    }
    draw();
  }

  function click(event) {
    const back = event.target.closest('[data-spec-back]');
    if (back && node && node.contains(back)) { open = null; draw(); return; }
    const projectButton = event.target.closest('[data-spec-project]');
    if (projectButton && node && node.contains(projectButton)) { project = projectButton.dataset.specProject; draw(); return; }
    const kindButton = event.target.closest('[data-spec-kind]');
    if (kindButton && node && node.contains(kindButton)) { kind = kindButton.dataset.specKind; draw(); return; }
    const row = event.target.closest('[data-spec-open]');
    if (row && node && node.contains(row)) showDocument(row.dataset.id);
  }

  CC.register('specs', {
    mount(el) {
      node = el;
      el.addEventListener('click', click);
      draw();
    },
    async refresh() {
      if (open) return;
      const result = await CC.get('/api/specs');
      docs = Array.isArray(result.docs) ? result.docs : [];
      draw();
    },
  });
})();
