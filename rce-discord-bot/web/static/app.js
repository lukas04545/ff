// RCE Bot Panel – kleines Single-Page-Frontend ohne externe Bibliotheken.
// Alle Daten vom Server/Spielern werden als Text eingefügt (textContent), nie als HTML.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function toast(message, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), text: message });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3500);
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["X-Requested-With"] = "rce-web";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* leere Antwort */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Nicht angemeldet"); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Führt eine Aktion aus und zeigt Erfolg/Fehler als Toast
async function act(fn, okText) {
  try { const r = await fn(); if (okText) toast(okText); return r; }
  catch (e) { toast(e.message, true); throw e; }
}

const fmtDuration = (s) => {
  if (s === null || s === undefined) return "–";
  s = Math.floor(s);
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} T ${hr} Std` : hr ? `${hr} Std ${m} Min` : `${m} Min`;
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();

// ------------------------------------------------------------------ Login

function showLogin() {
  stopTimers();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-password").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    $("#login-password").value = "";
    start();
  } catch (e) { $("#login-error").textContent = e.message; }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ------------------------------------------------------------------ Router & Timer

let timers = [];
function every(ms, fn) { fn(); timers.push(setInterval(fn, ms)); }
function stopTimers() { timers.forEach(clearInterval); timers = []; }

const views = {};
let status = null;

function route() {
  stopTimers();
  every(5000, refreshConnection);
  const view = (location.hash || "#overview").slice(1);
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const main = $("#main");
  main.replaceChildren();
  (views[view] || views.overview)(main);
}
window.addEventListener("hashchange", route);

async function refreshConnection() {
  try {
    status = await api("/api/status");
    const r = $("#conn-rcon"), d = $("#conn-discord");
    r.textContent = status.rcon_connected ? "RCON verbunden" : "RCON getrennt";
    r.className = "pill " + (status.rcon_connected ? "ok" : "bad");
    d.textContent = status.discord_ready ? "Discord online" : "Discord verbindet …";
    d.className = "pill " + (status.discord_ready ? "ok" : "warn");
  } catch { /* Login-Dialog übernimmt */ }
}

async function start() {
  try { await api("/api/me"); } catch { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  route();
}

// ------------------------------------------------------------------ Übersicht

views.overview = (main) => {
  const stats = h("div", { class: "grid" });
  const playersBody = h("tbody");
  const sayInput = h("input", { placeholder: "Nachricht an alle Spieler (Rich-Text wie <color=red> erlaubt)", maxlength: 400 });

  main.append(
    h("h1", { text: "Übersicht" }),
    stats,
    h("div", { class: "card" },
      h("h2", { text: "📢 Nachricht ins Spiel" }),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        if (!sayInput.value.trim()) return;
        await act(() => api("/api/say", { method: "POST", body: { message: sayInput.value } }), "Gesendet");
        sayInput.value = "";
      } }, sayInput, h("button", { class: "primary", text: "Senden" }))),
    h("div", { class: "card" },
      h("h2", { text: "👥 Online-Spieler" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Name", "Online seit", "Ping", ""].map((t) => h("th", { text: t })))),
        playersBody))),
    h("div", { class: "card" },
      h("h2", { text: "🔨 Spieler entbannen" }),
      unbanForm()),
  );

  every(5000, async () => {
    await refreshConnection();
    if (!status) return;
    const s = status.server || {};
    const stat = (label, value) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value", text: value ?? "–" }));
    stats.replaceChildren(
      stat("Server", s.name || (status.rcon_connected ? "verbunden" : "offline")),
      stat("Spieler", s.players !== undefined && s.players !== null ? `${s.players} / ${s.max_players}` + (s.queued ? ` (+${s.queued})` : "") : "–"),
      stat("Map", s.map),
      stat("Uptime", fmtDuration(s.uptime)),
      stat("Server-FPS", s.fps),
      stat("Offene Tickets", status.tickets_enabled ? status.open_tickets : "aus"),
    );
    playersBody.replaceChildren(...(status.players.length ? status.players.map((p) => h("tr", {},
      h("td", { text: p.name }),
      h("td", { text: fmtDuration(p.connected) }),
      h("td", { text: p.ping !== null && p.ping !== undefined ? `${p.ping} ms` : "–" }),
      h("td", {}, h("div", { class: "row" },
        h("button", { class: "small", text: "Kick", onclick: () => playerAction("kick", p.name) }),
        h("button", { class: "small danger", text: "Ban", onclick: () => playerAction("ban", p.name) })),
      ))) : [h("tr", {}, h("td", { colspan: 4, class: "muted", text: "Niemand online." }))]));
  });
};

function unbanForm() {
  const input = h("input", { placeholder: "Ingame-Name" });
  return h("form", { class: "row", onsubmit: async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    await playerAction("unban", input.value.trim());
    input.value = "";
  } }, input, h("button", { text: "Entbannen" }));
}

async function playerAction(action, name) {
  const labels = { kick: "kicken", ban: "bannen", unban: "entbannen" };
  if (action !== "unban" && !confirm(`${name} wirklich ${labels[action]}?`)) return;
  const reason = action === "unban" ? "" : (prompt("Grund (optional, nur fürs Log):") || "");
  await act(() => api(`/api/players/${action}`, { method: "POST", body: { name, reason } }), `${name}: ${action} gesendet`);
}

// ------------------------------------------------------------------ Konsole

views.console = (main) => {
  const out = h("div", { class: "console" });
  const input = h("input", { placeholder: "Konsolenbefehl, z. B. serverinfo" });
  let lastId = 0;
  const addLine = (cls, ts, text) => {
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.append(h("div", { class: cls }, h("span", { class: "ts", text: ts }), text));
    while (out.childElementCount > 1500) out.firstChild.remove();
    if (stick) out.scrollTop = out.scrollHeight;
  };

  main.append(h("h1", { text: "Konsole" }), out);
  if (status && !status.allow_raw_rcon) {
    main.append(h("p", { class: "muted", text: "Befehle sind deaktiviert (ALLOW_RAW_RCON=false)." }));
  } else {
    main.append(h("form", { class: "row", style: "margin-top:.75rem", onsubmit: async (e) => {
      e.preventDefault();
      const cmd = input.value.trim();
      if (!cmd) return;
      input.value = "";
      addLine("resp", "› ", cmd);
      try {
        const r = await api("/api/command", { method: "POST", body: { command: cmd } });
        addLine("resp", "‹ ", r.response || "(keine Antwort – bei RCE für viele Befehle normal)");
      } catch (err) { addLine("resp", "‹ ", "Fehler: " + err.message); }
    } }, input, h("button", { class: "primary", text: "Ausführen" })));
    input.focus();
  }

  every(2000, async () => {
    try {
      const r = await api(`/api/console?after=${lastId}`);
      for (const l of r.lines) { addLine("", fmtTime(l.ts), l.text); lastId = l.id; }
    } catch { /* ignorieren */ }
  });
};

// ------------------------------------------------------------------ Kits

let meta = null;
async function loadMeta() { if (!meta) meta = await api("/api/meta"); return meta; }

views.kits = async (main) => {
  main.append(h("h1", { text: "Kits" }));
  const box = h("div");
  main.append(box);
  const [data, m] = await Promise.all([api("/api/kits"), loadMeta()]).catch((e) => { toast(e.message, true); return []; });
  if (!data) return;
  const reload = () => { main.replaceChildren(); views.kits(main); };

  // Datalists für Autovervollständigung
  const playersList = h("datalist", { id: "dl-players" }, (status?.players || []).map((p) => h("option", { value: p.name })));
  const itemsList = h("datalist", { id: "dl-items" }, m.items.map((i) => h("option", { value: i.short, label: i.label })));
  box.append(playersList, itemsList);

  // --- Kit vergeben
  const kitOptions = [
    ...data.ingame.map((k) => h("option", { value: "ingame:" + k, text: "🎒 " + k })),
    ...data.custom.map((k) => h("option", { value: "custom:" + k.name, text: "🔒 " + k.name })),
  ];
  const giveKit = h("select", {}, kitOptions);
  const givePlayer = h("input", { placeholder: "Spieler (Ingame-Name)", list: "dl-players" });
  const giveSubmit = async (all) => {
    if (!giveKit.value) return;
    const [type, kit] = [giveKit.value.split(":")[0], giveKit.value.slice(giveKit.value.indexOf(":") + 1)];
    if (all && !confirm(`Kit ${kit} wirklich an ALLE Online-Spieler vergeben?`)) return;
    if (!all && !givePlayer.value.trim()) { toast("Spieler fehlt", true); return; }
    await act(() => api("/api/kits/give", { method: "POST", body: { kit, type, player: givePlayer.value.trim(), all } }), "Kit gesendet");
  };
  box.append(h("div", { class: "card" }, h("h2", { text: "🎁 Kit vergeben" }),
    h("div", { class: "row" }, giveKit, givePlayer,
      h("button", { class: "primary", text: "An Spieler", onclick: () => giveSubmit(false) }),
      h("button", { text: "An alle", onclick: () => giveSubmit(true) }))));

  // --- Ingame-Kits
  const ingameInfo = h("div");
  box.append(h("div", { class: "two-col" },
    h("div", { class: "card" }, h("h2", { text: "🎒 Ingame-Kits (Server)" }),
      data.ingame.length ? h("div", { class: "row" }, data.ingame.map((k) => h("button", { class: "small", text: k, onclick: async () => {
        const info = await act(() => api(`/api/kits/ingame/${encodeURIComponent(k)}`));
        ingameInfo.replaceChildren(h("h2", { text: k }),
          info.items.length ? h("table", {}, h("tbody", {}, info.items.map((i) => h("tr", {},
            h("td", { text: i.short_name }), h("td", { text: "× " + i.amount }), h("td", { class: "muted", text: i.container }),
            h("td", { class: "muted", text: i.item_id !== null ? "ID " + i.item_id : "" }))))) : h("pre", { class: "transcript", text: info.raw || "Keine Items." }));
      } }))) : h("p", { class: "muted", text: "Keine Kits gefunden (oder keine RCON-Verbindung)." }),
      ingameInfo),
    customKitsCard(data, reload)));

  // --- Autokits
  box.append(autokitCard(data, m, reload));

  // --- Verlauf
  box.append(h("div", { class: "card" }, h("h2", { text: "📜 Letzte Vergaben" }),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Zeit (UTC)", "Kit", "Spieler", "Quelle"].map((t) => h("th", { text: t })))),
      h("tbody", {}, data.history.map((r) => h("tr", {},
        h("td", { text: r.claimed_at }), h("td", { text: r.kit }), h("td", { text: r.player }),
        h("td", { class: "muted", text: r.rule_id !== null ? `Autokit #${r.rule_id} (${r.source})` : r.source }))))))));
};

function customKitsCard(data, reload) {
  const name = h("input", { placeholder: "Name des neuen Custom Kits", maxlength: 40 });
  const desc = h("input", { placeholder: "Notiz (optional)", maxlength: 300 });
  const card = h("div", { class: "card" }, h("h2", { text: "🔒 Custom Kits (nur im Bot)" }),
    h("p", { class: "muted", text: "Im Ingame-Kitmanager unsichtbar; Vergabe Item für Item per inventory.giveto." }),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault();
      await act(() => api("/api/custom-kits", { method: "POST", body: { name: name.value, description: desc.value } }), "Custom Kit erstellt");
      reload();
    } }, name, desc, h("button", { class: "primary", text: "Erstellen" })));

  for (const kit of data.custom) {
    const item = h("input", { placeholder: "Item-Shortname", list: "dl-items" });
    const amount = h("input", { type: "number", min: 1, value: 1, style: "max-width:90px" });
    card.append(h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", { text: kit.name }), kit.description ? h("span", { class: "muted", text: kit.description }) : null,
        h("button", { class: "small danger", style: "margin-left:auto", text: "Löschen", onclick: async () => {
          if (!confirm(`Custom Kit ${kit.name} samt Autokits löschen?`)) return;
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}`, { method: "DELETE" }), "Gelöscht");
          reload();
        } })),
      h("table", {}, h("tbody", {}, kit.items.map((i) => h("tr", {},
        h("td", { text: i.shortname }), h("td", { text: "× " + i.amount }),
        h("td", {}, h("button", { class: "small", text: "✕", title: "Entfernen", onclick: async () => {
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items/${i.id}`, { method: "DELETE" }));
          reload();
        } })))))),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        const r = await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items`, { method: "POST", body: { item: item.value, amount: Number(amount.value) } }));
        if (r && !r.known_item) toast("Unbekannter Shortname – bitte einmal an dich selbst testen.");
        reload();
      } }, item, amount, h("button", { text: "+ Item" }))));
  }
  return card;
}

function autokitCard(data, m, reload) {
  const kit = h("select", {}, [
    ...data.ingame.map((k) => h("option", { value: "ingame:" + k, text: "🎒 " + k })),
    ...data.custom.map((k) => h("option", { value: "custom:" + k.name, text: "🔒 " + k.name })),
  ]);
  const trigger = h("select", {}, h("option", { value: "respawn", text: "🔄 Bei Respawn" }), h("option", { value: "quickchat", text: "💬 Quick-Chat-Phrase" }));
  const phrase = h("select", { class: "hidden" }, m.phrases.map((p) => h("option", { value: p.raw, text: p.text })));
  trigger.addEventListener("change", () => phrase.classList.toggle("hidden", trigger.value !== "quickchat"));
  const cooldown = h("input", { type: "number", min: 0, value: 0, title: "Cooldown in Minuten", style: "max-width:120px" });
  const maxClaims = h("input", { type: "number", min: 0, value: 0, title: "Max. pro Spieler (0 = unbegrenzt)", style: "max-width:120px" });
  const phraseText = Object.fromEntries(m.phrases.map((p) => [p.raw, p.text]));

  return h("div", { class: "card" }, h("h2", { text: "🤖 Autokits" }),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Kit", "Auslöser", "Cooldown", "Max.", "Status", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, data.rules.map((r) => h("tr", {},
        h("td", { text: r.id }),
        h("td", { text: (r.kit_type === "custom" ? "🔒 " : "") + r.kit }),
        h("td", { text: r.trigger === "quickchat" ? `💬 „${phraseText[r.phrase] || r.phrase}“` : "🔄 Respawn" }),
        h("td", { text: r.cooldown_minutes ? fmtDuration(r.cooldown_minutes * 60) : "–" }),
        h("td", { text: r.max_claims || "∞" }),
        h("td", {}, h("span", { class: "pill " + (r.enabled ? "ok" : "warn"), text: r.enabled ? "aktiv" : "pausiert" })),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", text: r.enabled ? "Pausieren" : "Aktivieren", onclick: async () => {
            await act(() => api(`/api/autokits/${r.id}/toggle`, { method: "POST", body: { enabled: !r.enabled } })); reload();
          } }),
          h("button", { class: "small danger", text: "Löschen", onclick: async () => {
            if (!confirm(`Autokit #${r.id} löschen?`)) return;
            await act(() => api(`/api/autokits/${r.id}`, { method: "DELETE" })); reload();
          } })))))))),
    h("h2", { text: "Neues Autokit", style: "margin-top:1rem" }),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault();
      if (!kit.value) { toast("Erst ein Kit anlegen", true); return; }
      const [kitType, kitName] = [kit.value.split(":")[0], kit.value.slice(kit.value.indexOf(":") + 1)];
      await act(() => api("/api/autokits", { method: "POST", body: {
        kit: kitName, kit_type: kitType, trigger: trigger.value, phrase: phrase.value,
        cooldown: Number(cooldown.value), max_claims: Number(maxClaims.value) } }), "Autokit angelegt");
      reload();
    } }, kit, trigger, phrase,
      h("label", { class: "muted" }, "Cooldown (Min) ", cooldown),
      h("label", { class: "muted" }, "Max. pro Spieler ", maxClaims),
      h("button", { class: "primary", text: "Anlegen" })),
    h("div", { class: "row", style: "margin-top:.75rem" },
      h("button", { text: "♻️ Wipe-Reset (Cooldowns & Limits zurücksetzen)", onclick: async () => {
        if (!confirm("Alle Autokit-Cooldowns und Limits zurücksetzen?")) return;
        const r = await act(() => api("/api/autokits/reset", { method: "POST" }));
        toast(`${r.count} Vergaben zurückgesetzt`); reload();
      } })));
}

// ------------------------------------------------------------------ Tickets

views.tickets = async (main) => {
  const filter = h("select", {}, h("option", { value: "", text: "Alle" }), h("option", { value: "open", text: "Offen" }), h("option", { value: "closed", text: "Geschlossen" }));
  const body = h("tbody");
  const detail = h("div");
  const ai = h("div", { class: "card" }, h("h2", { text: "🤖 KI (DeepSeek)" }), h("p", { class: "muted", text: "Lade …" }));
  main.append(h("h1", { text: "Tickets" }), ai,
    h("div", { class: "card" }, h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🎫 Tickets", style: "margin:0" }), filter),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Status", "Thema", "Ingame-Name", "KI", "Staff", "Erstellt (UTC)"].map((t) => h("th", { text: t })))),
        body))),
    detail);

  const load = async () => {
    const r = await api("/api/tickets" + (filter.value ? `?status=${filter.value}` : ""));
    body.replaceChildren(...(r.tickets.length ? r.tickets.map((t) => h("tr", { class: "clickable", onclick: () => showTicket(t.id, detail) },
      h("td", { text: t.id }),
      h("td", {}, h("span", { class: "pill " + (t.status === "open" ? "ok" : ""), text: t.status === "open" ? "offen" : "geschlossen" })),
      h("td", { text: t.topic.length > 80 ? t.topic.slice(0, 80) + "…" : t.topic }),
      h("td", { text: t.ingame_name || "–" }),
      h("td", { text: `${t.ai_replies}×` + (t.ai_enabled ? "" : " (aus)") }),
      h("td", { text: t.escalated ? (t.staff_joined ? "übernommen" : "gerufen") : "–" }),
      h("td", { text: t.created_at }))) : [h("tr", {}, h("td", { colspan: 7, class: "muted", text: "Keine Tickets." }))]));
  };
  filter.addEventListener("change", load);
  every(10000, () => load().catch(() => {}));

  api("/api/ai").then((r) => {
    const kids = [h("h2", { text: "🤖 KI (DeepSeek)" })];
    if (!r.enabled) kids.push(h("p", { class: "muted", text: "Kein DEEPSEEK_API_KEY – Tickets gehen direkt an den Staff." }));
    else {
      kids.push(h("p", {}, "Modell: ", h("code", { text: r.model })));
      if (r.error) kids.push(h("p", { class: "error", text: r.error }));
      if (r.balance) {
        kids.push(h("p", {}, h("span", { class: "pill " + (r.balance.is_available ? "ok" : "bad"), text: r.balance.is_available ? "Guthaben ausreichend" : "Guthaben reicht nicht" })));
        for (const b of r.balance.balance_infos || []) kids.push(h("p", { text: `${b.currency}: ${b.total_balance} (aufgeladen ${b.topped_up_balance}, geschenkt ${b.granted_balance})` }));
      }
      kids.push(h("p", {}, h("a", { href: "https://platform.deepseek.com/usage", target: "_blank", rel: "noopener", text: "Verbrauch & Aufladen auf platform.deepseek.com ↗" })));
    }
    ai.replaceChildren(...kids);
  }).catch(() => {});
};

async function showTicket(id, target) {
  const t = await act(() => api(`/api/tickets/${id}`));
  target.replaceChildren(h("div", { class: "card" },
    h("h2", { text: `Ticket #${t.id}` }),
    h("p", {}, h("strong", { text: "Thema: " }), t.topic),
    h("p", { class: "muted", text: `Discord-User-ID ${t.user_id} · erstellt ${t.created_at} UTC` + (t.closed_at ? ` · geschlossen ${t.closed_at} von ${t.closed_by}` : "") + (t.close_reason ? ` · Grund: ${t.close_reason}` : "") }),
    t.status === "open" ? h("p", { class: "muted", text: "Offen – das Gespräch läuft im Discord-Channel. Das Transkript erscheint hier nach dem Schließen." })
      : h("pre", { class: "transcript", text: t.transcript || "(kein Transkript gespeichert)" })));
  target.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ Statistiken

views.stats = (main) => {
  const cat = h("select", {}, [["kills", "Kills"], ["deaths", "Tode"], ["kd", "K/D"], ["playtime", "Spielzeit"]].map(([v, t]) => h("option", { value: v, text: t })));
  const body = h("tbody");
  main.append(h("h1", { text: "Statistiken" }), h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🏆 Leaderboard", style: "margin:0" }), cat),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Spieler", "Kills", "Tode", "K/D", "Spielzeit", "Plattform", "Zuletzt gesehen (UTC)"].map((t) => h("th", { text: t })))),
      body))));
  const load = async () => {
    const r = await act(() => api(`/api/leaderboard?category=${cat.value}`));
    body.replaceChildren(...r.rows.map((p, i) => h("tr", {},
      h("td", { text: i + 1 }), h("td", { text: p.name }), h("td", { text: p.kills }), h("td", { text: p.deaths }),
      h("td", { text: (p.kills / Math.max(p.deaths, 1)).toFixed(2) }), h("td", { text: fmtDuration(p.playtime_seconds) }),
      h("td", { text: p.platform || "–" }), h("td", { text: p.last_seen }))));
  };
  cat.addEventListener("change", load);
  load();
};

// ------------------------------------------------------------------ Wissensbasis

views.knowledge = async (main) => {
  const area = h("textarea", { rows: 26, spellcheck: "true" });
  const info = h("p", { class: "muted" });
  main.append(h("h1", { text: "Wissensbasis für den KI-Support" }),
    h("div", { class: "card" }, info, area,
      h("div", { class: "row", style: "margin-top:.75rem" },
        h("button", { class: "primary", text: "Speichern", onclick: () => act(() => api("/api/knowledge", { method: "PUT", body: { text: area.value } }), "Gespeichert – gilt ab der nächsten KI-Antwort") }),
        h("span", { class: "muted", text: "Nichts Geheimes eintragen – die KI darf alles hieraus an Spieler weitergeben." }))));
  const r = await act(() => api("/api/knowledge"));
  area.value = r.text;
  info.textContent = `Datei: ${r.path}`;
};

// ------------------------------------------------------------------ Bot-Log

views.botlog = (main) => {
  const out = h("div", { class: "console" });
  main.append(h("h1", { text: "Bot-Log" }), h("p", { class: "muted", text: "Die letzten 500 Zeilen. Vollständiges Log: logs/bot.log im Bot-Ordner." }), out);
  every(4000, async () => {
    const r = await api("/api/botlog").catch(() => null);
    if (!r) return;
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.replaceChildren(...r.lines.map((l) => h("div", { text: l })));
    if (stick) out.scrollTop = out.scrollHeight;
  });
};

start();
