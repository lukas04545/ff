// Runs before the first paint (loaded in <head>): saved theme and motion setting, so the page doesn't flash.
// A file instead of an inline script – the panel's Content-Security-Policy only allows scripts from the bot itself.
try { const t = localStorage.getItem("rce-theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) { /* storage blocked */ }
// Animations are off unless switched on with the ✨ button (a still neon background is much lighter)
document.documentElement.dataset.motion = "off";
try { if (localStorage.getItem("rce-motion") === "on") delete document.documentElement.dataset.motion; } catch (e) { /* storage blocked */ }
// The default look (Ember) right away – also on the login page. The account's own theme is applied after login
// (skins.js); a remembered choice avoids a flash of the wrong one.
try {
  const skin = localStorage.getItem("rce-skin") || "glass";
  if (skin === "ember" || skin === "glass") {
    document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = "light";
    // Ember's motion is light (hover lifts, short reveals): on unless switched off with ✨
    if (localStorage.getItem("rce-motion") !== "off") delete document.documentElement.dataset.motion;
  }
} catch (e) { document.documentElement.dataset.skin = "glass"; document.documentElement.dataset.theme = "light"; }
