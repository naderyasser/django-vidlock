/*
 * The demo's motion: staggered entrances, a light under the pointer, card
 * spotlights and tilt, magnetic buttons with a ripple, counting numbers.
 * Does nothing for prefers-reduced-motion.
 */
(function () {
  'use strict';
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const fine = window.matchMedia('(pointer: fine)').matches;

  // Entrances, one after another.
  document.querySelectorAll('main > *').forEach(function (el, i) { el.style.setProperty('--i', i); el.classList.add('rise'); });
  document.querySelectorAll('.heat div, .hit').forEach(function (el) {
    el.style.setProperty('--i', Array.prototype.indexOf.call(el.parentNode.children, el));
  });

  // Numbers count up from zero: "27%", "80s", "1".
  document.querySelectorAll('.stat b').forEach(function (el) {
    const match = el.textContent.trim().match(/^(\d+(?:\.\d+)?)(.*)$/);
    if (!match) return;
    const target = parseFloat(match[1]), suffix = match[2], start = performance.now() + 250, span = 1400;
    el.textContent = '0' + suffix;
    (function tick(now) {
      const t = Math.min(1, Math.max(0, (now - start) / span));
      el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3))) + suffix;
      if (t < 1) requestAnimationFrame(tick);
    })(start);
  });

  if (!fine) return;

  // The light that follows the pointer.
  const glow = document.createElement('div');
  glow.className = 'glow';
  document.body.appendChild(glow);
  let gx = -999, gy = -999, tx = -999, ty = -999;
  document.addEventListener('pointermove', function (e) { tx = e.clientX; ty = e.clientY; });
  (function follow() {
    gx += (tx - gx) * 0.12; gy += (ty - gy) * 0.12;
    glow.style.setProperty('--gx', gx + 'px'); glow.style.setProperty('--gy', gy + 'px');
    requestAnimationFrame(follow);
  })();

  // Card spotlight, and tilt for lesson cards.
  document.querySelectorAll('.card').forEach(function (card) {
    card.addEventListener('pointermove', function (e) {
      const r = card.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      card.style.setProperty('--mx', x + 'px'); card.style.setProperty('--my', y + 'px');
      if (card.classList.contains('lesson-card')) {
        card.style.setProperty('--ry', ((x / r.width - 0.5) * 10).toFixed(2) + 'deg');
        card.style.setProperty('--rx', ((0.5 - y / r.height) * 10).toFixed(2) + 'deg');
      }
    });
    card.addEventListener('pointerleave', function () {
      ['--mx', '--my', '--rx', '--ry'].forEach(function (p) { card.style.removeProperty(p); });
    });
  });

  // Magnetic buttons, with a ripple where they are pressed.
  document.querySelectorAll('.beam, .gold-btn').forEach(function (btn) {
    btn.addEventListener('pointermove', function (e) {
      const r = btn.getBoundingClientRect();
      btn.style.transform = 'translate(' + ((e.clientX - r.left - r.width / 2) * 0.18).toFixed(1) + 'px,' +
        ((e.clientY - r.top - r.height / 2) * 0.3 - 2).toFixed(1) + 'px)';
    });
    btn.addEventListener('pointerleave', function () { btn.style.transform = ''; });
    btn.addEventListener('pointerdown', function (e) {
      const r = btn.getBoundingClientRect(), size = Math.max(r.width, r.height), dot = document.createElement('span');
      dot.className = 'ripple';
      dot.style.cssText = 'width:' + size + 'px;height:' + size + 'px;left:' + (e.clientX - r.left - size / 2) + 'px;top:' + (e.clientY - r.top - size / 2) + 'px';
      btn.appendChild(dot);
      setTimeout(function () { dot.remove(); }, 650);
    });
  });
})();
