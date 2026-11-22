// Stream overlay: fills in the live numbers and refreshes them every 15 seconds (data.json).
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let data = {};
  try { data = JSON.parse(document.body.dataset.init || "{}"); } catch (e) { data = {}; }
  let skew = data.now ? data.now - Date.now() / 1000 : 0;          // the server's clock
  let shown = null;

  function left(at) {
    const s = Math.max(0, Math.floor(at - (Date.now() / 1000 + skew)));
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}D ${h}H` : h ? `${h}H ${m}M` : `${m}M ${s % 60}S`;
  }

  function draw() {
    $("name").textContent = data.name || "";
    const wipe = data.wipe ? "WIPE IN " + left(data.wipe) : "";
    $("tags").textContent = data.tags || wipe || (data.online ? "ONLINE NOW" : "OFFLINE");
    const count = data.online ? (data.max ? `${data.players}/${data.max}` : String(data.players)) : "OFFLINE";
    if (count !== shown) {
      const el = $("players").parentElement;
      el.classList.remove("bump"); void el.offsetWidth; if (shown !== null) el.classList.add("bump");
      $("players").textContent = count; shown = count;
    }
    $("dot").className = "dot" + (data.online ? " on" : "");
    $("players").parentElement.classList.toggle("off", !data.online);
    $("lbl").textContent = !data.online ? "SERVER" : data.queued ? `PLAYERS · ${data.queued} QUEUED`
      : data.tags && data.wipe ? "PLAYERS · WIPE IN " + left(data.wipe) : "PLAYERS";
  }

  async function refresh() {
    try {
      const r = await fetch("data.json", { cache: "no-store" });
      if (r.ok) { data = await r.json(); skew = data.now - Date.now() / 1000; draw(); }
    } catch (e) { /* keep the last numbers, try again */ }
  }
  draw();
  setInterval(draw, 1000);                    // the wipe countdown
  setInterval(refresh, 15000);
})();
