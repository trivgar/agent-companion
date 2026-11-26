// The animations switch. It runs in <head>, before the first paint, so a page
// opened with animations off never shows one. The system's reduced-motion
// setting is honoured in CSS by itself; this is for a device whose owner
// wants stillness without changing the whole phone.
'use strict';
(function () {
  const KEY = 'cc-still';
  function read() {
    try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
  }
  function apply(on) {
    if (on) document.documentElement.setAttribute('data-still', '');
    else document.documentElement.removeAttribute('data-still');
  }
  apply(read());
  window.CCStill = {
    get: read,
    set(on) {
      try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) {}
      apply(!!on);
    },
  };
})();
