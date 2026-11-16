// Player website: live touches that work without it too – the wipe countdown ticks every second and the
// player count refreshes from status.json. No tracking, no outside requests.
(() => {
  "use strict";
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
})();
