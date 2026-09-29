/*
 * The drifting constellation background — adapted from ThreeUI Community's
 * "Constellation Field" (https://github.com/MengTo/threeui, MIT © 2026 Meng To).
 * Paused while the tab is hidden, off for prefers-reduced-motion.
 */
(function () {
  'use strict';
  const canvas = document.getElementById('sky');
  if (!canvas || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const ctx = canvas.getContext('2d');
  const LINK = 150;
  let width = 0, height = 0, nodes = [], running = true;
  const pointer = { x: -1000, y: -1000 };

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth; height = window.innerHeight;
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = width < 768 ? 36 : 70;
    nodes = Array.from({ length: count }, function () {
      return {
        x: Math.random() * width, y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3,
        r: Math.random() * 1.8 + 1.2,
      };
    });
  }

  function frame() {
    if (!running) return;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = '#E6C879';
    ctx.lineWidth = 1;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
        if (d < LINK) {
          ctx.globalAlpha = 0.12 + (1 - d / LINK) * 0.4;
          ctx.beginPath();
          ctx.moveTo(nodes[i].x, nodes[i].y);
          ctx.lineTo(nodes[j].x, nodes[j].y);
          ctx.stroke();
        }
      }
    }
    const now = Date.now();
    nodes.forEach(function (n) {
      n.x += n.vx; n.y += n.vy;
      if (n.x < 0 || n.x > width) n.vx *= -1;
      if (n.y < 0 || n.y > height) n.vy *= -1;
      if (Math.hypot(n.x - pointer.x, n.y - pointer.y) < 220) {
        n.x -= (n.x - pointer.x) * 0.005;
        n.y -= (n.y - pointer.y) * 0.005;
      }
      const pulse = 0.78 + Math.sin(now * 0.001 + n.x) * 0.22;
      ctx.fillStyle = '#E6C879';
      ctx.globalAlpha = pulse * 0.25;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r * 2.4, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = pulse;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize);
  document.addEventListener('mousemove', function (e) { pointer.x = e.clientX; pointer.y = e.clientY; });
  document.addEventListener('mouseleave', function () { pointer.x = pointer.y = -1000; });
  document.addEventListener('visibilitychange', function () {
    running = !document.hidden;
    if (running) requestAnimationFrame(frame);
  });
  resize();
  requestAnimationFrame(frame);
})();
