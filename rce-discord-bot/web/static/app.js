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

// Theme: neon (default) <-> light
const THEMES = { dark: "🌙", light: "☀️" };
const THEME_NAMES = { dark: "dark", light: "light" };
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
  const shown = theme === "light" ? "light" : "dark";
  $("#theme").textContent = THEMES[shown];
  $("#theme").title = `Theme: ${THEME_NAMES[shown]} (click for ${THEME_NAMES[shown === "light" ? "dark" : "light"]})`;
}
let theme = "dark";
try { theme = localStorage.getItem("rce-theme") === "light" ? "light" : "dark"; } catch { /* storage blocked */ }
applyTheme(theme);
$("#theme").addEventListener("click", () => {
  theme = theme === "light" ? "dark" : "light";
  try { localStorage.setItem("rce-theme", theme); } catch { /* storage blocked */ }
  applyTheme(theme);
});

// Animations on/off (✨): off (the default) = still neon background, no moving effects – light on any PC
function applyMotion(on) {
  if (on) delete document.documentElement.dataset.motion; else document.documentElement.dataset.motion = "off";
  $("#motion").textContent = on ? "✨" : "⏸️";
  $("#motion").title = on ? "Animations: on (click to switch off)" : "Animations: off (click to switch on)";
}
let motion = false;
try { motion = localStorage.getItem("rce-motion") === "on"; } catch { /* storage blocked */ }
applyMotion(motion);
$("#motion").addEventListener("click", () => {
  motion = !motion;
  try { localStorage.setItem("rce-motion", motion ? "on" : "off"); } catch { /* storage blocked */ }
  applyMotion(motion);
});

// ------------------------------------------------------------------ Sidebar groups, server card, top bar, search

// Menu groups: a header with icon, name, how many pages and a collapse arrow (remembered per browser)
const GROUP_EYEBROWS = { Server: "Live server operations", Kits: "Kits & store", Community: "Community tools", Admin: "Server configuration" };

// Line icons for the menu (own simple drawings, 24×24, drawn with the text colour)
const ICONS = {
  home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
  box: '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 6"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  overview: '<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/><circle cx="12" cy="14" r="1"/>',
  console: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  botlog: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M9 12h7M9 16h7"/>',
  kits: '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
  kitbuilder: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14v7M14 17.5h7"/>',
  shop: '<circle cx="9" cy="9" r="5"/><path d="M14.6 6.6a5 5 0 1 1-6.1 8"/>',
  webstore: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h4"/>',
  tickets: '<path d="M3 7h18v3a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4z"/><path d="M13 7v10" stroke-dasharray="2 2"/>',
  reports: '<path d="M5 21V4h11l-2 4 2 4H5"/>',
  knowledge: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 19V5M8 7h7"/>',
  stats: '<path d="M4 20V11M10 20V4M16 20v-7M2 20h20"/>',
  community: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M18 3l1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/>',
  protection: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M12 8v5l3 2"/>',
  plugins: '<path d="M10 3h4v3a2 2 0 1 0 4 0V3h3v7h-3a2 2 0 1 0 0 4h3v7h-7v-3a2 2 0 1 0-4 0v3H3v-7h3a2 2 0 1 0 0-4H3V3z"/>',
  makeplugin: '<path d="M4 20L15 9"/><path d="M15 3v3M18 6h3M19 10v2M17 4l2 2"/>',
  users2: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 6"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
};
const GROUP_ICON_NAMES = { Server: "home", Kits: "box", Community: "users", Admin: "shield" };
function svgIcon(name) {
  const span = document.createElement("span");
  span.className = "ico";
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.box}</svg>`;
  return span;
}
// every menu entry gets its own colour (text, icon box, edge)
const LINK_COLORS = ["#f472b6", "#c4b5fd", "#93c5fd", "#5eead4", "#86efac", "#bef264", "#fde047", "#fdba74", "#fda4af",
  "#f0abfc", "#a5b4fc", "#7dd3fc", "#6ee7b7", "#d9f99d", "#fcd34d", "#fb923c", "#f9a8d4"];
document.querySelectorAll("#nav a").forEach((a, i) => {
  const emoji = a.querySelector("i");
  a.dataset.emoji = emoji?.textContent || "•";
  emoji?.replaceWith(svgIcon(a.dataset.view === "users" ? "users2" : a.dataset.view));
  a.style.setProperty("--c", LINK_COLORS[i % LINK_COLORS.length]);
});
const NAV_CLOSED_KEY = "rce-nav-closed";
let navClosed = {};
try { navClosed = JSON.parse(localStorage.getItem(NAV_CLOSED_KEY) || "{}") || {}; } catch { /* storage blocked */ }
document.querySelectorAll("#nav .nav-group").forEach((group) => {
  const old = group.querySelector(".nav-label");
  const name = old.textContent.trim();
  const count = h("b", { class: "count" });
  const btn = h("button", { type: "button", class: "nav-label", "aria-expanded": "true" },
    h("span", { class: "gi" }, svgIcon(GROUP_ICON_NAMES[name] || "box")), h("span", { text: name }), count, h("em", { class: "chev", text: "⌃" }));
  btn.addEventListener("click", () => {
    const closed = !group.classList.contains("closed");
    group.classList.toggle("closed", closed);
    btn.setAttribute("aria-expanded", String(!closed));
    if (closed) navClosed[name] = 1; else delete navClosed[name];
    try { localStorage.setItem(NAV_CLOSED_KEY, JSON.stringify(navClosed)); } catch { /* ignore */ }
  });
  old.replaceWith(btn);
  group.dataset.name = name;
  if (navClosed[name]) { group.classList.add("closed"); btn.setAttribute("aria-expanded", "false"); }
});
function updateNavCounts() {
  document.querySelectorAll("#nav .nav-group").forEach((group) => {
    group.querySelector(".count").textContent = group.querySelectorAll("a:not(.hidden)").length;
    if (group.querySelector("a.active")) group.classList.remove("closed");     // the open page is never hidden
  });
}

// Server card (sidebar) and the chips in the top bar – from /api/status
function updateServerCard() {
  if (!status) return;
  const s = status.server || {};
  const name = s.name || (status.setup_mode ? "Setup" : "Rust server");
  $("#srv-name").textContent = name;
  $("#srv-chip-name").textContent = status.rcon_connected ? name : (status.setup_mode ? "Setup mode" : "Server offline");
  $("#srv-chip").className = "server-chip " + (status.rcon_connected ? "ok" : "bad");
  const players = s.players ?? (status.players || []).length;
  $("#srv-chip-players").textContent = status.rcon_connected ? `#${players}` : "–";
  $("#chip-players").replaceChildren(h("b", { text: status.rcon_connected ? String(players) : "–" }),
    ` player${players === 1 ? "" : "s"}` + (s.max_players ? ` / ${s.max_players}` : ""));
  const addr = $("#chip-address");
  addr.hidden = !status.rcon_connected || !s.map;
  if (s.map) addr.textContent = `🗺️ ${s.map}`;
}

// The page title sits in the top bar (with a small label: the menu group), the page's own <h1> is hidden
function eyebrow() {
  const h1 = $("#main").querySelector("h1");                 // the page's title (also inside a header row)
  const link = document.querySelector("#nav a.active");
  const group = link?.closest(".nav-group")?.dataset.name;
  const label = group ? (GROUP_EYEBROWS[group] || group) : "";
  const title = h1 ? h1.textContent.trim() : "";
  if ($("#top-h1").textContent !== title) $("#top-h1").textContent = title;
  if ($("#top-eyebrow").textContent !== label) $("#top-eyebrow").textContent = label;
  if (h1 && !h1.classList.contains("in-topbar")) { h1.classList.add("in-topbar"); h1.dataset.eyebrow = label; }
}
new MutationObserver(eyebrow).observe($("#main"), { childList: true, subtree: true, characterData: true });

// Search (Ctrl K): every page of the menu and every setting – opens it and highlights the setting
const palette = $("#palette"), paletteInput = $("#palette-input"), paletteList = $("#palette-list");
let paletteItems = [], paletteIndex = 0, settingsIndex = null;
async function loadSettingsIndex() {
  if (settingsIndex || !isOwner()) return settingsIndex || [];
  try {
    const d = await api("/api/settings");
    const groups = Object.fromEntries((d.groups || []).map((g) => [g.id, g.label]));
    settingsIndex = d.fields.map((f) => ({ label: f.label, where: `Settings · ${groups[f.group] || f.group}`, hash: "#settings", find: f.label, icon: "⚙️" }));
  } catch { settingsIndex = []; }
  return settingsIndex;
}
function pages() {
  return [...document.querySelectorAll("#nav a:not(.hidden)")].map((a) => ({
    label: a.querySelector(":scope > span:not(.ico)").textContent, where: a.closest(".nav-group")?.dataset.name || "",
    hash: a.getAttribute("href"), icon: a.dataset.emoji || "•" }));
}
function renderPalette() {
  const q = paletteInput.value.trim().toLowerCase();
  const all = [...pages(), ...(settingsIndex || [])];
  paletteItems = (q ? all.filter((it) => (it.label + " " + it.where).toLowerCase().includes(q)) : pages()).slice(0, 40);
  paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
  paletteList.replaceChildren(...(paletteItems.length ? paletteItems.map((it, i) => {
    const b = h("button", { type: "button", class: "palette-item" + (i === paletteIndex ? " on" : "") },
      h("span", { text: it.icon }), h("span", { text: it.label }), h("small", { text: it.where }));
    b.addEventListener("click", () => openItem(it));
    return b;
  }) : [h("p", { class: "muted", style: "padding:.6rem .8rem;margin:0", text: "Nothing found." })]));
}
async function openPalette() {
  palette.classList.remove("hidden");
  paletteInput.value = ""; paletteIndex = 0;
  renderPalette();
  paletteInput.focus();
  await loadSettingsIndex();
  renderPalette();
}
function closePalette() { palette.classList.add("hidden"); }
function openItem(it) {
  closePalette();
  if (location.hash !== it.hash) location.hash = it.hash;
  if (!it.find) return;
  const started = Date.now();
  const look = () => {
    const label = [...$("#main").querySelectorAll(".field > label")].find((l) => l.textContent.trim().replace(/\s*\*$/, "") === it.find);
    if (!label) { if (Date.now() - started < 4000) setTimeout(look, 150); return; }
    const row = label.closest(".field");
    row.classList.remove("hidden");
    const card = row.closest(".card.collapsed");
    if (card) setCollapsed(card, false);
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
    row.querySelector("input, select, textarea")?.focus({ preventScroll: true });
  };
  setTimeout(look, 50);
}
$("#search-open").addEventListener("click", openPalette);
palette.addEventListener("click", (e) => { if (e.target === palette) closePalette(); });
paletteInput.addEventListener("input", () => { paletteIndex = 0; renderPalette(); });
paletteInput.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    paletteIndex = (paletteIndex + (e.key === "ArrowDown" ? 1 : -1) + paletteItems.length) % Math.max(1, paletteItems.length);
    renderPalette();
  } else if (e.key === "Enter" && paletteItems[paletteIndex]) { e.preventDefault(); openItem(paletteItems[paletteIndex]); }
  else if (e.key === "Escape") closePalette();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !$("#app").classList.contains("hidden")) {
    e.preventDefault();
    if (palette.classList.contains("hidden")) openPalette(); else closePalette();
  }
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
  updateNavCounts();
  const main = $("#main");
  main.replaceChildren();
  if (!isOwner() && ["settings", "users", "plugins", "makeplugin", "webstore", "protection"].includes(view)) {
    main.append(h("h1", { text: "No access" }), h("p", { class: "muted", text: "Only owners can open this page. Ask the server owner if you need it." }));
    return;
  }
  (views[view] || views.overview)(main);
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ Collapsible cards (every page)
// Every card whose first element is an <h2> gets a toggle; the state is remembered per page and title.
// Works for cards that are built later or re-rendered (MutationObserver), and adds "Collapse all / Expand all".
const COLLAPSE_KEY = "rce-collapsed";
let collapsedState = {};
try { collapsedState = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}") || {}; } catch { /* storage blocked */ }
const saveCollapsed = () => { try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsedState)); } catch { /* ignore */ } };
const cardKey = (h2) => `${(location.hash || "#overview").slice(1)}|${h2.textContent.replace(/[\d.,]+/g, "#").replace(/\s+/g, " ").trim().slice(0, 60)}`;

// The card's header: its first element if that's an <h2>, or a row whose first element is the <h2>
function cardHeader(card) {
  const first = card.firstElementChild;
  if (!first) return null;
  if (first.tagName === "H2") return first;
  if (first.tagName === "DIV" && first.classList.contains("row") && first.firstElementChild?.tagName === "H2") return first;
  return null;
}
const headerTitle = (head) => (head.tagName === "H2" ? head : head.firstElementChild);

function setCollapsed(card, collapsed, remember = true) {
  card.classList.toggle("collapsed", collapsed);
  const head = card.querySelector(":scope > .card-head");
  if (head) {
    head.setAttribute("aria-expanded", String(!collapsed));
    const key = cardKey(headerTitle(head));
    if (remember) { if (collapsed) collapsedState[key] = 1; else delete collapsedState[key]; saveCollapsed(); }
  }
}

function decorateCards(root) {
  root.querySelectorAll(".card").forEach((card) => {
    const head = cardHeader(card);
    if (!head || head.dataset.toggle) return;
    head.dataset.toggle = "1";
    head.classList.add("card-head");
    head.tabIndex = 0;
    head.setAttribute("role", "button");
    head.append(h("span", { class: "chev", "aria-hidden": "true" }));
    const toggle = (e) => {
      if (e.target.closest("a, button, input, select, textarea")) return;
      setCollapsed(card, !card.classList.contains("collapsed"));
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => { if (e.target === head && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(e); } });
    if (collapsedState[cardKey(headerTitle(head))]) setCollapsed(card, true, false);
    else head.setAttribute("aria-expanded", "true");
  });
  const h1 = root.querySelector(":scope > h1");
  if (h1 && !root.querySelector(":scope > .page-tools") && root.querySelectorAll(".card > .card-head").length > 1) {
    const all = (collapsed) => root.querySelectorAll(".card > .card-head").forEach((t) => setCollapsed(t.parentElement, collapsed));
    h1.after(h("div", { class: "page-tools" },
      h("button", { class: "small ghost", type: "button", text: "▸ Collapse all", onclick: () => all(true) }),
      h("button", { class: "small ghost", type: "button", text: "▾ Expand all", onclick: () => all(false) })));
  }
}
new MutationObserver(() => decorateCards($("#main"))).observe($("#main"), { childList: true, subtree: true });

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
    updateServerCard();
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
      const address = ((await api("/api/me").catch(() => ({}))).public_url || location.origin).replace(/\/$/, "") + "/admin";
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

// ------------------------------------------------------------------ Population chart (Overview)

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c) el.append(c);
  return el;
}
const niceMax = (v) => { if (v <= 5) return 5; const p = 10 ** Math.floor(Math.log10(v)); return Math.ceil(v / p) * p; };

// Line chart: players over time, crosshair + tooltip
function lineChart(points, days) {
  const W = 720, H = 220, L = 34, R = 10, T = 12, B = 26;
  const wrap = h("div", { class: "chart" });
  if (points.length < 2) { wrap.append(h("p", { class: "muted", text: "Not enough data yet – the bot records the player count every 5 minutes." })); return wrap; }
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const step = niceMax(Math.max(...points.map((p) => p[1]), 1) / 4);
  const max = step * Math.max(1, Math.ceil(Math.max(...points.map((p) => p[1]), 1) / step));
  const x = (t) => L + (t - t0) / Math.max(t1 - t0, 1) * (W - L - R);
  const y = (v) => T + (1 - v / max) * (H - T - B);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": "Players online over time" });
  for (let v = 0; v <= max; v += step) {
    const yy = y(v);
    root.append(svg("line", { x1: L, x2: W - R, y1: yy, y2: yy, class: "grid" }));
    const t = svg("text", { x: L - 6, y: yy + 4, class: "tick", "text-anchor": "end" }); t.textContent = Math.round(v); root.append(t);
  }
  const fmtX = (ts) => { const d = new Date(ts * 1000); return days === 1 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { day: "numeric", month: "short" }); };
  for (let i = 0; i <= 4; i++) {
    const ts = t0 + (t1 - t0) * i / 4;
    const t = svg("text", { x: x(ts), y: H - 6, class: "tick", "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }); t.textContent = fmtX(ts); root.append(t);
  }
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  root.append(svg("path", { d: `${line}L${x(t1)},${y(0)}L${x(t0)},${y(0)}Z`, class: "area" }), svg("path", { d: line, class: "line" }));
  const last = points[points.length - 1];
  root.append(svg("circle", { cx: x(last[0]), cy: y(last[1]), r: 4, class: "dot" }));
  const cross = svg("line", { y1: T, y2: H - B, class: "cross", visibility: "hidden" });
  const hover = svg("circle", { r: 4, class: "dot", visibility: "hidden" });
  root.append(cross, hover);
  const tip = h("div", { class: "chart-tip hidden" });
  root.addEventListener("pointermove", (ev) => {
    const box = root.getBoundingClientRect();
    const px = (ev.clientX - box.left) / box.width * W;
    let best = points[0];
    for (const p of points) if (Math.abs(x(p[0]) - px) < Math.abs(x(best[0]) - px)) best = p;
    cross.setAttribute("x1", x(best[0])); cross.setAttribute("x2", x(best[0])); cross.setAttribute("visibility", "visible");
    hover.setAttribute("cx", x(best[0])); hover.setAttribute("cy", y(best[1])); hover.setAttribute("visibility", "visible");
    tip.replaceChildren(h("strong", { text: `${best[1]} players` }), h("span", { class: "muted", text: new Date(best[0] * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) + (days > 1 ? " (avg)" : "") }));
    tip.classList.remove("hidden");
    const left = x(best[0]) / W * box.width;
    tip.style.left = `${Math.min(Math.max(left, 70), box.width - 70)}px`;
  });
  root.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hover.setAttribute("visibility", "hidden"); tip.classList.add("hidden"); });
  wrap.append(root, tip);
  return wrap;
}

// Column chart: average players per hour of the day (local time of this browser)
function hoursChart(points) {
  const sums = Array(24).fill(0), counts = Array(24).fill(0);
  for (const [ts, v] of points) { const hr = new Date(ts * 1000).getHours(); sums[hr] += v; counts[hr]++; }
  const avg = sums.map((s, i) => counts[i] ? s / counts[i] : null);
  const wrap = h("div", { class: "chart" });
  if (avg.filter((v) => v !== null).length < 3) return wrap;
  const W = 720, H = 150, L = 34, R = 10, T = 18, B = 22;
  const max = niceMax(Math.max(...avg.map((v) => v || 0), 1));
  const slot = (W - L - R) / 24, bw = Math.min(24, slot - 2);
  const y = (v) => T + (1 - v / max) * (H - T - B);
  const peak = avg.indexOf(Math.max(...avg.map((v) => v ?? -1)));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": "Average players per hour of the day" });
  root.append(svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), class: "grid" }));
  const tip = h("div", { class: "chart-tip hidden" });
  avg.forEach((v, hr) => {
    const cx = L + slot * hr + slot / 2;
    if (hr % 3 === 0) { const t = svg("text", { x: cx, y: H - 6, class: "tick", "text-anchor": "middle" }); t.textContent = `${String(hr).padStart(2, "0")}:00`; root.append(t); }
    if (v === null) return;
    const top = y(v), x0 = cx - bw / 2, base = y(0), r = Math.min(4, (base - top) / 2);
    const bar = svg("path", { class: "bar" + (hr === peak ? " peak" : ""), tabindex: 0,
      d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + bw - r}Q${x0 + bw},${top} ${x0 + bw},${top + r}V${base}Z` });
    const show = () => {
      tip.replaceChildren(h("strong", { text: `${v.toFixed(1)} players` }), h("span", { class: "muted", text: `on average ${String(hr).padStart(2, "0")}:00–${String((hr + 1) % 24).padStart(2, "0")}:00` }));
      tip.classList.remove("hidden"); tip.style.left = `${cx / W * 100}%`;
    };
    bar.addEventListener("pointerenter", show); bar.addEventListener("focus", show);
    bar.addEventListener("pointerleave", () => tip.classList.add("hidden")); bar.addEventListener("blur", () => tip.classList.add("hidden"));
    root.append(svg("rect", { x: cx - slot / 2, y: T, width: slot, height: H - T - B, fill: "transparent" }), bar);
    if (hr === peak) { const t = svg("text", { x: cx, y: top - 5, class: "tick strong", "text-anchor": "middle" }); t.textContent = v.toFixed(0); root.append(t); }
  });
  root.querySelectorAll("rect").forEach((hit, i) => { const bars = root.querySelectorAll("path.bar"); hit.addEventListener("pointerenter", () => bars[i]?.dispatchEvent(new Event("pointerenter"))); hit.addEventListener("pointerleave", () => tip.classList.add("hidden")); });
  const table = h("details", { class: "chart-table" }, h("summary", { text: "Show as table" }),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Hour" }), h("th", { text: "Average players" }))),
      h("tbody", {}, avg.map((v, hr) => v === null ? null : h("tr", {}, h("td", { text: `${String(hr).padStart(2, "0")}:00` }), h("td", { text: v.toFixed(1) })))))));
  wrap.append(root, tip, table);
  return wrap;
}

function populationCard() {
  const card = h("div", { class: "card" });
  const body = h("div", {}, h("p", { class: "muted", text: "Loading …" }));
  let days = 1;
  const buttons = [[1, "24 h"], [7, "7 days"], [30, "30 days"]].map(([d, label]) => {
    const b = h("button", { class: "small" + (d === 1 ? " active" : ""), text: label });
    b.addEventListener("click", () => { days = d; buttons.forEach((x) => x.classList.toggle("active", x === b)); load(); });
    return b;
  });
  card.append(h("div", { class: "row", style: "justify-content:space-between;margin-bottom:.4rem" },
    h("h2", { text: "📈 Players online", style: "margin:0" }), h("div", { class: "seg" }, buttons)), body);
  const load = async () => {
    body.style.opacity = ".55";
    const r = await api(`/api/population?days=${days}`).catch(() => null);
    body.style.opacity = "";
    if (!r) return;
    const vals = r.points.map((p) => p[1]);
    const peakIdx = vals.indexOf(Math.max(...vals));
    const summary = vals.length ? h("div", { class: "row muted", style: "font-size:.88rem;gap:1.2rem;margin-bottom:.3rem" },
      h("span", {}, "Now ", h("strong", { text: String(r.online_now) })),
      h("span", {}, "Peak ", h("strong", { text: String(Math.round(vals[peakIdx])) }), ` (${new Date(r.points[peakIdx][0] * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })})`),
      h("span", {}, "Average ", h("strong", { text: (vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1) }))) : null;
    body.replaceChildren(...[summary, lineChart(r.points, days),
      days > 1 ? h("h3", { style: "margin:1rem 0 .3rem", text: "Busiest hours (your local time)" }) : null,
      days > 1 ? hoursChart(r.points) : null].filter(Boolean));
  };
  load();
  return card;
}

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
      const root = url.replace(/\/$/, ""), panel = root + "/admin";
      const copy = (text) => h("button", { class: "small", text: "📋 Copy", onclick: async () => {
        try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { prompt("Copy the address:", text); }
      } });
      publicCard.replaceChildren(h("h2", { text: "🌍 Reachable from anywhere" }),
        h("div", { class: "row" }, h("span", { class: "muted", text: "Control panel:" }),
          h("a", { href: panel, target: "_blank", rel: "noopener" }, h("code", { text: panel })), copy(panel)),
        h("div", { class: "row", style: "margin-top:.35rem" }, h("span", { class: "muted", text: "Player website:" }),
          h("a", { href: root + "/", target: "_blank", rel: "noopener" }, h("code", { text: root + "/" })), copy(root + "/")),
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
    populationCard(),
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

// Item picture (downloaded and cached by the bot); shows the emoji if there is none
function itemPic(short, emoji, cls = "pic") {
  const fallback = h("span", { class: cls + " emoji", text: emoji || "📦" });
  if (!short || meta?.item_images === false) return fallback;
  const img = h("img", { class: cls, src: `/img/items/${encodeURIComponent(short)}.png?s=96`, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => img.replaceWith(fallback), { once: true });
  return img;
}

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
        itemPic(i.short, i.icon, "kb-item-icon"), h("span", {}, i.label, h("small", { text: i.short })));
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
      s ? [itemPic(s.item, icon(s.item), "icon"), h("span", { class: "name", text: label(s.item) }),
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
      itemPic(s.item, icon(s.item), "kb-editor-pic"), h("strong", { text: label(s.item) }), h("code", { text: s.item }),
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
            h("td", {}, h("span", { class: "item-cell" }, itemPic(i.short_name, "📦", "item-pic"), i.short_name)), h("td", { text: "× " + i.amount }), h("td", { class: "muted", text: i.container }),
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
    roles.length > 12 ? h("input", { type: "search", placeholder: "🔍 Filter roles…", style: "margin-bottom:.4rem", oninput: (e) => {
      const q = e.target.value.trim().toLowerCase();
      for (const [cb, r] of boxes) cb.parentElement.hidden = !!q && !r.name.toLowerCase().includes(q) && !cb.checked;
    } }) : null,
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

// Discord accounts linked to in-game players – needed for /customkit claim
function linkedPlayersCard(data, reload) {
  const rows = data.linked || [];
  return h("div", { class: "card" }, h("h2", { text: `🔗 Linked players (${rows.length})` }),
    h("p", { class: "muted", text: "Members link themselves with /customkit link (dropdown of online players; optional quick chat check in Settings → Kits). Custom kit claims only go to the linked player." }),
    adminLinkForm(reload),
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

// Admins link a member to a player directly – the player doesn't have to be online
function adminLinkForm(reload) {
  const member = h("input", { placeholder: "Discord user ID or name", maxlength: 64 });
  const player = h("input", { placeholder: "In-game name (online or not)", maxlength: 64 });
  const force = h("input", { type: "checkbox" });
  return h("details", { style: "margin:.5rem 0 .75rem" }, h("summary", { text: "➕ Link a member to a player (admin)" }),
    h("div", { class: "row", style: "margin-top:.5rem;flex-wrap:wrap" }, member, player,
      h("label", { style: "display:flex;gap:.35rem;align-items:center" }, force, "move it if linked to someone else"),
      h("button", { class: "primary small", text: "Link", onclick: async () => {
        const r = await act(() => api("/api/linked-players", { method: "POST", body: { member: member.value, player: player.value, force: force.checked } }), "Linked");
        if (!r) return;
        if (!r.seen) toast(`${r.player} hasn't been seen on the server yet – check the spelling.`, true);
        reload();
      } })),
    h("p", { class: "muted", text: "Works while the player is offline. Use the exact in-game name; known names are matched without caring about upper/lower case." }));
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

const CK_OPEN = "customkits-open";
let ckFilter = "";   // search text survives the re-render after an edit

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

  // Each kit is collapsible (closed by default); open kits stay open across reloads and visits
  const openKits = new Set(kbStore.get(CK_OPEN, []));
  const saveOpen = () => kbStore.set(CK_OPEN, [...openKits].filter((n) => data.custom.some((k) => k.name === n)));
  const kitEls = [];
  const search = h("input", { type: "search", placeholder: "🔍 Search kits or items…", value: ckFilter, style: "flex:1;min-width:180px" });
  const shown = h("span", { class: "muted" });
  const applyFilter = () => {
    ckFilter = search.value;
    const q = search.value.trim().toLowerCase();
    let n = 0;
    for (const [el, kit] of kitEls) {
      const hit = !q || kit.name.toLowerCase().includes(q) || (kit.description || "").toLowerCase().includes(q)
        || kit.items.some((i) => i.shortname.toLowerCase().includes(q));
      el.hidden = !hit;
      n += hit ? 1 : 0;
    }
    shown.textContent = q ? `${n} of ${kitEls.length} kits` : `${kitEls.length} kits`;
  };
  const setAll = (open) => { for (const [el] of kitEls) if (!el.hidden) el.open = open; };
  if (data.custom.length) {
    card.append(h("div", { class: "row ck-toolbar" }, search, shown,
      h("button", { class: "small", type: "button", text: "Expand all", onclick: () => setAll(true) }),
      h("button", { class: "small", type: "button", text: "Collapse all", onclick: () => setAll(false) })));
    search.addEventListener("input", applyFilter);
  }

  for (const kit of data.custom) {
    const item = h("input", { placeholder: "Item shortname", list: "dl-items" });
    const amount = h("input", { type: "number", min: 1, value: 1, style: "max-width:90px" });
    const roles = (kit.claim_role_ids || "").split(",").filter(Boolean).length;
    const el = h("details", { class: "card ck-kit" },
      h("summary", {},
        h("strong", { text: kit.name }),
        h("span", { class: "pill", text: `${kit.items.length} item${kit.items.length === 1 ? "" : "s"}` }),
        roles ? h("span", { class: "pill ok", text: `🙋 ${roles} role${roles === 1 ? "" : "s"} · ${minutesToDuration(kit.cooldown_minutes) || "no cooldown"}` }) : null,
        kit.description ? h("span", { class: "muted ck-desc", text: kit.description }) : null),
      h("table", {}, h("tbody", {}, kit.items.map((i) => h("tr", {},
        h("td", {}, h("span", { class: "item-cell" }, itemPic(i.shortname, "📦", "item-pic"), i.shortname)), h("td", { text: "× " + i.amount }),
        h("td", {}, h("button", { class: "small", text: "✕", title: "Remove", onclick: async () => {
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items/${i.id}`, { method: "DELETE" }));
          reload();
        } })))))),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        const r = await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}/items`, { method: "POST", body: { item: item.value, amount: Number(amount.value) } }));
        if (r && !r.known_item) toast("Unknown shortname – please test it on yourself once.");
        reload();
      } }, item, amount, h("button", { text: "+ Item" }),
        h("button", { class: "small danger", type: "button", style: "margin-left:auto", text: "Delete kit", onclick: async () => {
          if (!confirm(`Delete custom kit ${kit.name} along with its autokits?`)) return;
          await act(() => api(`/api/custom-kits/${encodeURIComponent(kit.name)}`, { method: "DELETE" }), "Deleted");
          reload();
        } })),
      claimSettings(kit, opts || { available: false }, reload));
    el.open = openKits.has(kit.name);
    el.addEventListener("toggle", () => { if (el.open) openKits.add(kit.name); else openKits.delete(kit.name); saveOpen(); });
    kitEls.push([el, kit]);
    card.append(el);
  }
  applyFilter();
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
  const tCat = h("select", {}, [["kills", "Kills"], ["kd", "K/D"], ["playtime", "Playtime"], ["members", "Members"]].map(([v, t]) => h("option", { value: v, text: t })));
  const tBody = h("tbody");
  main.append(h("h1", { text: "Stats" }), h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🏆 Leaderboard", style: "margin:0" }), cat),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Player", "Kills", "Deaths", "K/D", "Playtime", "Platform", "Last seen (UTC)"].map((t) => h("th", { text: t })))),
      body))),
    h("div", { class: "card" },
      h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "👥 Teams", style: "margin:0" }), tCat),
      h("p", { class: "muted", style: "margin-top:0", text: "Teams with at least 2 members, named after their leader – learned from the server log since the bot runs. Stats of the current members summed up." }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Team", "Members", "Kills", "Deaths", "K/D", "Playtime"].map((t) => h("th", { text: t })))),
        tBody))));
  const loadTeams = async () => {
    const r = await api(`/api/leaderboard?teams=1&category=${tCat.value}`).catch(() => ({ rows: [] }));
    tBody.replaceChildren(...(r.rows.length ? r.rows.map((t, i) => h("tr", {},
      h("td", { text: i + 1 }), h("td", {}, h("strong", { text: `Team ${t.leader || t.team_id}` }), h("div", { class: "muted", style: "font-size:.82rem", text: t.names })),
      h("td", { text: t.members }), h("td", { text: t.kills }), h("td", { text: t.deaths }),
      h("td", { text: (t.kills / Math.max(t.deaths, 1)).toFixed(2) }), h("td", { text: fmtDuration(t.playtime) })))
      : [h("tr", {}, h("td", { colspan: 7, class: "muted", text: "No teams known yet." }))]));
  };
  tCat.addEventListener("change", loadTeams);
  loadTeams();
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
        h("span", { class: "muted", text: "Don't put anything secret here – the AI may pass on anything in it to players." })),
      h("p", { class: "muted", style: "margin:.6rem 0 0", text: "📚 The AI adds general facts from staff answers in closed tickets at the end, under \"Learned from tickets\" (switch: ⚙️ Settings → Tickets & AI). Check them now and then – edit or delete anything here." })));
  const r = await act(() => api("/api/knowledge"));
  area.value = r.text;
  info.textContent = `File: ${r.path}`;
};

// ------------------------------------------------------------------ Shop (points)

views.shop = async (main) => {
  main.append(h("h1", { text: "Shop" }));
  let d, m, kits;
  try { [d, m, kits] = await Promise.all([api("/api/shop"), loadMeta(), api("/api/kits").catch(() => ({ ingame: [] }))]); }
  catch (e) { main.append(h("p", { class: "error", text: e.message })); return; }
  const pts = d.points_name;
  const e = d.earning;
  if (!d.enabled) main.append(h("div", { class: "banner warn", text: "Points & shop are switched off (⚙️ Settings → 🪙 Points & shop)." }));
  main.append(h("div", { class: "card" }, h("h2", { text: `🪙 How members earn ${pts}` }),
    h("p", { class: "muted", style: "margin-top:0", text: "Only members who linked their in-game player with /customkit link. They see their balance with /points balance or /profile and buy with /shop buy." }),
    h("div", { class: "grid", style: "margin:0" },
      ...[["⏱️", "Per hour online", e.per_hour], ["⚔️", "Per PvP kill", e.per_kill], ["📅", "/daily", `${e.daily} (+${e.streak_bonus}/day streak)`],
        ["🎁", "Playtime reward", e.reward_hours ? `every ${e.reward_hours}h: ${[e.reward_kit, e.reward_points ? `${e.reward_points} ${pts}` : ""].filter(Boolean).join(" + ") || "–"}` : "off"]]
        .map(([icon, label, value]) => h("div", { class: "stat" }, h("div", { class: "label" }, icon, " ", label), h("div", { class: "value", style: "font-size:1.05rem", text: String(value) })))),
    isOwner() ? h("p", { class: "muted", style: "margin:.75rem 0 0" }, "Change these in ", h("a", { href: "#settings", text: "⚙️ Settings → 🪙 Points & shop" }), ".") : null));

  // Items
  const kindSel = h("select", {}, Object.entries(d.kinds).map(([k, label]) => h("option", { value: k, text: label })));
  const targetBox = h("span", { style: "display:contents" });
  const amount = h("input", { type: "number", min: 0, value: 1, style: "max-width:110px", title: "Amount" });
  const amountLabel = h("span", { class: "muted", style: "font-size:.85rem" });
  const name = h("input", { placeholder: "Name in the shop, e.g. VIP 7 days", maxlength: 60 });
  const price = h("input", { type: "number", min: 0, placeholder: `Price (${pts})`, style: "max-width:150px" });
  const desc = h("input", { placeholder: "Description (optional)", maxlength: 300 });
  let target = null;
  const itemsList = h("datalist", { id: "dl-shop-items" }, m.items.map((i) => h("option", { value: i.short, label: i.label })));
  const renderTarget = () => {
    const k = kindSel.value;
    const opts = (list) => h("select", {}, list.length ? list.map(([v, t]) => h("option", { value: v, text: t })) : [h("option", { value: "", text: "– none yet –" })]);
    if (k === "custom_kit") target = opts(d.custom_kits.map((n) => [n, n]));
    else if (k === "kit") target = opts((kits.ingame || []).map((n) => [n, n]));
    else if (k === "role") target = opts(d.roles.map((r) => [r.id, r.name]));
    else target = h("input", { placeholder: "Item shortname, e.g. rifle.ak", list: "dl-shop-items" });
    targetBox.replaceChildren(target);
    amount.style.display = amountLabel.style.display = ["item", "role"].includes(k) ? "" : "none";
    amountLabel.textContent = k === "role" ? "days (0 = permanent)" : "pieces";
    if (k === "role") amount.value = 7; else amount.value = 1;
  };
  kindSel.addEventListener("change", renderTarget);
  renderTarget();
  const iBody = h("tbody");
  const renderItems = () => iBody.replaceChildren(...(d.items.length ? d.items.map((it) => h("tr", {},
    h("td", {}, h("strong", { text: it.name }), it.description ? h("div", { class: "muted", style: "font-size:.85rem", text: it.description }) : null),
    h("td", { text: it.summary }),
    h("td", { text: `${it.price} ${pts}` }),
    h("td", {}, h("button", { class: "small", text: it.enabled ? "✅ on sale" : "⏸️ hidden", onclick: async () => {
      await act(() => api(`/api/shop/items/${it.id}`, { method: "PUT", body: { enabled: !it.enabled } }));
      it.enabled = !it.enabled; renderItems();
    } })),
    h("td", {}, h("button", { class: "small", text: "Price", onclick: async () => {
      const p = prompt(`New price for ${it.name} (${pts}):`, it.price);
      if (p === null || p === "") return;
      await act(() => api(`/api/shop/items/${it.id}`, { method: "PUT", body: { ...it, price: Number(p) } }), "Price changed");
      reload();
    } }), " ", h("button", { class: "small danger", text: "Delete", onclick: async () => {
      if (!confirm(`Delete ${it.name} from the shop?`)) return;
      await act(() => api(`/api/shop/items/${it.id}`, { method: "DELETE" }), "Deleted"); reload();
    } }))))
    : [h("tr", {}, h("td", { colspan: 5, class: "muted", text: "No items yet – add the first one below." }))]));
  renderItems();
  main.append(h("div", { class: "card" }, h("h2", { text: "🛒 Items" }),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Item", "Gives", "Price", "Status", ""].map((t) => h("th", { text: t })))), iBody)),
    h("h3", { style: "margin-top:1.1rem", text: "Add an item" }), itemsList,
    h("form", { class: "row", onsubmit: async (ev) => {
      ev.preventDefault();
      await act(() => api("/api/shop/items", { method: "POST", body: { name: name.value, kind: kindSel.value, target: target.value, amount: Number(amount.value || 0), price: Number(price.value || 0), description: desc.value } }), "Item added");
      name.value = desc.value = price.value = ""; reload();
    } }, name, kindSel, targetBox, amount, amountLabel, price, desc, h("button", { class: "primary", text: "Add" }))));

  // Balances
  const wBody = h("tbody", {}, d.wallets.length ? d.wallets.map((w) => h("tr", {},
    h("td", { text: w.user }), h("td", { class: "muted", text: w.player || "–" }),
    h("td", {}, h("strong", { text: String(w.points) })), h("td", { text: String(w.earned_total) }),
    h("td", { text: w.daily_streak ? `🔥 ${w.daily_streak}` : "–" }),
    h("td", {}, h("button", { class: "small", text: "± Points", onclick: async () => {
      const v = prompt(`Add (e.g. 100) or remove (e.g. -50) ${pts} for ${w.user}:`);
      if (!v) return;
      const reason = prompt("Reason (shown in their history):", "") || "";
      await act(() => api("/api/points", { method: "POST", body: { user_id: w.user_id, delta: Number(v), reason } }), "Saved"); reload();
    } }))))
    : [h("tr", {}, h("td", { colspan: 6, class: "muted", text: "Nobody has points yet." }))]);
  main.append(h("div", { class: "card" }, h("h2", { text: `🏦 Balances (top ${d.wallets.length || 50})` }),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Member", "Player", pts, "Earned total", "Streak", ""].map((t) => h("th", { text: t })))), wBody)),
    h("p", { class: "muted", style: "margin:.75rem 0 0", text: "In Discord: /points give, /points take, /points history." })));

  main.append(storeCard(), priceListCard());

  function reload() { route(); }
};

// ------------------------------------------------------------------ Tip4Serv purchases + custom kit price list

function storeCard() {
  const card = h("div", { class: "card" }, h("h2", { text: "💳 Tip4Serv purchases" }), h("p", { class: "muted", text: "Loading …" }));
  api("/api/store").then((d) => {
    const st = d.status || {};
    const pill = st.webhook_last ? h("span", { class: "pill ok", text: "webhook connected" }) : !st.enabled ? h("span", { class: "pill", text: "off" })
      : st.error ? h("span", { class: "pill bad", text: "error" }) : st.last_ok && st.problem ? h("span", { class: "pill warn", text: "no purchases yet" })
      : st.last_ok ? h("span", { class: "pill ok", text: "connected" })
      : h("span", { class: "pill warn", text: "connecting …" });
    const money = (v, c) => v === null || v === undefined ? "?" : `${Number(v).toFixed(2)} ${c || ""}`.trim();
    const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "–";
    const hook = st.webhook_url !== undefined ? h("div", { class: "card", style: "margin:.4rem 0 1rem;box-shadow:none;background:var(--panel-2)" },
      h("strong", { text: "🔗 Webhook for live payments (recommended)" }),
      h("p", { class: "muted", style: "margin:.3rem 0 .5rem", text: "tip4serv.com → Dashboard → Create webhook URL for live payments → paste this URL, tick \"Payment successful\" (and \"Payment refunded\" / \"Payment refused\"). Payments then arrive instantly – no product command needed. Keep the URL secret." }),
      st.webhook_url ? h("div", { class: "row" }, h("input", { value: st.webhook_url, readonly: true, style: "flex:1;font-family:monospace;font-size:.8rem" }),
        h("button", { class: "small", type: "button", text: "Copy", onclick: () => { navigator.clipboard?.writeText(st.webhook_url).then(() => toast("Copied")); } }))
        : h("p", { class: "error", text: "The web interface has no public address right now (Settings → Web interface → tunnel), so Tip4Serv can't reach the bot." }),
      st.webhook_url && !st.webhook_fixed ? h("p", { class: "muted", style: "margin:.4rem 0 0", text: "⚠️ This is a quick-tunnel address: it changes when the bot window is closed – then update the webhook on Tip4Serv, or set up a fixed address (WEB_TUNNEL_TOKEN + WEB_PUBLIC_URL)." }) : null) : null;
    card.replaceChildren(...[h("h2", {}, "💳 Tip4Serv purchases ", pill), hook,
      !st.enabled && !st.webhook_last ? h("p", { class: "muted", style: "margin-top:0" }, "Or with a server key: add a server for the bot on tip4serv.com, paste its key in ",
        h("a", { href: "#settings", text: "⚙️ Settings → 💳 Tip4Serv store" }), " and give your custom kit product the command ",
        h("code", { text: "rcebot {discord_id} {total_paid} {currency} {PlayerName}" }), " on that server.") : null,
      st.enabled && st.problem ? h("p", { class: "error", text: "⚠️ " + st.problem }) : null,
      st.enabled && st.last_ok ? h("p", { class: "muted", style: "margin-top:0", text: `Last check ${fmt(st.last_ok)} · checked every minute.` }) : null,
      d.pending.length ? h("div", {}, h("h3", { text: "Open kit requests" }), h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Kit", "Type", "Price", "Payment"].map((t) => h("th", { text: t })))),
        h("tbody", {}, d.pending.map((r) => h("tr", {}, h("td", { text: "#" + r.id }), h("td", { text: r.kit }),
          h("td", { text: r.source === "giveaway" ? "🎉 giveaway" : "🛒 purchase" }),
          h("td", { text: r.price !== null ? money(r.price, r.currency) : "–" }), h("td", { class: "muted", text: r.note || "–" }))))))) : null,
      h("h3", { text: "Payments" }),
      d.payments.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Received", "Amount", "Buyer", "Payment", "Used by"].map((t) => h("th", { text: t })))),
        h("tbody", {}, d.payments.map((p) => h("tr", {}, h("td", { text: fmt(p.received_at) }), h("td", { text: money(p.amount, p.currency) }),
          h("td", { text: [p.discord_id ? "Discord " + p.discord_id : "", p.player || p.username || p.customer || ""].filter(Boolean).join(" · ") || "?" }),
          h("td", { class: "muted", text: p.payment_id }),
          h("td", { text: p.used_by ? `request #${p.used_by}` : "unused" })))))) : h("p", { class: "muted", text: "No payments yet." })]
      .filter(Boolean));
  }).catch((e) => card.replaceChildren(h("h2", { text: "💳 Tip4Serv purchases" }), h("p", { class: "error", text: e.message })));
  return card;
}

function priceListCard() {
  const card = h("div", { class: "card" }, h("h2", { text: "🧾 Custom kit price list" }), h("p", { class: "muted", text: "Loading …" }));
  api("/api/prices").then((d) => {
    const area = h("textarea", { rows: 22, spellcheck: "false", style: "width:100%;font-family:var(--mono, monospace);font-size:.85rem" });
    area.value = d.text;
    area.readOnly = !d.can_edit;
    const info = h("p", { class: "muted", style: "margin:.5rem 0 0" });
    const showInfo = (r) => {
      const warn = [...(r.warnings || []), ...(r.unknown_items || []).map((i) => `unknown item: ${i}`)];
      info.className = warn.length ? "error" : "muted";
      info.textContent = warn.length ? warn.join(" · ") : `${r.rules} prices · currency ${r.currency || d.currency} · file ${d.path}`;
    };
    showInfo(d);
    card.replaceChildren(h("h2", { text: "🧾 Custom kit price list" }),
      h("p", { class: "muted", style: "margin-top:0", text: "The ticket AI tells players the price of their custom kit with this list, and new custom kits are approved automatically when the Tip4Serv payment covers the price and every item has a price. Format: see the comments at the top." }),
      area, info,
      d.can_edit ? h("div", { class: "row", style: "margin-top:.6rem" }, h("button", { class: "primary", text: "Save", onclick: async () => {
        const r = await act(() => api("/api/prices", { method: "PUT", body: { text: area.value } }), "Price list saved – applies right away");
        if (r) showInfo(r);
      } })) : h("p", { class: "muted", text: "Only the owner can change the price list." }));
  }).catch((e) => card.replaceChildren(h("h2", { text: "🧾 Custom kit price list" }), h("p", { class: "error", text: e.message })));
  return card;
}

// ------------------------------------------------------------------ Community: wipe + scheduled messages

function wipeCard() {
  const card = h("div", { class: "card" }, h("h2", { text: "🧹 Wipe" }), h("p", { class: "muted", text: "Loading …" }));
  const load = async () => {
    const w = await api("/api/wipe").catch((e) => ({ error: e.message }));
    if (w.error) { card.replaceChildren(h("h2", { text: "🧹 Wipe" }), h("p", { class: "error", text: w.error })); return; }
    const when = h("input", { type: "datetime-local", value: w.local || "" });
    const repeat = h("select", {}, Object.entries(w.repeats).map(([k, t]) => h("option", { value: k, text: t, selected: k === w.repeat })));
    const resets = Object.entries(w.resets).filter(([, on]) => on).map(([k]) => ({ kits: "kit cooldowns", stats: "leaderboard", points: "points" }[k]));
    card.replaceChildren(h("h2", { text: "🧹 Wipe" }),
      w.next ? h("p", { style: "margin-top:0" }, "Next wipe: ", h("strong", { text: new Date(w.next * 1000).toLocaleString() }),
        h("span", { class: "muted", text: ` (in ${fmtDuration(w.next - Date.now() / 1000)}, ${w.repeat_text})` }))
        : h("p", { class: "muted", style: "margin-top:0", text: "No wipe planned." }),
      h("p", { class: "muted", style: "font-size:.85rem", text: `Countdown in the status message and /rust, posts 24 h and 1 h before, in-game 1 h and 10 min before. At the wipe: ${resets.join(", ") || "nothing is reset"} (change in ⚙️ Settings → 🧹 Wipe). Time zone: ${w.timezone}.` }),
      h("form", { class: "row", onsubmit: async (ev) => {
        ev.preventDefault();
        await act(() => api("/api/wipe", { method: "PUT", body: { when: when.value, repeat: repeat.value } }), "Wipe planned"); load();
      } }, when, repeat, h("button", { class: "primary", text: "Save" }),
        w.next ? h("button", { type: "button", class: "ghost", text: "Clear", onclick: async () => { await act(() => api("/api/wipe", { method: "DELETE" }), "Cleared"); load(); } }) : null,
        h("button", { type: "button", class: "small", text: "🧹 Wipe done now", title: "Runs the resets and posts \"Server wiped!\"", onclick: async () => {
          if (!confirm("Mark the wipe as done now? This runs the resets and posts the announcement.")) return;
          await act(() => api("/api/wipe/done", { method: "POST" }), "Wipe done"); load();
        } })));
  };
  load();
  return card;
}

function scheduleCard() {
  const card = h("div", { class: "card" }, h("h2", { text: "🔁 Scheduled messages" }));
  const body = h("tbody");
  const text = h("input", { placeholder: "Message, e.g. Join our Discord: discord.gg/… (in-game rich text allowed)", maxlength: 400 });
  const minutes = h("input", { type: "number", min: 5, value: 30, style: "max-width:100px", title: "Every … minutes" });
  const target = h("select");
  const channel = h("select");
  const syncChannel = () => { channel.style.display = target.value === "game" ? "none" : ""; };
  target.addEventListener("change", syncChannel);
  card.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Message", "Every", "Where", "Last sent", ""].map((t) => h("th", { text: t })))), body)),
    h("form", { class: "row", style: "margin-top:.8rem", onsubmit: async (ev) => {
      ev.preventDefault();
      await act(() => api("/api/schedules", { method: "POST", body: { text: text.value, minutes: Number(minutes.value), target: target.value, channel_id: channel.value } }), "Message scheduled");
      text.value = ""; load();
    } }, text, minutes, h("span", { class: "muted", text: "min" }), target, channel, h("button", { class: "primary", text: "Add" })));
  const load = async () => {
    const r = await api("/api/schedules").catch(() => null);
    if (!r) return;
    if (!target.options.length) {
      target.replaceChildren(...Object.entries(r.targets).map(([k, t]) => h("option", { value: k, text: t })));
      channel.replaceChildren(...(r.channels.length ? r.channels.map((c) => h("option", { value: c.id, text: "#" + c.name })) : [h("option", { value: "", text: "(Discord not connected)" })]));
      syncChannel();
    }
    body.replaceChildren(...(r.rows.length ? r.rows.map((m) => h("tr", {},
      h("td", { text: m.text }), h("td", { text: fmtDuration(m.interval_minutes * 60) }),
      h("td", { text: r.targets[m.target] + (m.channel ? ` ${m.channel}` : "") }),
      h("td", { class: "muted", text: m.last_sent ? new Date(m.last_sent * 1000).toLocaleTimeString() : "–" }),
      h("td", {}, h("button", { class: "small", text: m.enabled ? "⏸️ Pause" : "▶️ Resume", onclick: async () => { await act(() => api(`/api/schedules/${m.id}`, { method: "PUT", body: { enabled: !m.enabled } })); load(); } }), " ",
        h("button", { class: "small danger", text: "Delete", onclick: async () => { if (!confirm("Delete this message?")) return; await act(() => api(`/api/schedules/${m.id}`, { method: "DELETE" }), "Deleted"); load(); } }))))
      : [h("tr", {}, h("td", { colspan: 5, class: "muted", text: "No scheduled messages yet." }))]));
  };
  load();
  return card;
}

// ------------------------------------------------------------------ Reports & watchlist

views.reports = async (main) => {
  const rBody = h("tbody"), wBody = h("tbody");
  const filter = h("select", {}, h("option", { value: "open", text: "Open" }), h("option", { value: "", text: "All" }));
  const wPlayer = h("input", { placeholder: "In-game name (exact)", maxlength: 64 });
  const wReason = h("input", { placeholder: "Why (e.g. ban evader)", maxlength: 300 });
  main.append(h("h1", { text: "Reports" }),
    h("div", { class: "card" }, h("h2", { text: "🚨 Admin abuse (rockets/C4)" }), abuseBox()),
    h("div", { class: "card" }, h("div", { class: "row", style: "margin-bottom:.5rem" }, h("h2", { text: "🚩 Player reports", style: "margin:0" }), filter),
      h("p", { class: "muted", style: "margin-top:0", text: "Members report players with /report in Discord. Staff closes them here or with the ✅ button in Discord." }),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Player", "Reason", "By", "When (UTC)", ""].map((t) => h("th", { text: t })))), rBody))),
    h("div", { class: "card" }, h("h2", { text: "👀 Watchlist" }),
      h("p", { class: "muted", style: "margin-top:0", text: "Staff gets pinged when one of these players joins the server (at most every 30 minutes)." }),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Player", "Why", "Added by", ""].map((t) => h("th", { text: t })))), wBody)),
      h("form", { class: "row", style: "margin-top:.8rem", onsubmit: async (ev) => {
        ev.preventDefault();
        await act(() => api("/api/watchlist", { method: "POST", body: { player: wPlayer.value, reason: wReason.value } }), "Added to the watchlist");
        wPlayer.value = wReason.value = ""; load();
      } }, wPlayer, wReason, h("button", { class: "primary", text: "Watch" }))));
  const load = async () => {
    const r = await api(`/api/reports?status=${filter.value}`);
    rBody.replaceChildren(...(r.reports.length ? r.reports.map((x) => h("tr", {},
      h("td", { text: x.id }), h("td", {}, h("strong", { text: x.player }), x.online ? h("span", { class: "pill ok", style: "margin-left:.4rem", text: "online" }) : null),
      h("td", { text: x.reason }), h("td", { class: "muted", text: x.reporter }), h("td", { class: "muted", text: x.created_at }),
      h("td", {}, x.status === "open"
        ? h("button", { class: "small", text: "✅ Handled", onclick: async () => { await act(() => api(`/api/reports/${x.id}/handled`, { method: "POST" }), "Report closed"); load(); } })
        : h("span", { class: "muted", text: `handled by ${x.handled_by}` }))))
      : [h("tr", {}, h("td", { colspan: 6, class: "muted", text: filter.value ? "No open reports. 🎉" : "No reports yet." }))]));
    wBody.replaceChildren(...(r.watchlist.length ? r.watchlist.map((w) => h("tr", {},
      h("td", {}, h("strong", { text: w.player }), w.online ? h("span", { class: "pill warn", style: "margin-left:.4rem", text: "online now" }) : null),
      h("td", { text: w.reason || "–" }), h("td", { class: "muted", text: w.added_by || "–" }),
      h("td", {}, h("button", { class: "small danger", text: "Remove", onclick: async () => { await act(() => api(`/api/watchlist/${encodeURIComponent(w.player)}`, { method: "DELETE" }), "Removed"); load(); } }))))
      : [h("tr", {}, h("td", { colspan: 4, class: "muted", text: "Nobody on the watchlist." }))]));
  };
  filter.addEventListener("change", load);
  every(20000, () => load().catch(() => {}));
};

// ------------------------------------------------------------------ Community

views.community = async (main) => {
  const info = h("div", { class: "card" }, h("p", { class: "muted", text: "Loading …" }));
  const gBody = h("tbody");
  const tBody = h("tbody");
  const rulesArea = h("textarea", { rows: 12 });
  main.append(h("h1", { text: "Community" }), info, wipeCard(), scheduleCard(),
    h("div", { class: "card" }, h("h2", { text: "⏳ Timed roles" }),
      h("p", { class: "muted", text: "Roles that end by themselves – give one with /temprole give in Discord (e.g. VIP for 7d). Giveaway prizes like \"VIP 7 days\" land here too." }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Member", "Role", "Ends", "Reason", ""].map((t) => h("th", { text: t })))),
        tBody))),
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
  const loadTimed = async () => {
    const t = await api("/api/timed-roles");
    tBody.replaceChildren(...(t.rows.length ? t.rows.map((r) => h("tr", {},
      h("td", { text: r.user }), h("td", {}, h("span", { class: "pill", text: r.role })),
      h("td", { text: `${new Date(r.expires_at * 1000).toLocaleString()} (in ${fmtDuration(r.expires_at - Date.now() / 1000)})` }),
      h("td", { class: "muted", text: r.reason || "–" }),
      h("td", {}, h("button", { class: "small danger", text: "Remove now", onclick: async () => {
        if (!confirm(`Remove ${r.role} from ${r.user} now?`)) return;
        await act(() => api(`/api/timed-roles/${r.id}`, { method: "DELETE" }), "Role removed"); loadTimed();
      } }))))
      : [h("tr", {}, h("td", { colspan: 5, class: "muted", text: "No timed roles running." }))]));
  };
  every(15000, () => { load().catch(() => {}); loadTimed().catch(() => {}); });
  const r = await api("/api/rules").catch(() => ({ text: "" }));
  rulesArea.value = r.text;
};

// ------------------------------------------------------------------ Settings

// "Use your own domain": tunnel + DNS record are created through the Cloudflare API (token is not stored)
function ownDomainBox(rows) {
  // the settings form's own fields – kept in sync so a later "Save" doesn't write the old values back
  const field = (key) => rows[key] && rows[key].row.querySelector("input, select, textarea");
  const current = (field("WEB_PUBLIC_URL") && field("WEB_PUBLIC_URL").value) || "";
  const host = h("input", { placeholder: "panel.yourdomain.com", maxlength: 253, autocomplete: "off",
    value: current.replace(/^https?:\/\//, "") });
  const token = h("input", { type: "password", placeholder: "Cloudflare API token", autocomplete: "off" });
  const out = h("div");
  const btn = h("button", { class: "primary", type: "button", text: "🌐 Connect my domain" });
  const checkBtn = h("button", { type: "button", text: "Check the address" });
  const check = async (tries = 1) => {
    checkBtn.disabled = true;
    for (let i = 0; i < tries; i++) {
      try {
        const r = await api("/api/settings/cloudflare-domain/check", { method: "POST", body: {} });
        out.replaceChildren(h("div", { class: "banner " + (r.ok ? "ok" : "warn") },
          r.ok ? h("span", {}, "✅ ", r.message, " ", h("a", { href: r.url, target: "_blank", rel: "noopener", text: "Open ↗" })) : r.message));
        if (r.ok) break;
      } catch (e) { out.replaceChildren(h("div", { class: "banner bad", text: e.message })); break; }
      if (i < tries - 1) await new Promise((res) => setTimeout(res, 6000));
    }
    checkBtn.disabled = false;
  };
  checkBtn.addEventListener("click", () => check(1));
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.replaceChildren(h("p", { class: "muted", text: "Setting up the tunnel and the DNS record at Cloudflare …" }));
    try {
      const r = await api("/api/settings/cloudflare-domain", { method: "POST", body: { hostname: host.value, api_token: token.value } });
      token.value = "";
      if (field("WEB_PUBLIC_URL")) field("WEB_PUBLIC_URL").value = r.url;
      const on = field("WEB_TUNNEL");
      if (on) { if (on.type === "checkbox") on.checked = true; else on.value = "true"; }
      out.replaceChildren(h("div", { class: "banner ok", text: `Done: tunnel ${r.tunnel}, DNS record ${r.dns}. The bot is connecting ${r.url} now – checking …` }));
      await check(8);                           // new DNS + tunnel: give it up to ~45 s
    } catch (e) { out.replaceChildren(h("div", { class: "banner bad", text: e.message })); }
    btn.disabled = false;
  });
  return h("div", { class: "card", style: "margin-top:1rem" },
    h("h2", { text: "🌐 Use your own domain (Cloudflare)" }),
    h("p", { class: "muted", text: "Your domain must be on Cloudflare (bought there, or its nameservers moved there). The bot creates the tunnel and the DNS record for you – the address then never changes." }),
    h("ol", { class: "muted", style: "margin:.3rem 0 .8rem 1.1rem;padding:0" },
      h("li", {}, "Cloudflare → My Profile → ", h("a", { href: "https://dash.cloudflare.com/profile/api-tokens", target: "_blank", rel: "noopener", text: "API Tokens ↗" }),
        " → Create Token → Custom token."),
      h("li", { text: "Permissions: Account › Cloudflare Tunnel › Edit, Zone › DNS › Edit, Zone › Zone › Read. Zone resources: your domain." }),
      h("li", { text: "Paste the token below with the address you want (e.g. panel.yourdomain.com) and click Connect. The token is only used for this and is not saved." })),
    h("div", { class: "row" }, host, token),
    h("div", { class: "row", style: "margin-top:.5rem" }, btn, current ? checkBtn : null), out);
}

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
    showBanner("warn", "These saved settings only take effect after a restart: " + (data.restart_for || []).join(", ") + ". ", restartButton());
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
    if (g.id === "web") card.append(ownDomainBox(rows));
    if (g.id === "updates" && !data.setup_mode) card.firstChild.after(updateStatus());
    if (g.id === "alerts" && !data.setup_mode) card.firstChild.after(backupBox());
    if (g.id === "abuse" && !data.setup_mode) card.firstChild.after(abuseBox());
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
        showBanner("warn", h("strong", { text: "Saved and applied. " }), "Only these need a restart: " + r.restart_for.join(", ") + " ", restartButton());
      } else {
        banner.replaceChildren();
        toast(r.applied ? "Saved – active right away, no restart needed" : "Saved");
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

// Admin abuse protection: active?, who is allowed, last raid item spawns the bot saw
function abuseBox() {
  const box = h("div", { class: "update-box" }, h("span", { class: "muted", text: "Loading …" }));
  const render = (s) => {
    const state = !s.enabled ? h("span", { class: "pill warn", text: "switched off" })
      : s.active ? h("span", { class: "pill ok", text: "active" })
      : h("span", { class: "pill warn", text: "waiting for setup – tick allowed roles or enter allowed names below, then Save" });
    const allowed = [
      ...s.roles.map((r) => r.members === null ? `${r.name} (checked when someone spawns)`
        : `${r.name} (${r.members} member(s)${r.linked_players.length ? ": " + r.linked_players.join(", ") : ", none linked yet"})`),
      ...s.players];
    box.replaceChildren(
      h("div", { class: "row" }, h("strong", { text: "Status:" }), state),
      h("div", { class: "muted", style: "font-size:.85rem", text: "Allowed: " + (allowed.join(" · ") || "nobody yet") }),
      ...(s.cases.length ? [h("div", { style: "font-size:.85rem;margin-top:.3rem" }, h("strong", { text: "Cases (deletion only after approval):" })),
        ...s.cases.map((c) => h("div", { class: "row", style: "font-size:.82rem" },
          h("span", {}, `#${c.id} · `, h("strong", { text: c.player }), ` ${c.what} · ${c.created_at} UTC · `),
          c.status === "pending"
            ? h("span", { class: "row tight" },
                h("button", { class: "small danger", text: "🗑️ Approve deletion", onclick: () => decide(c, "approve") }),
                h("button", { class: "small", text: "✖️ Reject", onclick: () => decide(c, "reject") }))
            : h("span", { class: "muted", text: `${c.status} by ${c.decided_by}${c.result && c.status === "approved" ? ": " + c.result : ""}` })))] : []),
      h("div", { style: "font-size:.85rem;margin-top:.3rem" }, h("strong", { text: "Last rockets/C4 spawns the bot received:" })),
      ...(s.recent.length ? s.recent.slice(0, 8).map((e) => h("div", { class: "muted", style: "font-size:.82rem" },
        `${new Date(e.ts * 1000).toLocaleTimeString()} · `, h("strong", { text: e.player }), ` ${e.what} → ${e.result}`))
        : [h("div", { class: "muted", style: "font-size:.82rem", text: "None since the bot started. If you spawned some and nothing shows here, the server's \"[ServerVar] giving …\" lines don't reach the bot – check 🖥️ Console." })]),
      h("div", { class: "row", style: "margin-top:.4rem" }, h("button", { class: "small", text: "↻ Refresh", onclick: load })));
  };
  const decide = async (c, action) => {
    if (action === "approve" && !confirm(`Run ${s_cmds} for ${c.player}? This deletes everything they own and can't be undone.`)) return;
    await act(() => api(`/api/abuse/cases/${c.id}/${action}`, { method: "POST" }), action === "approve" ? "Approved – commands sent" : "Rejected");
    load();
  };
  let s_cmds = "the approval commands";
  const load = () => api("/api/abuse").then((s) => { s_cmds = (s.approval_commands || []).map((t) => t.replace("{name}", "…")).join(", ") || s_cmds; render(s); })
    .catch((e) => box.replaceChildren(h("span", { class: "error", text: e.message })));
  load();
  return box;
}

// Backups: list, download, "Backup now" (settings page, group "Alerts & backups")
function backupBox() {
  const box = h("div", { class: "update-box" }, h("span", { class: "muted", text: "Loading backups …" }));
  const size = (b) => b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
  const render = (r) => {
    const btn = h("button", { class: "small", text: "💾 Backup now", onclick: async () => {
      btn.disabled = true;
      try { await act(() => api("/api/backups", { method: "POST" }), "Backup created"); render(await api("/api/backups")); }
      catch { btn.disabled = false; }
    } });
    const list = r.backups.slice(0, 8).map((b) => h("div", { class: "row", style: "font-size:.85rem" },
      h("a", { href: `/api/backups/${encodeURIComponent(b.name)}`, download: b.name, text: b.name }),
      h("span", { class: "muted", text: `${size(b.size)} · ${new Date(b.created * 1000).toLocaleString()}` })));
    const copy = r.copy_dir ? h("div", { class: "muted", style: "font-size:.85rem" },
      `☁️ Copied to ${r.copy_dir}` + (r.live_minutes ? ` · live backup every ${r.live_minutes} min` + (r.live_last ? ` (last ${new Date(r.live_last * 1000).toLocaleTimeString()})` : " (waiting for the first change)") : ""))
      : h("div", { class: "muted", style: "font-size:.85rem", text: "☁️ Tip: set \"Also copy backups to folder\" to a Google Drive for desktop folder for an off-PC copy." });
    const errs = [r.copy_error, r.live_error].filter(Boolean).map((e) => h("div", { class: "error", style: "font-size:.85rem", text: "⚠️ " + e }));
    box.replaceChildren(
      h("div", { class: "row" }, h("strong", { text: "Backups " }),
        h("span", { class: "muted", text: `${r.enabled ? `daily + before every update, kept ${r.keep_days} days` : "daily backup off"} · data/backups/ · ${r.include_env ? "with .env" : "without .env"}` })),
      copy, ...errs,
      ...(list.length ? list : [h("span", { class: "muted", text: "No backup yet." })]),
      h("div", { class: "row", style: "margin-top:.4rem" }, btn));
  };
  api("/api/backups").then(render).catch((e) => box.replaceChildren(h("span", { class: "error", text: e.message })));
  return box;
}

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
    box.replaceChildren(...[
      h("div", { class: "row" },
        h("strong", { text: "Installed version: " }),
        cur.sha ? h("code", { text: cur.sha.slice(0, 7) }) : h("span", { class: "muted", text: "unknown (first check pending)" }),
        cur.title ? h("span", { class: "muted", text: cur.title }) : null),
      h("div", { class: "row muted", style: "font-size:.85rem" },
        `${u.enabled ? `Auto-update on – every ${u.interval} min` : "Auto-update off"} · from ${u.repo} (${u.branch}) · ` +
        `${u.mode === "git" ? "git checkout" : "ZIP install"} · last check: ${ago(u.last_check)}`, state),
      u.launcher ? null : h("p", { class: "muted", style: "font-size:.85rem;margin:.2rem 0 0", text: "Tip: start the bot with start.bat / start.sh – then it restarts by itself after an update." }),
      h("div", { class: "row", style: "margin-top:.4rem" }, btn),
    ].filter(Boolean));
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

// ------------------------------------------------------------------ Plugins
// Every plugin is one file (plugins/ or data/plugins/). Switch on/off and settings take effect right away.

function pluginImageControl(f, p, reload) {
  const url = `/api/plugins/${p.id}/file/${f.key}`;
  const preview = f.value ? h("img", { class: "plugin-img", src: `${url}?t=${Date.now()}`, alt: "" })
    : h("span", { class: "muted", text: "Default picture" + (p.public_path ? " (see the website)" : "") });
  const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif" });
  file.addEventListener("change", () => {
    const f0 = file.files[0];
    if (!f0) return;
    if (f0.size > 700000) { toast("The picture is too big (max. 700 KB).", true); file.value = ""; return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try { await api(url, { method: "POST", body: { data: reader.result } }); toast("Picture saved"); reload(); }
      catch (e) { toast(e.message, true); }
    };
    reader.readAsDataURL(f0);
  });
  return h("div", { class: "row" }, preview, file,
    f.value ? h("button", { class: "small ghost", text: "Use default", onclick: async () => {
      await act(() => api(url, { method: "DELETE" }), "Default picture restored"); reload();
    } }) : null);
}

function pluginControl(f, opts, kits, inputs, p, reload) {
  if (f.kind === "image") return pluginImageControl(f, p, reload);
  const value = f.value === null || f.value === undefined ? "" : String(f.value);
  const listFor = { channel: opts.text_channels, role: opts.roles }[f.kind] || [];
  let el;
  if (f.kind === "bool") {
    el = h("select", {}, h("option", { value: "true", text: "On" }), h("option", { value: "false", text: "Off" }));
    el.value = f.value ? "true" : "false";
  } else if (f.kind === "choice") {
    el = h("select", {}, f.choices.map((c) => h("option", { value: c, text: c })));
    el.value = value;
  } else if ((f.kind === "channel" || f.kind === "role") && opts.available) {
    el = h("select", {}, h("option", { value: "", text: "– none –" }), listFor.map((o) => h("option", { value: o.id, text: o.name })));
    if (value && !listFor.some((o) => o.id === value)) el.append(h("option", { value, text: `ID ${value} (not found)` }));
    el.value = value;
  } else if (f.kind === "textarea") {
    el = h("textarea", { rows: 4 });
    el.value = value;
  } else if (f.kind === "kit") {
    const id = `kits-${f.key}-${Math.random().toString(36).slice(2, 8)}`;
    el = h("input", { value, list: id, placeholder: "Kit name (empty = none)" });
    inputs[f.key] = () => el.value;
    return h("div", {}, el, h("datalist", { id }, kits.map((k) => h("option", { value: k }))));
  } else {
    el = h("input", { value, inputmode: ["int", "channel", "role"].includes(f.kind) ? "numeric" : null,
      placeholder: f.kind === "channel" || f.kind === "role" ? "Discord ID" : "" });
  }
  inputs[f.key] = () => el.value;
  return el;
}

function pluginCard(p, opts, kits, reload, { tab = false } = {}) {
  const inputs = {}, rows = {};
  const pill = p.error ? h("span", { class: "pill bad keep", text: "error" })
    : !p.enabled ? h("span", { class: "pill keep", text: "off" })
    : !p.running ? h("span", { class: "pill bad keep", text: "not running" })
    : (p.problems.length || p.missing_intents.length) ? h("span", { class: "pill warn keep", text: "needs setup" })
    : h("span", { class: "pill ok keep", text: "on" });
  const sw = h("input", { type: "checkbox" });
  sw.checked = p.enabled;
  sw.disabled = !!(p.error && !p.enabled);
  sw.addEventListener("change", async () => {
    sw.disabled = true;
    try {
      const r = await api(`/api/plugins/${p.id}/toggle`, { method: "POST", body: { enabled: sw.checked } });
      toast(`${p.name} ${r.plugin.enabled ? "switched on" : "switched off"}`);
      if (r.plugin.enabled && r.plugin.missing_intents.length) toast("This plugin needs a Discord intent – see the note on its card.", true);
      reload();
    } catch (e) { toast(e.message, true); sw.checked = !sw.checked; sw.disabled = false; }
  });
  const card = h("div", { class: "card plugin" },
    h("div", { class: "row" }, h("h2", { text: `${p.icon} ${p.name}` }), pill,
      h("label", { class: "switch keep", title: "On/off" }, sw, h("span", { text: "On" }))),
    h("p", { class: "muted", text: p.description || "" }));
  if (p.error) card.append(h("div", { class: "banner bad" }, h("strong", { text: "Error: " }), p.error));
  if (p.enabled && p.missing_intents.length) {
    const names = p.missing_intents.map((i) => ({ members: "Server Members Intent", message_content: "Message Content Intent" }[i] || i));
    card.append(h("div", { class: "banner warn" }, h("strong", { text: "Needs a Discord setting: " }),
      `turn on "${names.join('" and "')}" in the Discord Developer Portal (your app → Bot → Privileged Gateway Intents → Save), then restart the bot (Settings → Restart).`));
  }
  if (p.enabled && p.problems.length) card.append(h("div", { class: "banner warn" }, h("strong", { text: "To do: " }), p.problems.join(" ")));
  const status = Object.entries(p.status || {});
  if (p.running && status.length) {
    card.append(h("div", { class: "grid", style: "margin:.25rem 0 .5rem" }, status.map(([k, v]) =>
      h("div", { class: "stat" }, h("div", { class: "label", text: k }), h("div", { class: "value", style: "font-size:1.05rem", text: String(v) })))));
  }
  if (p.public) {
    const url = location.origin + (p.public_path || `/p/${p.id}`);
    card.append(h("p", {}, "Public address: ", h("a", { href: url, target: "_blank", rel: "noopener", text: url }),
      h("span", { class: "muted", text: p.running ? " – share it anywhere (works with your public web link)." : " – switch the plugin on first." })));
  }
  if (p.settings.length) {
    const form = h("div", {});
    for (const f of p.settings) {
      const err = h("div", { class: "field-error" });
      const row = h("div", { class: "field" }, h("label", { text: f.label }),
        h("div", { class: "control" }, pluginControl(f, opts, kits, inputs, p, reload), f.help ? h("div", { class: "help", text: f.help }) : null, err));
      rows[f.key] = { row, err };
      form.append(row);
    }
    const save = h("button", { class: "primary", text: "Save settings" });
    save.addEventListener("click", async () => {
      Object.values(rows).forEach(({ row, err }) => { row.classList.remove("has-error"); err.textContent = ""; });
      save.disabled = true;
      try {
        const values = Object.fromEntries(Object.entries(inputs).map(([k, get]) => [k, get()]));
        await api(`/api/plugins/${p.id}/settings`, { method: "PUT", body: { values } });
        toast("Saved – active right away");
        reload();
      } catch (e) {
        for (const [key, msg] of Object.entries(e.data?.errors || {})) if (rows[key]) { rows[key].row.classList.add("has-error"); rows[key].err.textContent = msg; }
        toast(e.message, true);
      } finally { save.disabled = false; }
    });
    card.append(form, h("div", { class: "row", style: "margin-top:.6rem" }, save));
  }
  const foot = h("div", { class: "row muted", style: "margin-top:.6rem;font-size:.83rem" },
    h("span", { text: `${p.builtin ? "Built-in" : p.overrides ? "Your copy – replaces the built-in" : "Your plugin"} · ${p.file}${p.version ? " · v" + p.version : ""}${p.author ? " · by " + p.author : ""}`,
      title: p.builtin ? "Comes with the bot and is updated with it" : "In data/plugins/ – never touched by updates" }),
    h("button", { class: "small ghost", text: "↻ Reload", title: "Read the file again (after editing it)", onclick: async () => {
      await act(() => api(`/api/plugins/${p.id}/reload`, { method: "POST" }), "Reloaded"); reload();
    } }),
    p.builtin ? null : h("button", { class: "small danger", text: p.overrides ? "Delete my copy" : "Delete", onclick: async () => {
      const q = p.overrides ? `Delete your copy ${p.file}? The built-in plugin comes back (same on/off state and settings).`
        : `Delete the plugin file ${p.file}? Its settings are removed too.`;
      if (!confirm(q)) return;
      await act(() => api(`/api/plugins/${p.id}`, { method: "DELETE" }), p.overrides ? "Back to the built-in" : "Plugin deleted"); reload();
    } }));
  if (!tab) card.append(foot);                       // a feature with its own tab: no file / reload line
  return card;
}

function pluginUploadCard(reload) {
  const file = h("input", { type: "file", accept: ".py" });
  const btn = h("button", { class: "primary", text: "Upload plugin" });
  btn.addEventListener("click", async () => {
    const f = file.files[0];
    if (!f) { toast("Choose a .py file first.", true); return; }
    if (!confirm("Only upload plugins from people you trust – a plugin runs inside the bot and can do anything the bot can. Upload?")) return;
    btn.disabled = true;
    try {
      const code = await f.text();
      const r = await api("/api/plugins/upload", { method: "POST", body: { filename: f.name, code } });
      toast(`${r.plugin.name} uploaded – switch it on below`);
      file.value = "";
      reload();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  });
  return h("div", { class: "card" }, h("h2", { text: "⬆️ Add your own plugin" }),
    h("p", { class: "muted", text: "A plugin is one .py file. It is saved in data/plugins/ (never touched by updates) and starts switched off. A file with the same name as a built-in plugin replaces it – that's how you keep a changed built-in across updates. How to write one: PLUGINS.md in the bot folder." }),
    h("div", { class: "banner warn" }, "Only upload plugins from people you trust – a plugin runs inside the bot with full access."),
    h("div", { class: "row" }, file, btn));
}

function pluginAiCard(reload) {
  const desc = h("textarea", { rows: 4, placeholder: "Describe the plugin in plain words, e.g. \"Every time a player joins the game, greet them in the game chat and give them 5 points. Owner can set the welcome text.\"" });
  const draftBtn = h("button", { class: "primary", text: "✨ Draft with AI" });
  const result = h("div", {});                         // filled after the AI answers

  draftBtn.addEventListener("click", async () => {
    const text = desc.value.trim();
    if (text.length < 5) { toast("Describe the plugin in a bit more detail.", true); return; }
    draftBtn.disabled = true;
    draftBtn.textContent = "Writing …";
    result.replaceChildren(h("p", { class: "muted", text: "The AI is writing your plugin – this can take a few seconds …" }));
    try {
      const r = await api("/api/plugins/generate", { method: "POST", body: { description: text } });
      result.replaceChildren(aiDraftReview(r.draft, reload));
    } catch (e) {
      result.replaceChildren(h("div", { class: "banner bad", text: e.message }));
    } finally { draftBtn.disabled = false; draftBtn.textContent = "✨ Draft with AI"; }
  });

  return h("div", { class: "card" }, h("h2", { text: "✨ Create a plugin with AI" }),
    h("p", { class: "muted", text: "Describe what you want and the integrated AI writes the plugin for you. You can read and edit the code before it is created – it is saved switched off, like any plugin." }),
    h("div", { class: "banner warn" }, "AI-written code runs inside the bot with full access. Read it before you create the plugin, and only create what you understand."),
    desc, h("div", { class: "row" }, draftBtn), result);
}

function aiDraftReview(draft, reload) {
  const filename = h("input", { type: "text", value: draft.filename || "ai_plugin.py", style: "max-width:14rem" });
  const code = h("textarea", { rows: 20, spellcheck: "false", value: draft.code || "",
    style: "width:100%;font-family:var(--mono, monospace);font-size:.85rem" });
  const createBtn = h("button", { class: "primary", text: "Create plugin" });

  createBtn.addEventListener("click", async () => {
    const body = { filename: filename.value.trim(), code: code.value };
    if (!body.code.trim()) { toast("There is no code to create.", true); return; }
    if (!confirm("Create this plugin? AI-written code runs inside the bot with full access – only create it if you have read and trust it. It will be saved switched off.")) return;
    createBtn.disabled = true;
    try {
      const r = await api("/api/plugins/upload", { method: "POST", body });
      toast(`${r.plugin.name} created – switch it on below`);
      reload();
    } catch (e) { toast(e.message, true); } finally { createBtn.disabled = false; }
  });

  return h("div", { class: "ai-draft" },
    draft.summary ? h("p", {}, h("strong", { text: "What it does: " }), draft.summary) : null,
    (draft.warnings && draft.warnings.length)
      ? h("div", { class: "banner warn" }, "Check before creating: " + draft.warnings.join(" "))
      : h("div", { class: "banner ok", text: "No obvious problems found – still read the code below." }),
    h("label", { class: "muted", text: "File name" }), h("div", { class: "row" }, filename),
    h("label", { class: "muted", text: "Plugin code (you can edit it)" }), code,
    h("div", { class: "row" }, createBtn));
}

const goTo = (hash) => () => { location.hash = hash; };

function pluginCreateLink() {
  return h("div", { class: "card" }, h("h2", { text: "➕ Add a plugin" }),
    h("p", { class: "muted", text: "Build a new plugin with AI from a plain-language description, or upload your own .py file – on the Create plugin page. New plugins start switched off and then show up here." }),
    h("div", { class: "row" }, h("button", { class: "primary", text: "✨ Create a plugin", onclick: goTo("#makeplugin") })));
}

views.plugins = async (main) => {
  main.append(h("h1", { text: "Plugins" }),
    h("p", { class: "muted", text: "Extra features as plugins – switch them on/off and change their settings here. Everything takes effect right away, no restart needed." }));
  const list = h("div", {}, h("p", { class: "muted", text: "Loading …" }));
  main.append(list);
  const opts = await api("/api/settings/discord-options").catch(() => ({ available: false }));
  const reload = async () => {
    try {
      const r = await api("/api/plugins");
      list.replaceChildren(...r.plugins.filter((p) => !p.tab).map((p) => pluginCard(p, opts, r.kits, reload)), pluginCreateLink());
    } catch (e) { list.replaceChildren(h("div", { class: "banner bad", text: e.message })); }
  };
  reload();
};

// Offline protection zones (plugins/offline_protection.py) – a feature with its own tab
views.protection = async (main) => {
  main.append(h("h1", { text: "Offline protection" }),
    h("p", { class: "muted", text: "Team leaders place their base's zone in-game with the quick chats \"Can I build here?\" + \"Yes\". The zone turns red (no building damage) while the whole team is offline and green the moment someone is back. Zones are deleted after the set time with nobody of the team online." }));
  const box = h("div", {}, h("p", { class: "muted", text: "Loading …" }));
  main.append(box);
  const opts = await api("/api/settings/discord-options").catch(() => ({ available: false }));
  const reload = async () => {
    try {
      const r = await api("/api/protection");
      box.replaceChildren(protectionZones(r, reload), pluginCard(r.plugin, opts, r.kits, reload, { tab: true }));
    } catch (e) { box.replaceChildren(h("div", { class: "banner bad", text: e.message })); }
  };
  reload();
};

function protectionZones(r, reload) {
  const card = h("div", { class: "card" }, h("h2", { text: `🗺️ Zones (${r.zones.length})` }));
  if (!r.plugin.running) { card.append(h("p", { class: "muted", text: "Switch offline protection on to see and manage the zones." })); return card; }
  if (!r.zones.length) { card.append(h("p", { class: "muted", text: "No zones yet – a team leader places one in-game." })); return card; }
  const now = r.now, fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "–";
  card.append(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Zone", "Leader", "Team", "Status", "Offline since", "Deleted at", "Position", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, r.zones.map((z) => h("tr", {},
      h("td", {}, h("code", { text: z.name })),
      h("td", { text: z.leader }),
      h("td", { class: "muted", text: z.members.length ? z.members.join(", ") : "–" }),
      h("td", {}, h("span", { class: `pill keep ${z.state === "red" ? "bad" : "ok"}`, text: z.state === "red" ? "🔴 red – offline" : "🟢 green" })),
      h("td", { class: "muted", text: z.offline_since ? `${fmt(z.offline_since)} (${fmtDuration(now - z.offline_since)} ago)` : "someone is online" }),
      h("td", { text: z.expires_at ? `${fmt(z.expires_at)} (in ${fmtDuration(Math.max(0, z.expires_at - now))})` : z.offline_since ? "never" : "–" }),
      h("td", { class: "muted", text: `${Math.round(z.x)} ${Math.round(z.y)} ${Math.round(z.z)}` }),
      h("td", {}, h("button", { class: "small danger", text: "Delete", onclick: async () => {
        if (!confirm(`Delete the zone ${z.name} of ${z.leader}'s team on the server? The leader can place a new one.`)) return;
        await act(() => api(`/api/protection/zones/${z.team}`, { method: "DELETE" }), "Zone deleted"); reload();
      } }))))))));
  return card;
}

views.makeplugin = async (main) => {
  main.append(h("h1", { text: "Create a plugin" }),
    h("p", { class: "muted", text: "Make a new plugin without touching the bot's code: describe it and let the AI write it, or upload your own .py file. New plugins are saved switched off – turn them on under Plugins." }));
  const toPlugins = goTo("#plugins");   // after creating, jump to the list to switch it on
  main.append(pluginAiCard(toPlugins), pluginUploadCard(toPlugins),
    h("div", { class: "card" }, h("h2", { text: "🧩 Your plugins" }),
      h("p", { class: "muted", text: "Switch plugins on or off and change their settings on the Plugins page." }),
      h("div", { class: "row" }, h("button", { text: "Go to Plugins", onclick: toPlugins }))));
};

// ------------------------------------------------------------------ Web store (owner): kits for sale, gift cards, orders

const money = (v, cur) => `${Number(v).toFixed(2)} ${cur}`;
const ORDER_PILL = { delivered: "ok", failed: "bad", review: "warn", pending: "warn", paid: "warn", waiting: "warn" };

function itemPicturesCard() {
  const out = h("div", {});
  const btn = h("button", { class: "primary", text: "🔍 Search item pictures" });
  const show = (st) => {
    if (st.never) { out.replaceChildren(h("p", { class: "muted", text: "Not scanned yet." })); return; }
    const kids = [];
    if (st.error) kids.push(h("div", { class: "banner bad", text: st.error }));
    if (st.running) kids.push(h("p", { text: `Searching … ${st.done} / ${st.total}` }));
    else {
      kids.push(h("p", { text: `${st.found} items have a picture, ${st.missing.length} don't.` }));
      const sources = Object.entries(st.sources || {});
      if (sources.length) {
        kids.push(h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Picture source", "Pictures", "Not there", "Refused / failed", "Last problem"].map((t) => h("th", { text: t })))),
          h("tbody", {}, sources.map(([host, s]) => h("tr", {},
            h("td", { text: host }), h("td", { text: s.ok }), h("td", { text: s.not_found }),
            h("td", {}, s.errors ? h("span", { class: "pill " + (s.ok ? "warn" : "bad"), text: s.errors }) : "0"),
            h("td", { class: "muted", text: s.last_error || "–" })))))));
        if (sources.some(([, s]) => s.errors && !s.ok)) kids.push(h("p", { class: "muted", text: "A source that refuses everything (HTTP 403/429) blocks your server's connection – pick another one under Settings → Web → Item pictures from, or put your own pictures into data/item_images/<shortname>.img." }));
      }
      if (st.missing.length) {
        kids.push(h("details", {}, h("summary", { class: "muted", text: "Items without a picture" }),
          h("p", { class: "muted", text: st.missing.join(", ") }),
          h("p", { class: "muted", text: "Put your own picture into data/item_images/<shortname>.img (PNG/JPG/WebP) and it is used right away." })));
      }
    }
    out.replaceChildren(...kids);
  };
  const poll = async () => {
    try {
      const st = await api("/api/item-images/scan");
      show(st);
      if (st.running && out.isConnected) setTimeout(poll, 2000);
      else btn.disabled = false;
    } catch (e) { out.replaceChildren(h("div", { class: "banner bad", text: e.message })); btn.disabled = false; }
  };
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try { show(await api("/api/item-images/scan", { method: "POST", body: {} })); setTimeout(poll, 1500); }
    catch (e) { toast(e.message, true); btn.disabled = false; }
  });
  setTimeout(poll, 0);
  return h("div", { class: "card" }, h("h2", { text: "🖼️ Item pictures" }),
    h("p", { class: "muted", text: "Pictures are fetched once and kept on the bot. This looks for every item of the kit builder now (several sources, alternative spellings) and lists the ones still without a picture." }),
    h("div", { class: "row" }, btn), out);
}

views.webstore = async (main) => {
  main.append(h("h1", { text: "Store" }),
    h("p", { class: "muted", text: "Players buy kits on the player website (Store) with PayPal or gift cards. The kit is given to the Discord account their in-game name is linked to (/customkit link) – they claim it with /customkit claim." }));
  const holder = h("div", {}, h("p", { class: "muted", text: "Loading …" }));
  main.append(holder, itemPicturesCard());
  let editId = null, rechecked = false;

  const load = async () => {
    let d;
    try { d = await api("/api/webshop"); } catch (e) { holder.replaceChildren(h("div", { class: "banner bad", text: e.message })); return; }
    try { render(d); } catch (e) {
      console.error(e);
      holder.replaceChildren(h("div", { class: "banner bad", text: "This page could not be shown: " + e.message }));
    }
    // New kits are looked for in the background – show them once that's done
    if (d.syncing && !rechecked) { rechecked = true; setTimeout(() => { if (location.hash === "#webstore") load(); }, 8000); }
  };

  const render = (d) => {
    const st = d.status, cur = st.currency;

    // --- Status
    const pill = (ok, yes, no) => h("span", { class: "pill " + (ok ? "ok" : "warn"), text: ok ? yes : no });
    const status = h("div", { class: "card" }, h("h2", { text: "⚙️ Setup" }),
      h("div", { class: "row" },
        pill(st.paypal, `PayPal connected (${st.paypal_mode})`, "PayPal not set up"),
        h("span", { class: "pill", text: `Currency ${cur}` }),
        pill(st.builder, "Players can build their own kit", `Own kits: off (${st.builder_problem || "?"})`),
        pill(st.giftcard_values.length, `Gift cards for sale: ${st.giftcard_values.map((v) => v + " " + cur).join(", ")}`, "Gift cards: owners only"),
        st.admin_key ? h("span", { class: "pill warn", text: "🔑 Admin key on (store address + ?admin=1)" }) : h("span", { class: "pill", text: "Admin key off" })),
      h("p", { class: "muted", style: "margin:.6rem 0 0" },
        "PayPal, currency and gift card values: Settings → 💳 Store. The store shows up on the player website (Plugins → Player website) at ",
        h("a", { href: "/store", target: "_blank", rel: "noopener", text: "/store" }), "."),
      st.paypal && st.paypal_mode === "sandbox" ? h("div", { class: "banner warn", style: "margin:.6rem 0 0", text: "PayPal is in sandbox (test) mode – no real money. Switch PAYPAL_MODE to live with your live Client ID/Secret when you're ready." }) : null);

    // --- Kits for sale
    const kName = h("input", { placeholder: "Name on the website, e.g. Raid kit" });
    const kKit = h("select", {}, h("option", { value: "", text: "– custom kit –" }), d.custom_kits.map((k) => h("option", { value: k, text: k })));
    const kPrice = h("input", { type: "number", min: "0.01", step: "0.01", placeholder: `Price (${cur})` });
    const kDays = h("input", { type: "number", min: "0", step: "1", placeholder: "Days (0 = permanent)" });
    const kDesc = h("input", { placeholder: "Description (optional)" });
    const catList = h("datalist", { id: "store-cats" }, [...new Set(d.kits.map((k) => k.category).filter(Boolean))].sort().map((c) => h("option", { value: c })));
    const kCat = h("input", { placeholder: "Category, e.g. Raid kits (optional)", list: "store-cats", maxlength: "40" });
    const kSave = h("button", { class: "primary", text: "Add kit" });
    const kCancel = h("button", { class: "ghost hidden", text: "Cancel", onclick: () => { editId = null; load(); } });
    kSave.addEventListener("click", async () => {
      const body = { name: kName.value, kit: kKit.value, price: kPrice.value, days: kDays.value || 0, description: kDesc.value, category: kCat.value };
      await act(() => editId ? api(`/api/webshop/kits/${editId}`, { method: "PUT", body }) : api("/api/webshop/kits", { method: "POST", body }), editId ? "Kit saved" : "Kit added");
      editId = null; load();
    });
    const ingame = (k) => k.source === "ingame";
    const kitRows = d.kits.map((k) => h("tr", {},
      h("td", {}, h("strong", { text: k.name }), k.category ? h("span", { class: "pill", style: "margin-left:.4rem", text: k.category }) : null,
        k.description ? h("div", { class: "muted", text: k.description }) : null),
      h("td", {}, k.kit, h("div", { class: "muted", text: (ingame(k) ? "in-game kit" : "bot kit") + (k.auto ? " · added automatically" : "")
        + (!ingame(k) && !d.custom_kits.includes(k.kit) ? " · missing!" : "") + ` · ${k.items} item${k.items === 1 ? "" : "s"}` })),
      h("td", {}, k.price > 0 ? money(k.price, cur) : h("span", { class: "pill warn", text: "needs a price" })),
      h("td", { text: ingame(k) ? "one-time (in-game)" : k.days ? `${k.days} days` : "permanent" }),
      h("td", {}, h("span", { class: "pill " + (k.enabled && k.price > 0 ? "ok" : ""), text: k.enabled && k.price > 0 ? "for sale" : "hidden" })),
      h("td", { class: "row tight" },
        k.price > 0 ? h("button", { class: "small", text: k.enabled ? "Hide" : "Show", onclick: async () => { await act(() => api(`/api/webshop/kits/${k.id}`, { method: "PUT", body: { enabled: !k.enabled } })); load(); } })
          : h("button", { class: "small primary", text: "Set price", onclick: async () => {
            const price = prompt(`Price for ${k.name} (${cur}) – it goes on sale right away:`);
            if (!price) return;
            await act(() => api(`/api/webshop/kits/${k.id}`, { method: "PUT", body: { price: price.replace(",", ".") } }), `${k.name} is on sale`); load();
          } }),
        h("button", { class: "small", text: "Edit", onclick: () => {
          editId = k.id; kName.value = k.name; kKit.value = k.kit; kPrice.value = k.price; kDays.value = k.days; kDesc.value = k.description || ""; kCat.value = k.category || "";
          kSave.textContent = "Save kit"; kCancel.classList.remove("hidden"); kName.focus();
        } }),
        h("button", { class: "small danger", text: "Remove", onclick: async () => { if (!confirm(`Remove ${k.name} from the store?` + (k.auto ? " It won't be added again automatically." : "") + " (The kit itself stays.)")) return; await act(() => api(`/api/webshop/kits/${k.id}`, { method: "DELETE" }), "Removed"); load(); } }))));
    const kits = h("div", { class: "card" }, h("h2", { text: "🎒 Kits for sale" }),
      h("p", { class: "muted", text: "New bot kits (Kit builder / Kits page) and the server's in-game kits are added here automatically – hidden until you set a price. Bot kits give the kit's claim role (for N days or permanently); in-game kits are given in-game once. You can also add a bot kit by hand below." }),
      d.kits.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Kit", "Custom kit", "Price", "Duration", "", ""].map((t) => h("th", { text: t })))), h("tbody", {}, kitRows))) : h("p", { class: "muted", text: "Nothing for sale yet." }),
      h("div", { class: "row", style: "margin-top:.75rem" }, kName, kKit, kPrice, kDays),
      h("div", { class: "row", style: "margin-top:.5rem" }, kCat, catList, kDesc, kSave, kCancel));

    // --- Gift cards
    const gAmount = h("input", { type: "number", min: "0.01", step: "0.01", placeholder: `Value (${cur})` });
    const gCount = h("input", { type: "number", min: "1", max: "100", value: "1", title: "How many" });
    const gDays = h("input", { type: "number", min: "0", placeholder: "Valid for days (0 = no end)" });
    const gNote = h("input", { placeholder: "Note, e.g. giveaway prize (optional)" });
    const gOut = h("textarea", { rows: 3, readonly: true, class: "hidden", placeholder: "New codes" });
    const gBtn = h("button", { class: "primary", text: "Create gift cards", onclick: async () => {
      const r = await act(() => api("/api/webshop/giftcards", { method: "POST", body: { amount: gAmount.value, count: gCount.value, days: gDays.value || 0, note: gNote.value } }));
      if (!r) return;
      toast(`${r.codes.length} gift card(s) created`);
      await load();
      const out = $("#gift-codes");
      if (out) { out.value = r.codes.join("\n"); out.classList.remove("hidden"); out.select(); }
    } });
    gOut.id = "gift-codes";
    const now = Date.now() / 1000;
    const giftRows = d.gift_cards.map((g) => {
      const state = g.disabled ? ["disabled", "bad"] : (g.expires_at && g.expires_at < now) ? ["expired", ""] : g.balance <= 0 ? ["used up", ""] : ["active", "ok"];
      return h("tr", {},
        h("td", {}, h("code", { text: g.code })),
        h("td", { text: `${money(g.balance, g.currency)} / ${money(g.initial, g.currency)}` }),
        h("td", { class: "muted", text: (g.order_id ? `bought (order #${g.order_id})` : `by ${g.created_by}`) + (g.note ? ` · ${g.note}` : "") }),
        h("td", { class: "muted", text: new Date(g.created_at * 1000).toLocaleDateString() + (g.expires_at ? ` → ${new Date(g.expires_at * 1000).toLocaleDateString()}` : "") }),
        h("td", {}, h("span", { class: "pill " + state[1], text: state[0] })),
        h("td", {}, h("button", { class: "small" + (g.disabled ? "" : " danger"), text: g.disabled ? "Enable" : "Disable", onclick: async () => {
          await act(() => api(`/api/webshop/giftcards/${encodeURIComponent(g.code)}/toggle`, { method: "POST", body: { disabled: !g.disabled } })); load();
        } })));
    });
    const gifts = h("div", { class: "card" }, h("h2", { text: "🎁 Gift cards" }),
      h("p", { class: "muted", text: "Create codes to hand out (giveaways, apologies, prizes). Players enter the code at checkout; it pays all or part of an order, the rest stays on the card." }),
      h("div", { class: "row" }, gAmount, gCount, gDays, gNote, gBtn), gOut,
      d.gift_cards.length ? h("div", { class: "table-wrap", style: "margin-top:.75rem" }, h("table", {}, h("thead", {}, h("tr", {}, ["Code", "Balance", "From", "Created → ends", "", ""].map((t) => h("th", { text: t })))), h("tbody", {}, giftRows))) : null);

    // --- Orders
    const orderRows = d.orders.map((o) => h("tr", {},
      h("td", { text: "#" + o.id }), h("td", { class: "muted", text: new Date(o.created_at * 1000).toLocaleString() }),
      h("td", {}, o.title, o.result && o.status === "delivered" && o.kind !== "giftcard" ? h("div", { class: "muted", text: "→ " + o.result }) : null),
      h("td", { text: o.player || "–" }),
      h("td", {}, money(o.total, o.currency), h("div", { class: "muted", text: [o.gift_amount ? `gift ${money(o.gift_amount, o.currency)}` : "", o.paypal_amount ? `PayPal ${money(o.paypal_amount, o.currency)}` : "", o.admin_key ? "🔑 free (admin key)" : ""].filter(Boolean).join(" + ") })),
      h("td", {}, h("span", { class: "pill " + (ORDER_PILL[o.status] || ""), text: o.status }),
        (o.status === "failed" || o.status === "review") && o.result ? h("div", { class: "muted", text: o.result }) : null),
      h("td", { class: "muted", style: "font-size:.8rem" }, o.capture_id ? `capture ${o.capture_id}` : o.paypal_order ? `PayPal ${o.paypal_order}` : "", o.payer_email ? h("div", { text: o.payer_email }) : null),
      h("td", {}, ["failed", "review"].includes(o.status) ? h("button", { class: "small", text: "Retry delivery", onclick: async () => {
        const r = await act(() => api(`/api/webshop/orders/${o.id}/retry`, { method: "POST" }));
        if (r) toast(r.ok ? "Delivered" : `Still failing: ${r.result || r.status}`, !r.ok);
        load();
      } }) : null)));
    const orders = h("div", { class: "card" }, h("h2", { text: "🧾 Orders" }),
      d.orders.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Date", "Product", "Player", "Paid", "Status", "PayPal", ""].map((t) => h("th", { text: t })))), h("tbody", {}, orderRows))) : h("p", { class: "muted", text: "No orders yet." }));

    holder.replaceChildren(status, kits, gifts, orders);
    if (editId) { kSave.textContent = "Save kit"; kCancel.classList.remove("hidden"); }
  };
  load();
};

start();
