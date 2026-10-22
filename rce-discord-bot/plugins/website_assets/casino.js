// Casino on the player website: plays without reloading the page, counts the balance up/down when a spin ends
// and fires a short confetti burst on a win. Without this script everything still works (plain forms + CSS).
// Loaded from the website itself only (Content-Security-Policy: script-src 'self').
(() => {
  "use strict";
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let busy = false;

  // seconds until the running spin ends (the --t variable the page sets on the game card)
  function spinTime(root) {
    const el = root.querySelector("[style*='--t']");
    const t = el ? parseFloat(getComputedStyle(el).getPropertyValue("--t")) : 0;
    return reduce || !isFinite(t) ? 0 : t;
  }

  // -------------------------------------------------------- Play without reloading
  document.addEventListener("submit", async (ev) => {
    const form = ev.target;
    const box = document.querySelector(".casino");
    if (!box || !box.contains(form) || (form.method || "").toLowerCase() !== "post") return;
    ev.preventDefault();
    if (busy) return;
    busy = true;
    form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const res = await fetch(form.action, {
        method: "POST", body: new URLSearchParams(new FormData(form)),
        credentials: "same-origin", headers: { "Accept": "text/html" },
      });
      if (!res.ok && res.status !== 404) throw new Error(String(res.status));
      const page = new DOMParser().parseFromString(await res.text(), "text/html");
      const fresh = page.querySelector(".casino");
      if (!fresh) throw new Error("no casino");
      box.replaceWith(fresh);
      if (page.title) document.title = page.title;
      play(fresh);
    } catch (err) {
      // trouble: open the game page normally (never sends the form twice – a bet could be placed twice)
      window.location.href = form.action;
      return;
    }
    busy = false;
  });

  // -------------------------------------------------------- After a spin: count the balance, celebrate a win
  function play(root) {
    const t = spinTime(root) * 1000;
    const swap = root.querySelector(".swap[data-before]");
    if (swap) setTimeout(() => countBalance(swap), t);
    const win = root.querySelector(".result.win");
    if (win && !reduce) setTimeout(() => confetti(win), t + 150);
  }

  function countBalance(swap) {
    const from = Number(swap.dataset.before), to = Number(swap.dataset.after);
    const shown = swap.querySelector(".reveal");
    if (!shown || !isFinite(from) || !isFinite(to)) return;
    const unit = shown.textContent.replace(/^[\d\s.,]*\d/, "");      // " points"
    const fmt = (n) => Math.round(n).toLocaleString("en-US").replace(/,/g, " ") + unit;
    const start = performance.now(), dur = reduce ? 0 : 900;
    const step = (now) => {
      const k = dur ? Math.min(1, (now - start) / dur) : 1;
      shown.textContent = fmt(from + (to - from) * (1 - Math.pow(1 - k, 3)));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function confetti(anchor) {
    const canvas = document.createElement("canvas");
    const w = window.innerWidth, h = window.innerHeight;
    canvas.width = w; canvas.height = h;
    canvas.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:50";
    document.body.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const r = anchor.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const colours = ["#e5341c", "#f7931e", "#ffb627", "#3ddc6a", "#ffffff"];
    const bits = Array.from({ length: 90 }, () => {
      const a = Math.random() * Math.PI * 2, v = 4 + Math.random() * 7;
      return { x: cx, y: cy, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 4, s: 4 + Math.random() * 5,
        rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4, c: colours[(Math.random() * colours.length) | 0] };
    });
    const start = performance.now();
    const frame = (now) => {
      const age = (now - start) / 1000;
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = Math.max(0, 1 - age / 1.6);
      for (const b of bits) {
        b.vy += 0.25; b.vx *= 0.99; b.x += b.vx; b.y += b.vy; b.rot += b.vr;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.rot);
        ctx.fillStyle = b.c; ctx.fillRect(-b.s / 2, -b.s / 4, b.s, b.s / 2);
        ctx.restore();
      }
      if (age < 1.6) requestAnimationFrame(frame); else canvas.remove();
    };
    requestAnimationFrame(frame);
  }

  // a page that was loaded normally (no script round trip) after a spin
  const first = document.querySelector(".casino");
  if (first) play(first);
})();
