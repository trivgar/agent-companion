// Comms: plays clips while this page is open, one at a time.
// Two sources, picked by what the server answers:
// - the coordinator outbox (/api/tts/outbox), when tts_dir names one;
// - otherwise the Piper clips comms/install.sh sets up (/api/comms/next), which
//   is what a fresh install has. Those play through Web Audio, not an <audio>
//   element: an element takes audio focus and would pause the owner's music.
// Android lets a page play only after a tap, so the Play button is that tap.
'use strict';
(() => {
  const CC = window.CC;
  const identity = window.CCIdentity;
  const NOT_SET_UP = /not set up|: 503$/;   // the server's reason, or a bare 503
  let box, on = false, running = false, latest = null, error = '';
  let mode = '';   // '' until the server answers, then 'outbox' or 'clips'
  const said = {speaker: '', text: ''};
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

  function speaker(name) {
    const value = String(name || '');
    return value ? `<p class="who"><span class="identity-heading identity-label">${identity.sprite(value)}<span>${CC.esc(value)}</span></span></p>` : '';
  }

  function draw() {
    if (!box) return;
    const status = error || (latest && latest.held ? 'Comms are on hold.' : on ? 'Listening for clips.' : 'Press Play to listen.');
    const who = said.speaker
      ? `<div class="card">${speaker(said.speaker)}<p class="said">${CC.esc(said.text)}</p></div>` : '';
    box.innerHTML = `<button class="act primary" id="tts-toggle" aria-pressed="${on}">${on ? 'Pause' : 'Play'}</button>${who}<p class="empty">${CC.esc(status)}</p>`;
    box.querySelector('#tts-toggle').onclick = toggle;
  }

  // --- the coordinator outbox ---

  async function mark(clip, result, detail) {
    try { await CC.post('/api/tts/played', {seq: clip.seq, result, detail}); } catch (error) {}
  }

  async function play(clip) {
    let audio;
    try {
      const response = await CC.fetchAuthed(`/api/tts/clip/${encodeURIComponent(clip.seq)}?who=web`);
      if (!response.ok) return;
      audio = new Audio(URL.createObjectURL(await response.blob()));
      const finished = new Promise(resolve => {
        audio.onended = () => resolve({result: 'played', detail: ''});
        audio.onerror = () => resolve({result: 'failed', detail: 'audio playback failed'});
      });
      try {
        await audio.play();
      } catch (error) {
        await mark(clip, 'failed', 'audio could not start');
        return;
      }
      const outcome = await finished;
      await mark(clip, outcome.result, outcome.detail);
    } catch (error) {
      await mark(clip, 'failed', 'audio could not load');
    } finally {
      if (audio) URL.revokeObjectURL(audio.src);
    }
  }

  async function loop() {
    if (running) return;
    running = true;
    while (on) {
      if (mode === 'clips') { await clipsTurn(); continue; }
      try {
        latest = await CC.get('/api/tts/outbox?who=web'); error = '';
        if (latest.set_up === false) { mode = 'clips'; continue; }
        mode = 'outbox';
        const clips = latest.clips || [];
        CC.badge('comms', clips.length);
        draw();
        if (clips.length && !latest.held) await play(clips[0]);
      } catch (caught) {
        if (NOT_SET_UP.test(caught.message || '')) { mode = 'clips'; continue; }
        latest = {held: false}; on = false; error = caught.message || 'Comms is not set up.';
      }
      await pause(1200);
    }
    running = false;
    draw();
  }

  // --- Piper clips (comms/install.sh) ---

  const STALE_S = 300, LEAD_S = 0.3, TAIL_MS = 2500;
  let ctx = null, quiet = 0;

  async function fetchClip(id) {
    for (let i = 0; i < 3; i++) {
      try {
        const r = await CC.fetchAuthed(`/api/comms/clip/${encodeURIComponent(id)}`);
        if (r.status === 404) return {gone: true};
        if (r.ok) return {data: await r.arrayBuffer()};
      } catch (e) {}
      await pause(1500 * (i + 1));
    }
    return {};
  }

  async function answer(id) {
    for (let i = 0; i < 3; i++) {
      try { await CC.post('/api/comms/played', {id}); return; } catch (e) {
        if (/404|no such/.test(String(e.message))) return;
      }
      await pause(1500 * (i + 1));
    }
  }

  function playBuffer(buf, vol) {
    return new Promise(resolve => {
      clearTimeout(quiet);
      const s = ctx.createBufferSource();
      s.buffer = buf;
      const g = ctx.createGain();
      g.gain.value = typeof vol === 'number' ? vol : 1;
      s.connect(g); g.connect(ctx.destination);
      s.onended = () => {
        // Bluetooth lags the page: stay awake a moment so the tail is heard.
        quiet = setTimeout(() => { ctx.suspend().catch(() => {}); }, TAIL_MS);
        setTimeout(resolve, 600);
      };
      s.start(ctx.currentTime + LEAD_S);
    });
  }

  // One poll and at most one clip.
  async function clipsTurn() {
    let body = null;
    try { body = await CC.get('/api/comms/next'); error = ''; } catch (e) { error = e.message || 'Cannot reach the server.'; }
    latest = {held: !!(body && body.held)};
    const c = body && body.clip;
    CC.badge('comms', c ? 1 : 0);
    if (!c) { said.speaker = ''; said.text = ''; draw(); await pause(1500); return; }
    said.speaker = c.speaker; said.text = c.text; draw();
    if ((c.age || 0) > STALE_S) { await answer(c.id); return; }   // old news is no news
    const got = await fetchClip(c.id);
    if (got.gone) { await answer(c.id); return; }
    if (!got.data) { await pause(1500); return; }
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state !== 'running') await ctx.resume();
      await playBuffer(await ctx.decodeAudioData(got.data), c.volume);
    } catch (e) {
      error = 'Could not play a clip. Tap Play again.';
      on = false;
    }
    await answer(c.id);
  }

  function toggle() {
    on = !on;
    if (on) {
      // Made inside the tap, so Android lets it play.
      try { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); ctx.resume(); } catch (e) {}
    }
    draw();
    if (on) loop();
  }

  async function peek() {
    if (mode !== 'clips') {
      try {
        latest = await CC.get('/api/tts/outbox?who=web'); error = '';
        if (latest.set_up !== false) {
          mode = 'outbox';
          CC.badge('comms', (latest.clips || []).length);
          return;
        }
        mode = 'clips';
      } catch (caught) {
        if (!NOT_SET_UP.test(caught.message || '')) { latest = {held: false}; error = caught.message || 'Comms is not set up.'; return; }
        mode = 'clips';
      }
    }
    try {
      const b = await CC.get('/api/comms/next'); error = '';
      latest = {held: !!b.held};
      said.speaker = b.clip ? b.clip.speaker : ''; said.text = b.clip ? b.clip.text : '';
      CC.badge('comms', b.clip ? 1 : 0);
    } catch (caught) { latest = {held: false}; error = caught.message || 'Cannot reach the server.'; }
  }

  CC.register('comms', {
    mount(el) { box = el; draw(); },
    async refresh() {
      if (on) return;
      await peek();
      draw();
    },
  });
})();
