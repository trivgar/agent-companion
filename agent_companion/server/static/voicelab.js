// Voice lab: the coordinator's builds, auditions and cast controls, or,
// with no coordinator, what the voicelab/ pipeline built.
'use strict';
(() => {
  const CC = window.CC;
  let box, data, open = '', playing = '';
  const esc = value => CC.esc(value == null ? '' : value);
  const clipUrl = (dir, clip) => `/api/voicelab/clip/${encodeURIComponent(dir)}/${encodeURIComponent(clip)}`;
  const auditionUrl = (name, clip) => `/api/voices/auditions/${encodeURIComponent(name)}/${encodeURIComponent(clip)}`;

  function clips(dir, items, label) {
    if (!Array.isArray(items) || !items.length) return '';
    return `<p class="dim">${esc(label)}: ${items.map((clip, index) =>
      `<button class="act" data-play="${esc(clipUrl(dir, clip))}" data-key="${esc(`${dir}/${clip}`)}">${index + 1}</button>`).join(' ')}</p>`;
  }

  function audition(item) {
    return `<article class="card"><h3>${esc(item.name)} <span class="chip quiet dim">audition</span></h3>` +
      (item.note ? `<p>${esc(item.note)}</p>` : '') +
      `<p>${(item.clips || []).map((clip, index) => `<button class="act" data-play="${esc(auditionUrl(item.name, clip))}" data-key="${esc(`${item.name}/${clip}`)}">Play ${index + 1}</button>`).join(' ')}</p>` +
      `<p>${[1, 2, 3].map(tier => `<button class="act" data-audition="${esc(item.name)}" data-tier="${tier}">Tier ${tier}</button>`).join(' ')} ` +
      `<button class="act" data-audition="${esc(item.name)}" data-tier="no">No</button></p></article>`;
  }

  function buildNeed(item) {
    return `<article class="card"><h3>${esc(item.name)}</h3><p>${esc(item.verdict)}</p>` +
      clips(item.dir, item.clips && item.clips.new, 'New') + clips(item.dir, item.clips && item.clips.live, 'Live') +
      `<p><button class="act primary" data-decide="${esc(item.dir)}" data-want="true">Want it</button> ` +
      `<button class="act" data-decide="${esc(item.dir)}" data-want="false">Not this one</button></p></article>`;
  }

  function voice(row) {
    const isOpen = open === row.voice;
    const tiers = [1, 2, 3].map(tier => `<button class="act" data-cast="${esc(row.name)}" data-tier="${tier}"` +
      `${row.tier === tier ? ' aria-pressed="true"' : ''}>${tier}</button>`).join(' ');
    const latest = row.builds && row.builds[0];
    const card = !isOpen ? '' : `<div class="card"><p><b>${esc(row.words)}</b></p>` +
      `<p><input type="text" data-test-text="${esc(row.name)}" maxlength="200" placeholder="Test line"><button class="act" data-test="${esc(row.name)}">Say</button></p>` +
      (latest ? `<p>${esc(latest.verdict || '')}</p>${clips(latest.dir, latest.clips && latest.clips.new, 'New')}${clips(latest.dir, latest.clips && latest.clips.live, 'Live')}` : '') +
      `</div>`;
    return `<article class="card"><h3><button class="act" data-open="${esc(row.voice)}">${esc(row.name)}</button>` +
      (row.aliases && row.aliases.length ? ` <span class="dim">also ${esc(row.aliases.join(', '))}</span>` : '') +
      `</h3><p>${tiers} <button class="act" data-test="${esc(row.name)}">Test</button></p>${card}</article>`;
  }

  // A voice the voicelab/ pipeline made (no coordinator): stage and auditions.
  function pipelineVoice(row) {
    const url = clip => `/api/voicelab/audition/${encodeURIComponent(row.name)}/${encodeURIComponent(clip)}`;
    const mb = (row.bytes || 0) / 1e6;
    return `<article class="card"><h3>${esc(row.name)} <span class="chip quiet dim">${esc(row.stage)}, ${esc(mb.toFixed(mb < 10 ? 1 : 0))} MB</span></h3>` +
      ((row.audition || []).length ? `<p>${row.audition.map((clip, index) =>
        `<button class="act" data-play="${esc(url(clip))}" data-key="${esc(`${row.name}/${clip}`)}">Play ${index + 1}</button>`).join(' ')}</p>` : '') +
      `</article>`;
  }

  function drawPipeline() {
    const rows = (data.pipeline.voices || []).map(pipelineVoice).join('');
    box.innerHTML = `<p class="dim"><b>Right now:</b> ${esc((data.now || {}).text || 'not known')}</p>` +
      `<h2 class="sec-head">Voices</h2>${rows || '<p class="empty">No voices yet.</p>'}` +
      `<p class="dim">${esc((data.voices || {}).why || '')}</p>`;
    CC.badge('voicelab', 0);
  }

  function draw() {
    if (!box) return;
    if (data && data.pipeline) { drawPipeline(); return; }
    if (!data) { box.innerHTML = '<p class="empty">Voice lab could not load.</p>'; return; }
    const needs = data.needs || {}, running = data.running || {}, cast = data.voices || {}, other = data.other || {};
    const needItems = (needs.items || []).map(item => item.kind === 'audition' ? audition(item) : buildNeed(item)).join('') || '<p class="empty">Nothing waiting on you.</p>';
    const runItems = (running.builds || []).map(item => `<article class="card"><h3>${esc(item.voice_name || item.name)}</h3>` +
      `<p>${esc(item.next || item.phase || 'running')}${item.step ? `, step ${esc(item.step)} of ${esc(item.of)}` : ''}</p></article>`).join('') || '<p class="empty">Nothing building.</p>';
    const castItems = (cast.voices || []).map(voice).join('') || '<p class="empty">No voices in the cast.</p>';
    const otherItems = (other.builds || []).map(item => `<li>${esc(item.name)}: ${esc(item.phase || '')}</li>`).join('');
    box.innerHTML = `<p class="dim"><b>Right now:</b> ${esc((data.now || {}).text || 'not known')}</p>` +
      `<h2 class="sec-head">Needs you</h2>${needs.ok === false ? `<p class="empty">${esc(needs.why)}</p>` : needItems}` +
      `<h2 class="sec-head">Running now</h2>${running.ok === false ? `<p class="empty">${esc(running.why)}</p>` : runItems}` +
      `<h2 class="sec-head">The cast</h2>${cast.ok === false ? `<p class="empty">${esc(cast.why)}</p>` : castItems}` +
      (otherItems ? `<details><summary>Other builds</summary><ul>${otherItems}</ul></details>` : '');
    CC.badge('voicelab', (needs.items || []).length + (running.builds || []).length);
  }

  async function play(button) {
    const key = button.dataset.key;
    if (playing === key) { playing = ''; return; }
    const response = await CC.fetchAuthed(button.dataset.play);
    if (!response.ok) throw new Error('clip is unavailable');
    const audio = new Audio(URL.createObjectURL(await response.blob()));
    playing = key;
    audio.onended = () => { playing = ''; URL.revokeObjectURL(audio.src); };
    await audio.play();
  }

  async function action(button) {
    if (button.dataset.play) return play(button);
    if (button.dataset.open) { open = open === button.dataset.open ? '' : button.dataset.open; draw(); return; }
    if (button.dataset.audition) {
      return CC.post(`/api/voices/auditions/${encodeURIComponent(button.dataset.audition)}`, {tier: button.dataset.tier === 'no' ? 'no' : Number(button.dataset.tier)});
    }
    if (button.dataset.decide) {
      return CC.post(`/api/voicelab/decide/${encodeURIComponent(button.dataset.decide)}`, {want: button.dataset.want === 'true'});
    }
    if (button.dataset.cast) {
      return CC.post(`/api/voicelab/tier/${encodeURIComponent(button.dataset.cast)}`, {tier: Number(button.dataset.tier)});
    }
    if (button.dataset.test) {
      const input = [...box.querySelectorAll('[data-test-text]')].find(item => item.dataset.testText === button.dataset.test);
      const response = await CC.fetchWrite(`/api/voicelab/test/${encodeURIComponent(button.dataset.test)}`, {text: input ? input.value : ''});
      if (!response.ok) throw new Error('test line could not be rendered');
      const audio = new Audio(URL.createObjectURL(await response.blob()));
      return audio.play();
    }
  }

  CC.register('voicelab', {
    mount(el) {
      box = el;
      box.addEventListener('click', async event => {
        const button = event.target.closest('button');
        if (!button || !box.contains(button)) return;
        try { await action(button); await this.refresh(); } catch (error) {
          box.insertAdjacentHTML('afterbegin', `<p class="empty">${esc(error.message || 'Could not save.')}</p>`);
        }
      });
    },
    async refresh() {
      try { data = await CC.get('/api/voicelab'); }
      catch (error) { data = {now: {text: error.message || 'Comms is not set up'}, needs: {items: []}, running: {builds: []}, voices: {voices: []}, other: {builds: []}}; }
      draw();
    },
  });
})();
