// Passkeys (WebAuthn) in the browser – shared by the control panel and the owner panel (web/passkeys.py checks
// the answers). The server sends binary fields as base64url; this turns them into bytes and back.
(function () {
  "use strict";
  const toBytes = (s) => {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  };
  const toB64u = (buf) => {
    let s = "";
    new Uint8Array(buf).forEach((b) => { s += String.fromCharCode(b); });
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const supported = () => !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);
  async function create(o) {
    const publicKey = { ...o, challenge: toBytes(o.challenge), user: { ...o.user, id: toBytes(o.user.id) },
      excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })) };
    const cred = await navigator.credentials.create({ publicKey });
    return { id: cred.id, type: cred.type, response: {
      clientDataJSON: toB64u(cred.response.clientDataJSON), attestationObject: toB64u(cred.response.attestationObject) } };
  }
  async function get(o) {
    const publicKey = { ...o, challenge: toBytes(o.challenge),
      allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })) };
    const cred = await navigator.credentials.get({ publicKey });
    return { id: cred.id, type: cred.type, response: {
      clientDataJSON: toB64u(cred.response.clientDataJSON), authenticatorData: toB64u(cred.response.authenticatorData),
      signature: toB64u(cred.response.signature),
      userHandle: cred.response.userHandle ? toB64u(cred.response.userHandle) : null } };
  }
  const friendly = (e) => (e && e.name === "NotAllowedError") ? "Cancelled or timed out."
    : (e && e.name === "InvalidStateError") ? "This device already has a passkey for this account."
    : (e && e.message) || String(e);
  window.RCE_PASSKEY = { supported, create, get, friendly };

  // Owner panel: forms marked data-passkey="login" / "register" (no inline scripts there – CSP)
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-passkey]").forEach((btn) => {
      if (!supported()) { btn.disabled = true; btn.title = "This browser or address (needs https) can't use passkeys."; return; }
      btn.addEventListener("click", async (ev) => {
        ev.preventDefault();
        const form = btn.closest("form");
        const out = form.querySelector(".passkey-msg");
        const say = (t) => { if (out) out.textContent = t; };
        try {
          const csrf = form.querySelector("input[name=csrf]");
          const base = btn.dataset.passkeyUrl;
          const body = new URLSearchParams();
          if (csrf) body.set("csrf", csrf.value);
          const r = await fetch(base + "/options", { method: "POST", body, credentials: "same-origin" });
          const opts = await r.json();
          if (!r.ok) throw new Error(opts.error || "Not possible right now.");
          const answer = btn.dataset.passkey === "register" ? await create(opts.publicKey) : await get(opts.publicKey);
          const send = new URLSearchParams();
          if (csrf) send.set("csrf", csrf.value);
          send.set("challenge_id", opts.challenge_id);
          send.set("answer", JSON.stringify(answer));
          const nameInput = form.querySelector("input[name=passkey_name]");
          if (nameInput) send.set("name", nameInput.value);
          const done = await fetch(base, { method: "POST", body: send, credentials: "same-origin" });
          const res = await done.json();
          if (!done.ok) throw new Error(res.error || "Didn't work.");
          location.href = res.next || location.href;
        } catch (e) { say("⚠️ " + friendly(e)); }
      });
    });
  });
})();
