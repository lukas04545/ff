// Animated neon background for the admin panel: a synthwave grid gliding towards you, a glowing striped sun that
// breathes on the horizon, embers drifting up, the odd shooting star, and a gentle parallax following the mouse.
// One <canvas> behind everything (z-index -1).
// Cheap on purpose: 30 frames a second at normal resolution, the sky/vignette are drawn once into a cached image,
// the embers are pre-rendered sprites. Pauses while the tab is hidden or the light theme is on; a single still frame
// when the system asks for reduced motion, when animations are switched off (✨ button, html[data-motion="off"]),
// or when the computer can't keep up (it switches itself to the still frame).
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

  const ORANGE = [255, 122, 26], HOT = [255, 61, 0], GOLD = [255, 179, 71], CYAN = [0, 240, 255], VIOLET = [177, 75, 255];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const lerp = (a, b, f) => a + (b - a) * f;
  let W = 0, H = 0, dpr = 1, horizon = 0, sunR = 0;
  let sky = null, vignette = null;                    // cached layers
  const sprites = {};                                 // ember glow sprites
  let embers = [], stars = [], shooting = null;
  let running = false, last = 0, t = 0, drawn = 0;
  let slowFrames = 0, slow = false;                   // can't keep up -> still frame
  const FRAME = 1000 / 30;
  let mx = 0, my = 0, px = 0, py = 0;                 // mouse target / smoothed parallax (-1..1)

  const isLight = () => root.dataset.theme === "light";
  const motionOff = () => root.dataset.motion === "off";
  const layer = (w, h) => { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; };

  function skyColor(y) {
    const k = Math.max(0, Math.min(1, y / horizon));
    const top = [5, 3, 12], mid = [22, 7, 18], low = [70, 18, 8];
    const f = k < 0.65 ? k / 0.65 : (k - 0.65) / 0.35;
    const [a, b] = k < 0.65 ? [top, mid] : [mid, low];
    return [0, 1, 2].map((i) => Math.round(lerp(a[i], b[i], f)));
  }

  function buildLayers() {
    // sky with stars and the sun's glow – drawn once per resize
    const w = Math.round(W * dpr), h = Math.round(H * dpr);
    sky = layer(w, h);
    const s = sky.getContext("2d");
    s.setTransform(dpr, 0, 0, dpr, 0, 0);
    const g = s.createLinearGradient(0, 0, 0, horizon);
    for (const stop of [0, 0.4, 0.65, 0.85, 1]) g.addColorStop(stop, rgba(skyColor(horizon * stop), 1));
    s.fillStyle = g; s.fillRect(0, 0, W, horizon);
    s.fillStyle = "#06040a"; s.fillRect(0, horizon, W, H - horizon);
    const halo = s.createRadialGradient(W / 2, horizon, sunR * 0.3, W / 2, horizon, sunR * 3.4);
    halo.addColorStop(0, rgba(ORANGE, 0.5)); halo.addColorStop(0.3, rgba(HOT, 0.16));
    halo.addColorStop(0.6, rgba(VIOLET, 0.06)); halo.addColorStop(1, rgba(VIOLET, 0));
    s.fillStyle = halo; s.fillRect(0, 0, W, horizon);
    // vignette keeps the panels readable
    vignette = layer(w, h);
    const v = vignette.getContext("2d");
    v.setTransform(dpr, 0, 0, dpr, 0, 0);
    const vg = v.createRadialGradient(W / 2, H * 0.55, Math.min(W, H) * 0.22, W / 2, H * 0.55, Math.max(W, H) * 0.82);
    vg.addColorStop(0, "rgba(0,0,0,0)"); vg.addColorStop(1, "rgba(0,0,0,.6)");
    v.fillStyle = vg; v.fillRect(0, 0, W, H);
  }

  function buildSprites() {
    for (const [key, c] of [["o", ORANGE], ["g", GOLD], ["c", CYAN]]) {
      const size = 48, sp = layer(size, size), x = sp.getContext("2d");
      const r = x.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
      r.addColorStop(0, "rgba(255,240,220,1)"); r.addColorStop(0.12, rgba(c, 0.95));
      r.addColorStop(0.4, rgba(c, 0.25)); r.addColorStop(1, rgba(c, 0));
      x.fillStyle = r; x.fillRect(0, 0, size, size);
      sprites[key] = sp;
    }
  }

  function ember(anywhere) {
    const roll = Math.random();
    return {
      x: Math.random() * W, y: anywhere ? Math.random() * H : H + 20,
      s: 5 + Math.random() * 13, v: 14 + Math.random() * 40, sway: 10 + Math.random() * 30,
      ph: Math.random() * 6.28, tw: 0.5 + Math.random() * 1.6, depth: 0.3 + Math.random() * 0.7,
      kind: roll < 0.12 ? "c" : roll < 0.4 ? "g" : "o",
    };
  }

  function resize() {
    dpr = 1;                                          // a soft background doesn't need retina pixels
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    horizon = Math.round(H * 0.62);
    sunR = Math.min(W, H) * 0.19;
    buildLayers();
    const count = Math.max(20, Math.min(55, Math.round((W * H) / 30000)));
    embers = Array.from({ length: count }, () => ember(true));
    stars = Array.from({ length: Math.round(count * 0.8) }, () => ({
      x: Math.random() * W, y: Math.random() * horizon * 0.8, r: Math.random() * 1.1 + 0.2, ph: Math.random() * 6.28,
    }));
    if (!running) draw(0);
  }

  function drawSun(time, ox) {
    const sx = W / 2 + ox * 0.3, sy = horizon, breathe = 1 + Math.sin(time * 0.8) * 0.015;
    const r = sunR * breathe;
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, W, horizon); ctx.clip();
    const glow = ctx.createRadialGradient(sx, sy, r * 0.8, sx, sy, r * 1.6);
    glow.addColorStop(0, rgba(ORANGE, 0.35 + Math.sin(time * 0.8) * 0.06)); glow.addColorStop(1, rgba(ORANGE, 0));
    ctx.fillStyle = glow; ctx.fillRect(sx - r * 1.7, sy - r * 1.7, r * 3.4, r * 1.7);
    const body = ctx.createLinearGradient(0, sy - r, 0, sy);
    body.addColorStop(0, rgba(GOLD, 1)); body.addColorStop(0.5, rgba(ORANGE, 1)); body.addColorStop(1, rgba(HOT, 1));
    ctx.fillStyle = body; ctx.beginPath(); ctx.arc(sx, sy, r, Math.PI, 0); ctx.fill();
    // stripes glide down and thicken towards the horizon
    const top = sy - r * 0.75, span = sy - top, gap = r * 0.12, slide = (time * 7) % gap;
    for (let y = top + slide; y < sy; y += gap) {
      const k = (y - top) / span;
      ctx.fillStyle = rgba(skyColor(y), 1); ctx.fillRect(sx - r - 2, y, r * 2 + 4, 0.8 + k * gap * 0.62);
    }
    ctx.restore();
  }

  function drawGrid(time, ox) {
    const gh = H - horizon, vx = W / 2 + ox * 0.6;
    // horizon line: orange core with cyan edges
    const hl = ctx.createLinearGradient(0, 0, W, 0);
    hl.addColorStop(0, rgba(CYAN, 0)); hl.addColorStop(0.25, rgba(CYAN, 0.55)); hl.addColorStop(0.5, rgba(GOLD, 1));
    hl.addColorStop(0.75, rgba(CYAN, 0.55)); hl.addColorStop(1, rgba(CYAN, 0));
    ctx.fillStyle = hl; ctx.fillRect(0, horizon - 1, W, 2);
    ctx.lineCap = "round";
    // horizontal lines gliding towards the viewer (eased spacing = depth)
    const lines = 18, scroll = (time * 0.16) % 1;
    for (let i = 0; i < lines; i++) {
      const p = (i + scroll) / lines;
      const y = horizon + gh * Math.pow(p, 2.4);
      const a = Math.min(1, p * 1.25);
      ctx.strokeStyle = rgba(ORANGE, 0.15 + a * 0.75); ctx.lineWidth = 0.7 + 1.3 * p;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    // lines fanning out from the vanishing point (cyan towards the edges)
    const n = 22;
    for (let k = -n; k <= n; k++) {
      const f = Math.abs(k) / n, xb = vx + (k / n) * W * 0.95;
      const c = f > 0.6 ? CYAN : ORANGE, a = 0.55 - f * 0.32;
      ctx.strokeStyle = rgba(c, a); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(vx, horizon); ctx.lineTo(xb, H); ctx.stroke();
    }
    // haze where the grid meets the horizon
    const haze = ctx.createLinearGradient(0, horizon, 0, horizon + gh * 0.3);
    haze.addColorStop(0, "rgba(70,18,8,.95)"); haze.addColorStop(1, "rgba(6,4,10,0)");
    ctx.fillStyle = haze; ctx.fillRect(0, horizon, W, gh * 0.3);
  }

  function drawSkyBits(time, ox, oy) {
    for (const s of stars) {
      const a = 0.25 + 0.45 * (0.5 + 0.5 * Math.sin(time * 1.3 + s.ph));
      ctx.fillStyle = `rgba(255,236,220,${a})`;
      ctx.fillRect(s.x + ox * 0.15, s.y + oy * 0.1, s.r, s.r);
    }
    if (shooting) {
      const k = (time - shooting.t0) / shooting.dur;
      if (k >= 1) shooting = null;
      else {
        const x = lerp(shooting.x0, shooting.x1, k), y = lerp(shooting.y0, shooting.y1, k);
        const tail = ctx.createLinearGradient(x, y, x - shooting.dx * 0.18, y - shooting.dy * 0.18);
        const fade = Math.sin(k * Math.PI);
        tail.addColorStop(0, rgba(CYAN, 0.9 * fade)); tail.addColorStop(1, rgba(VIOLET, 0));
        ctx.strokeStyle = tail; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - shooting.dx * 0.18, y - shooting.dy * 0.18); ctx.stroke();
      }
    } else if (Math.random() < 0.0025) {
      const x0 = Math.random() * W * 0.8, y0 = Math.random() * horizon * 0.35, dx = W * (0.25 + Math.random() * 0.2);
      shooting = { t0: time, dur: 1.1, x0, y0, x1: x0 + dx, y1: y0 + dx * 0.32, dx, dy: dx * 0.32 };
    }
  }

  function drawEmbers(time, ox, oy) {
    ctx.globalCompositeOperation = "lighter";
    for (const e of embers) {
      const tw = 0.4 + 0.6 * (0.5 + 0.5 * Math.sin(time * e.tw + e.ph));
      const x = e.x + Math.sin(time * 0.45 + e.ph) * e.sway + ox * e.depth * 0.5;
      const y = e.y + oy * e.depth * 0.3;
      const size = e.s * (0.7 + 0.3 * tw);
      ctx.globalAlpha = tw * (0.45 + e.depth * 0.55);
      ctx.drawImage(sprites[e.kind], x - size / 2, y - size / 2, size, size);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }

  function draw(time) {
    const ox = px * 22, oy = py * 12;
    ctx.drawImage(sky, 0, 0, W, H);
    drawSkyBits(time, ox, oy);
    drawSun(time, ox);
    drawGrid(time, ox);
    drawEmbers(time, ox, oy);
    ctx.drawImage(vignette, 0, 0, W, H);
  }

  function step(now) {
    if (!running) return;
    requestAnimationFrame(step);
    if (now - drawn < FRAME - 2) return;              // 30 fps is plenty for a background
    const took = now - last;
    drawn = now;
    // frames keep coming late (> 2 per 30 fps slot): the computer is busy – stop and keep a still frame
    slowFrames = took > FRAME * 2.2 ? slowFrames + 1 : Math.max(0, slowFrames - 1);
    if (slowFrames > 45) { slow = true; stop(); return; }
    const dt = Math.min(took / 1000, 0.05);           // frame-rate independent, no jumps after a stall
    last = now; t += dt;
    px += (mx - px) * Math.min(1, dt * 2.5);         // parallax glides to the mouse
    py += (my - py) * Math.min(1, dt * 2.5);
    for (const e of embers) {
      e.y -= e.v * e.depth * dt;
      if (e.y < -20) Object.assign(e, ember(false));
    }
    draw(t);
  }

  function start() {
    if (running || reduce || slow || motionOff() || isLight() || document.hidden) return;
    running = true; last = drawn = performance.now(); slowFrames = 0; requestAnimationFrame(step);
  }
  function stop() { running = false; }
  function sync() { if (isLight() || document.hidden || motionOff()) stop(); else start(); }

  window.addEventListener("resize", () => { clearTimeout(resize.timer); resize.timer = setTimeout(resize, 150); });
  window.addEventListener("pointermove", (ev) => {
    mx = (ev.clientX / (W || 1)) * 2 - 1; my = (ev.clientY / (H || 1)) * 2 - 1;
  }, { passive: true });
  document.addEventListener("visibilitychange", sync);
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["data-theme", "data-motion"] });

  buildSprites();
  resize();
  draw(0);
  start();
})();
