// RCE Companion – the players' web app (iPhone: Share → Add to Home Screen). Same data as the Android app:
// /p/companion/account, chat, map, mapimage, claim, alerts, rf, prefs – with the pairing token of this phone.
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const main = $("#main"), tabs = $("#tabs");
  const KEY = "companion-token";
  const store = { get: () => { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } },
                  set: (v) => { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) { /* private mode */ } } };
  let token = store.get(), acc = null, map = null, mapImg = null, mapImgVersion = "", chat = [], lastChat = 0;
  let tab = "hub", timer = null, rounds = 0;
  const standalone = window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
    return el;
  }

  function toast(text) {
    if (!text) return;
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { t.hidden = true; }, 3500);
  }

  async function api(path, body) {
    const r = await fetch(path, { method: body === undefined ? "GET" : "POST", cache: "no-store",
      headers: Object.assign({ Authorization: "Bearer " + token }, body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status === 401) { token = ""; store.set(""); stop(); welcome(); throw new Error("Not paired any more."); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "The server answered " + r.status + ".");
    return data;
  }

  // ---------------------------------------------------------------- pairing

  function welcome(problem) {
    tabs.hidden = true;
    $("#sub").textContent = "Not paired yet";
    const code = h("input", { type: "text", class: "code", maxlength: "8", placeholder: "CODE", autocapitalize: "characters", autocomplete: "off" });
    code.value = (new URLSearchParams(location.search).get("code") || "").toUpperCase();
    const go = async () => {
      try {
        const r = await fetch("exchange", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: code.value.trim(), device: ios ? "iPhone (web app)" : "Web app" }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || "Pairing failed.");
        token = data.token;
        store.set(token);
        history.replaceState(null, "", location.pathname);
        start();
      } catch (e) { toast(e.message); }
    };
    main.replaceChildren(
      ios && !standalone ? h("div", { class: "card" }, h("h2", { text: "First: put it on your Home Screen" }),
        h("ol", {}, h("li", {}, "Tap ", h("b", { text: "Share" }), " (the square with the arrow) in Safari."),
          h("li", {}, "Choose ", h("b", { text: "Add to Home Screen" }), "."),
          h("li", {}, "Open ", h("b", { text: "Companion" }), " from your Home Screen and pair there.")),
        h("p", { class: "muted", text: "Only the Home Screen app can send you raid alarms – and it keeps its own pairing, so pair inside it." })) : null,
      h("div", { class: "card" }, h("h2", { text: "Pair this phone" }),
        h("p", { class: "muted", text: "On the server's Discord type /companion – you get a code that works once, for 15 minutes." }),
        problem ? h("p", { class: "warn-text", text: problem }) : null, code,
        h("div", { style: "height:10px" }), h("button", { class: "btn primary wide", text: "Pair", onclick: go })));
    if (code.value && (standalone || !ios)) go();
  }

  // ---------------------------------------------------------------- loading

  async function load(quiet) {
    try {
      const [a, c] = await Promise.all([api("account"), api("chat?since=" + lastChat)]);
      acc = a;
      takeChat(c.lines);
      if (!typing()) draw();
      if (tab === "map") loadMap();
    } catch (e) { if (!quiet) toast(e.message); }
  }

  function takeChat(lines) {
    if (!lines || !lines.length) return false;
    for (const l of lines) { chat.push(l); lastChat = Math.max(lastChat, l.id); }
    chat = chat.slice(-200);
    return true;
  }

  async function loadMap() {
    try {
      map = await api("map");
      if (map.image && map.image !== mapImgVersion) {
        const r = await fetch("mapimage", { headers: { Authorization: "Bearer " + token } });
        if (r.ok) {
          const img = new Image();
          img.src = URL.createObjectURL(await r.blob());
          await img.decode().catch(() => {});
          mapImg = img;
          mapImgVersion = map.image;
        }
      } else if (!map.image) { mapImg = null; mapImgVersion = ""; }
      if (tab === "map") draw();
    } catch (e) { toast(e.message); }
  }

  async function poll() {
    if (document.hidden || !token) return;
    rounds++;
    try {
      const c = await api("chat?since=" + lastChat);
      const fresh = takeChat(c.lines);
      if (rounds % 6 === 0) { acc = await api("account"); if (tab === "map") await loadMap(); else if (tab !== "chat" && !typing()) draw(); }
      if (tab === "chat" && fresh) chatLines();
    } catch (e) { /* the next round tries again */ }
  }

  function start() {
    tabs.hidden = false;
    load();
    stop();
    timer = setInterval(() => { poll(); tick(); }, 5000);
    setInterval(tick, 1000);
  }
  function stop() { clearInterval(timer); }
  const typing = () => document.activeElement && document.activeElement.tagName === "INPUT" && document.activeElement.type === "text";

  async function post(path, body, loud) {
    try {
      const out = await api(path, body);
      if (out.message) loud ? alert(out.message) : toast(out.message);
    } catch (e) { toast(e.message); }
    load(true);
  }

  // ---------------------------------------------------------------- helpers

  const until = (at) => {
    let left = at - Date.now() / 1000;
    if (left <= 0) return "now";
    const d = Math.floor(left / 86400), hh = Math.floor(left % 86400 / 3600), m = Math.floor(left % 3600 / 60), s = Math.floor(left % 60);
    return d ? `${d} d ${hh} h` : hh ? `${hh} h ${m} min` : m ? `${m} min ${s} s` : `${s} s`;
  };
  const clock = (at, long) => new Date(at * 1000).toLocaleString([], long ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
  const hours = (sec) => { const hh = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60); return hh >= 48 ? `${Math.floor(hh / 24)}d ${hh % 24}h` : hh ? `${hh}h ${m}m` : `${m}m`; };
  const EARTH = ["#8c3b2a", "#5e6b3a", "#4f6470", "#8a6a2f", "#6b4a5e", "#3f6b5e"];
  function badge(name) {
    let n = 0;
    for (const ch of String(name).toLowerCase()) n = (n * 31 + ch.charCodeAt(0)) | 0;
    const letter = (String(name).match(/[A-Za-z0-9]/) || ["?"])[0].toUpperCase();
    return h("div", { class: "badge", style: "background:" + EARTH[Math.abs(n) % EARTH.length], text: letter });
  }
  const tile = (value, name, color) => h("div", { class: "tile" }, h("b", { text: value, style: (color ? "color:var(--" + color + ");" : "") + (String(value).length > 7 ? "font-size:15px;line-height:26px" : "") }), h("span", { text: name }));
  const card = (title, ...kids) => h("div", { class: "card" }, h("h2", { text: title }), ...kids);
  const pill = (text, kind) => h("span", { class: "pill " + (kind || ""), text });
  const row = (name, sub, right) => h("div", { class: "row" }, badge(name), h("div", { class: "grow" }, h("b", { text: name }), sub ? h("small", { text: sub }) : null), right);
  function toggle(title, sub, on, change) {
    const box = h("input", { type: "checkbox", onchange: () => change(box.checked) });
    box.checked = !!on;
    return h("div", { class: "row" }, h("div", { class: "grow" }, h("b", { text: title }), sub ? h("small", { text: sub }) : null), h("label", { class: "switch" }, box, h("i")));
  }
  function tick() { for (const el of document.querySelectorAll("[data-until]")) el.textContent = until(+el.dataset.until); }

  // ---------------------------------------------------------------- tabs

  function draw() {
    if (!acc) return;
    const st = acc.status || {}, up = !!st.online;
    $("#title").textContent = acc.server || "Companion";
    $("#sub").textContent = up ? `${st.players} / ${st.max_players} players online` : "Server offline";
    for (const b of tabs.children) b.className = b.dataset.tab === tab ? "on" : "";
    ({ hub, team, chat: chatTab, map: mapTab, alarms })[tab](st, up);
  }

  function hub(st, up) {
    const wipe = st.next_wipe && st.next_wipe > Date.now() / 1000;
    const wipeTile = tile(wipe ? until(st.next_wipe) : "–", "Next wipe", "warn");
    if (wipe) wipeTile.firstChild.dataset.until = st.next_wipe;
    const kits = (acc.kits || []).map((k) => {
      let right;
      if (k.ready) right = h("button", { class: "btn primary", text: "Claim", onclick: () => post("claim", { kit: k.kit }, true) });
      else { right = pill(until(k.ready_at), "warn"); right.dataset.until = k.ready_at; }
      return row(k.kit, `every ${k.cooldown} · last ${k.last}`, right);
    });
    const s = acc.stats;
    main.replaceChildren(
      card("Server", h("div", { class: "tiles" }, tile(up ? `${st.players}/${st.max_players}` : "–", "Players", up ? "ok" : "bad"),
        tile(up ? String(st.queued || 0) : "–", "Queue"), tile(up && st.game_time ? st.game_time : "–", "Game time"),
        tile(up && st.map ? st.map : "–", "Map"), tile(up && st.uptime ? hours(st.uptime) : "–", "Uptime"), wipeTile),
        up && st.max_players ? h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, 100 * st.players / st.max_players)}%` })) : null),
      card("You", acc.player ? row(acc.player, acc.player_online ? "In game now" : "Not in game", pill(acc.player_online ? "online" : "offline", acc.player_online ? "ok" : ""))
          : h("p", { class: "warn-text", text: "Not linked yet – use /link on the server's Discord." }),
        s ? h("div", { class: "tiles four", style: "margin-top:8px" }, tile(String(s.kills), "Kills"), tile(String(s.deaths), "Deaths"), tile(String(s.kd), "K/D", "accent"), tile(Math.floor(s.playtime / 3600) + "h", "Played")) : null),
      card("Kits", kits.length ? kits : h("p", { class: "muted", text: "No kits for you on this server yet." }),
        (acc.owned_kits || []).length ? h("p", { class: "muted", text: "Your own kits: " + acc.owned_kits.join(", ") }) : null),
      card("Events", (acc.events || []).length ? acc.events.slice(-6).reverse().map((e) => h("div", { class: "row" }, h("div", { class: "grow", text: e.name }), h("small", { text: clock(e.at, true) })))
        : h("p", { class: "muted", text: "Nothing yet – cargo ship, helicopter and other events show up here." })),
      acc.website ? h("a", { class: "btn wide", href: acc.website + "/", text: "Website and store" }) : null);
  }

  function team() {
    const t = acc.team, p = acc.protection;
    const out = [];
    if (!t) out.push(h("div", { class: "card note" }, h("h3", { text: "No team" }), h("p", { text: acc.player ? "Create or join a team in game – your team mates show up here." : "Link your player with /link on the server's Discord first." })));
    else out.push(card(`Team · ${t.members.filter((m) => m.online).length} of ${t.members.length} online`,
      t.members.map((m) => row(m.name, acc.player && m.name.toLowerCase() === acc.player.toLowerCase() ? "You" : "",
        h("span", {}, m.leader ? pill("leader", "warn") : null, pill(m.online ? "online" : "offline", m.online ? "ok" : ""))))));
    if (p) {
      const red = p.status === "red";
      out.push(card("Offline protection", p.team === null || p.team === undefined ? h("p", { class: "muted", text: "Only teams get a protection zone." })
        : !p.status ? h("p", { class: "warn-text", text: "No zone yet. " + (p.how || "") })
        : h("div", { class: "row" }, h("div", { class: "grow" }, h("b", { text: red ? "Your base is protected" : "Team online – raidable" })), pill(red ? "protected" : "open", red ? "ok" : "warn"))));
    }
    main.replaceChildren(...out);
  }

  function chatTab() {
    const can = acc.chat_send && acc.player;
    const input = h("input", { type: "text", maxlength: "140", placeholder: "Message to the server …", enterkeyhint: "send" });
    const send = () => { const text = input.value.trim(); if (!text) return; input.value = ""; post("chat", { text }).then(() => chatLines()); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    main.replaceChildren(card("Game chat", h("div", { id: "chat" }),
      can ? h("div", { class: "send" }, input, h("button", { class: "btn primary", text: "Send", onclick: send })) : null,
      h("p", { class: "muted", text: can ? `Goes to the global chat as "${acc.player}". Team chat can only be read here.`
        : acc.player ? "Writing from the app is switched off on this server." : "Link your player (/link) to write here." })));
    chatLines();
  }

  function chatLines() {
    const box = $("#chat");
    if (!box) return;
    box.replaceChildren(...(chat.length ? chat.map((l) => h("div", { class: "line " + (l.channel === "TEAM" ? "team" : l.channel === "SERVER" ? "server" : "") },
      h("header", {}, h("b", { text: (l.channel === "TEAM" ? "[TEAM] " : l.channel === "APP" ? "[APP] " : "") + l.player }), h("span", { text: clock(l.at) })),
      h("div", { text: l.text }))) : [h("p", { class: "muted", text: "Nothing yet – messages from the game appear here." })]));
    box.scrollTop = box.scrollHeight;
  }

  const CELL = 146.3;
  const letters = (c) => c < 26 ? String.fromCharCode(65 + c) : String.fromCharCode(64 + Math.floor(c / 26)) + String.fromCharCode(65 + c % 26);
  function square(size, x, z) {
    const max = Math.ceil(size / CELL) - 1, cl = (v) => Math.max(0, Math.min(max, v));
    return letters(cl(Math.floor((x + size / 2) / CELL))) + cl(Math.floor((size / 2 - z) / CELL));
  }

  function mapTab() {
    if (!acc.map) { main.replaceChildren(h("div", { class: "card note" }, h("h3", { text: "Map is off" }), h("p", { text: "The team map is switched off on this server." }))); return; }
    const size = map ? map.size : 4000, who = map ? map.players : [];
    const cv = h("canvas");
    const note = !map ? "Loading positions …" : !who.length ? (map.why || "Nobody of your team is in game right now.") : "";
    main.replaceChildren(card("Team map", cv, note ? h("p", { class: "warn-text", text: note }) : null,
      who.map((p) => h("div", { class: "row" }, h("div", { class: "grow" }, h("b", { text: p.name + (p.me ? " (you)" : ""), style: "color:var(--" + (p.me ? "accent" : "ok") + ")" })), pill(square(size, p.x, p.z)))),
      h("div", { style: "height:10px" }), h("button", { class: "btn wide", text: "Refresh positions", onclick: loadMap }),
      h("p", { class: "muted", text: "Shows you and your online team mates. Positions are up to 20 seconds old. "
        + (mapImg ? (map && map.credit ? map.credit + "." : "") : map && map.no_picture ? map.no_picture : "") })));
    paint(cv, size, who);
  }

  function paint(cv, size, who) {
    const css = cv.clientWidth || 340, dpr = window.devicePixelRatio || 1;
    cv.width = cv.height = Math.round(css * dpr);
    cv.style.height = css + "px";
    const c = cv.getContext("2d"), w = cv.width, pad = 18 * dpr, side = w - pad, scale = side / size, cells = Math.ceil(size / CELL);
    c.fillStyle = "#15140f";
    c.fillRect(pad, pad, side, side);
    if (mapImg && mapImg.naturalWidth) c.drawImage(mapImg, pad, pad, side, side);
    c.font = `700 ${9 * dpr}px -apple-system,sans-serif`;
    c.textAlign = "center";
    const step = cells > 20 ? 2 : 1;
    for (let i = 0; i <= cells; i++) {
      const at = pad + Math.min(side, i * CELL * scale);
      c.strokeStyle = mapImg ? "rgba(0,0,0,.25)" : "#34312b";
      c.lineWidth = 1;
      c.beginPath(); c.moveTo(at, pad); c.lineTo(at, w); c.moveTo(pad, at); c.lineTo(w, at); c.stroke();
      const mid = pad + (i + 0.5) * CELL * scale;
      if (i < cells && i % step === 0 && mid < w) { c.fillStyle = "#8d867a"; c.fillText(letters(i), mid, pad - 5 * dpr); c.fillText(String(i), pad / 2, mid + 3 * dpr); }
    }
    c.font = `700 ${12 * dpr}px -apple-system,sans-serif`;
    for (const p of who) {
      const x = Math.max(pad + 6, Math.min(w - 6, pad + (p.x + size / 2) * scale)), y = Math.max(pad + 6, Math.min(w - 6, pad + (size / 2 - p.z) * scale));
      const r = (p.me ? 7 : 6) * dpr, right = x > w * 0.75;
      c.fillStyle = "#100f0d"; c.beginPath(); c.arc(x, y, r + 2 * dpr, 0, 7); c.fill();
      c.fillStyle = p.me ? "#cb4129" : "#9dc23c"; c.beginPath(); c.arc(x, y, r, 0, 7); c.fill();
      c.textAlign = right ? "right" : "left";
      c.shadowColor = "#000"; c.shadowBlur = 4 * dpr; c.fillStyle = "#e6dcd2";
      c.fillText(p.name, x + (right ? -1 : 1) * (r + 5 * dpr), y + 4 * dpr);
      c.shadowBlur = 0;
    }
  }

  // ---------------------------------------------------------------- alarms and notifications on this phone

  const pushable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const b64key = (s) => Uint8Array.from(atob((s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (ch) => ch.charCodeAt(0));

  async function pushState() {
    if (!pushable) return "none";
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    return sub && Notification.permission === "granted" ? "on" : Notification.permission === "denied" ? "denied" : "off";
  }

  async function pushOn() {
    try {
      const reg = await navigator.serviceWorker.register("sw.js");
      await navigator.serviceWorker.ready;
      if (await Notification.requestPermission() !== "granted") { toast("Notifications weren't allowed."); return; }
      const key = (await api("push/key")).key;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64key(key) });
      const out = await api("push/subscribe", { subscription: sub.toJSON() });
      toast(out.message || "Notifications are on.");
    } catch (e) { toast(e.message || "Notifications couldn't be switched on."); }
    draw();
  }

  async function pushOff() {
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) { await api("push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      toast("Notifications are off on this phone.");
    } catch (e) { toast(e.message); }
    draw();
  }

  function alarms() {
    const raid = acc.raid, prefs = acc.prefs || {};
    const phone = card("This phone", h("p", { class: "muted", id: "push-note", text: "Checking …" }));
    const rf = h("input", { type: "number", min: "1", max: "9999", placeholder: "1 – 9999", inputmode: "numeric" });
    main.replaceChildren(phone,
      card("Raid alarm", !raid ? h("p", { class: "muted", text: "Raid alerts aren't switched on on this server." }) : [
        toggle("Alert me when I'm raided", "Notification on this phone and a Discord message", raid.dm, (on) => post("alerts", { on })),
        raid.rf_on ? h("div", {}, h("div", { class: "row" }, h("div", { class: "grow" }, h("b", { text: "RF alarm" + (raid.rf ? " · " + raid.rf : "") }), h("small", { text: "Explosion sensor → RF broadcaster in your base, set to your frequency" }))),
          h("div", { class: "send" }, rf, h("button", { class: "btn", text: "Save", onclick: () => post("rf", { frequency: rf.value }, true) }),
            raid.rf ? h("button", { class: "btn", text: "Release", onclick: () => post("rf", { release: true }, true) }) : null)) : null]),
      card("Notifications", toggle("Team mate comes online", null, prefs.team, (v) => post("prefs", { team: v })),
        toggle("I was killed", null, prefs.death, (v) => post("prefs", { death: v })),
        toggle("Server events", "Cargo ship, helicopter, air drop …", prefs.events, (v) => post("prefs", { events: v })),
        toggle("Server wiped", null, prefs.wipe, (v) => post("prefs", { wipe: v }))),
      card("Latest", (acc.alerts || []).length ? acc.alerts.slice(-10).reverse().map((a) => h("div", { class: "line " + (a.kind === "raid" || a.kind === "death" ? "bad" : "") },
        h("header", {}, h("b", { text: a.title }), h("span", { text: clock(a.at, true) })), h("div", { class: "muted", text: a.text })))
        : h("p", { class: "muted", text: "No notifications yet." })),
      h("button", { class: "btn wide", text: "Unpair this phone", onclick: async () => {
        if (!confirm("Unpair this phone from the server?")) return;
        await pushOff().catch(() => {});
        try { await api("unpair", {}); } catch (e) { /* gone already */ }
        token = ""; store.set(""); acc = null; stop(); welcome();
      } }));
    pushState().then((state) => {
      const note = $("#push-note");
      if (!note) return;
      if (state === "none") note.textContent = ios && !standalone ? "To get raid alarms on this iPhone: Share → Add to Home Screen, then open Companion from the Home Screen (iOS 16.4 or newer)."
        : "This browser can't show notifications.";
      else if (state === "denied") note.textContent = "Notifications are blocked for this app – allow them in your phone's settings.";
      else { note.textContent = state === "on" ? "Notifications arrive on this phone, also when the app is closed." : "Switch on notifications to get raid alarms on this phone.";
        note.after(h("button", { class: "btn wide " + (state === "on" ? "" : "primary"), text: state === "on" ? "Switch notifications off" : "Switch notifications on", onclick: state === "on" ? pushOff : pushOn })); }
    });
  }

  // ---------------------------------------------------------------- start

  tabs.addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; tab = b.dataset.tab; draw(); if (tab === "map") loadMap(); });
  $("#reload").addEventListener("click", () => (token ? load() : welcome()));
  document.addEventListener("visibilitychange", () => { if (!document.hidden && token) load(true); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  token ? start() : welcome();
})();
