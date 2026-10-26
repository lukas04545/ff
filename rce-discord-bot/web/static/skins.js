// Panel themes the owner picks per account (Users → Panel themes). app.js calls RCE_SKINS.apply(me.skin) after login.
// neon: nothing to do. retro: an early-2000s home page – marquee, "under construction", visitor counter, sparkle
// trail and 20 popup ads (in-page windows, no real popups or links). jumpscare: a dark horror look with a scary face
// and a scream now and then (synthesised with WebAudio – no files), red eyes, whispers. No fast flashing.
(function () {
  "use strict";
  const rand = (a, b) => a + Math.random() * (b - a);
  const pick = (list) => list[Math.floor(Math.random() * list.length)];
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  };

  // ------------------------------------------------------------------ Early 2000s
  const ADS = [
    ["🎉", "CONGRATULATIONS!!!", "You are the 1,000,000th visitor! Claim your PRIZE now!!!", "CLAIM PRIZE"],
    ["💾", "DOWNLOAD MORE RAM", "100% FREE!!! Makes your server 1000x faster!!!", "DOWNLOAD"],
    ["⚠️", "WARNING!!! Your PC is SLOW", "1,337 errors found. Scan now for FREE!", "SCAN NOW"],
    ["🐵", "PUNCH THE MONKEY", "Hit the monkey and WIN $20!!!", "PUNCH!!!"],
    ["😎", "FREE SMILEYS!!!", "Over 9000 smileys for your e-mail!", "GET THEM"],
    ["💰", "Make $$$ from HOME", "Rust admins HATE this one weird trick!", "LEARN MORE"],
    ["🔥", "HOT SCRAP in YOUR AREA", "Lonely scrap piles want to meet you!!!", "MEET NOW"],
    ["📞", "Cheap RINGTONES", "Text RUST to 8888 for a polyphonic AK sound!", "TEXT NOW"],
    ["🖱️", "Your mouse is DIRTY!", "Click here to clean it!!!", "CLEAN IT"],
    ["🎰", "ONLINE CASINO", "500% BONUS on your first deposit!!!", "PLAY NOW"],
    ["📧", "1 NEW MESSAGE", "A prince needs your help moving 10,000 scrap!", "READ IT"],
    ["🛢️", "Low on low grade?", "Doctors HATE him! Find out why!", "FIND OUT"],
    ["🐹", "FREE SCREENSAVER", "Dancing hamsters!!! Totally virus free!!!", "INSTALL"],
    ["📖", "Sign my GUESTBOOK", "Pleeeease!!! I need 10 entries!!!", "SIGN IT"],
    ["🛳️", "You have been SELECTED", "A FREE cruise is waiting for you!", "ACCEPT"],
    ["🧲", "Lose 20 lbs of SULFUR", "...in only 2 days! Guaranteed!", "TELL ME"],
    ["🐸", "FREE TOOLBAR!!!", "Makes the internet 300% FASTER!!!", "ADD TOOLBAR"],
    ["💌", "Someone has a CRUSH on you", "Find out who!!! (it's the server)", "WHO???"],
    ["📡", "Dial-up too SLOW?", "Get 56k NOW and download a JPEG in 3 minutes!", "UPGRADE"],
    ["🐻", "HOT SINGLE BEARS", "...near your base!!! Click to see!", "SEE BEARS"],
  ];
  const KIDDING = ["Just kidding 😜", "Nice try 😂", "Nothing happened. Or did it?", "Error 404: prize not found",
    "Your prize: absolutely nothing 🎁", "lol"];
  let zTop = 2147482000;

  function popupAd(ad, i, gap) {
    const [icon, headline, text, button] = ad;
    const win = el("div", `retro-ad ${pick(["", "flashy", "hot", "cool"])}`);
    const bar = el("div", "bar");
    bar.append(el("span", "", pick(["Special Offer!!!", "WINNER!!!", "Important Message", "FREE!!!", "ATTENTION"])));
    const close = el("button", "", "×");
    close.type = "button";
    close.title = "Close";
    close.addEventListener("click", () => win.remove());
    bar.append(close);
    const body = el("div", "body");
    body.append(el("span", "big retro-rainbow", icon), el("span", "headline retro-blink", headline), el("div", "", text));
    const go = el("span", "go", button);
    go.addEventListener("click", () => { body.replaceChildren(el("span", "big", "🤡"), el("span", "headline", pick(KIDDING))); });
    body.append(el("br"), go);
    win.append(bar, body);
    const w = Math.max(window.innerWidth - 290, 10), h = Math.max(window.innerHeight - 220, 10);
    win.style.left = `${Math.round(rand(10, w))}px`;
    win.style.top = `${Math.round(rand(10, h))}px`;
    win.style.zIndex = String(++zTop);
    win.addEventListener("mousedown", () => { win.style.zIndex = String(++zTop); });
    // drag by the title bar
    bar.addEventListener("pointerdown", (e) => {
      if (e.target === close) return;
      const sx = e.clientX - win.offsetLeft, sy = e.clientY - win.offsetTop;
      const move = (ev) => { win.style.left = `${ev.clientX - sx}px`; win.style.top = `${ev.clientY - sy}px`; };
      const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); };
      addEventListener("pointermove", move);
      addEventListener("pointerup", up);
    });
    setTimeout(() => document.body.append(win), i * gap);
  }

  function retro(options) {
    const content = document.querySelector(".content") || document.body;
    const marquee = el("div", "retro-marquee");
    marquee.append(el("span", "", "★·.·´¯`·.·★ WELCOME 2 MY RUST SERVER CONTROL PANEL!!! ★·.·´¯`·.·★   " +
      "SIGN MY GUESTBOOK!!!   ★   THIS SITE IS BEST VIEWED AT 800x600   ★   NO HOTLINKING!!!   ★   " +
      "U R VISITOR #" + String(Math.floor(rand(1000, 99999))) + "   ★"));
    content.prepend(marquee);
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) {
      const banner = el("div", "retro-construction");
      banner.append(el("b", "", "🚧 UNDER CONSTRUCTION 🚧"));
      sidebar.insertBefore(banner, sidebar.children[1] || null);
    }
    const footer = el("div", "retro-footer");
    let count = 13370 + Math.floor(rand(0, 5000));
    try { count = Number(localStorage.getItem("rce-retro-count") || count) + 1; localStorage.setItem("rce-retro-count", String(count)); } catch { /* storage blocked */ }
    footer.append(el("div", "retro-blink", "✨ You are visitor number ✨"), el("span", "counter", String(count).padStart(7, "0")),
      el("div", "", "© 2003 My Rust Server · Made with Notepad · Webring: ‹ prev | random | next ›"));
    content.append(footer);
    // sparkle trail
    let last = 0;
    addEventListener("pointermove", (e) => {
      const now = Date.now();
      if (now - last < 60) return;
      last = now;
      const s = el("span", "retro-spark", pick(["✨", "⭐", "💫", "✦"]));
      s.style.left = `${e.clientX + 6}px`;
      s.style.top = `${e.clientY + 6}px`;
      document.body.append(s);
      requestAnimationFrame(() => { s.style.opacity = "0"; s.style.transform = "translateY(18px)"; });
      setTimeout(() => s.remove(), 850);
    });
    // 20 popup ads after login, a couple more every few minutes
    // popup ads: how many and how often – the owner sets it (Users → Panel themes)
    const o = { popups_start: 20, popups_every: 3, popups_wave: 2, popups_max: 6, ...options };
    const gap = Math.min(450, 9000 / Math.max(o.popups_start, 1));
    for (let i = 0; i < o.popups_start; i++) popupAd(ADS[i % ADS.length], i, gap);
    if (o.popups_every > 0) setInterval(() => {
      const room = o.popups_max - document.querySelectorAll(".retro-ad").length;
      for (let i = 0; i < Math.min(o.popups_wave, room); i++) popupAd(pick(ADS), i, 450);
    }, o.popups_every * 60 * 1000);
  }

  // ------------------------------------------------------------------ Jump scares
  const FACE = `<svg viewBox="0 0 200 240" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
<defs><radialGradient id="sk" cx="50%" cy="38%" r="65%"><stop offset="0" stop-color="#e2dccd"/><stop offset=".6" stop-color="#8d8577"/><stop offset="1" stop-color="#1c1814"/></radialGradient>
<radialGradient id="ey" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#ff2a2a"/><stop offset=".25" stop-color="#600"/><stop offset=".5" stop-color="#000"/></radialGradient></defs>
<ellipse cx="100" cy="112" rx="82" ry="108" fill="url(#sk)"/>
<path d="M30 70 Q60 40 88 74 Q60 70 30 70Z M170 70 Q140 40 112 74 Q140 70 170 70Z" fill="#1a1410"/>
<ellipse cx="66" cy="98" rx="24" ry="30" fill="url(#ey)"/><ellipse cx="134" cy="98" rx="24" ry="30" fill="url(#ey)"/>
<path d="M94 118 L100 146 L106 118 Q100 128 94 118Z" fill="#2a2018"/>
<path d="M48 168 Q100 252 152 168 Q100 196 48 168Z" fill="#120000"/>
<path d="M54 172 L62 188 L70 178 L78 196 L86 182 L94 202 L100 184 L106 202 L114 182 L122 196 L130 178 L138 188 L146 172 Q100 192 54 172Z" fill="#e8e0cc"/>
<path d="M70 197 L78 186 L86 202 L94 190 L100 208 L106 190 L114 202 L122 186 L130 197 Q100 214 70 197Z" fill="#d8ceb8"/>
<path d="M20 130 Q10 180 40 210 M180 130 Q190 180 160 210" stroke="#3a0000" stroke-width="4" fill="none"/>
<path d="M60 24 L66 60 M140 20 L132 58 M100 8 L100 40" stroke="#5a0000" stroke-width="3"/></svg>`;
  let audio = null;
  let lastScare = 0;

  function scream() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
      const t = audio.currentTime, len = 1.4;
      const master = audio.createGain();
      master.gain.setValueAtTime(0.0001, t);
      master.gain.exponentialRampToValueAtTime(0.7, t + 0.03);
      master.gain.exponentialRampToValueAtTime(0.0001, t + len);
      master.connect(audio.destination);
      // noise burst (breath / static)
      const buf = audio.createBuffer(1, audio.sampleRate * len, audio.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
      const noise = audio.createBufferSource();
      noise.buffer = buf;
      const band = audio.createBiquadFilter();
      band.type = "bandpass"; band.frequency.value = 1800; band.Q.value = 0.8;
      noise.connect(band).connect(master);
      noise.start(t);
      // screeching voices sliding down
      for (const [f0, f1] of [[1250, 380], [930, 260], [1650, 520]]) {
        const o = audio.createOscillator();
        o.type = "sawtooth";
        o.frequency.setValueAtTime(f0, t);
        o.frequency.exponentialRampToValueAtTime(f1, t + len);
        const vib = audio.createOscillator(), depth = audio.createGain();
        vib.frequency.value = 18; depth.gain.value = 40;
        vib.connect(depth).connect(o.frequency);
        const g = audio.createGain();
        g.gain.value = 0.22;
        o.connect(g).connect(master);
        o.start(t); vib.start(t); o.stop(t + len); vib.stop(t + len);
      }
    } catch { /* no audio */ }
  }

  function scare() {
    if (document.querySelector(".scare-overlay") || document.hidden) return;
    lastScare = Date.now();
    const overlay = el("div", "scare-overlay");
    overlay.innerHTML = FACE;
    document.body.append(overlay);
    scream();
    const face = overlay.firstElementChild;
    // Web Animations: runs even with the panel's "animations off" setting
    face.animate([{ transform: "scale(.15)", filter: "brightness(.4)" }, { transform: "scale(1.15)", filter: "brightness(1)" },
      { transform: "scale(1.05) translate(-6px, 4px)" }, { transform: "scale(1.12) translate(6px, -4px)" },
      { transform: "scale(1.08)" }], { duration: 900, easing: "ease-out", fill: "forwards" });
    overlay.animate([{ backgroundColor: "#300" }, { backgroundColor: "#000" }], { duration: 600, fill: "forwards" });
    setTimeout(() => {
      overlay.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 400, fill: "forwards" }).onfinish = () => overlay.remove();
    }, 1300);
  }

  function whisper() {
    const w = el("div", "scare-whisper", pick(["behind you…", "I see you", "don't look back", "you're not alone",
      "it's in the walls", "close the door…", "they're online"]));
    document.body.append(w);
    requestAnimationFrame(() => w.classList.add("on"));
    setTimeout(() => w.classList.remove("on"), 2600);
    setTimeout(() => w.remove(), 4500);
  }

  function jumpscare() {
    const eyes = el("div", "scare-eyes");
    eyes.append(el("i"), el("i"));
    document.body.append(eyes);
    setInterval(() => { eyes.classList.add("on"); setTimeout(() => eyes.classList.remove("on"), 5000); }, 47000);
    // sound needs one click on the page first (browser rule) – the first scare waits for it
    let armed = false;
    const arm = () => {
      if (armed) return;
      armed = true;
      try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch { /* no audio */ }
      const next = () => setTimeout(() => { scare(); next(); }, rand(90, 300) * 1000);
      setTimeout(() => { scare(); next(); }, rand(25, 70) * 1000);
    };
    addEventListener("pointerdown", arm, { once: true });
    addEventListener("keydown", arm, { once: true });
    // now and then on a menu click
    document.addEventListener("click", (e) => {
      if (armed && e.target.closest("#nav a") && Date.now() - lastScare > 40000 && Math.random() < 0.08) setTimeout(scare, 700);
    });
    setInterval(() => { if (Math.random() < 0.5) whisper(); }, 75000);
  }

  window.RCE_SKINS = {
    applied: null,
    apply(skin, options) {
      if (this.applied || !skin || skin === "neon") return;
      this.applied = skin;
      const root = document.documentElement;
      root.dataset.skin = skin;
      root.dataset.theme = "light";       // the skins bring their own colours – no neon/dark layer underneath
      delete root.dataset.motion;          // they need their animations
      const toggle = document.getElementById("theme");
      if (toggle) toggle.hidden = true;
      if (skin === "retro") retro(options || {});
      if (skin === "jumpscare") jumpscare();
    },
  };
})();
