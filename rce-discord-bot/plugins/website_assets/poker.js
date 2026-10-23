// Poker table on the player website: shows the table live (long polling – the server answers as soon as something
// changes) and sends the player's actions. The server deals and checks every move; this only shows and asks.
(() => {
  "use strict";
  const root = document.getElementById("poker");
  if (!root) return;
  const base = root.dataset.base, tableId = root.dataset.table, points = root.dataset.points || "points";
  const minBuy = Number(root.dataset.min), maxBuy = Number(root.dataset.max);
  const url = (what) => `${base}/casino/poker/${encodeURIComponent(tableId)}/${what}`;
  const felt = root.querySelector(".felt"), board = root.querySelector(".board"), potEl = root.querySelector(".pot");
  const banner = root.querySelector(".banner"), bar = document.getElementById("pbar"), logEl = document.getElementById("plog");
  // seat places around the table (left %, top %), the player's own seat always at the bottom
  const PLACES = [[50, 103], [5, 72], [13, 6], [50, -6], [87, 6], [95, 72]];
  const PLACES_PHONE = [[50, 103], [8, 70], [8, 24], [50, -7], [92, 24], [92, 70]];     // tall table: two columns
  let state = null, version = -1, barKey = "", lastHand = -1, shownBoard = 0, received = 0;

  const fmt = (n) => Number(n).toLocaleString("en-US").replace(/,/g, " ");
  const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function card(label, still) {
    if (!label) { const b = h("div", "pcard back"); if (still) b.style.animation = "none"; return b; }
    const c = h("div", "pcard" + (/[♥♦]/.test(label) ? " red" : ""), label);
    if (still) c.style.animation = "none";
    return c;
  }

  function toast(text) {
    const n = h("div", "notice bad", text);
    root.prepend(n);
    setTimeout(() => n.remove(), 4000);
  }

  function setBalance(balance) {
    const el = document.querySelector(".bal .price");
    if (el && balance !== undefined) el.textContent = `${fmt(balance)} ${points}`;
  }

  // -------------------------------------------------------- Server

  async function poll() {
    for (;;) {
      try {
        const r = await fetch(`${url("state")}?v=${version}`, { credentials: "same-origin", cache: "no-store" });
        if (r.status === 429) { await sleep(5000); continue; }
        if (!r.ok) throw new Error(String(r.status));
        render(await r.json());
      } catch (e) {
        await sleep(2000);
      }
    }
  }

  async function send(what, data = {}) {
    try {
      const r = await fetch(url(what), { method: "POST", credentials: "same-origin",
        body: new URLSearchParams(data), headers: { "Accept": "application/json" } });
      const out = await r.json();
      if (out.error) toast(out.error);
      if (out.state) render(out.state);
      setBalance(out.balance);
    } catch (e) {
      toast("Connection problem – try again.");
    }
  }

  // -------------------------------------------------------- Drawing

  function render(st) {
    state = st; version = st.version; received = performance.now();
    if (st.hand !== lastHand) { lastHand = st.hand; shownBoard = 0; }
    const me = st.you;
    const winners = new Set((st.street === "showdown" ? st.result : []).map((r) => r.seat));

    felt.querySelectorAll(".pseat").forEach((e) => e.remove());
    st.seats.forEach((s, i) => {
      const places = felt.clientWidth < 500 ? PLACES_PHONE : PLACES;
      const place = places[me === null ? i : (i - me + 6) % 6];
      const el = h("div", "pseat");
      el.style.left = place[0] + "%"; el.style.top = place[1] + "%";
      const box = h("div", "box");
      if (!s) {
        el.classList.add("empty");
        box.textContent = me === null ? "Free seat – sit here" : "Free seat";
        if (me === null) box.addEventListener("click", () => bar.querySelector("input")?.focus());
        el.append(box); felt.append(el);
        return;
      }
      if (s.turn) el.classList.add("turn");
      if (s.folded) el.classList.add("folded");
      if (s.sitting_out) el.classList.add("away");
      if (winners.has(i)) el.classList.add("win");
      const hole = h("div", "hole");
      if (s.cards) s.cards.forEach((c) => hole.append(card(c, true)));
      else for (let k = 0; k < s.card_count; k++) hole.append(card(null, true));
      const top = place[1] < 50;
      box.append(h("div", "nm", (i === me ? "🙂 " : "") + s.name), h("div", "st", fmt(s.stack)),
        h("div", "act", s.sitting_out ? "sitting out" : s.allin ? "ALL-IN" : s.action || ""));
      if (s.button) el.append(h("div", "dealer", "D"));
      if (s.turn && st.turn_left > 0) {
        const bar = h("div", "timer");
        bar.style.transform = `scaleX(${Math.min(1, st.turn_left / st.turn_time)})`;
        bar.style.transition = `transform ${st.turn_left}s linear`;
        box.append(bar);
        requestAnimationFrame(() => requestAnimationFrame(() => { bar.style.transform = "scaleX(0)"; }));
      }
      if (top) el.append(box, hole); else el.append(hole, box);
      if (s.bet) {
        const chip = h("div", "bet", fmt(s.bet));
        chip.style[top ? "top" : "bottom"] = "calc(100% + 6px)";
        el.append(chip);
      }
      felt.append(el);
    });

    board.replaceChildren(...st.board.map((c, k) => card(c, k < shownBoard)));
    shownBoard = st.board.length;
    potEl.textContent = st.pot ? `Pot ${fmt(st.pot)}` : "";
    if (st.street === "showdown" && st.result.length) {
      banner.textContent = st.result.map((r) => `${r.name} wins ${fmt(r.amount)}${r.hand ? " – " + r.hand : ""}`).join(" · ");
    } else if (st.legal) banner.textContent = "Your turn";
    else if (st.next_in === null && st.street !== "showdown" && st.street !== "idle") banner.textContent = "";
    else if (st.next_in === null) banner.textContent = me === null ? "Pick your buy-in below to sit down" : "Waiting for players …";
    else countdown();

    logEl.replaceChildren(...st.log.slice().reverse().map((line) => h("div", "", line)));
    drawBar(st);
  }

  function drawBar(st) {
    const me = st.you, seat = me === null ? null : st.seats[me];
    const key = JSON.stringify([me, st.legal, seat && seat.sitting_out, st.hand, st.street === "showdown"]);
    if (key === barKey) return;                       // don't reset a slider the player is moving
    barKey = key;
    bar.replaceChildren();
    const button = (text, cls, fn) => { const b = h("button", cls, text); b.type = "button"; b.addEventListener("click", fn); bar.append(b); return b; };

    if (me === null) {
      const amount = h("input", "amt");
      Object.assign(amount, { type: "number", min: minBuy, max: maxBuy, step: 1, value: Math.min(maxBuy, Math.max(minBuy, minBuy * 5)) });
      bar.append(h("span", "", "Buy-in"), amount);
      button("Sit down", "pay", () => send("sit", { buyin: amount.value }));
      return;
    }
    const legal = st.legal;
    if (legal) {
      button("Fold", "ghost", () => send("act", { action: "fold" }));
      if (legal.check) button("Check", "pay", () => send("act", { action: "check" }));
      else button(`Call ${fmt(legal.call)}`, "pay", () => send("act", { action: "call" }));
      if (legal.raise) {
        const range = h("input"), amount = h("input", "amt");
        Object.assign(range, { type: "range", min: legal.min_to, max: legal.max_to, step: 1, value: legal.min_to });
        Object.assign(amount, { type: "number", min: legal.min_to, max: legal.max_to, step: 1, value: legal.min_to });
        const go = button(`${legal.bet_word} to ${fmt(legal.min_to)}`, "pay", () => send("act", { action: "raise", amount: amount.value }));
        const set = (v) => {
          v = Math.max(legal.min_to, Math.min(legal.max_to, Math.round(v)));
          range.value = amount.value = v;
          go.textContent = v >= legal.max_to ? "All-in" : `${legal.bet_word} to ${fmt(v)}`;
        };
        range.addEventListener("input", () => set(range.value));
        amount.addEventListener("input", () => set(amount.value));
        const myBet = seat.bet, pot = st.pot;
        bar.append(range, amount);
        button("½ pot", "ghost", () => set(myBet + legal.call + (pot + legal.call) / 2));
        button("Pot", "ghost", () => set(myBet + legal.call + pot + legal.call));
        button("All-in", "ghost", () => set(legal.max_to));
      }
    }
    if (seat.sitting_out) button("I'm back", "pay", () => send("act", { action: "back" }));
    else if (!legal) button("Sit out", "ghost", () => send("act", { action: "away" }));
    button("Leave table", "ghost", () => {
      const inHand = st.street !== "idle" && st.street !== "showdown" && seat.in_hand && !seat.folded;
      if (!inHand || window.confirm("Leave now? Your hand is folded; the rest of your chips go back to your points.")) send("leave");
    });
  }

  // "Next hand in 5 …" – the dealer deals by the server's timer; this only shows it counting down
  function countdown() {
    if (!state || state.next_in === null || state.next_in === undefined) return;
    if (state.street === "showdown" && state.result.length && (performance.now() - received) / 1000 < 3) return;
    const left = Math.max(0, Math.ceil(state.next_in - (performance.now() - received) / 1000));
    banner.textContent = left ? `Next hand in ${left} …` : "Dealing …";
  }
  setInterval(countdown, 250);

  // keep the countdown honest when the tab was in the background
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state) render(state); });
  poll();
})();
