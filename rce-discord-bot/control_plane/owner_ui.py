"""The owner panel's look – the same "command centre" style as a server's admin panel (web/static/neon.css):
midnight navy, a sidebar with coloured menu groups, stat cards with a lime-to-teal edge, pastel gradient buttons.

Inline CSS only (the owner panel's Content-Security-Policy allows no scripts or external files).
"""
from __future__ import annotations

import html

from aiohttp import web

OWNER_CSS = """
:root{--bg:#0a0d1a;--side:#0c1020;--panel:rgba(17,22,40,.9);--panel2:rgba(28,34,58,.75);--input:rgba(9,12,24,.85);
--text:#e6e8f5;--muted:#9aa3c7;--faint:#6b7396;--border:rgba(148,163,255,.14);--border2:rgba(148,163,255,.28);
--pink:#f472b6;--violet:#a78bfa;--teal:#2dd4bf;--amber:#fbbf24;--green:#4ade80;--lime:#a3e635;--lav:#c4b5fd;
--ok:#4ade80;--warn:#fbbf24;--bad:#fb7185;--grad:linear-gradient(100deg,#f9a8d4,#c4b5fd 35%,#99f6e4 70%,#fde68a);
--edge:linear-gradient(180deg,var(--lime),var(--teal))}
*{box-sizing:border-box}html,body{margin:0}
body{font-family:Inter,"Segoe UI",system-ui,sans-serif;color:var(--text);min-height:100vh;
background:repeating-linear-gradient(90deg,rgba(148,163,255,.035) 0 1px,transparent 1px 64px),
radial-gradient(70rem 40rem at 70% -10%,rgba(99,102,241,.18),transparent 60%),
radial-gradient(50rem 35rem at 0% 100%,rgba(45,212,191,.08),transparent 60%),linear-gradient(180deg,#0b0f1f,#080b16)}
a{color:var(--lav);text-decoration:none}a:hover{color:#fff}
.app{display:grid;grid-template-columns:250px 1fr;min-height:100vh}
aside{background:var(--side);border-right:1px solid var(--border);padding:14px 12px;position:sticky;top:0;height:100vh;
overflow:auto}
.brandbox{border:1px solid var(--border2);border-radius:16px;padding:12px 14px;margin-bottom:14px;
background:linear-gradient(135deg,rgba(167,139,250,.12),rgba(45,212,191,.06))}
.brandbox small{display:block;font-size:.66rem;letter-spacing:.14em;color:var(--teal);font-weight:800}
.brandbox b{font-size:1.05rem}.brandbox span{display:block;color:var(--muted);font-size:.78rem}
.navhead{display:flex;justify-content:space-between;font-size:.66rem;letter-spacing:.14em;color:var(--teal);
font-weight:800;margin:6px 4px 8px}
.group{--tone:var(--violet);border:1px solid color-mix(in srgb,var(--tone) 55%,transparent);border-radius:16px;
padding:8px;margin-bottom:12px;background:rgba(255,255,255,.015)}
.group.pink{--tone:var(--pink)}.group.violet{--tone:var(--violet)}.group.teal{--tone:var(--teal)}
.group.amber{--tone:var(--amber)}
.group .lbl{font-size:.68rem;letter-spacing:.14em;color:var(--tone);font-weight:800;margin:2px 6px 6px;text-transform:uppercase}
.group a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:11px;color:var(--text);font-size:.93rem;
margin:2px 0;border:1px solid transparent}
.group a i{font-style:normal;width:26px;height:26px;display:grid;place-items:center;border-radius:8px;
background:rgba(255,255,255,.05);border:1px solid var(--border)}
.group a:hover{background:rgba(255,255,255,.04)}
.group a.on{border-color:color-mix(in srgb,var(--tone) 70%,transparent);background:color-mix(in srgb,var(--tone) 12%,transparent)}
main{padding:22px 26px 40px;max-width:1250px;width:100%}
.kicker{font-size:.7rem;letter-spacing:.16em;color:var(--teal);font-weight:800}
h1{margin:.1rem 0 1rem;font-size:1.7rem;color:var(--lav)}h2{margin:0 0 .7rem;font-size:1.12rem}h3{color:var(--lav)}
.card{background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:18px 20px;margin:14px 0;
box-shadow:0 10px 30px -14px rgba(0,0,0,.7)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:6px 0 4px}
.stat{position:relative;background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:14px 16px 14px 20px;
overflow:hidden}.stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--edge)}
.stat .l{font-size:.68rem;letter-spacing:.14em;color:var(--muted);font-weight:800;text-transform:uppercase}
.stat .v{font-size:1.7rem;font-weight:800;margin-top:4px}.stat .s{color:var(--muted);font-size:.8rem}
table{width:100%;border-collapse:collapse;margin:.3rem 0 .6rem}
th{font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);background:rgba(255,255,255,.03);
text-align:left;padding:.55rem .6rem}td{padding:.6rem;border-bottom:1px solid var(--border);vertical-align:top;font-size:.9rem}
input,select,textarea{background:var(--input);color:var(--text);border:1px solid var(--border2);border-radius:11px;
padding:.55rem .75rem;font:inherit;font-size:.9rem;margin:.15rem 0}input:focus,select:focus,textarea:focus{outline:2px solid
rgba(167,139,250,.45)}
button{background:var(--grad);color:#120d26;border:0;border-radius:11px;padding:.5rem .95rem;font-weight:800;cursor:pointer;
font-size:.86rem}button:hover{filter:brightness(1.08)}button.danger{background:rgba(251,113,133,.15);color:#fecdd3;
border:1px solid rgba(251,113,133,.45)}
.pill{display:inline-block;padding:.12rem .6rem;border-radius:99px;font-size:.75rem;font-weight:700;
background:rgba(148,163,255,.12);border:1px solid var(--border2)}.pill.ok{background:rgba(74,222,128,.13);color:#bbf7d0;
border-color:rgba(74,222,128,.4)}.pill.warn{background:rgba(251,191,36,.13);color:#fde68a;border-color:rgba(251,191,36,.4)}
.pill.bad{background:rgba(251,113,133,.14);color:#fecdd3;border-color:rgba(251,113,133,.45)}
.muted{color:var(--muted)}.err{color:var(--bad)}form.inline{display:inline}
code{background:rgba(6,8,18,.92);border:1px solid var(--border);padding:.1rem .35rem;border-radius:6px;color:#d7defa}
.bare main{max-width:460px;margin:8vh auto}
@media (max-width:900px){.app{grid-template-columns:1fr}aside{position:static;height:auto}main{padding:16px}}
"""

# (group label, tone, [(key, icon, label, path)])
NAV = (
    ("Control", "pink", (("overview", "📊", "Overview", "/"), ("system", "🖥️", "System", "/system"),
                         ("audit", "🧾", "Audit log", "/audit"))),
    ("Customers", "violet", (("customers", "👤", "Customers", "/customers"), ("servers", "🛰️", "Servers", "/servers"),
                             ("domains", "🌐", "Domains", "/domains"))),
    ("Shop", "teal", (("plans", "🛒", "Packages", "/plans"),)),
    ("Admin", "amber", (("settings", "⚙️", "Settings", "/settings"),)),
)
BARE = {"Owner login", "One-time login", "Link used", "Signed in", "Restarting"}
ACTIVE = {"Owner": "overview", "System": "system", "Audit": "audit", "Customers": "customers", "Servers": "servers",
          "Domains": "domains", "Packages": "plans", "Settings": "settings"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def owner_frame(title: str, body: str, *, prefix: str = "", shop: str = "") -> web.Response:
    active = ACTIVE.get(title, "customers" if title.startswith("Customer") else
                        "servers" if title.startswith("Management AI") else "")
    if title in BARE:
        inner = f"<div class=bare><main>{body}</main></div>"
    else:
        groups = "".join(
            f"<div class='group {tone}'><div class=lbl>{esc(label)}</div>"
            + "".join(f"<a href='{prefix}{path}' class='{'on' if key == active else ''}'><i>{icon}</i>{esc(text)}</a>"
                      for key, icon, text, path in items) + "</div>" for label, tone, items in NAV)
        inner = (f"<div class=app><aside><div class=brandbox><small>RCE BOT · OWNER</small><b>{esc(shop or 'Control')}"
                 f"</b><span>Every customer and server</span></div><div class=navhead><span>Navigation</span>"
                 f"<span>Owner</span></div>{groups}<form method=post action='{prefix}/logout'><button class=danger "
                 f"style='width:100%'>Sign out</button></form></aside><main>{body}</main></div>")
    return web.Response(
        text=f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
             f"content='width=device-width,initial-scale=1'><title>{esc(title)} · Owner</title><style>{OWNER_CSS}"
             f"</style></head><body>{inner}</body></html>", content_type="text/html",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
                                            "https://checkout.stripe.com https://billing.stripe.com; "
                                            "frame-ancestors 'none'"})


def stat(label: str, value, sub: str = "") -> str:
    return (f"<div class=stat><div class=l>{esc(label)}</div><div class=v>{esc(value)}</div>"
            f"{f'<div class=s>{esc(sub)}</div>' if sub else ''}</div>")
