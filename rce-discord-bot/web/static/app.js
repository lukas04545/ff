// RCE Bot Panel – small single-page frontend without external libraries.
// All data from the server/players is inserted as text (textContent), never as HTML.
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
  // Clear message instead of the browser's bare "Failed to fetch"
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  let res;
  try {
    res = await fetch(path, { ...opts, signal: ctrl.signal });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? "The bot did not answer within 90 s. If you clicked into the bot window, press Esc there (Windows pauses a program while text is selected in its window)."
      : "No connection to the bot. Is the bot window still open? If you use the public link: it changes when the bot window is closed – take the current one from the bot window or PUBLIC_LINK.txt and reload.");
  } finally { clearTimeout(timer); }
  let data = {};
  try { data = await res.json(); } catch { /* empty response */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Not logged in"); }
  if (!res.ok) { const err = new Error(data.error || `HTTP ${res.status}`); err.data = data; throw err; }
  return data;
}

// Runs an action and shows success/error as a toast
async function act(fn, okText) {
  try { const r = await fn(); if (okText) toast(okText); return r; }
  catch (e) { toast(e.message, true); throw e; }
}

const fmtDuration = (s) => {
  if (s === null || s === undefined) return "–";
  s = Math.floor(s);
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${hr}h` : hr ? `${hr}h ${m}m` : `${m}m`;
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();

// ------------------------------------------------------------------ Login

function showLogin() {
  stopTimers();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  ($("#login-username").value ? $("#login-password") : $("#login-username")).focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: $("#login-username").value.trim(), password: $("#login-password").value } });
    $("#login-password").value = "";
    start();
  } catch (e) { $("#login-error").textContent = e.message; }
});

$("#account").addEventListener("click", () => { location.hash = "#account"; });

// Theme: auto (follows the system) -> light -> dark
const THEMES = { auto: "🌓", light: "☀️", dark: "🌙" };
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  $("#theme").textContent = THEMES[theme] || THEMES.auto;
  $("#theme").title = `Theme: ${theme} (click to change)`;
}
let theme = "auto";
try { theme = localStorage.getItem("rce-theme") || "auto"; } catch { /* storage blocked */ }
applyTheme(theme);
$("#theme").addEventListener("click", () => {
  theme = { auto: "light", light: "dark", dark: "auto" }[theme] || "auto";
  try { localStorage.setItem("rce-theme", theme); } catch { /* storage blocked */ }
  applyTheme(theme);
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
  if (!isOwner() && ["settings", "users"].includes(view)) {
    main.append(h("h1", { text: "No access" }), h("p", { class: "muted", text: "Only owners can open this page. Ask the server owner if you need it." }));
    return;
  }
  (views[view] || views.overview)(main);
}
window.addEventListener("hashchange", route);

async function refreshConnection() {
  try {
    status = await api("/api/status");
    if (status.setup_mode) {
      $("#conn-rcon").textContent = "Setup"; $("#conn-rcon").className = "pill warn";
      $("#conn-discord").textContent = "Bot stopped"; $("#conn-discord").className = "pill warn";
      return;
    }
    const r = $("#conn-rcon"), d = $("#conn-discord");
    r.textContent = status.rcon_connected ? "RCON connected" : "RCON disconnected";
    r.className = "pill " + (status.rcon_connected ? "ok" : "bad");
    d.textContent = status.discord_ready ? "Discord online" : "Discord connecting …";
    d.className = "pill " + (status.discord_ready ? "ok" : "warn");
    if (status.restart_required) { d.textContent = "Restart required ⚙️"; d.className = "pill warn"; }
  } catch { /* login dialog takes over */ }
}

let setupMode = false;
let me = { user: "", role: "admin" };
const isOwner = () => me.role === "owner";

async function start() {
  try { me = await api("/api/me"); } catch { return; }
  setupMode = !!me.setup_mode;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("hidden",
    (setupMode && a.dataset.view !== "settings") || (a.hasAttribute("data-owner") && !isOwner())));
  $("#whoami").replaceChildren(h("strong", { text: me.user }), h("small", { text: me.role === "owner" ? "Owner" : "Admin" }));
  $("#avatar").textContent = (me.user || "?").slice(0, 1);
  $("#account").classList.toggle("hidden", me.user === "owner");
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  if (setupMode && location.hash !== "#settings") location.hash = "#settings";
  else route();
}

// Waits until the bot is reachable again after a restart, then reloads
async function waitForBot(target) {
  await new Promise((r) => setTimeout(r, 3000));
  const started = Date.now();
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      if (r.ok) {
        const me = await r.json();
        if (!me.setup_mode) { location.hash = target; location.reload(); return; }
        // Back in setup mode -> reload the page so the new reason is shown
        if (Date.now() - started > 8000) { location.hash = "#settings"; location.reload(); return; }
      } else if (r.status === 401) { location.reload(); return; }
    } catch { /* bot is still starting */ }
    await new Promise((r) => setTimeout(r, 2000));
  }
  toast("The bot is not responding – please check the bot window.", true);
}

// ------------------------------------------------------------------ Users (owners only) & own account

views.users = async (main) => {
  main.append(h("h1", { text: "Users" }),
    h("p", { class: "muted", text: "Accounts for other admins. Admin = everything except Settings, bot restart and this page. Owner = everything. Every action in the web interface is logged with the username in the admin log channel." }));
  const list = h("div");
  const name = h("input", { placeholder: "Username", maxlength: 32, autocomplete: "off" });
  const pw = h("input", { type: "password", placeholder: "Password (min. 8 characters)", autocomplete: "new-password" });
  const role = h("select", {}, h("option", { value: "admin", text: "Admin" }), h("option", { value: "owner", text: "Owner" }));
  const genBtn = h("button", { type: "button", text: "🎲 Generate", onclick: () => {
    const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    pw.value = Array.from(crypto.getRandomValues(new Uint32Array(14)), (n) => chars[n % chars.length]).join("");
    pw.type = "text";
  } });
  main.append(h("div", { class: "card" }, h("h2", { text: "➕ New account" }),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault();
      const r = await act(() => api("/api/users", { method: "POST", body: { username: name.value.trim(), password: pw.value, role: role.value } }), "Account created").catch(() => null);
      if (!r) return;
      const address = (await api("/api/me").catch(() => ({}))).public_url || location.origin;
      alert(`Account created – send this to the admin:\n\nAddress: ${address}\nUsername: ${name.value.trim()}\nPassword: ${pw.value}\n\nThey can change the password after logging in (🔑 My password).`);
      name.value = ""; pw.value = ""; pw.type = "password";
      load();
    } }, name, pw, genBtn, role, h("button", { class: "primary", text: "Create" }))),
  list);

  const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "–";
  async function load() {
    let data;
    try { data = await api("/api/users"); } catch (e) { toast(e.message, true); return; }
    const rows = data.users.map((u) => {
      const roleSel = h("select", {}, h("option", { value: "admin", text: "Admin" }), h("option", { value: "owner", text: "Owner" }));
      roleSel.value = u.role;
      roleSel.addEventListener("change", async () => { await act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PUT", body: { role: roleSel.value } }), "Role changed").catch(() => {}); load(); });
      return h("tr", {},
        h("td", {}, h("strong", { text: u.username }), u.disabled ? h("span", { class: "pill warn", style: "margin-left:.4rem", text: "disabled" }) : null),
        h("td", {}, roleSel),
        h("td", { class: "muted", text: fmt(u.last_login) }),
        h("td", { class: "muted", text: `${fmt(u.created_at)} by ${u.created_by || "?"}` }),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", text: "Reset password", onclick: async () => {
            const npw = prompt(`New password for ${u.username} (min. 8 characters):`);
            if (!npw) return;
            await act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PUT", body: { password: npw } }), "Password changed – their old sessions are logged out").catch(() => {});
          } }),
          h("button", { class: "small", text: u.disabled ? "Enable" : "Disable", onclick: async () => {
            await act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PUT", body: { disabled: !u.disabled } }), u.disabled ? "Enabled" : "Disabled – logged out").catch(() => {});
            load();
          } }),
          h("button", { class: "small danger", text: "Delete", onclick: async () => {
            if (!confirm(`Delete the account ${u.username}?`)) return;
            await act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "DELETE" }), "Deleted").catch(() => {});
            load();
          } }))));
    });
    list.replaceChildren(h("div", { class: "card" }, h("h2", { text: `👥 Accounts (${data.users.length})` }),
      h("p", { class: "muted" }, "Built-in: ", h("code", { text: "owner" }), " – password is WEB_PASSWORD (Settings → Web interface)",
        data.owner_password_set ? "." : " – ⚠️ not set yet, set one so you can log in from other devices."),
      rows.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Username", "Role", "Last login", "Created", ""].map((t) => h("th", { text: t })))),
        h("tbody", {}, rows))) : h("p", { class: "muted", text: "No other accounts yet." })),
      h("div", { class: "card" }, h("h2", { text: "🌍 How other admins reach the web interface" }),
        h("ul", {},
          h("li", {}, h("strong", { text: "Same home network: " }), "Settings → Web interface → Reachable from: “Also home network (0.0.0.0)”, restart the bot, then they open ", h("code", { text: "http://<your PC's IP>:" + (location.port || 80) }), "."),
          h("li", {}, h("strong", { text: "From anywhere (recommended): " }), "Settings → Web interface → “Reachable from the internet (Cloudflare tunnel)” → On, restart the bot. You get an encrypted https:// address (shown on the Overview and in the admin log channel) – nothing to open on your router."),
          h("li", {}, h("strong", { text: "Don't " }), "forward the port on your router: the web interface uses plain HTTP, passwords would travel unencrypted."))));
  }
  load();
};

views.account = async (main) => {
  main.append(h("h1", { text: "My password" }));
  if (me.user === "owner") { main.append(h("p", { class: "muted", text: "The owner password is WEB_PASSWORD – change it under Settings → Web interface." })); return; }
  const cur = h("input", { type: "password", placeholder: "Current password", autocomplete: "current-password" });
  const npw = h("input", { type: "password", placeholder: "New password (min. 8 characters)", autocomplete: "new-password" });
  const npw2 = h("input", { type: "password", placeholder: "Repeat new password", autocomplete: "new-password" });
  main.append(h("div", { class: "card", style: "max-width:460px" },
    h("p", { class: "muted", text: `Logged in as ${me.user} (${me.role}). Changing the password logs out your other sessions.` }),
    h("form", { style: "display:grid;gap:.6rem", onsubmit: async (e) => {
      e.preventDefault();
      if (npw.value !== npw2.value) { toast("The new passwords don't match", true); return; }
      const r = await act(() => api("/api/account/password", { method: "POST", body: { current: cur.value, new: npw.value } }), "Password changed").catch(() => null);
      if (r) { cur.value = npw.value = npw2.value = ""; }
    } }, cur, npw, npw2, h("button", { class: "primary", text: "Change password" }))));
};

// ------------------------------------------------------------------ Overview

views.overview = (main) => {
  const stats = h("div", { class: "grid" });
  const playersBody = h("tbody");
  const sayInput = h("input", { placeholder: "Message to all players (rich text like <color=red> allowed)", maxlength: 400 });

  // Red card while RCON is not connected – with the cause from the automatic diagnosis
  const rconInfo = h("div");
  const rconTestOut = h("div");
  const rconCard = h("div", { class: "card hidden", style: "border-left:4px solid var(--bad)" },
    h("h2", { text: "🔴 No connection to the Rust server (RCON)" }), rconInfo,
    h("div", { class: "row", style: "margin-top:.5rem" },
      h("button", { class: "primary", text: "🔌 Test now", onclick: async (ev) => {
        ev.target.disabled = true;
        rconTestOut.replaceChildren(h("p", { class: "muted", text: "Testing … (up to 20 seconds)" }));
        try { rconTestOut.replaceChildren(renderSteps(await api("/api/settings/test-rcon", { method: "POST", body: {} }))); }
        catch (e) { rconTestOut.replaceChildren(h("p", { class: "error", text: e.message })); }
        ev.target.disabled = false;
      } }),
      h("a", { href: "#settings" }, h("button", { text: "⚙️ Open RCON settings" }))),
    rconTestOut);

  // Yellow card: bot is running, but in limited mode (privileged intent disabled in the Developer Portal)
  const intentInfo = h("div");
  const intentCard = h("div", { class: "card hidden", style: "border-left:4px solid var(--warn)" },
    h("h2", { text: "⚠️ Limited mode" }), intentInfo);

  // Orange card: the bot is missing Discord permissions in channels/roles
  const permList = h("ul", { style: "margin:.25rem 0" });
  const inviteLink = h("a", { target: "_blank", rel: "noopener" }, h("button", { class: "small", text: "🔗 Re-invite bot with all permissions ↗" }));
  const permCard = h("div", { class: "card hidden", style: "border-left:4px solid var(--warn)" },
    h("h2", { text: "🔐 The bot is missing Discord permissions" }), permList,
    h("p", { class: "muted", style: "margin:.25rem 0" }, "To fix: Discord → Server Settings → Roles → the bot's role: enable the permissions and drag the role near the top. ",
      "If a channel/category has its own permission overrides, allow the bot role there as well. ",
      "Alternatively, open the invite link again – this resets the bot role's permissions (channel overrides stay as they are)."),
    h("div", { class: "row" }, inviteLink));

  // Public address via Cloudflare tunnel (Settings → Web interface → Reachable from the internet)
  const publicCard = h("div", { class: "card hidden" });
  let shownPublic = null;
  const renderPublic = () => {
    const url = status.public_url, err = status.tunnel_error;
    const key = `${status.tunnel_enabled}|${url}|${status.tunnel_state}|${err}`;
    publicCard.classList.toggle("hidden", !status.tunnel_enabled);
    if (!status.tunnel_enabled || key === shownPublic) return;
    shownPublic = key;
    publicCard.style.borderLeft = "4px solid var(" + (url ? "--ok" : err ? "--bad" : "--warn") + ")";
    if (url) {
      publicCard.replaceChildren(h("h2", { text: "🌍 Reachable from anywhere" }),
        h("div", { class: "row" }, h("a", { href: url, target: "_blank", rel: "noopener" }, h("code", { text: url })),
          h("button", { class: "small", text: "📋 Copy", onclick: async () => {
            try { await navigator.clipboard.writeText(url); toast("Copied"); } catch { prompt("Copy the address:", url); }
          } })),
        h("p", { class: "muted", style: "margin:.5rem 0 0", text: "Encrypted Cloudflare tunnel – share it only with your admins (they log in with their account from 👥 Users). A random address changes when the bot window is closed; the current one is always posted in the admin log channel." }));
    } else {
      publicCard.replaceChildren(h("h2", { text: err ? "🌍 Tunnel problem" : "🌍 Tunnel is starting …" }),
        h("p", { class: err ? "error" : "muted", text: err || "Connecting to Cloudflare – the public address appears here in a few seconds (the first start downloads cloudflared, ~40 MB)." }));
    }
  };

  main.append(
    h("h1", { text: "Overview" }),
    publicCard,
    rconCard,
    intentCard,
    permCard,
    stats,
    h("div", { class: "card" },
      h("h2", { text: "📢 Message to the game" }),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        if (!sayInput.value.trim()) return;
        await act(() => api("/api/say", { method: "POST", body: { message: sayInput.value } }), "Sent");
        sayInput.value = "";
      } }, sayInput, h("button", { class: "primary", text: "Send" }))),
    h("div", { class: "card" },
      h("h2", { text: "👥 Online players" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Name", "Online for", "Ping", ""].map((t) => h("th", { text: t })))),
        playersBody))),
    h("div", { class: "card" },
      h("h2", { text: "🔨 Unban player" }),
      unbanForm()),
  );

  every(5000, async () => {
    await refreshConnection();
    if (!status) return;
    renderPublic();
    intentCard.classList.toggle("hidden", !status.intent_warning);
    if (status.intent_warning && !intentInfo.dataset.shown) {
      intentInfo.dataset.shown = "1";
      intentInfo.replaceChildren(h("p", { style: "margin:0", text: status.intent_warning }), fixLink(status.intent_fix_url));
    }
    const perms = status.permission_problems || [];
    permCard.classList.toggle("hidden", !perms.length);
    permList.replaceChildren(...perms.map((p) => h("li", { text: p })));
    inviteLink.classList.toggle("hidden", !status.invite_url);
    if (status.invite_url) inviteLink.href = status.invite_url;
    rconCard.classList.toggle("hidden", !!status.rcon_connected);
    if (!status.rcon_connected) {
      const d = status.rcon_diagnosis;
      rconInfo.replaceChildren(
        h("p", {}, h("strong", { text: "Last error: " }), status.rcon_last_error || "Connecting …"),
        d ? h("p", { class: "muted", text: `Automatic diagnosis from ${new Date(d.ts * 1000).toLocaleTimeString()}:` }) : null,
        d ? renderSteps(d) : h("p", { class: "muted", text: "The automatic diagnosis runs as soon as a connection attempt fails." }));
    }
    const s = status.server || {};
    const stat = (icon, label, value, tone) => h("div", { class: "stat " + (tone || "") },
      h("div", { class: "label" }, icon, " ", label), h("div", { class: "value", text: value ?? "–" }));
    const hasPlayers = s.players !== undefined && s.players !== null;
    stats.replaceChildren(
      stat("🖥️", "Server", s.name || (status.rcon_connected ? "connected" : "offline"), status.rcon_connected ? "ok" : "bad"),
      stat("👥", "Players", hasPlayers ? `${s.players} / ${s.max_players}` + (s.queued ? ` (+${s.queued})` : "") : "–", hasPlayers ? "accent" : ""),
      stat("🗺️", "Map", s.map),
      stat("⏱️", "Uptime", fmtDuration(s.uptime)),
      stat("⚡", "Server FPS", s.fps, s.fps === undefined || s.fps === null ? "" : s.fps < 20 ? "warn" : "ok"),
      stat("🎫", "Open tickets", status.tickets_enabled ? status.open_tickets : "off", status.open_tickets ? "warn" : ""),
    );
    playersBody.replaceChildren(...(status.players.length ? status.players.map((p) => h("tr", {},
      h("td", { text: p.name }),
      h("td", { text: fmtDuration(p.connected) }),
      h("td", { text: p.ping !== null && p.ping !== undefined ? `${p.ping} ms` : "–" }),
      h("td", {}, h("div", { class: "row" },
        h("button", { class: "small", text: "Kick", onclick: () => playerAction("kick", p.name) }),
        h("button", { class: "small danger", text: "Ban", onclick: () => playerAction("ban", p.name) })),
      ))) : [h("tr", {}, h("td", { colspan: 4, class: "muted", text: "Nobody online." }))]));
  });
};

function unbanForm() {
  const input = h("input", { placeholder: "In-game name" });
  return h("form", { class: "row", onsubmit: async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    await playerAction("unban", input.value.trim());
    input.value = "";
  } }, input, h("button", { text: "Unban" }));
}

async function playerAction(action, name) {
  const labels = { kick: "kick", ban: "ban", unban: "unban" };
  if (action !== "unban" && !confirm(`Really ${labels[action]} ${name}?`)) return;
  const reason = action === "unban" ? "" : (prompt("Reason (optional, for the log only):") || "");
  await act(() => api(`/api/players/${action}`, { method: "POST", body: { name, reason } }), `${name}: ${action} sent`);
}

// ------------------------------------------------------------------ Console

views.console = (main) => {
  const out = h("div", { class: "console" });
  const input = h("input", { placeholder: "Console command, e.g. serverinfo" });
  let lastId = 0;
  const addLine = (cls, ts, text) => {
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.append(h("div", { class: cls }, h("span", { class: "ts", text: ts }), text));
    while (out.childElementCount > 1500) out.firstChild.remove();
    if (stick) out.scrollTop = out.scrollHeight;
  };

  main.append(h("h1", { text: "Console" }), out);
  if (status && !status.allow_raw_rcon) {
    main.append(h("p", { class: "muted", text: "Commands are disabled (ALLOW_RAW_RCON=false)." }));
  } else {
    main.append(h("form", { class: "row", style: "margin-top:.75rem", onsubmit: async (e) => {
      e.preventDefault();
      const cmd = input.value.trim();
      if (!cmd) return;
      input.value = "";
      addLine("resp", "› ", cmd);
      try {
        const r = await api("/api/command", { method: "POST", body: { command: cmd } });
        addLine("resp", "‹ ", r.response || "(no response – normal for many commands on RCE)");
      } catch (err) { addLine("resp", "‹ ", "Error: " + err.message); }
    } }, input, h("button", { class: "primary", text: "Run" })));
    input.focus();
  }

  every(2000, async () => {
    try {
      const r = await api(`/api/console?after=${lastId}`);
      for (const l of r.lines) { addLine("", fmtTime(l.ts), l.text); lastId = l.id; }
    } catch { /* ignore */ }
  });
};

// ------------------------------------------------------------------ Kits

let meta = null;
async function loadMeta() { if (!meta) meta = await api("/api/meta"); return meta; }

// ------------------------------------------------------------------ Kit builder

const KB_SIZES = { Wear: 7, Main: 24, Belt: 6 };
const KB_LABELS = { Wear: "👕 Wear", Main: "🎒 Main inventory", Belt: "🔥 Belt" };
const KB_DRAFT = "kitbuilder-draft";
const KB_SAVED = "kitbuilder-saved";
const KB_SHORT = /^[a-z0-9][a-z0-9._ -]*$/;  // spaces only for real items like "lumberjack hoodie" (checked below)

// Browser storage may be unavailable (private mode etc.) – the builder still works without it
const kbStore = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
};

// Parses pasted 'kit add "Kit" "item" amount condition container' lines (same rules as the bot)
function kbParseCommands(text) {
  const out = [];
  for (const line of text.replace(/\\n/g, "\n").split(/\n/)) {
    for (const seg of line.split(/\bkit\s+add\b/i).slice(1)) {
      const tokens = [...seg.matchAll(/"([^"]*)"|(\S+)/g)].map((m) => m[1] ?? m[2]);
      const item = (tokens[1] || "").trim().replace(/\s+/g, " ").toLowerCase();
      if (!tokens[0] || !KB_SHORT.test(item)) continue;
      const container = { main: "Main", belt: "Belt", wear: "Wear" }[(tokens[4] || "").toLowerCase()] || "Main";
      out.push({ kit: tokens[0].trim(), item, amount: Math.max(1, parseInt(tokens[2]) || 1),
        condition: Math.min(100, Math.max(1, parseInt(tokens[3]) || 100)), container });
    }
  }
  return out;
}

views.kitbuilder = async (main) => {
  let m, kitData;
  try { [m, kitData] = await Promise.all([loadMeta(), api("/api/kits")]); }
  catch (e) { main.append(h("h1", { text: "Kit builder" })); toast(e.message, true); return; }

  // --- State: slots per container, each null or {item, amount, condition}
  const empty = () => Object.fromEntries(Object.entries(KB_SIZES).map(([c, n]) => [c, Array(n).fill(null)]));
  let slots = empty(), selected = null;
  const allItems = m.catalogue.flatMap((c) => c.items.map((i) => ({ ...i, category: c.category, icon: c.icon })));
  const byShort = Object.fromEntries(allItems.map((i) => [i.short, i]));
  const label = (s) => byShort[s]?.label || s;
  const icon = (s) => byShort[s]?.icon || "📦";
  const kitName = h("input", { placeholder: "Kit name, e.g. Starter", maxlength: 40 });

  const setSlots = (src) => {
    slots = empty();
    for (const c of Object.keys(KB_SIZES)) {
      (src?.[c] || []).slice(0, KB_SIZES[c]).forEach((s, i) => {
        if (s && KB_SHORT.test(s.item)) slots[c][i] = { item: s.item, amount: Math.max(1, s.amount | 0 || 1), condition: Math.min(100, Math.max(1, s.condition | 0 || 100)) };
      });
    }
  };
  const draft = kbStore.get(KB_DRAFT, null);
  if (draft) { setSlots(draft.slots); kitName.value = draft.name || ""; }

  const filled = () => Object.entries(slots).flatMap(([container, arr]) => arr.map((s) => s && { ...s, container }).filter(Boolean));
  const firstFree = (container) => slots[container].findIndex((s) => !s);
  const preferredContainer = (short) => {
    const cat = byShort[short]?.category;
    if (cat === "Attire" && firstFree("Wear") >= 0) return "Wear";
    if (["Weapons", "Tools", "Medical"].includes(cat) && firstFree("Belt") >= 0) return "Belt";
    return "Main";
  };
  const place = (short, target) => {
    short = short.trim().replace(/\s+/g, " ").toLowerCase();
    if (!KB_SHORT.test(short) || (short.includes(" ") && !byShort[short])) { toast("Invalid shortname", true); return; }
    if (!target) {
      target = selected && !slots[selected.container][selected.index] ? selected : null;
      for (const c of [preferredContainer(short), "Main", "Belt"]) {
        if (target) break;
        const i = firstFree(c); if (i >= 0) target = { container: c, index: i };
      }
    }
    if (!target) { toast("The kit is full", true); return; }
    slots[target.container][target.index] = { item: short, amount: 1, condition: 100 };
    selected = target;
    render();
  };

  // --- Header stats (like Helios: items loaded / slots filled / commands)
  const stats = h("div", { class: "kb-stats" });

  // --- Item browser
  const catSelect = h("select", {}, h("option", { value: "", text: "All categories" }),
    m.catalogue.map((c) => h("option", { value: c.category, text: `${c.icon} ${c.category}` })));
  catSelect.value = m.catalogue[0]?.category || "";
  const search = h("input", { type: "search", placeholder: "AK, rockets, hazmat …" });
  const itemGrid = h("div", { class: "kb-items" });
  const renderItems = () => {
    const q = search.value.trim().toLowerCase();
    const pool = allItems.filter((i) => (!catSelect.value || q || i.category === catSelect.value)
      && (!q || i.short.includes(q) || i.label.toLowerCase().includes(q)));
    const children = [];
    let lastGroup = null;
    for (const i of pool) {
      const group = `${i.category} – ${i.group}`;
      if (group !== lastGroup) { children.push(h("div", { class: "kb-group", text: group })); lastGroup = group; }
      const btn = h("button", { class: "kb-item", title: `${i.label} (${i.short}) – click or drag into a slot`, draggable: "true", onclick: () => place(i.short) },
        h("span", { class: "kb-item-icon", text: i.icon }), h("span", {}, i.label, h("small", { text: i.short })));
      btn.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", JSON.stringify({ add: i.short })); e.dataTransfer.effectAllowed = "copy"; });
      children.push(btn);
    }
    if (q && KB_SHORT.test(q) && !q.includes(" ") && !byShort[q]) {
      children.push(h("button", { class: "kb-item", onclick: () => place(q) }, h("span", { class: "kb-item-icon", text: "➕" }), h("span", {}, `Use "${q}"`, h("small", { text: "custom shortname" }))));
    }
    itemGrid.replaceChildren(...(children.length ? children : [h("p", { class: "muted", text: "No items found." })]));
  };
  search.addEventListener("input", renderItems);
  catSelect.addEventListener("change", () => { search.value = ""; renderItems(); });

  // --- Slots with drag & drop (move/swap between slots, drop new items from the list)
  const inventory = h("div");
  const editor = h("div", { class: "kb-editor" });
  const output = h("textarea", { class: "kb-output", rows: 8, readonly: true, placeholder: "Generated commands …" });

  const slotEl = (container, index) => {
    const s = slots[container][index];
    const isSel = selected && selected.container === container && selected.index === index;
    const el = h("div", { class: "kb-slot" + (s ? " filled" : "") + (isSel ? " selected" : ""), draggable: s ? "true" : null,
      title: s ? `${label(s.item)} (${s.item})` : `Empty ${container.toLowerCase()} slot – click it, then pick an item (or drag one here)`,
      onclick: () => { selected = { container, index }; render(); } },
      s ? [h("span", { class: "icon", text: icon(s.item) }), h("span", { class: "name", text: label(s.item) }),
        s.amount > 1 ? h("span", { class: "amt", text: "×" + s.amount }) : null,
        s.condition < 100 ? h("span", { class: "cond", text: s.condition + "%" }) : null]
        : h("span", { class: "num", text: String(index + 1).padStart(2, "0") }));
    el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", JSON.stringify({ container, index })); e.dataTransfer.effectAllowed = "move"; });
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("over"); });
    el.addEventListener("dragleave", () => el.classList.remove("over"));
    el.addEventListener("drop", (e) => {
      e.preventDefault(); el.classList.remove("over");
      let data;
      try { data = JSON.parse(e.dataTransfer.getData("text/plain")); } catch { return; }
      if (data?.add) { place(data.add, { container, index }); return; }
      if (!data || !slots[data.container]) return;
      [slots[container][index], slots[data.container][data.index]] = [slots[data.container][data.index], slots[container][index]];
      selected = { container, index };
      render();
    });
    return el;
  };

  const renderEditor = () => {
    const s = selected && slots[selected.container][selected.index];
    if (!s) {
      editor.replaceChildren(h("span", { class: "muted", text: selected ? "Empty slot selected – click an item on the left to put it here." : "Click or drag items into the slots. Click a slot to edit amount/condition, drag to move or swap." }));
      return;
    }
    const amount = h("input", { type: "number", min: 1, max: 100000, value: s.amount });
    const condition = h("input", { type: "number", min: 1, max: 100, value: s.condition });
    amount.addEventListener("change", () => { s.amount = Math.max(1, Math.min(100000, parseInt(amount.value) || 1)); render(); });
    condition.addEventListener("change", () => { s.condition = Math.max(1, Math.min(100, parseInt(condition.value) || 100)); render(); });
    const quick = [1, 10, 64, 128, 256, 1000].map((n) => h("button", { class: "small", text: "×" + n, onclick: () => { s.amount = n; render(); } }));
    editor.replaceChildren(
      h("strong", { text: `${icon(s.item)} ${label(s.item)}` }), h("code", { text: s.item }),
      h("label", { class: "row", style: "gap:.3rem" }, "Amount", amount), ...quick,
      h("label", { class: "row", style: "gap:.3rem" }, "Condition %", condition),
      h("button", { class: "small danger", text: "Remove", onclick: () => { slots[selected.container][selected.index] = null; render(); } }));
  };

  const commands = () => {
    const name = kitName.value.replace(/"/g, "").trim() || "Kit name";
    return filled().map((i) => m.kit_add_template.replaceAll("{kit}", name).replaceAll("{item}", i.item)
      .replaceAll("{amount}", i.amount).replaceAll("{condition}", i.condition).replaceAll("{container}", i.container)).join("\n");
  };

  function render() {
    inventory.replaceChildren(...["Wear", "Main", "Belt"].flatMap((c) => [
      h("div", { class: "kb-section" }, h("span", { text: KB_LABELS[c] }), h("span", { text: `${slots[c].filter(Boolean).length}/${KB_SIZES[c]} slots` })),
      h("div", { class: "kb-grid" + (c === "Wear" ? " wear" : "") }, slots[c].map((_, i) => slotEl(c, i))),
    ]));
    renderEditor();
    output.value = commands();
    const n = filled().length;
    stats.replaceChildren(
      h("span", {}, h("strong", { text: allItems.length }), " items loaded"),
      h("span", {}, h("strong", { text: n }), " slots filled"),
      h("span", {}, h("strong", { text: n }), " commands"));
    kbStore.set(KB_DRAFT, { name: kitName.value, slots });
  }
  kitName.addEventListener("input", render);

  const loadItems = (items, name) => {
    slots = empty(); selected = null;
    let skipped = 0;
    for (const i of items) {
      let c = KB_SIZES[i.container] ? i.container : "Main";
      if (firstFree(c) < 0) c = firstFree("Main") >= 0 ? "Main" : "Belt";
      const idx = firstFree(c);
      if (idx >= 0) slots[c][idx] = { item: i.item, amount: i.amount, condition: i.condition || 100 };
      else skipped++;
    }
    if (name) kitName.value = name;
    if (skipped) toast(`${skipped} items did not fit (max. 37 slots)`, true);
    render();
  };
  const confirmReplace = () => !filled().length || confirm("Replace the current kit in the builder?");

  // --- Load kits from the server (in-game kits / custom kits)
  const serverSel = h("select", {}, h("option", { value: "", text: "📥 Load from server …" }),
    kitData.ingame.map((k) => h("option", { value: "ingame:" + k, text: "🎒 " + k })),
    kitData.custom.map((k) => h("option", { value: "custom:" + k.name, text: "🔒 " + k.name })));
  serverSel.addEventListener("change", async () => {
    const v = serverSel.value; serverSel.value = "";
    if (!v || !confirmReplace()) return;
    const [type, name] = [v.split(":")[0], v.slice(v.indexOf(":") + 1)];
    try {
      if (type === "ingame") {
        const info = await api(`/api/kits/ingame/${encodeURIComponent(name)}`);
        if (!info.items.length) { toast("Could not read the items of this kit (see Kits page).", true); return; }
        loadItems(info.items.map((i) => ({ item: i.short_name, amount: i.amount, condition: i.condition, container: i.container })), name);
      } else {
        loadItems((kitData.custom.find((k) => k.name === name)?.items || []).map((i) => ({ item: i.shortname, amount: i.amount, condition: 100, container: "Main" })), name);
      }
    } catch (e) { toast(e.message, true); }
  });

  // --- Saved kits in this browser + import/export
  const panel = h("div");
  const showPanel = (...content) => panel.replaceChildren(h("div", { class: "card" }, ...content,
    h("div", { class: "row", style: "margin-top:.5rem" }, h("button", { class: "small", text: "Close", onclick: () => panel.replaceChildren() }))));
  const saveLocal = () => {
    const name = kitName.value.trim();
    if (!name) { toast("Enter a kit name first", true); kitName.focus(); return; }
    if (!filled().length) { toast("The kit is empty", true); return; }
    const saved = kbStore.get(KB_SAVED, {});
    if (saved[name] && !confirm(`Overwrite the saved kit "${name}"?`)) return;
    saved[name] = { slots, savedAt: Date.now() };
    kbStore.set(KB_SAVED, saved);
    toast(`Saved "${name}" in this browser`);
  };
  const manageLocal = () => {
    const saved = kbStore.get(KB_SAVED, {});
    const names = Object.keys(saved).sort((a, b) => a.localeCompare(b));
    showPanel(h("h2", { text: "💾 Saved kits (this browser)" }),
      names.length ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, names.map((n) => h("tr", {},
        h("td", { text: n }),
        h("td", { class: "muted", text: `${Object.values(saved[n].slots || {}).flat().filter(Boolean).length} items` }),
        h("td", { class: "muted", text: saved[n].savedAt ? new Date(saved[n].savedAt).toLocaleString() : "" }),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small primary", text: "Load", onclick: () => { if (!confirmReplace()) return; setSlots(saved[n].slots); kitName.value = n; selected = null; render(); panel.replaceChildren(); } }),
          h("button", { class: "small danger", text: "Delete", onclick: () => { if (!confirm(`Delete "${n}"?`)) return; delete saved[n]; kbStore.set(KB_SAVED, saved); manageLocal(); } })))))))) 
        : h("p", { class: "muted", text: "Nothing saved yet – build a kit and click \"Save kit\"." }));
  };
  const exportCode = () => {
    if (!filled().length) { toast("The kit is empty", true); return; }
    const code = "RCEKIT:" + btoa(unescape(encodeURIComponent(JSON.stringify({ v: 1, name: kitName.value.trim(), slots }))));
    const box = h("textarea", { class: "kb-output", rows: 4, readonly: true, value: code });
    showPanel(h("h2", { text: "📤 Export code" }), h("p", { class: "muted", text: "Share this code – paste it into \"Import\" on any bot's kit builder to get the exact same layout." }), box,
      h("div", { class: "row", style: "margin-top:.5rem" }, h("button", { class: "primary small", text: "📋 Copy code", onclick: async () => {
        try { await navigator.clipboard.writeText(code); toast("Copied"); } catch { box.select(); document.execCommand("copy"); toast("Copied"); }
      } })));
  };
  const importDialog = () => {
    const box = h("textarea", { class: "kb-output", rows: 6, placeholder: 'Paste an export code (RCEKIT:…) or "kit add" commands, e.g.\nkit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main' });
    showPanel(h("h2", { text: "📥 Import" }), box, h("div", { class: "row", style: "margin-top:.5rem" }, h("button", { class: "primary small", text: "Import", onclick: () => {
      const text = box.value.trim();
      if (!text) return;
      if (text.startsWith("RCEKIT:")) {
        try {
          const data = JSON.parse(decodeURIComponent(escape(atob(text.slice(7)))));
          if (!confirmReplace()) return;
          setSlots(data.slots); kitName.value = data.name || kitName.value; selected = null; render(); panel.replaceChildren(); toast("Imported");
        } catch { toast("Invalid export code", true); }
        return;
      }
      const items = kbParseCommands(text);
      if (!items.length) { toast('No "kit add" commands found', true); return; }
      const kits = [...new Set(items.map((i) => i.kit))];
      if (!confirmReplace()) return;
      loadItems(items.filter((i) => i.kit === kits[0]), kits[0]);
      panel.replaceChildren();
      toast(kits.length > 1 ? `Imported "${kits[0]}" (the paste contained ${kits.length} kits – import the others separately)` : `Imported ${items.length} items`);
    } })));
  };

  // --- Actions
  const replace = h("input", { type: "checkbox" }); replace.checked = true;
  const payload = () => ({ kit: kitName.value.trim(), items: filled().map(({ item, amount, condition, container }) => ({ item, amount, condition, container })) });
  const check = () => {
    if (!kitName.value.trim()) { toast("Enter a kit name first", true); kitName.focus(); return false; }
    if (!filled().length) { toast("The kit is empty", true); return false; }
    return true;
  };
  const actions = h("div", { class: "row", style: "margin-top:.5rem" },
    h("button", { class: "primary", text: "📋 Copy commands", onclick: async () => {
      if (!filled().length) { toast("The kit is empty", true); return; }
      try { await navigator.clipboard.writeText(output.value); toast("Copied"); }
      catch { output.select(); document.execCommand("copy"); toast("Copied"); }
    } }),
    h("button", { text: "🎒 Create in-game kit on server", onclick: async () => {
      if (!check()) return;
      if (!confirm(`Send ${filled().length} "kit add" commands for "${kitName.value.trim()}" to the server?\n\nIf the kit already exists in-game, the items are ADDED to it.`)) return;
      const r = await act(() => api("/api/kit-builder/ingame", { method: "POST", body: payload() })).catch(() => null);
      if (r) toast(`In-game kit ${r.kit}: ${r.added} items sent`);
    } }),
    h("button", { text: "🔒 Save as custom kit", onclick: async () => {
      if (!check()) return;
      const r = await act(() => api("/api/kit-builder/custom", { method: "POST", body: { ...payload(), replace: replace.checked } })).catch(() => null);
      if (r) toast(`Custom kit ${r.kit} ${r.created ? "created" : "saved"} (${r.added} items)`);
    } }),
    h("label", { class: "row muted", style: "gap:.3rem" }, replace, "replace existing custom kit items"),
    h("button", { class: "danger", style: "margin-left:auto", text: "Clear all", onclick: () => {
      if (filled().length && !confirm("Remove all items from the builder?")) return;
      slots = empty(); selected = null; render();
    } }));
  const secondary = h("div", { class: "row", style: "margin-top:.5rem" },
    h("button", { text: "💾 Save kit", onclick: saveLocal }),
    h("button", { text: "📂 Saved kits", onclick: manageLocal }),
    h("button", { text: "📤 Export code", onclick: exportCode }),
    h("button", { text: "📥 Import", onclick: importDialog }));

  main.append(
    h("div", { class: "row", style: "justify-content:space-between;align-items:flex-end" }, h("h1", { text: "Kit builder", style: "margin:0" }), stats),
    h("div", { class: "card", style: "margin-top:.75rem" }, h("div", { class: "row" }, kitName, serverSel)),
    panel,
    h("div", { class: "kb-layout" },
      h("div", { class: "card" }, h("h2", { text: "Items" }), h("div", { class: "row" }, catSelect, search), itemGrid,
        h("p", { class: "muted", style: "margin:.5rem 0 0", text: "Not in the list? Type the shortname into the search and click \"Use\"." })),
      h("div", {},
        h("div", { class: "card" }, inventory, editor),
        h("div", { class: "card" }, h("h2", { text: "Generated commands" }),
          h("p", { class: "muted", style: "margin:0 0 .5rem", text: "Each filled slot becomes one kit add command (uses your KIT_ADD_TEMPLATE)." }),
          output, actions, secondary,
          h("p", { class: "muted", style: "margin:.5rem 0 0", text: "In-game kits keep slot (wear/belt/inventory) and condition. Custom kits are bot-only (invisible in the in-game kit manager) and put everything into the inventory. Saved kits stay in this browser only." })))));
  renderItems();
  render();
};

views.kits = async (main) => {
  main.append(h("h1", { text: "Kits" }));
  const box = h("div");
  main.append(box);
  const [data, m, opts] = await Promise.all([api("/api/kits"), loadMeta(),
    api("/api/settings/discord-options").catch(() => ({ available: false }))]).catch((e) => { toast(e.message, true); return []; });
  if (!data) return;
  const reload = () => { main.replaceChildren(); views.kits(main); };

  // Datalists for autocomplete
  const playersList = h("datalist", { id: "dl-players" }, (status?.players || []).map((p) => h("option", { value: p.name })));
  const itemsList = h("datalist", { id: "dl-items" }, m.items.map((i) => h("option", { value: i.short, label: i.label })));
  box.append(playersList, itemsList);

  // --- Give kit
  const kitOptions = [
    ...data.ingame.map((k) => h("option", { value: "ingame:" + k, text: "🎒 " + k })),
    ...data.custom.map((k) => h("option", { value: "custom:" + k.name, text: "🔒 " + k.name })),
  ];
  const giveKit = h("select", {}, kitOptions);
  const givePlayer = h("input", { placeholder: "Player (in-game name)", list: "dl-players" });
  const giveSubmit = async (all) => {
    if (!giveKit.value) return;
    const [type, kit] = [giveKit.value.split(":")[0], giveKit.value.slice(giveKit.value.indexOf(":") + 1)];
    if (all && !confirm(`Really give kit ${kit} to ALL online players?`)) return;
    if (!all && !givePlayer.value.trim()) { toast("Player missing", true); return; }
    await act(() => api("/api/kits/give", { method: "POST", body: { kit, type, player: givePlayer.value.trim(), all } }), "Kit sent");
  };
  box.append(h("div", { class: "card" }, h("h2", { text: "🎁 Give kit" }),
    h("div", { class: "row" }, giveKit, givePlayer,
      h("button", { class: "primary", text: "To player", onclick: () => giveSubmit(false) }),
      h("button", { text: "To everyone", onclick: () => giveSubmit(true) }))));

  // --- In-game kits
  const ingameInfo = h("div");
  box.append(h("div", { class: "two-col" },
    h("div", { class: "card" }, h("h2", { text: "🎒 In-game kits (server)" }),
      data.ingame.length ? h("div", { class: "row" }, data.ingame.map((k) => h("button", { class: "small", text: k, onclick: async () => {
        const info = await act(() => api(`/api/kits/ingame/${encodeURIComponent(k)}`));
        ingameInfo.replaceChildren(h("h2", { text: k }),
          info.items.length ? h("table", {}, h("tbody", {}, info.items.map((i) => h("tr", {},
            h("td", { text: i.short_name }), h("td", { text: "× " + i.amount }), h("td", { class: "muted", text: i.container }),
            h("td", { class: "muted", text: i.item_id !== null ? "ID " + i.item_id : "" }))))) : h("pre", { class: "transcript", text: info.raw || "No items." }));
      } }))) : h("p", { class: "muted", text: "No kits found (or no RCON connection)." }),
      ingameInfo),
    customKitsCard(data, reload, opts)), linkedPlayersCard(data, reload));

  // --- Autokits
  box.append(autokitCard(data, m, reload, opts));

  // --- History
  box.append(h("div", { class: "card" }, h("h2", { text: "📜 Recent claims" }),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Time (UTC)", "Kit", "Player", "Source"].map((t) => h("th", { text: t })))),
      h("tbody", {}, data.history.map((r) => h("tr", {},
        h("td", { text: r.claimed_at }), h("td", { text: r.kit }), h("td", { text: r.player }),
        h("td", { class: "muted", text: r.rule_id !== null ? `Autokit #${r.rule_id} (${r.source})` : r.source }))))))));
};

// Cooldown in minutes -> "1d 12h" (input format of the cooldown field)
function minutesToDuration(min) {
  if (!min) return "0";
  const d = Math.floor(min / 1440), hr = Math.floor((min % 1440) / 60), mi = min % 60;
  return [d && d + "d", hr && hr + "h", mi && mi + "m"].filter(Boolean).join(" ");
}

// Self-service claim: roles that may claim the kit (/customkit claim) and cooldown
function claimSettings(kit, opts, reload) {
  const current = new Set((kit.claim_role_ids || "").split(",").filter(Boolean));
  const roles = opts.available ? opts.roles : [];
  const boxes = roles.map((r) => { const cb = h("input", { type: "checkbox", value: r.id }); cb.checked = current.has(r.id); return [cb, r]; });
  const unknown = [...current].filter((id) => !roles.some((r) => r.id === id));
  const manual = h("input", { placeholder: "Role IDs, comma-separated", value: roles.length ? "" : [...current].join(",") });
  const cooldown = h("input", { value: minutesToDuration(kit.cooldown_minutes), placeholder: "e.g. 12h, 1d, 0", style: "max-width:140px" });
  return h("details", { style: "margin-top:.5rem" },
    h("summary", { text: `🙋 Claim by role: ${current.size ? current.size + " role(s), cooldown " + minutesToDuration(kit.cooldown_minutes) : "off (admins only)"}` }),
    h("p", { class: "muted", text: "Members with one of these roles can claim the kit with /customkit claim. It always goes to their own in-game player, which they verify once with /customkit link. They must be online; the cooldown counts per player." }),
    roles.length ? h("div", { class: "checklist" }, boxes.map(([cb, r]) => h("label", {}, cb, r.name)),
      unknown.length ? h("div", { class: "muted", text: `Other role IDs: ${unknown.join(", ")}` }) : null) : manual,
    h("div", { class: "row", style: "margin-top:.5rem" }, h("span", { text: "Cooldown:" }), cooldown,
      h("button", { class: "primary small", text: "Save", onclick: async () => {
        const role_ids = roles.length ? [...boxes.filter(([cb]) => cb.checked).map(([cb]) => cb.value), ...unknown]
          : manual.value.split(",").map((v) => v.trim()).filter(Boolean);
        const r = await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/claim`, { method: "PUT", body: { role_ids, cooldown: cooldown.value } }), "Saved");
        if (r) reload();
      } })));
}

// Discord accounts linked (verified) to in-game players – needed for /customkit claim
function linkedPlayersCard(data, reload) {
  const rows = data.linked || [];
  return h("div", { class: "card" }, h("h2", { text: `🔗 Linked players (${rows.length})` }),
    h("p", { class: "muted", text: "Members link themselves with /customkit link (verified in-game by sending two quick chat phrases). Custom kit claims only go to the linked player." }),
    rows.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["In-game player", "Discord", "Linked", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        h("td", { text: r.player }), h("td", { text: r.user }), h("td", { class: "muted", text: r.linked_at }),
        h("td", {}, h("button", { class: "small danger", text: "Unlink", onclick: async () => {
          if (!confirm(`Unlink ${r.player} from ${r.user}?`)) return;
          await act(() => api(`/api/linked-players/${r.user_id}`, { method: "DELETE" }), "Unlinked");
          reload();
        } }))))))) : h("p", { class: "muted", text: "Nobody linked yet." }));
}

// Kit builder output ("kit add "Kit" "item" 1 100 main") -> custom kits
function importBox(reload) {
  const text = h("textarea", { rows: 6, placeholder: 'kit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main\nkit add "Custom kit 1" "rifle.ak" 1 100 belt' });
  const kit = h("input", { placeholder: "Optional: import everything into this kit", maxlength: 40 });
  const replace = h("input", { type: "checkbox" });
  replace.checked = true;
  const out = h("div");
  return h("details", { style: "margin:.75rem 0" }, h("summary", { text: "📥 Import from kit builder" }),
    h("p", { class: "muted", text: "Paste the kit add commands from a kit builder. \"kit add\" is stripped; kit name, item and amount are used (container/condition are ignored – custom kit items go into the inventory). Missing kits are created." }),
    text,
    h("div", { class: "row", style: "margin-top:.5rem" }, kit,
      h("label", { class: "row", style: "gap:.3rem" }, replace, "Replace existing items"),
      h("button", { class: "primary", text: "Import", onclick: async () => {
        const r = await act(() => api("/api/custom-kits/import", { method: "POST", body: { text: text.value, kit: kit.value, replace: replace.checked } }));
        if (!r) return;
        out.replaceChildren(h("ul", { class: "steps" }, [
          ...r.kits.map((k) => h("li", { class: k.skipped ? "warn" : "ok", text: `${k.name}: ${k.added} items ${k.created ? "(new kit)" : k.replaced ? `(replaced ${k.replaced} old items)` : "(added)"}${k.skipped ? ` – ${k.skipped} skipped (item limit)` : ""}${k.unknown.length ? ` – unknown shortnames: ${k.unknown.join(", ")}` : ""}` })),
          ...r.errors.map((e) => h("li", { class: "fail", text: "Could not read: " + e })),
        ]));
        toast("Imported");
        setTimeout(reload, 2500);
      } })),
    out);
}

function customKitsCard(data, reload, opts) {
  const name = h("input", { placeholder: "Name of the new custom kit", maxlength: 40 });
  const desc = h("input", { placeholder: "Note (optional)", maxlength: 300 });
  const card = h("div", { class: "card" }, h("h2", { text: "🔒 Custom kits (bot only)" }),
    h("p", { class: "muted", text: "Invisible in the in-game kit manager; given item by item via inventory.giveto." }),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault();
      await act(() => api("/api/custom-kits", { method: "POST", body: { name: name.value, description: desc.value } }), "Custom kit created");
      reload();
    } }, name, desc, h("button", { class: "primary", text: "Create" })),
    importBox(reload));

  for (const kit of data.custom) {
    const item = h("input", { placeholder: "Item shortname", list: "dl-items" });
    const amount = h("input", { type: "number", min: 1, value: 1, style: "max-width:90px" });
    card.append(h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", { text: kit.name }), kit.description ? h("span", { class: "muted", text: kit.description }) : null,
        h("button", { class: "small danger", style: "margin-left:auto", text: "Delete", onclick: async () => {
          if (!confirm(`Delete custom kit ${kit.name} along with its autokits?`)) return;
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}`, { method: "DELETE" }), "Deleted");
          reload();
        } })),
      h("table", {}, h("tbody", {}, kit.items.map((i) => h("tr", {},
        h("td", { text: i.shortname }), h("td", { text: "× " + i.amount }),
        h("td", {}, h("button", { class: "small", text: "✕", title: "Remove", onclick: async () => {
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items/${i.id}`, { method: "DELETE" }));
          reload();
        } })))))),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        const r = await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items`, { method: "POST", body: { item: item.value, amount: Number(amount.value) } }));
        if (r && !r.known_item) toast("Unknown shortname – please test it on yourself once.");
        reload();
      } }, item, amount, h("button", { text: "+ Item" })),
      claimSettings(kit, opts || { available: false }, reload)));
  }
  return card;
}

function autokitCard(data, m, reload, opts) {
  // Required Discord role (players must be linked with /customkit link)
  const roles = opts?.available ? opts.roles : [];
  const roleSelect = (value) => {
    const el = h("select", {}, h("option", { value: "", text: "Everyone" }), roles.map((r) => h("option", { value: r.id, text: "🎖️ " + r.name })));
    if (value && !roles.some((r) => r.id === String(value))) el.append(h("option", { value: String(value), text: `Role ID ${value}` }));
    el.value = value ? String(value) : "";
    return el;
  };
  const newRole = roleSelect("");
  const kit = h("select", {}, [
    ...data.ingame.map((k) => h("option", { value: "ingame:" + k, text: "🎒 " + k })),
    ...data.custom.map((k) => h("option", { value: "custom:" + k.name, text: "🔒 " + k.name })),
  ]);
  const trigger = h("select", {}, h("option", { value: "respawn", text: "🔄 On respawn" }), h("option", { value: "quickchat", text: "💬 Quick chat phrase" }));
  const phrase = h("select", { class: "hidden" }, m.phrases.map((p) => h("option", { value: p.raw, text: p.text })));
  trigger.addEventListener("change", () => phrase.classList.toggle("hidden", trigger.value !== "quickchat"));
  const cooldown = h("input", { type: "number", min: 0, value: 0, title: "Cooldown in minutes", style: "max-width:120px" });
  const maxClaims = h("input", { type: "number", min: 0, value: 0, title: "Max. per player (0 = unlimited)", style: "max-width:120px" });
  const phraseText = Object.fromEntries(m.phrases.map((p) => [p.raw, p.text]));

  return h("div", { class: "card" }, h("h2", { text: "🤖 Autokits" }),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Kit", "Trigger", "Cooldown", "Max.", "Required role", "Status", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, data.rules.map((r) => h("tr", {},
        h("td", { text: r.id }),
        h("td", { text: (r.kit_type === "custom" ? "🔒 " : "") + r.kit }),
        h("td", { text: r.trigger === "quickchat" ? `💬 "${phraseText[r.phrase] || r.phrase}"` : "🔄 Respawn" }),
        h("td", { text: r.cooldown_minutes ? fmtDuration(r.cooldown_minutes * 60) : "–" }),
        h("td", { text: r.max_claims || "∞" }),
        h("td", {}, (() => {
          const sel = roleSelect(r.required_role_id);
          sel.addEventListener("change", async () => {
            await act(() => api(`/api/autokits/${r.id}/role`, { method: "PUT", body: { required_role_id: sel.value } }), "Saved");
          });
          return sel;
        })()),
        h("td", {}, h("span", { class: "pill " + (r.enabled ? "ok" : "warn"), text: r.enabled ? "active" : "paused" })),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", text: r.enabled ? "Pause" : "Activate", onclick: async () => {
            await act(() => api(`/api/autokits/${r.id}/toggle`, { method: "POST", body: { enabled: !r.enabled } })); reload();
          } }),
          h("button", { class: "small danger", text: "Delete", onclick: async () => {
            if (!confirm(`Delete autokit #${r.id}?`)) return;
            await act(() => api(`/api/autokits/${r.id}`, { method: "DELETE" })); reload();
          } })))))))),
    h("h2", { text: "New autokit", style: "margin-top:1rem" }),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault();
      if (!kit.value) { toast("Create a kit first", true); return; }
      const [kitType, kitName] = [kit.value.split(":")[0], kit.value.slice(kit.value.indexOf(":") + 1)];
      await act(() => api("/api/autokits", { method: "POST", body: {
        kit: kitName, kit_type: kitType, trigger: trigger.value, phrase: phrase.value,
        cooldown: Number(cooldown.value), max_claims: Number(maxClaims.value), required_role_id: newRole.value } }), "Autokit created");
      reload();
    } }, kit, trigger, phrase,
      h("label", { class: "muted" }, "Cooldown (min) ", cooldown),
      h("label", { class: "muted" }, "Max. per player ", maxClaims),
      h("label", { class: "muted", title: "Only players whose linked Discord account (/customkit link) has this role" }, "Required role ", newRole),
      h("button", { class: "primary", text: "Create" })),
    h("p", { class: "muted", style: "margin:.5rem 0 0", text: "Required role: players must link their in-game player to Discord once with /customkit link – the bot then checks their Discord roles (updates within 2 minutes)." }),
    h("div", { class: "row", style: "margin-top:.75rem" },
      h("button", { text: "♻️ Wipe reset (reset cooldowns & limits)", onclick: async () => {
        if (!confirm("Reset all autokit cooldowns and limits?")) return;
        const r = await act(() => api("/api/autokits/reset", { method: "POST" }));
        toast(`${r.count} claims reset`); reload();
      } })));
}

// ------------------------------------------------------------------ Tickets

views.tickets = async (main) => {
  const filter = h("select", {}, h("option", { value: "", text: "All" }), h("option", { value: "open", text: "Open" }), h("option", { value: "closed", text: "Closed" }));
  const body = h("tbody");
  const detail = h("div");
  const ai = h("div", { class: "card" }, h("h2", { text: "🤖 AI (DeepSeek)" }), h("p", { class: "muted", text: "Loading …" }));
  main.append(h("h1", { text: "Tickets" }), ai,
    h("div", { class: "card" }, h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🎫 Tickets", style: "margin:0" }), filter),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Status", "Topic", "In-game name", "AI", "Staff", "Created (UTC)"].map((t) => h("th", { text: t })))),
        body))),
    detail);

  const load = async () => {
    const r = await api("/api/tickets" + (filter.value ? `?status=${filter.value}` : ""));
    body.replaceChildren(...(r.tickets.length ? r.tickets.map((t) => h("tr", { class: "clickable", onclick: () => showTicket(t.id, detail) },
      h("td", { text: t.id }),
      h("td", {}, h("span", { class: "pill " + (t.status === "open" ? "ok" : ""), text: t.status === "open" ? "open" : "closed" })),
      h("td", { text: t.topic.length > 80 ? t.topic.slice(0, 80) + "…" : t.topic }),
      h("td", { text: t.ingame_name || "–" }),
      h("td", { text: `${t.ai_replies}×` + (t.ai_enabled ? "" : " (off)") }),
      h("td", { text: t.escalated ? (t.staff_joined ? "taken over" : "called") : "–" }),
      h("td", { text: t.created_at }))) : [h("tr", {}, h("td", { colspan: 7, class: "muted", text: "No tickets." }))]));
  };
  filter.addEventListener("change", load);
  every(10000, () => load().catch(() => {}));

  api("/api/ai").then((r) => {
    const kids = [h("h2", { text: "🤖 AI (DeepSeek)" })];
    if (!r.enabled) kids.push(h("p", { class: "muted", text: "No DEEPSEEK_API_KEY – tickets go straight to staff." }));
    else {
      kids.push(h("p", {}, "Model: ", h("code", { text: r.model })));
      if (r.error) kids.push(h("p", { class: "error", text: r.error }));
      if (r.balance) {
        kids.push(h("p", {}, h("span", { class: "pill " + (r.balance.is_available ? "ok" : "bad"), text: r.balance.is_available ? "Balance sufficient" : "Balance insufficient" })));
        for (const b of r.balance.balance_infos || []) kids.push(h("p", { text: `${b.currency}: ${b.total_balance} (topped up ${b.topped_up_balance}, granted ${b.granted_balance})` }));
      }
      kids.push(h("p", {}, h("a", { href: "https://platform.deepseek.com/usage", target: "_blank", rel: "noopener", text: "Usage & top-up at platform.deepseek.com ↗" })));
    }
    ai.replaceChildren(...kids);
  }).catch(() => {});
};

async function showTicket(id, target) {
  const t = await act(() => api(`/api/tickets/${id}`));
  target.replaceChildren(h("div", { class: "card" },
    h("h2", { text: `Ticket #${t.id}` }),
    h("p", {}, h("strong", { text: "Topic: " }), t.topic),
    h("p", { class: "muted", text: `Discord user ID ${t.user_id} · created ${t.created_at} UTC` + (t.closed_at ? ` · closed ${t.closed_at} by ${t.closed_by}` : "") + (t.close_reason ? ` · Reason: ${t.close_reason}` : "") }),
    t.status === "open" ? h("p", { class: "muted", text: "Open – the conversation is happening in the Discord channel. The transcript appears here once it is closed." })
      : h("pre", { class: "transcript", text: t.transcript || "(no transcript saved)" })));
  target.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ Stats

views.stats = (main) => {
  const cat = h("select", {}, [["kills", "Kills"], ["deaths", "Deaths"], ["kd", "K/D"], ["playtime", "Playtime"]].map(([v, t]) => h("option", { value: v, text: t })));
  const body = h("tbody");
  main.append(h("h1", { text: "Stats" }), h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🏆 Leaderboard", style: "margin:0" }), cat),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Player", "Kills", "Deaths", "K/D", "Playtime", "Platform", "Last seen (UTC)"].map((t) => h("th", { text: t })))),
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

// ------------------------------------------------------------------ Knowledge base

views.knowledge = async (main) => {
  const area = h("textarea", { rows: 26, spellcheck: "true" });
  const info = h("p", { class: "muted" });
  main.append(h("h1", { text: "Knowledge base for AI support" }),
    h("div", { class: "card" }, info, area,
      h("div", { class: "row", style: "margin-top:.75rem" },
        h("button", { class: "primary", text: "Save", onclick: () => act(() => api("/api/knowledge", { method: "PUT", body: { text: area.value } }), "Saved – applies from the next AI reply") }),
        h("span", { class: "muted", text: "Don't put anything secret here – the AI may pass on anything in it to players." }))));
  const r = await act(() => api("/api/knowledge"));
  area.value = r.text;
  info.textContent = `File: ${r.path}`;
};

// ------------------------------------------------------------------ Community

views.community = async (main) => {
  const info = h("div", { class: "card" }, h("p", { class: "muted", text: "Loading …" }));
  const gBody = h("tbody");
  const rulesArea = h("textarea", { rows: 12 });
  main.append(h("h1", { text: "Community" }), info,
    h("div", { class: "card" }, h("h2", { text: "🎉 Giveaways" }),
      h("p", { class: "muted", text: "Start one with /giveaway start in the desired Discord channel." }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Prize", "Entries", "Status", ""].map((t) => h("th", { text: t })))),
        gBody))),
    h("div", { class: "card" }, h("h2", { text: "📜 Rules (/rules)" }),
      h("p", { class: "muted", text: "One rule per line. Lines starting with # are ignored." }), rulesArea,
      h("div", { class: "row", style: "margin-top:.75rem" },
        h("button", { class: "primary", text: "Save", onclick: () => act(() => api("/api/rules", { method: "PUT", body: { text: rulesArea.value } }), "Rules saved") }))));

  const load = async () => {
    const c = await api("/api/community");
    const item = (label, value) => h("div", { class: "stat" }, h("div", { class: "label", text: label }), h("div", { class: "value", style: "font-size:1rem", text: value || "– (off)" }));
    info.replaceChildren(h("div", { class: "grid", style: "margin:0" },
      item("Discord server", c.guild && `${c.guild} (${c.members ?? "?"} members)`),
      item("Welcome channel", c.welcome_channel),
      item("Auto role", c.auto_role),
      item("Discord log", c.discord_log_channel),
      item("Rules channel", c.rules_channel)),
      h("p", { class: "muted", style: "margin:.75rem 0 0", text: "Change these in the .env (WELCOME_CHANNEL_ID, AUTO_ROLE_ID, DISCORD_LOG_CHANNEL_ID, …) and restart the bot." }));
    gBody.replaceChildren(...(c.giveaways.length ? c.giveaways.map((g) => h("tr", {},
      h("td", { text: g.id }), h("td", { text: g.prize }), h("td", { text: g.entries }),
      h("td", { text: g.ended ? `ended${g.winner_ids ? " – " + g.winner_ids.split(",").length + " winner(s)" : " – no winners"}` : "running until " + new Date(g.ends_at * 1000).toLocaleString() }),
      h("td", {}, g.ended
        ? h("button", { class: "small", text: "Reroll", onclick: async () => { await act(() => api(`/api/giveaways/${g.id}/reroll`, { method: "POST" }), "Rerolled"); load(); } })
        : h("button", { class: "small", text: "End now", onclick: async () => { if (!confirm("Draw the giveaway now?")) return; await act(() => api(`/api/giveaways/${g.id}/end`, { method: "POST" }), "Winners drawn"); load(); } }))))
      : [h("tr", {}, h("td", { colspan: 5, class: "muted", text: "No giveaways yet." }))]));
  };
  every(15000, () => load().catch(() => {}));
  const r = await api("/api/rules").catch(() => ({ text: "" }));
  rulesArea.value = r.text;
};

// ------------------------------------------------------------------ Settings

views.settings = async (main) => {
  main.append(h("h1", { text: setupMode ? "Setup" : "Settings" }));
  const banner = h("div");
  main.append(banner);
  let data, opts;
  try {
    [data, opts] = await Promise.all([api("/api/settings"), api("/api/settings/discord-options").catch(() => ({ available: false }))]);
  } catch (e) { toast(e.message, true); return; }

  const showBanner = (kind, ...content) => banner.replaceChildren(h("div", { class: "banner " + kind }, ...content));
  if (data.setup_mode) {
    showBanner("bad", h("strong", { text: "The bot has not started yet. " }), data.setup_error || "",
      fixLink(data.setup_fix_url),
      h("p", { class: "muted", style: "margin:.4rem 0 0", text: "Then click \"Start bot with saved settings\" below (or fill in the required fields and save)." }));
  } else if (data.restart_required) {
    showBanner("warn", "Changed settings only take effect after a restart. ", restartButton());
  } else if (status?.intent_warning) {
    showBanner("warn", h("strong", { text: "Limited mode: " }), status.intent_warning, fixLink(status.intent_fix_url),
      h("p", { class: "muted", style: "margin:.4rem 0 0" }, "After enabling it in the Developer Portal (don't forget \"Save Changes\"): ", restartButton()));
  } else if (!opts.available) {
    showBanner("", h("span", { class: "muted", text: "Tip: once the bot is connected to Discord, you can pick channels and roles here directly." }));
  }

  const inputs = {}, rows = {};
  const advancedCards = [];
  const optionList = (pick) => ({ guild: opts.guilds, text_channel: opts.text_channels, category: opts.categories, role: opts.roles }[pick] || []);

  const control = (f) => {
    const value = f.value ?? "";
    if (f.kind === "secret") {
      const el = h("input", { type: "password", autocomplete: "new-password",
        placeholder: f.is_set ? "•••••• saved – leave empty to keep" : "not set yet" });
      inputs[f.key] = () => el.value;
      return el;
    }
    if (f.kind === "bool") {
      const el = h("select", {}, h("option", { value: "", text: "Default" }), h("option", { value: "true", text: "On" }), h("option", { value: "false", text: "Off" }));
      el.value = ["true", "false"].includes(value.toLowerCase()) ? value.toLowerCase() : "";
      inputs[f.key] = () => el.value;
      return el;
    }
    if (f.choices.length) {
      const el = h("select", {}, h("option", { value: "", text: "Default" }), f.choices.map((c) => h("option", { value: c, text: c === "127.0.0.1" ? "This PC only (127.0.0.1)" : c === "0.0.0.0" ? "Home network too (0.0.0.0)" : c })));
      el.value = value;
      inputs[f.key] = () => el.value;
      return el;
    }
    if (f.kind === "textarea") {
      const el = h("textarea", { rows: 3, placeholder: f.placeholder });
      el.value = value;
      inputs[f.key] = () => el.value;
      return el;
    }
    const list = f.pick && opts.available ? optionList(f.pick) : [];
    if (f.kind === "id" && list.length) {
      const el = h("select", {}, h("option", { value: "", text: "– off / none –" }), list.map((o) => h("option", { value: o.id, text: o.name })));
      if (value && !list.some((o) => o.id === value)) el.append(h("option", { value, text: `ID ${value} (not found)` }));
      el.value = value;
      inputs[f.key] = () => el.value;
      return el;
    }
    if (f.kind === "ids" && list.length) {
      const current = new Set(value.split(",").map((v) => v.trim()).filter(Boolean));
      const boxes = list.map((o) => { const cb = h("input", { type: "checkbox", value: o.id }); cb.checked = current.has(o.id); return [cb, o]; });
      const unknown = [...current].filter((id) => !list.some((o) => o.id === id));
      inputs[f.key] = () => [...boxes.filter(([cb]) => cb.checked).map(([cb]) => cb.value), ...unknown].join(",");
      return h("div", { class: "checklist" }, boxes.map(([cb, o]) => h("label", {}, cb, o.name)),
        unknown.length ? h("div", { class: "muted", text: `Other IDs: ${unknown.join(", ")}` }) : null);
    }
    const el = h("input", { value, placeholder: f.placeholder || (f.kind === "id" || f.kind === "ids" ? "Discord ID (right-click → Copy ID)" : ""),
      inputmode: ["int", "id", "ids"].includes(f.kind) ? "numeric" : null });
    inputs[f.key] = () => el.value;
    return el;
  };

  for (const g of data.groups) {
    const fields = data.fields.filter((f) => f.group === g.id);
    const card = h("div", { class: "card" }, h("h2", { text: g.label }));
    for (const f of fields) {
      const err = h("div", { class: "field-error" });
      const row = h("div", { class: "field" + (f.advanced ? " advanced hidden" : "") },
        h("label", {}, f.label, f.required ? h("span", { class: "req", text: " *" }) : null),
        h("div", { class: "control" }, control(f), f.help ? h("div", { class: "help", text: f.help }) : null, err));
      rows[f.key] = { row, err };
      card.append(row);
    }
    if (g.id === "discord") card.append(discordTester(inputs));
    if (g.id === "rcon") card.append(rconTester(inputs));
    if (g.id === "updates" && !data.setup_mode) card.firstChild.after(updateStatus());
    if (g.id === "templates") { card.classList.add("hidden"); advancedCards.push(card); }
    main.append(card);
  }

  let advancedShown = false;
  const advancedToggle = h("button", { class: "ghost", text: "Show advanced settings", onclick: () => {
    advancedShown = !advancedShown;
    const show = advancedShown;
    advancedCards.forEach((c) => c.classList.toggle("hidden", !show));
    document.querySelectorAll(".field.advanced").forEach((r) => r.classList.toggle("hidden", !show));
    advancedToggle.textContent = show ? "Hide advanced settings" : "Show advanced settings";
  } });

  const saveBtn = h("button", { class: "primary", text: data.setup_mode ? "Save & start bot" : "Save" });
  saveBtn.addEventListener("click", async () => {
    Object.values(rows).forEach(({ row, err }) => { row.classList.remove("has-error"); err.textContent = ""; });
    const values = Object.fromEntries(Object.entries(inputs).map(([k, get]) => [k, get()]));
    saveBtn.disabled = true;
    try {
      const r = await api("/api/settings", { method: "PUT", body: { values } });
      document.querySelectorAll('input[type="password"]').forEach((el) => { el.value = ""; });
      if (r.rcon_test && rconResultBox) {
        rconResultBox.replaceChildren(h("p", { style: "margin:.5rem 0 0", text: "Automatic test of the saved RCON settings:" }), renderSteps(r.rcon_test));
        if (!r.rcon_test.ok) toast("Saved – but RCON doesn't work with these settings yet (see the RCON section).", true);
      }
      if (r.preflight_error && data.setup_mode) {
        showBanner("bad", h("strong", { text: "Saved – but the bot can't start like this yet: " }), r.preflight_error, fixLink(r.fix_url));
        banner.scrollIntoView({ behavior: "smooth" });
      } else if (r.starting) {
        showBanner(r.intent_warning ? "warn" : "ok", h("strong", { text: "Saved – the bot is starting now …" }), " This page will reload automatically.",
          r.intent_warning ? h("p", { style: "margin:.4rem 0 0", text: "Note: " + r.intent_warning }) : null);
        waitForBot("#overview");
      } else if (r.config_error) {
        showBanner("warn", h("strong", { text: "Saved. Something is still missing: " }), r.config_error);
      } else if (r.preflight_error) {
        showBanner("bad", h("strong", { text: "Saved – fix this before restarting: " }), r.preflight_error, fixLink(r.fix_url));
      } else if (r.restart_required) {
        showBanner("warn", h("strong", { text: "Saved. " }), "Restart the bot for the changes to take effect: ", restartButton());
      } else {
        toast("Saved");
      }
    } catch (e) {
      const errors = e.data?.errors || {};
      for (const [key, msg] of Object.entries(errors)) {
        if (rows[key]) { rows[key].row.classList.add("has-error"); rows[key].err.textContent = msg; rows[key].row.classList.remove("hidden"); }
      }
      const first = Object.keys(errors)[0];
      if (first && rows[first]) rows[first].row.scrollIntoView({ behavior: "smooth", block: "center" });
      toast(e.message, true);
    } finally { saveBtn.disabled = false; }
  });

  const bar = h("div", { class: "savebar" }, saveBtn, advancedToggle,
    h("span", { class: "muted", text: "Settings are saved to the .env in the bot folder." }));
  if (data.setup_mode) {
    bar.append(h("button", { text: "Start bot with saved settings", onclick: async () => {
      try {
        const r = await api("/api/setup/continue", { method: "POST" });
        if (!r.ok) { showBanner("bad", h("strong", { text: "Not possible yet: " }), r.preflight_error, fixLink(r.fix_url)); banner.scrollIntoView({ behavior: "smooth" }); return; }
        showBanner(r.intent_warning ? "warn" : "ok", "Bot is starting …",
          r.intent_warning ? h("p", { style: "margin:.4rem 0 0", text: "Note: " + r.intent_warning }) : null);
        waitForBot("#overview");
      } catch (e) { toast(e.message, true); }
    } }));
  }
  main.append(bar);
};

// Installed version + "Check now" (settings page, group "Updates")
function updateStatus() {
  const box = h("div", { class: "update-box" }, h("span", { class: "muted", text: "Loading version …" }));
  const ago = (ts) => ts ? fmtDuration(Date.now() / 1000 - ts) + " ago" : "not yet";
  const render = (u) => {
    if (u.available === false) { box.replaceChildren(h("span", { class: "muted", text: "Updates module not loaded." })); return; }
    const cur = u.current || {};
    const state = u.last_status === "error" || u.last_status === "skipped"
      ? h("span", { class: "pill warn", text: u.last_message })
      : u.last_status ? h("span", { class: "pill ok", text: u.last_status === "updated" ? "Updated – restarting" : "Up to date" }) : null;
    const btn = h("button", { class: "small", text: "🔄 Check now", onclick: async () => {
      btn.disabled = true; btn.textContent = "Checking …";
      try {
        const r = await api("/api/update/check", { method: "POST" });
        render(r);
        if (r.restarting) { toast("New version installed – the bot restarts …"); waitForBot(location.hash || "#settings"); }
        else toast(r.status === "up_to_date" ? "Already the newest version" : r.message, r.status === "error");
      } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "🔄 Check now"; }
    } });
    box.replaceChildren(
      h("div", { class: "row" },
        h("strong", { text: "Installed version: " }),
        cur.sha ? h("code", { text: cur.sha.slice(0, 7) }) : h("span", { class: "muted", text: "unknown (first check pending)" }),
        cur.title ? h("span", { class: "muted", text: cur.title }) : null),
      h("div", { class: "row muted", style: "font-size:.85rem" },
        `${u.enabled ? `Auto-update on – every ${u.interval} min` : "Auto-update off"} · from ${u.repo} (${u.branch}) · ` +
        `${u.mode === "git" ? "git checkout" : "ZIP install"} · last check: ${ago(u.last_check)}`, state),
      u.launcher ? null : h("p", { class: "muted", style: "font-size:.85rem;margin:.2rem 0 0", text: "Tip: start the bot with start.bat / start.sh – then it restarts by itself after an update." }),
      h("div", { class: "row", style: "margin-top:.4rem" }, btn));
  };
  api("/api/update").then(render).catch((e) => box.replaceChildren(h("span", { class: "error", text: e.message })));
  return box;
}

function restartButton() {
  return h("button", { class: "primary small", text: "Restart bot now", onclick: async (ev) => {
    if (!confirm("Restart the bot? It will be offline for a few seconds.")) return;
    ev.target.disabled = true;
    const r = await act(() => api("/api/restart", { method: "POST" })).catch(() => null);
    if (!r) { ev.target.disabled = false; return; }
    if (!r.ok) {
      ev.target.disabled = false;
      alert("Restart cancelled, otherwise the bot would fail to start:\n\n" + r.preflight_error + (r.fix_url ? "\n\nLink: " + r.fix_url : ""));
      return;
    }
    toast(r.intent_warning ? "Bot is restarting – in limited mode (intent still missing)" : "Bot is restarting …", !!r.intent_warning);
    waitForBot(location.hash || "#overview");
  } });
}

// Link to fix the problem (e.g. Developer Portal), opens in a new tab
function fixLink(url) {
  return url ? h("p", { style: "margin:.5rem 0 0" }, h("a", { href: url, target: "_blank", rel: "noopener" },
    h("button", { class: "primary small", text: "🔧 Fix in the Discord Developer Portal ↗" }))) : null;
}

// Result list of an RCON test ({ok, steps})
function renderSteps(r) {
  return h("div", {}, h("ul", { class: "steps" }, r.steps.map((s) => h("li", { class: s.level, text: s.text }))),
    h("p", { style: "margin:.5rem 0 0", text: r.ok ? "✅ RCON works." : "❌ RCON doesn't work yet – see above." }));
}

let rconResultBox = null;
function rconTester(inputs) {
  const out = h("div");
  rconResultBox = out;
  const btn = h("button", { text: "🔌 Test RCON", onclick: async () => {
    btn.disabled = true;
    out.replaceChildren(h("p", { class: "muted", text: "Testing … (up to 20 seconds)" }));
    try {
      const r = await api("/api/settings/test-rcon", { method: "POST", body: {
        host: inputs.RCON_HOST(), port: inputs.RCON_PORT(), password: inputs.RCON_PASSWORD() } });
      out.replaceChildren(renderSteps(r),
        r.ok ? null : h("p", { class: "muted", text: "If no password is typed in, the saved one is used." }));
    } catch (e) { out.replaceChildren(h("p", { class: "error", text: e.message })); }
    btn.disabled = false;
  } });
  return h("div", { style: "margin-top:.75rem" }, btn, out);
}

function discordTester(inputs) {
  const out = h("div");
  const btn = h("button", { text: "🔑 Check bot token", onclick: async () => {
    btn.disabled = true;
    out.replaceChildren(h("p", { class: "muted", text: "Checking …" }));
    try {
      const r = await api("/api/settings/test-discord", { method: "POST", body: { token: inputs.DISCORD_TOKEN() } });
      if (!r.ok) { out.replaceChildren(h("p", { class: "error", text: r.error })); }
      else {
        const pill = (on, label) => h("span", { class: "pill " + (on ? "ok" : "bad"), text: (on ? "✓ " : "✗ ") + label });
        out.replaceChildren(
          h("p", {}, "✅ Token valid – bot: ", h("strong", { text: r.bot_name || "?" })),
          r.intents ? h("p", { class: "row" }, "Intents in the Developer Portal: ", pill(r.intents.message_content, "Message Content"), pill(r.intents.members, "Server Members")) : null,
          r.intents && !(r.intents.message_content && r.intents.members) ? h("p", { class: "muted", text: "Enable missing intents in the Developer Portal under Bot → Privileged Gateway Intents (only needed if you use the chat bridge, support tickets, welcome message/auto role or Discord log)." }) : null,
          r.invite_url ? h("p", {}, h("a", { href: r.invite_url, target: "_blank", rel: "noopener", text: "➕ Invite the bot to your Discord server (with all required permissions)" })) : null);
      }
    } catch (e) { out.replaceChildren(h("p", { class: "error", text: e.message })); }
    btn.disabled = false;
  } });
  return h("div", { style: "margin-top:.75rem" }, btn, out);
}

// ------------------------------------------------------------------ Bot log

views.botlog = (main) => {
  const out = h("div", { class: "console" });
  main.append(h("h1", { text: "Bot log" }), h("p", { class: "muted", text: "The last 500 lines. Full log: logs/bot.log in the bot folder." }), out);
  every(4000, async () => {
    const r = await api("/api/botlog").catch(() => null);
    if (!r) return;
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.replaceChildren(...r.lines.map((l) => h("div", { text: l })));
    if (stick) out.scrollTop = out.scrollHeight;
  });
};

start();
