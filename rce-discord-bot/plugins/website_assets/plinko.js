// Plinko on the player website: drops the balls without reloading the page. The server picks every ball's path
// (POST …/casino/plinko/drop); this script only animates it peg by peg on a canvas, bumps the slot it lands in,
// counts the balance and keeps a history. Drop again while balls are still falling. Without this script the form
// still works (the page shows where the balls landed). Loaded from the website itself only (script-src 'self').
(() => {
  "use strict";
  const root = document.getElementById("plinko");
  if (!root) return;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const form = root.querySelector("form");
  const canvas = root.querySelector("canvas");
  const ctx = canvas.getContext("2d");
  const hist = root.querySelector(".phist");
  const out = root.querySelector(".presult");
  const button = form.querySelector("button");
  const tables = JSON.parse(root.dataset.tables);
  const colours = JSON.parse(root.dataset.colours);
  const unit = root.dataset.unit || "";
  const SEG = reduce ? 0.03 : 0.17;            // seconds per row
  const STAGGER = reduce ? 0.02 : 0.11;        // seconds between balls of one drop

  let shown = Number(root.dataset.balance), server = shown;
  let balls = [], flashes = new Map(), bumps = new Map();
  let geo = null, running = false, pending = false;
  let state = { risk: value("risk"), rows: Number(value("rows")) };

  root.classList.add("live");
  // a page loaded after a plain form drop shows the old/new balance swap – show one live balance instead
  const bal = document.querySelector(".bal");
  const swap = bal && bal.querySelector(".swap");
  if (swap) { const p = document.createElement("span"); p.className = "price"; swap.replaceWith(p); }
  const balEl = bal && bal.querySelector(".price");
  showBalance();

  function value(name) {
    const el = form.querySelector(`[name="${name}"]:checked`) || form.querySelector(`select[name="${name}"]`);
    return el ? el.value : "";
  }
  function fmt(n) { return Math.round(n).toLocaleString("en-US").replace(/,/g, " ") + unit; }
  function showBalance() { if (balEl) balEl.textContent = fmt(shown); }
  function mult(cents) {
    const whole = Math.floor(cents / 100), part = cents % 100;
    return part ? (whole + "." + String(part).padStart(2, "0")).replace(/0$/, "") : String(whole);
  }

  // -------------------------------------------------------- Board geometry
  function layout() {
    const n = state.rows, w = canvas.clientWidth || 600, s = w / (n + 2);
    const rowH = s * 0.92, top = s * 0.9, pegR = Math.max(1.6, s * 0.11), ballR = Math.max(3, s * 0.27);
    const slotY = top + (n - 1) * rowH + rowH * 0.75, slotH = Math.max(18, s * 0.72);
    const h = slotY + slotH + 8, dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    geo = { n, w, s, rowH, top, pegR, ballR, slotY, slotH, h, cx: w / 2 };
  }
  const pegX = (r, j) => geo.cx + (j - 1 - r / 2) * geo.s;
  const pegY = (r) => geo.top + r * geo.rowH;

  // where a ball is t seconds after it was dropped
  function where(b, t) {
    const n = b.path.length, i = Math.floor(t / SEG), f = t / SEG - i;
    const point = (r) => {
      if (r < 0) return { x: geo.cx + b.jitter, y: geo.top - geo.rowH * 1.3 };
      const right = b.path.slice(0, r).reduce((a, v) => a + v, 0);
      const x = geo.cx + (right - r / 2) * geo.s;
      if (r >= n) return { x, y: geo.slotY + geo.slotH * 0.35 };
      return { x, y: pegY(r) - geo.pegR - geo.ballR };
    };
    const a = point(i - 1), z = point(i);
    const lift = i === 0 ? 0 : geo.rowH * 0.42;
    return { x: a.x + (z.x - a.x) * f, y: a.y + (z.y - a.y) * f * f - lift * 4 * f * (1 - f) * (1 - f), row: i };
  }

  // -------------------------------------------------------- Drawing
  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  function draw(now) {
    const { n, w, h, s, pegR, ballR, slotY, slotH } = geo;
    ctx.clearRect(0, 0, w, h);
    for (let r = 0; r < n; r++) {
      for (let j = 0; j < r + 3; j++) {
        const lit = flashes.get(r * 100 + j), k = lit ? Math.max(0, 1 - (now - lit) / 300) : 0;
        if (k > 0) {
          ctx.fillStyle = `rgba(255,255,255,${0.25 * k})`;
          ctx.beginPath(); ctx.arc(pegX(r, j), pegY(r), pegR * (1 + 2.2 * k), 0, 7); ctx.fill();
        }
        ctx.fillStyle = "#fff";
        ctx.beginPath(); ctx.arc(pegX(r, j), pegY(r), pegR, 0, 7); ctx.fill();
      }
    }
    const table = tables[`${state.risk}-${n}`], cols = colours[n];
    let size = Math.min(13, s * 0.36);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (let k = 0; k <= n; k++) {
      const hit = bumps.get(k), d = hit ? Math.max(0, 1 - (now - hit) / 350) : 0;
      const x = geo.cx + (k - n / 2) * s - s / 2 + 1.5, y = slotY + Math.sin(d * Math.PI) * 6;
      ctx.fillStyle = "rgba(0,0,0,.35)"; roundRect(x, y + 3, s - 3, slotH, 4); ctx.fill();
      ctx.fillStyle = cols[k]; roundRect(x, y, s - 3, slotH, 4); ctx.fill();
      const label = mult(table[k]) + (s > 34 ? "×" : "");
      let fs = size;
      ctx.font = `800 ${fs}px system-ui,sans-serif`;
      while (fs > 6 && ctx.measureText(label).width > s - 5) { fs -= 0.5; ctx.font = `800 ${fs}px system-ui,sans-serif`; }
      ctx.fillStyle = "#1a0900"; ctx.fillText(label, x + (s - 3) / 2, y + slotH / 2 + 0.5);
    }
    for (const b of balls) {
      const t = (now - b.start) / 1000;
      if (t < 0) continue;
      const p = where(b, t);
      const g = ctx.createRadialGradient(p.x - ballR * 0.35, p.y - ballR * 0.35, ballR * 0.1, p.x, p.y, ballR);
      g.addColorStop(0, "#ff9db0"); g.addColorStop(1, "#ff1f4b");
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, ballR, 0, 7); ctx.fill();
    }
  }

  function frame(now) {
    for (const b of balls.slice()) {
      const t = (now - b.start) / 1000;
      if (t < 0) continue;
      const row = Math.floor(t / SEG);
      while (b.row < row && b.row < b.path.length) {          // reached the peg of row b.row
        b.row++;
        if (b.row - 1 < b.path.length && b.row >= 1) {
          const r = b.row - 1, right = b.path.slice(0, r).reduce((a, v) => a + v, 0);
          flashes.set(r * 100 + right + 1, now);
        }
      }
      if (t >= SEG * (b.path.length + 1)) land(b, now);
    }
    draw(now);
    const busy = balls.length || [...bumps.values(), ...flashes.values()].some((v) => now - v < 400);
    if (busy) requestAnimationFrame(frame); else { running = false; unlock(); }
  }

  function kick() { if (!running) { running = true; requestAnimationFrame(frame); } }

  function land(b, now) {
    balls.splice(balls.indexOf(b), 1);
    bumps.set(b.slot, now);
    shown += b.payout;
    if (!balls.length) shown = server;                       // everything landed: show the real balance
    showBalance();
    const chip = document.createElement("div");
    chip.style.background = colours[geo.n][b.slot] || "#ffc000";
    chip.textContent = b.multiplier + "×";
    hist.prepend(chip);
    while (hist.children.length > 6) hist.lastChild.remove();
    let list = out.querySelector(".plist");
    if (!list) { out.innerHTML = '<div class="plist"></div>'; list = out.firstChild; }
    const line = document.createElement("div");
    const left = document.createElement("span"), right = document.createElement("b");
    left.textContent = b.multiplier + "× on " + fmt(b.bet);
    right.textContent = fmt(b.payout);
    if (b.payout > b.bet) right.style.color = "var(--ok)";
    line.append(left, right);
    list.prepend(line);
    while (list.children.length > 40) list.lastChild.remove();
  }

  // -------------------------------------------------------- Risk and rows (fixed while balls are falling)
  function unlock() { root.classList.remove("dropping"); }
  form.addEventListener("change", (ev) => {
    const name = ev.target.name;
    if (name !== "risk" && name !== "rows") return;
    if (balls.length) {                                      // can't change the board under falling balls
      const keep = name === "risk" ? state.risk : String(state.rows);
      form.querySelectorAll(`[name="${name}"]`).forEach((el) => {
        if (el.tagName === "SELECT") el.value = keep; else el.checked = el.value === keep;
      });
      return;
    }
    state = { risk: value("risk"), rows: Number(value("rows")) };
    layout(); draw(performance.now());
  });

  // -------------------------------------------------------- Drop
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (pending) return;
    if (!form.reportValidity()) return;
    pending = true; button.disabled = true;
    const body = new URLSearchParams(new FormData(form));
    try {
      const res = await fetch(root.dataset.drop, {
        method: "POST", body, credentials: "same-origin", headers: { "Accept": "application/json" },
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      if (data.error) {
        out.innerHTML = "";
        const p = document.createElement("p"); p.className = "notice bad"; p.textContent = data.error;
        out.append(p);
        if (!balls.length) { server = shown = data.balance; showBalance(); }
        return;
      }
      const err = out.querySelector(".notice"); if (err) err.remove();
      server = data.balance;
      shown -= data.bet * data.balls.length;
      showBalance();
      root.classList.add("dropping");
      const now = performance.now();
      data.balls.forEach((b, i) => balls.push({
        ...b, bet: data.bet, row: 0, start: now + i * STAGGER * 1000, jitter: (Math.random() - 0.5) * geo.s * 0.5,
      }));
      kick();
    } catch (err) {
      // trouble: reload the game page (never sends the bet twice)
      window.location.href = form.action;
    } finally {
      pending = false; button.disabled = false;
    }
  });

  window.addEventListener("resize", () => { layout(); draw(performance.now()); });
  layout();
  draw(performance.now());
})();
