// The session figure: one plain 32x32 person, drawn as SVG paths, one per
// colour. The body takes the session's hue; hair, skin and shape come from
// three separate hashes of the name, so two sessions with a near hue still
// differ. Rows, palettes, shapes and overlays are the reference mock's.
'use strict';
(function () {
  const ROWS = [
    '................................', '................................',
    '...........oooooooooo...........', '..........ohhhhhhhhhho..........',
    '.........ohhhhhhhhhhhho.........', '........ohhhhhhhhhhhhhho........',
    '........ohhsssssssssshho........', '........ohssssssssssssho........',
    '........osssssssssssssso........', '........ossseesssseessso........',
    '........ossseesssseessso........', '........osssssssssssssso........',
    '........oSssssssssssssSo........', '.........ossssSSSSsssSo.........',
    '.........osssssssssssso.........', '..........oSssssssssSo..........',
    '..........oooSssssSooo..........', '........oobbbbbbbbbbbboo........',
    '.......obbbbbbbbbbbbbbbbo.......', '......obbbbbbbbbbbbbbbbbbo......',
    '......obbbbbbbbbbbbbbbbbbo......', '.....obbbbbBbbbbbbbbBbbbbbo.....',
    '.....obbbobBbbbbbbbbBbobbbo.....', '.....osssobbbbbbbbbbbbossso.....',
    '.....osssobbbbbbbbbbbbossso.....', '......ooooppppppppppppoooo......',
    '.........opppppoopppppo.........', '.........opppppoopppppo.........',
    '.........opppppoopppppo.........', '........oeeeeeeooeeeeeeo........',
    '.........oooooo..oooooo.........', '................................'];
  const HAIR = ['#2b2320', '#6a4526', '#c9a24a', '#8d9199', '#a5432a'];
  const SKIN = [['#f1c9a5', '#d9a982'], ['#c98f63', '#a9744d'], ['#8a5a3c', '#6d4429']];
  const cache = new Map();

  function hash(value) {
    let out = 2166136261;
    for (const char of String(value == null ? '' : value)) {
      out ^= char.charCodeAt(0);
      out = Math.imul(out, 16777619);
    }
    return out >>> 0;
  }

  function escapeAttr(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g,
      char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  }

  function put(row, x, ch) { return row.slice(0, x) + ch + row.slice(x + 1); }

  function shape(style) {
    const rows = ROWS.slice();
    if (style === 1) {            // long hair down the sides
      for (let y = 8; y <= 12; y++) rows[y] = put(put(rows[y], 9, 'h'), 22, 'h');
      rows[13] = put(put(rows[13], 10, 'h'), 21, 'h');
    } else if (style === 2) {     // a cap in the session's colour
      for (let y = 3; y <= 5; y++) rows[y] = rows[y].replace(/h/g, 'c');
      for (let x = 9; x <= 22; x++) rows[6] = put(rows[6], x, 'C');
      rows[7] = put(put(rows[7], 9, 's'), 22, 's');
    }
    return rows;
  }

  function svg(name) {
    const label = String(name == null ? '' : name);
    const hue = window.CCIdentity.hue(label);
    const key = `${label}\u0000${hue}`;
    if (cache.has(key)) return cache.get(key);
    const skin = SKIN[hash(`${label}:skin`) % 3];
    const pal = {
      o: '#1b1f2a', e: '#1f2430', p: '#3a3f4c', s: skin[0], S: skin[1], h: HAIR[hash(`${label}:hair`) % 5],
      b: `hsl(${hue} 42% 48%)`, B: `hsl(${hue} 42% 35%)`, c: `hsl(${hue} 62% 54%)`, C: `hsl(${hue} 62% 38%)`,
    };
    const runs = {};
    shape(hash(`${label}:shape`) % 3).forEach((rowText, y) => {
      let x = 0;
      while (x < rowText.length) {
        const ch = rowText[x];
        let w = 1;
        while (rowText[x + w] === ch) w++;
        if (ch !== '.' && pal[ch]) (runs[ch] = runs[ch] || []).push(`M${x} ${y}h${w}v1h-${w}z`);
        x += w;
      }
    });
    const paths = Object.keys(runs).map(ch => `<path fill="${pal[ch]}" d="${runs[ch].join('')}"/>`).join('');
    const out = `<svg class="identity-sprite" viewBox="0 0 32 32" role="img" aria-label="Session sprite for ${escapeAttr(label)}" shape-rendering="crispEdges">${paths}</svg>`;
    cache.set(key, out);
    return out;
  }

  function state(row) {
    if (row.waiting) return 'wait';
    if (row.status === 'busy') return 'work';
    if (Number(row.context_pct) >= 85) return 'full';
    if (Number(row.last_activity) > 3600) return 'sleep';
    return 'idle';
  }

  const FX = {
    work: '<svg class="fx kb" viewBox="0 0 20 6" shape-rendering="crispEdges"><path fill="#2a2e38" d="M0 1h20v5h-20z"/><path fill="#aeb6c6" d="M1 2h2v1h-2zM4 2h2v1h-2zM7 2h2v1h-2zM10 2h2v1h-2zM13 2h2v1h-2zM16 2h3v1h-3zM2 4h13v1h-13z"/></svg>',
    wait: '<svg class="fx bang" viewBox="0 0 5 9" shape-rendering="crispEdges"><path fill="#1b1f2a" d="M0 0h5v9h-5z"/><path fill="#ff8a5c" d="M1 1h3v4h-3zM1 6h3v2h-3z"/></svg>',
    sleep: '<svg class="fx z" viewBox="0 0 7 7" shape-rendering="crispEdges"><path fill="#8891ab" d="M1 1h5v1h-5zM4 2h1v1h-1zM3 3h1v1h-1zM2 4h1v1h-1zM1 5h5v1h-5z"/></svg>',
    full: '<svg class="fx meter" viewBox="0 0 9 4" shape-rendering="crispEdges"><path fill="#1b1f2a" d="M0 0h9v4h-9z"/><path style="fill:var(--alert)" d="M1 1h7v2h-7z"/></svg>',
  };

  function html(name, row) {
    const st = state(row || {});
    return `<span class="spr" data-st="${st}" aria-hidden="true">${svg(name)}${FX[st] || ''}</span>`;
  }

  const css = document.createElement('style');
  css.textContent = `
    .identity-sprite { display:block; flex:0 0 auto; height:1.7rem; width:1.7rem; }
    .identity-label { align-items:center; display:inline-flex; gap:6px; min-width:0; }
    .spr .identity-sprite { height:40px; width:40px; image-rendering:pixelated; }
    .spr .fx { position:absolute; }
    .spr .kb { left:7.5px; bottom:-3px; width:25px; height:7.5px; }
    .spr .bang { right:1px; top:-3px; width:9px; height:15px; }
    .spr .z { right:2px; top:-2px; width:12px; height:12px; }
    .spr .meter { right:0; top:-2px; width:11px; height:5px; }
    .spr[data-st="work"] .identity-sprite { animation:cc-sprite-work 1.2s steps(2, jump-none) infinite; }
    @keyframes cc-sprite-work { from { transform:translateY(0); } to { transform:translateY(-1px); } }`;
  document.head.appendChild(css);

  window.CCSprite = {svg, state, html};
})();
