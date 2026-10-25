// Runs before the first paint (loaded in <head>): saved theme and motion setting, so the page doesn't flash.
// A file instead of an inline script – the panel's Content-Security-Policy only allows scripts from the bot itself.
try { const t = localStorage.getItem("rce-theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) { /* storage blocked */ }
// Animations are off unless switched on with the ✨ button (a still neon background is much lighter)
document.documentElement.dataset.motion = "off";
try { if (localStorage.getItem("rce-motion") === "on") delete document.documentElement.dataset.motion; } catch (e) { /* storage blocked */ }
