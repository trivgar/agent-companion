// Stable, local-only visual identities for session names. The shapes are
// generated here rather than shipped as character art.
'use strict';
(function () {
  const HEX = /^#[0-9a-f]{6}$/i;
  let overrides = Object.create(null);

  function hash(value) {
    let out = 2166136261;
    for (const char of String(value == null ? '' : value)) {
      out ^= char.charCodeAt(0);
      out = Math.imul(out, 16777619);
    }
    return out >>> 0;
  }

  function hexHue(value) {
    if (!HEX.test(value)) return null;
    const red = parseInt(value.slice(1, 3), 16) / 255;
    const green = parseInt(value.slice(3, 5), 16) / 255;
    const blue = parseInt(value.slice(5, 7), 16) / 255;
    const high = Math.max(red, green, blue), low = Math.min(red, green, blue);
    const delta = high - low;
    if (!delta) return 0;
    let result;
    if (high === red) result = ((green - blue) / delta) % 6;
    else if (high === green) result = (blue - red) / delta + 2;
    else result = (red - green) / delta + 4;
    return Math.round((result * 60 + 360) % 360);
  }

  function setOverrides(value) {
    overrides = Object.create(null);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    for (const [name, color] of Object.entries(value)) {
      if (typeof name === 'string' && typeof color === 'string' && HEX.test(color)) {
        overrides[name] = color;
      }
    }
  }

  function hue(name) {
    const pinned = hexHue(overrides[String(name == null ? '' : name)] || '');
    return pinned == null ? hash(name) % 360 : pinned;
  }

  function sprite(name) {
    return window.CCSprite.svg(name);
  }

  setOverrides(window.CC_IDENTITY_OVERRIDES);
  window.CCIdentity = {hue, setOverrides, sprite};
})();
