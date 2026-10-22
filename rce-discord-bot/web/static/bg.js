// Animated neon background for the admin panel: a synthwave grid rushing towards you, a glowing striped sun on the
// horizon and embers drifting up. One <canvas> behind everything (z-index -1).
// Cheap on purpose: ~30 fps, pauses while the tab is hidden or the light theme is on, draws a single still frame
// when the system asks for reduced motion.
(() => {
  "use strict";
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const root = document.documentElement;
  const canvas = document.createElement("canvas");
  canvas.id = "bg";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) { canvas.remove(); return; }

  const FRAME_MS = 1000 / 30;
  const ORANGE = [255, 122, 26], HOT = [255, 61, 0], GOLD = [255, 179, 71];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  let W = 0, H = 0, dpr = 1, horizon = 0;
  let embers = [];
  let running = false, last = 0, t = 0;

  const isLight = () => root.dataset.theme === "light";

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 1.25);          // sharp enough, much cheaper than 2-3x
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    horizon = Math.round(H * 0.62);
    const count = Math.max(24, Math.min(80, Math.round((W * H) / 24000)));
    embers = Array.from({ length: count }, () => ember(true));
    if (!running) draw(0);                                         // keep the still frame right after a resize
  }

  function ember(anywhere) {
    return {
      x: Math.random() * W, y: anywhere ? Math.random() * H : H + 10,
      r: 0.6 + Math.random() * 1.9, v: 12 + Math.random() * 38, sway: 8 + Math.random() * 26,
      ph: Math.random() * 6.28, tw: 0.6 + Math.random() * 1.8, hot: Math.random() < 0.3,
    };
  }

  // sky colour at a given y (so the sun's stripes blend in perfectly)
  function sky(y) {
    const k = Math.max(0, Math.min(1, y / horizon));
    const top = [6, 4, 10], mid = [20, 8, 10], low = [58, 16, 6];
    const mix = (a, b, f) => a.map((v, i) => Math.round(v + (b[i] - v) * f));
    return k < 0.7 ? mix(top, mid, k / 0.7) : mix(mid, low, (k - 0.7) / 0.3);
  }

  function draw(time) {
    // sky + ground
    const g = ctx.createLinearGradient(0, 0, 0, horizon);
    g.addColorStop(0, rgba(sky(0), 1)); g.addColorStop(0.7, rgba(sky(horizon * 0.7), 1)); g.addColorStop(1, rgba(sky(horizon), 1));
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, horizon);
    ctx.fillStyle = "#07050a"; ctx.fillRect(0, horizon, W, H - horizon);

    // sun with stripes
    const sunR = Math.min(W, H) * 0.19, sx = W / 2, sy = horizon;
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, W, horizon); ctx.clip();
    const halo = ctx.createRadialGradient(sx, sy, sunR * 0.2, sx, sy, sunR * 3.1);
    halo.addColorStop(0, rgba(ORANGE, 0.55)); halo.addColorStop(0.35, rgba(HOT, 0.18)); halo.addColorStop(1, rgba(HOT, 0));
    ctx.fillStyle = halo; ctx.fillRect(0, 0, W, horizon);
    const sun = ctx.createLinearGradient(0, sy - sunR, 0, sy);
    sun.addColorStop(0, rgba(GOLD, 1)); sun.addColorStop(0.55, rgba(ORANGE, 1)); sun.addColorStop(1, rgba(HOT, 1));
    ctx.fillStyle = sun; ctx.beginPath(); ctx.arc(sx, sy, sunR, Math.PI, 0); ctx.fill();
    // dark stripes that slide down the sun and get thicker towards the horizon
    const top = sy - sunR * 0.72, span = sy - top, gap = sunR * 0.13, slide = (t * 9) % gap;
    for (let y = top + slide; y < sy; y += gap) {
      const h = 1 + ((y - top) / span) * gap * 0.6;
      ctx.fillStyle = rgba(sky(y), 1); ctx.fillRect(sx - sunR, y, sunR * 2, h);
    }
    ctx.restore();

    // horizon glow line
    const hl = ctx.createLinearGradient(0, 0, W, 0);
    hl.addColorStop(0, rgba(ORANGE, 0)); hl.addColorStop(0.5, rgba(GOLD, 0.95)); hl.addColorStop(1, rgba(ORANGE, 0));
    ctx.fillStyle = hl; ctx.fillRect(0, horizon - 1, W, 2);

    // perspective grid
    const gh = H - horizon;
    ctx.lineCap = "round";
    // horizontal lines rushing towards the viewer
    const lines = 16, scroll = (t * 0.18) % 1;
    for (let i = 0; i < lines; i++) {
      const p = (i + scroll) / lines;                              // 0 (far) .. 1 (near)
      const y = horizon + gh * Math.pow(p, 2.6);
      const a = 0.08 + 0.7 * p;
      ctx.strokeStyle = rgba(HOT, a * 0.35); ctx.lineWidth = 5 * p + 2;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      ctx.strokeStyle = rgba(ORANGE, a); ctx.lineWidth = 0.6 + 1.2 * p;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    // vertical lines fanning out from the vanishing point
    const spread = W * 1.5, n = 22;
    for (let k = -n; k <= n; k++) {
      const xb = W / 2 + (k / n) * spread * 0.62;
      const a = 0.55 - Math.abs(k) / (n * 2.4);
      ctx.strokeStyle = rgba(HOT, a * 0.3); ctx.lineWidth = 4;
      ctx.beginPath(); ctx.moveTo(W / 2, horizon); ctx.lineTo(xb, H); ctx.stroke();
      ctx.strokeStyle = rgba(ORANGE, a); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(W / 2, horizon); ctx.lineTo(xb, H); ctx.stroke();
    }
    // fade the grid into the horizon haze
    const haze = ctx.createLinearGradient(0, horizon, 0, horizon + gh * 0.35);
    haze.addColorStop(0, "rgba(58,16,6,.95)"); haze.addColorStop(1, "rgba(7,5,10,0)");
    ctx.fillStyle = haze; ctx.fillRect(0, horizon, W, gh * 0.35);

    // embers
    ctx.globalCompositeOperation = "lighter";
    for (const e of embers) {
      const tw = 0.45 + 0.55 * Math.sin(time * e.tw + e.ph);
      const x = e.x + Math.sin(time * 0.5 + e.ph) * e.sway;
      const c = e.hot ? GOLD : ORANGE;
      const glow = ctx.createRadialGradient(x, e.y, 0, x, e.y, e.r * 6);
      glow.addColorStop(0, rgba(c, 0.55 * tw)); glow.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = glow; ctx.fillRect(x - e.r * 6, e.y - e.r * 6, e.r * 12, e.r * 12);
      ctx.fillStyle = rgba([255, 235, 200], 0.9 * tw);
      ctx.beginPath(); ctx.arc(x, e.y, e.r * 0.7, 0, 6.283); ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";

    // vignette so the content stays readable
    const vg = ctx.createRadialGradient(W / 2, H * 0.55, Math.min(W, H) * 0.25, W / 2, H * 0.55, Math.max(W, H) * 0.8);
    vg.addColorStop(0, "rgba(0,0,0,0)"); vg.addColorStop(1, "rgba(0,0,0,.55)");
    ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H);
  }

  function step(now) {
    if (!running) return;
    requestAnimationFrame(step);
    if (now - last < FRAME_MS) return;
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now; t += dt;
    for (const e of embers) {
      e.y -= e.v * dt;
      if (e.y < -12) Object.assign(e, ember(false), { y: H + 10 });
    }
    draw(t);
  }

  function start() {
    if (running || reduce || isLight() || document.hidden) return;
    running = true; last = performance.now(); requestAnimationFrame(step);
  }
  function stop() { running = false; }
  function sync() { if (isLight() || document.hidden) stop(); else start(); }

  window.addEventListener("resize", () => { clearTimeout(resize.timer); resize.timer = setTimeout(resize, 150); });
  document.addEventListener("visibilitychange", sync);
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["data-theme"] });

  resize();
  draw(0);
  start();
})();
