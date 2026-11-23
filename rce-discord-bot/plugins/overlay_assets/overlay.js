// Stream overlay: fills in the live numbers and the look, and refreshes both every 15 seconds (data.json) – a
// changed setting shows on the stream by itself.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let data = {};
  try { data = JSON.parse(document.body.dataset.init || "{}"); } catch (e) { data = {}; }
  let skew = data.now ? data.now - Date.now() / 1000 : 0;          // the server's clock
  let shown = null, line = 0, lineAt = 0, lineText = null;

  function left(at) {
    const s = Math.max(0, Math.floor(at - (Date.now() / 1000 + skew)));
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}D ${h}H` : h ? `${h}H ${m}M` : `${m}M ${s % 60}S`;
  }

  function style() {
    const look = data.look || {}, b = document.body, show = look.show || {};
    for (const [k, v] of Object.entries(look.colours || {})) b.style.setProperty(k, v);
    b.style.setProperty("--alpha", (look.opacity ?? 100) + "%");
    b.style.setProperty("--edge", ((look.edge ?? 6) / 9) + "vw");
    b.className = [`f-${look.font || "game"}`, `s-${look.shape || "square"}`, look.upper !== false ? "upper" : "",
      look.outline ? "outline" : "", look.shadow !== false ? "shadow" : ""].filter(Boolean).join(" ");
    $("star").classList.toggle("hide", look.icon !== "star");
    const logo = $("logo");
    if (look.icon === "logo" && look.logo) {
      if (logo.getAttribute("src") !== look.logo) logo.src = look.logo;
      logo.hidden = false;
    } else logo.hidden = true;
    $("count").classList.toggle("hide", show.players === false);
    $("dot").classList.toggle("hide", show.dot === false);
    $("fill").classList.toggle("hide", !show.progress);
  }

  // the line under the name: your line, then your extra lines in turn (the wipe countdown when there's nothing)
  function lines() {
    const look = data.look || {}, out = [];
    if (data.tags) out.push(data.tags);
    for (const m of look.messages || []) out.push(m);
    if (!out.length) out.push(data.wipe ? "WIPE IN " + left(data.wipe) : data.online ? "ONLINE NOW" : "OFFLINE");
    return out;
  }

  function draw() {
    const look = data.look || {}, show = look.show || {};
    $("name").textContent = data.name || "";
    const all = lines(), now = Date.now();
    if (all.length > 1 && now - lineAt > (look.rotate || 8) * 1000) { line = (line + 1) % all.length; lineAt = now; }
    if (!lineAt) lineAt = now;
    const text = all[line % all.length];
    if (text !== lineText) {
      const el = $("tags");
      if (lineText !== null && all.length > 1) {
        el.classList.add("swap");
        setTimeout(() => { el.textContent = text; el.classList.remove("swap"); }, 350);
      } else el.textContent = text;
      lineText = text;
    }
    const count = data.online ? (data.max ? `${data.players}/${data.max}` : String(data.players)) : "OFFLINE";
    if (count !== shown) {
      const el = $("players").parentElement;
      el.classList.remove("bump"); void el.offsetWidth; if (shown !== null) el.classList.add("bump");
      $("players").textContent = count; shown = count;
    }
    $("dot").className = "dot" + (data.online ? " on" : "") + (show.dot === false ? " hide" : "");
    $("players").parentElement.classList.toggle("off", !data.online);
    const word = look.label || "PLAYERS", extra = [];
    if (data.online && show.queue !== false && data.queued) extra.push(`${data.queued} QUEUED`);
    if (data.online && show.wipe !== false && data.wipe && (data.tags || (look.messages || []).length)) extra.push("WIPE IN " + left(data.wipe));
    $("lbl").textContent = !data.online ? "SERVER" : [word, ...extra].join(" · ");
    $("filli").style.width = data.online && data.max ? Math.min(100, 100 * data.players / data.max) + "%" : "0";
  }

  async function refresh() {
    try {
      const r = await fetch("data.json", { cache: "no-store" });
      if (r.ok) { data = await r.json(); skew = data.now - Date.now() / 1000; style(); draw(); }
    } catch (e) { /* keep the last numbers, try again */ }
  }
  style();
  draw();
  setInterval(draw, 1000);                    // the wipe countdown and the turning lines
  setInterval(refresh, 15000);
})();
