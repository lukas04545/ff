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
