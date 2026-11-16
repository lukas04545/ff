// Player website: live touches that work without it too – the wipe countdown ticks every second and the
// player count refreshes from status.json. No tracking, no outside requests.
(() => {
  "use strict";
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const mouse = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  // ---- things come in as you scroll to them (once), a row of cards one after the other
  if (!calm && "IntersectionObserver" in window) {
    const groups = ".stats,.tiles,.grid,.two,.podium,.people,.rulelist,.online";
    const items = [];
    for (const el of document.querySelectorAll("main > *")) {
      if (el.matches(groups)) [...el.children].forEach((kid, i) => { kid.style.setProperty("--i", Math.min(i, 8)); items.push(kid); });
      else items.push(el);
    }
    const seen = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); seen.unobserve(e.target); count(e.target); }
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    for (const el of items) { el.classList.add("rv"); seen.observe(el); }
    document.documentElement.classList.add("js");
    setTimeout(() => items.forEach((el) => el.classList.add("in")), 4000);       // never leave anything hidden
  }

  // ---- numbers count up when they come into view
  function count(root) {
    for (const el of root.matches("[data-count]") ? [root] : root.querySelectorAll("[data-count]")) {
      const end = parseInt(el.textContent, 10);
      if (!Number.isFinite(end) || end < 2 || el.dataset.done) continue;
      el.dataset.done = "1";
      const t0 = performance.now(), ms = Math.min(1400, 500 + end * 6);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / ms);
        el.textContent = Math.round(end * (1 - Math.pow(1 - k, 3)));
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
  }

  // ---- how far down the page: the thin bar at the top
  const prog = document.querySelector(".prog");
  if (prog && !calm) {
    let queued = false;
    const draw = () => {
      queued = false;
      const max = document.documentElement.scrollHeight - innerHeight;
      prog.style.setProperty("--p", max > 200 ? Math.min(1, scrollY / max).toFixed(4) : 0);
    };
    addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(draw); } }, { passive: true });
    draw();
  }

  // ---- with a mouse: a soft light follows the pointer over cards, the logo and the podium tilt a little
  if (mouse && !calm) {
    document.addEventListener("pointermove", (e) => {
      const card = e.target.closest?.(".card,a.tile");
      if (card) {
        const r = card.getBoundingClientRect();
        card.style.setProperty("--mx", (e.clientX - r.left) + "px");
        card.style.setProperty("--my", (e.clientY - r.top) + "px");
      }
    }, { passive: true });
    for (const el of document.querySelectorAll("[data-tilt],.podium .card")) {
      const max = +el.dataset.tilt || 6;
      el.classList.add("tilt");
      el.addEventListener("pointermove", (e) => {
        const r = el.getBoundingClientRect();
        el.style.setProperty("--ry", (((e.clientX - r.left) / r.width - 0.5) * 2 * max).toFixed(2) + "deg");
        el.style.setProperty("--rx", (-((e.clientY - r.top) / r.height - 0.5) * 2 * max).toFixed(2) + "deg");
      });
      el.addEventListener("pointerleave", () => { el.style.setProperty("--rx", "0deg"); el.style.setProperty("--ry", "0deg"); });
    }
  }

  const pad = (n) => String(n).padStart(2, "0");
  function tick() {
    const now = Date.now() / 1000;
    for (const el of document.querySelectorAll("[data-until]")) {
      let left = Math.max(0, Math.floor(+el.dataset.until - now));
      const d = Math.floor(left / 86400), h = Math.floor(left % 86400 / 3600), m = Math.floor(left % 3600 / 60), s = left % 60;
      const parts = { d, h: pad(h), m: pad(m), s: pad(s) };
      for (const [k, v] of Object.entries(parts)) {
        const cell = el.querySelector(`[data-u="${k}"]`);
        if (cell && cell.textContent !== String(v)) cell.textContent = v;
      }
      if (left === 0) el.classList.add("done");
    }
  }
  if (document.querySelector("[data-until]")) { tick(); setInterval(tick, 1000); }

  const base = document.body.dataset.base || "";
  async function refresh() {
    if (document.hidden || !document.querySelector("[data-live]")) return;
    try {
      const r = await fetch(base + "/status.json", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      const text = s.online ? (s.max_players ? `${s.players} / ${s.max_players}` : String(s.players)) : "–";
      for (const el of document.querySelectorAll('[data-live="players"]')) el.textContent = text;
      for (const el of document.querySelectorAll('[data-live="meter"]')) {
        el.style.width = s.online && s.max_players ? Math.min(100, Math.round(100 * s.players / s.max_players)) + "%" : "0";
      }
    } catch (e) { /* the next round tries again */ }
  }
  setInterval(refresh, 30000);

  // "Copy" next to the server's name (to paste it into the game's server search)
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) {
      try {
        await navigator.clipboard.writeText(b.dataset.copy);
        const label = b.querySelector("span"), old = label.textContent;
        b.classList.add("ok");
        label.textContent = "Copied";
        setTimeout(() => { b.classList.remove("ok"); label.textContent = old; }, 1600);
      } catch (err) { /* no clipboard: the name is right there to type */ }
      return;
    }
    // the "More" menu closes when you click anywhere else
    for (const d of document.querySelectorAll("header details[open]")) if (!d.contains(e.target)) d.open = false;
  });
  // the phone menu closes when a page in it is picked
  document.querySelector(".sheet")?.addEventListener("click", (e) => {
    if (e.target.closest("a")) document.getElementById("mnav").checked = false;
  });
})();
