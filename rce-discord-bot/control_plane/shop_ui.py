"""The shop's look: page frame, package cards and the set-up steps of the customer dashboard (portal only).

Plain HTML + inline CSS (the portal's Content-Security-Policy allows no scripts, fonts or images from elsewhere).
"""
from __future__ import annotations

import html

from aiohttp import web

from control_plane import plans

SHOP_CSS = """
:root{--bg:#0b0d12;--panel:#141821;--panel2:#1a1f2b;--line:#262d3b;--text:#eef1f6;--muted:#9aa4b5;--acc:#ff8a1f;
--acc2:#ffb347;--ok:#2ecc71;--bad:#ff5d5d}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);
background:radial-gradient(1200px 600px at 80% -10%,rgba(255,138,31,.18),transparent 60%),
radial-gradient(900px 500px at -10% 10%,rgba(255,179,71,.08),transparent 60%),var(--bg);min-height:100vh}
a{color:var(--acc2);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px}
nav{display:flex;align-items:center;justify-content:space-between;padding:18px 0}
.brand{font-weight:800;font-size:1.25rem;letter-spacing:.3px;color:var(--text)}.brand span{color:var(--acc)}
.navlinks a,.navlinks form{margin-left:14px}
.btn,button{display:inline-block;border:0;border-radius:12px;padding:.7rem 1.15rem;font-weight:700;font-size:.95rem;
cursor:pointer;background:linear-gradient(135deg,var(--acc),var(--acc2));color:#1a0f00;text-decoration:none}
.btn:hover,button:hover{filter:brightness(1.08);text-decoration:none}
.btn.ghost,button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
button.danger{background:#3a1717;color:#ffb4b4;border:1px solid #5a2323}
.small{padding:.45rem .8rem;font-size:.85rem;border-radius:10px}
.hero{padding:56px 0 30px;text-align:center}.hero h1{font-size:clamp(2rem,5vw,3.3rem);margin:.2em 0;line-height:1.1}
.hero h1 em{font-style:normal;background:linear-gradient(135deg,var(--acc),var(--acc2));-webkit-background-clip:text;
background-clip:text;color:transparent}.hero p{color:var(--muted);font-size:1.15rem;max-width:720px;margin:0 auto 26px}
.feats{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:26px 0 10px}
.feat{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px}
.feat b{display:block;margin-bottom:4px}.feat span{color:var(--muted);font-size:.92rem}
h2.section{text-align:center;font-size:1.9rem;margin:46px 0 6px}p.section{text-align:center;color:var(--muted);margin:0 0 26px}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px;align-items:stretch}
.plan{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:26px 22px;
display:flex;flex-direction:column}
.plan.hot{border:2px solid var(--acc);box-shadow:0 0 0 6px rgba(255,138,31,.08),0 20px 50px rgba(0,0,0,.4);
transform:translateY(-6px)}
.badge{position:absolute;top:-13px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--acc),var(--acc2));
color:#1a0f00;font-weight:800;font-size:.75rem;padding:.3rem .8rem;border-radius:99px;letter-spacing:.5px;white-space:nowrap}
.plan h3{margin:0;font-size:1.35rem}.plan .tag{color:var(--muted);min-height:2.6em;margin:.35rem 0 1rem}
.price{font-size:2.5rem;font-weight:800}.price small{font-size:1rem;color:var(--muted);font-weight:500}
.plan ul{list-style:none;padding:0;margin:18px 0 22px;flex:1}.plan li{padding:.38rem 0 .38rem 1.6rem;position:relative}
.plan li:before{content:"✓";position:absolute;left:0;color:var(--acc);font-weight:900}
.plan .btn,.plan button{width:100%;text-align:center}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px;margin:18px 0}
.card h2{margin-top:0}.muted{color:var(--muted)}.err{color:var(--bad)}.okc{color:var(--ok)}
.flash{background:var(--panel2);border:1px solid var(--acc);border-radius:14px;padding:12px 16px;margin:16px 0}
input,select,textarea{width:100%;padding:.75rem .9rem;border-radius:12px;border:1px solid var(--line);
background:#0e1117;color:var(--text);font-size:1rem;margin:.3rem 0 .8rem}
.auth{max-width:440px;margin:30px auto}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.row input{flex:1;min-width:180px;margin:0}
.steps{display:grid;gap:12px;counter-reset:s}.step{display:flex;gap:14px;background:var(--panel2);border:1px solid var(--line);
border-radius:16px;padding:16px}.step .n{flex:0 0 36px;height:36px;border-radius:50%;display:grid;place-items:center;
font-weight:800;background:#262d3b}.step.done .n{background:var(--ok);color:#06210f}.step.now{border-color:var(--acc)}
.step.now .n{background:var(--acc);color:#1a0f00}.step h4{margin:.1rem 0 .3rem}.step p{margin:.2rem 0;color:var(--muted)}
.step form{margin-top:.6rem}
table{width:100%;border-collapse:collapse}td,th{padding:.6rem .5rem;border-bottom:1px solid var(--line);text-align:left;
vertical-align:top;font-size:.95rem}
.pill{display:inline-block;padding:.12rem .55rem;border-radius:99px;font-size:.78rem;background:#262d3b}
.pill.ok{background:#14532d;color:#bbf7d0}.pill.bad{background:#5a1d1d;color:#fecaca}.pill.warn{background:#5a4210;color:#fde68a}
code{background:#0e1117;border:1px solid var(--line);padding:.15rem .4rem;border-radius:6px}
footer{color:var(--muted);text-align:center;padding:40px 0 30px;font-size:.85rem}
form.inline{display:inline}
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def shop_page(shop: str, title: str, body: str, *, nav: str = "") -> web.Response:
    """A page of the shop (portal) – same security headers as the rest of the control plane."""
    name = esc(shop)
    words = name.split(" ", 1)
    brand = f"{words[0]}<span>{(' ' + words[1]) if len(words) > 1 else ''}</span>"
    return web.Response(
        text=f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
             f"content='width=device-width,initial-scale=1'><title>{esc(title)} · {name}</title><style>{SHOP_CSS}</style>"
             f"</head><body><div class=wrap><nav><a class=brand href=/>{brand}</a><div class=navlinks>{nav}</div></nav>"
             f"{body}<footer>{name} · Discord bot for Rust Console Edition servers</footer></div></body></html>",
        content_type="text/html",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
                                            "https://checkout.stripe.com https://billing.stripe.com; "
                                            "frame-ancestors 'none'"})


def plan_cards(items: list[dict], *, action) -> str:
    """The package cards. action(plan) -> the HTML of its button/form."""
    out = []
    for p in items:
        perks = "".join(f"<li>{esc(line)}</li>" for line in p["perks"].splitlines() if line.strip())
        out.append(f"""<div class="plan{' hot' if p['highlight'] else ''}">{'<div class=badge>MOST POPULAR</div>' if p['highlight'] else ''}
<h3>{esc(p['name'])}</h3><div class=tag>{esc(p['tagline'])}</div>
<div class=price>{esc(plans.price_text(p))}<small> / month</small></div><ul>{perks}</ul>{action(p)}</div>""")
    return f"<div class=plans>{''.join(out)}</div>"


def step(n: int, state: str, title: str, text: str, extra: str = "") -> str:
    """state: done | now | later"""
    mark = "✓" if state == "done" else str(n)
    return (f"<div class='step {state}'><div class=n>{mark}</div><div><h4>{title}</h4><p>{text}</p>"
            f"{extra if state == 'now' else ''}</div></div>")
