"""Store pages of the player website (/play/store …): kits for sale, build your own kit, gift cards, checkout.

Pure server-rendered HTML forms – no scripts. Prices are always calculated by the bot (cogs/webshop.py); the
browser only sends what was picked. Checkout redirects to PayPal; PayPal sends the buyer back to /store/return.
"""
from __future__ import annotations

import html
import time
from datetime import datetime
from urllib.parse import quote

from aiohttp import web

from rce import items as itemlist
from support import pricing

PAYPAL_HOSTS = "https://www.paypal.com https://www.sandbox.paypal.com"
STORE_CSS = """
.store-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px;margin-bottom:14px}
.store-grid .card{margin:0;display:flex;flex-direction:column}.store-grid form{margin-top:auto}
.field{display:flex;flex-direction:column;gap:4px;margin:10px 0 0}.field span{font-size:.82rem;color:var(--muted)}
.field input,.field select{padding:10px 12px;border:1px solid var(--line);background:rgba(0,0,0,.45);color:var(--text);
font:inherit;outline:none}.field input:focus,.field select:focus{border-color:var(--orange)}
button.pay{margin-top:14px;width:100%;padding:12px 16px;border:0;background:var(--grad);color:#fff;cursor:pointer;
font-family:var(--head);font-size:1.15rem;letter-spacing:.06em;text-transform:uppercase;
clip-path:polygon(10px 0,100% 0,calc(100% - 10px) 100%,0 100%)}button.pay:hover{filter:brightness(1.1)}
button.ghost{padding:9px 16px;border:1px solid var(--line);background:var(--card2);color:var(--text);cursor:pointer;
font-family:var(--head);font-size:1rem;letter-spacing:.06em;text-transform:uppercase}
.notice{padding:12px 16px;margin-bottom:14px;border-left:4px solid var(--orange);background:rgba(247,147,30,.1)}
.notice.bad{border-color:var(--bad);background:rgba(229,52,28,.12)}.notice.ok{border-color:var(--ok);
background:rgba(61,220,106,.1)}.days{font-size:.85rem;color:var(--muted)}
.build-wrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:4px 18px}
.build-group{margin-bottom:14px}.build-group h3{margin:0 0 8px;font-size:1.05rem;color:#fff}
.build-items{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.bi{display:flex;align-items:center;gap:8px;padding:6px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.07)}
.bi img{width:40px;height:40px;object-fit:contain;flex:none}.bi label{display:flex;flex-direction:column;font-size:.8rem;
line-height:1.2;min-width:0;flex:1}.bi input{width:100%;margin-top:4px;padding:5px 6px;border:1px solid var(--line);
background:rgba(0,0,0,.5);color:var(--text);font:inherit}.code{font-family:ui-monospace,Consolas,monospace;
font-size:1.4rem;letter-spacing:.08em;color:var(--gold);user-select:all}
.tabs-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
"""
STATUS_TEXT = {
    "pending": ("Waiting for payment", "If you already paid, this page updates within a few minutes – refresh it."),
    "paid": ("Paid – delivering …", "Refresh in a moment."),
    "waiting": ("Paid – join the server", "You get the kit in-game automatically the next time you're online."),
    "delivered": ("Done!", ""),
    "failed": ("Paid – delivery needs staff", "Your payment arrived but the kit couldn't be given automatically. "
                                              "Staff got a message and will fix it – no need to pay again."),
    "review": ("Payment is being checked", "PayPal hasn't confirmed the payment yet. Staff got a message."),
    "cancelled": ("Cancelled", "Nothing was charged. A gift card amount was returned to the card."),
    "expired": ("Expired", "The order wasn't paid within 3 hours. A gift card amount was returned to the card."),
}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def item_name(short: str) -> str:
    return itemlist.COMMON_ITEMS.get(short, short)


def notice(text: str, kind: str = "") -> str:
    return f'<div class="notice {kind}">{esc(text)}</div>' if text else ""


def buyer_fields(*, player: bool = True, gift: bool = True, values: dict | None = None,
                 admin: bool = False) -> str:
    values = values or {}
    out = ""
    if admin:       # only on store pages opened with ?admin=1 (staff)
        out += ('<input type="hidden" name="admin" value="1"><label class="field"><span>Admin key – staff only, '
                'takes it without payment</span><input name="admin_key" type="password" maxlength="200" '
                'autocomplete="off"></label>')
    if player:
        out += ('<label class="field"><span>Your in-game name (linked on Discord with /customkit link)</span>'
                f'<input name="player" maxlength="64" required value="{esc(values.get("player", ""))}"></label>')
    if gift:
        out += ('<label class="field"><span>Gift card code (optional)</span>'
                f'<input name="gift" maxlength="40" placeholder="XXXX-XXXX-XXXX-XXXX" '
                f'value="{esc(values.get("gift", ""))}"></label>')
    return out


def store_tabs(base: str, active: str, shop, admin: bool = False) -> str:
    tabs = [("store", "Kits")]
    if shop.builder_problem() is None:
        tabs.append(("store/build", "Build your own"))
    tabs.append(("store/giftcards", "Gift cards"))
    if len(tabs) == 1:
        return ""
    return '<div class="tabs">' + "".join(
        f'<a href="{base}/{p}{"?admin=1" if admin else ""}" class="{"active" if p == active else ""}">{esc(label)}</a>'
        for p, label in tabs) + "</div>"


def wrap(site, shop, base: str, active: str, title: str, body: str, admin: bool = False) -> str:
    sandbox = ('<div class="notice">Test mode: payments use the PayPal sandbox – no real money.</div>'
               if shop.paypal and shop.bot.config.paypal_mode == "sandbox" else "")
    if admin:
        sandbox += ('<div class="notice">🔑 Admin mode: enter the admin key at checkout to take an item without '
                    'payment. Every use is logged.</div>')
    page = site.page(base, "store", title,
                     f"<h1>{esc(title)}</h1>{store_tabs(base, active, shop, admin)}{sandbox}{body}")
    return page.replace("</style>", STORE_CSS + "</style>", 1)


# ------------------------------------------------------------ Pages

def pay_button(admin: bool, text: str = "Buy with PayPal") -> str:
    return f'<button class="pay" type="submit">{esc("Checkout (PayPal or admin key)" if admin else text)}</button>'


def kits_page(site, shop, base: str, error: str = "", form: dict | None = None, admin: bool = False) -> str:
    db = shop.bot.db
    cards = []
    for row in db.store_kits(enabled_only=True):
        if row["source"] == "custom" and db.custom_kit(row["kit"]) is None:
            continue
        items = shop.kit_items(row)
        if row["source"] == "ingame":
            days = '<div class="days">One-time – given to you in-game</div>'
        else:
            days = f'<div class="days">{row["days"]} days</div>' if row["days"] else '<div class="days">Permanent</div>'
        desc = f'<div class="muted">{esc(row["description"])}</div>' if row["description"] else ""
        values = form if form and str(form.get("kit")) == str(row["id"]) else {}
        cards.append(f'<div class="card"><h2>{esc(row["name"])}</h2><div class="price">{esc(shop.fmt(row["price"]))}'
                     f'</div>{days}{desc}{site.item_tiles(items)}'
                     f'<form method="post" action="{base}/store/checkout"><input type="hidden" name="kind" value="kit">'
                     f'<input type="hidden" name="kit" value="{row["id"]}">{buyer_fields(values=values, admin=admin)}'
                     f'{pay_button(admin)}</form></div>')
    intro = ('<div class="card"><h2>How it works</h2>Pick a kit, enter your in-game name and pay with PayPal (or a gift '
             'card). Your name has to be linked to your Discord account first – join our Discord and use '
             '<b>/customkit link</b>. After the payment the kit is yours right away: one-time kits are given to you '
             'in-game (when you\'re online), the others you claim with <b>/customkit claim</b> on Discord while '
             'you\'re online.</div>')
    body = notice(error, "bad") + intro + (f'<div class="store-grid">{"".join(cards)}</div>' if cards else
                                           '<p class="muted">No kits for sale right now.</p>')
    return wrap(site, shop, base, "store", "Store", body, admin)


def build_page(site, shop, base: str, query, error: str = "", admin: bool = False) -> str:
    problem = shop.builder_problem()
    if problem:
        return wrap(site, shop, base, "store/build", "Build your own kit",
                    f'<p class="muted">Not available right now ({esc(problem)}).</p>')
    pl = shop.price_list()
    amounts = {k[2:]: v for k, v in query.items() if k.startswith("q.") and str(v).strip() not in ("", "0")}
    timer = query.get("timer")
    groups = []
    for rule in pl.rules:
        shorts = sorted(rule.items, key=item_name) + sorted(rule.extras - rule.items, key=item_name)
        per = f"{shop.fmt(rule.price)} per {rule.per:,}".replace(",", " ")
        tiles = "".join(
            f'<div class="bi"><img src="/img/items/{quote(s)}.png?ph=1" alt="" loading="lazy"><label>{esc(item_name(s))}'
            f'<input type="number" name="q.{esc(s)}" min="0" max="100000" step="1" inputmode="numeric" '
            f'value="{esc(amounts.get(s, ""))}" placeholder="0"></label></div>' for s in shorts)
        extras = (f' · free with it (same amount): {", ".join(esc(item_name(e)) for e in sorted(rule.extras))}'
                  if rule.extras else "")
        groups.append(f'<div class="build-group"><h3>{esc(per)}<span class="muted" style="font-family:inherit;'
                      f'text-transform:none;letter-spacing:0;font-size:.85rem">{extras}</span></h3>'
                      f'<div class="build-items">{tiles}</div></div>')
    timers = ""
    if pl.timers:
        opts = "".join(f'<option value="{h}" {"selected" if str(h) == str(timer or pl.default_timer) else ""}>'
                       f'Claim every {h} h' + (f' (+{esc(shop.fmt(p))})' if p else " (included)") + "</option>"
                       for h, p in sorted(pl.timers.items(), reverse=True))
        timers = f'<label class="field"><span>Claim timer</span><select name="timer">{opts}</select></label>'
    caps = "".join(f"<li>max. {c.limit:,} {'of each of' if c.each else 'in total of'} "
                   f"{esc(', '.join(item_name(i) for i in sorted(c.items)))}</li>" for c in pl.caps)
    form = (f'<form method="get" action="{base}/store/build"><div class="card"><h2>Pick your items</h2>'
            f'<p class="muted">Enter how many you want. Prices are per bundle and rounded up to full bundles.</p>'
            + (f'<details><summary class="muted">Limits</summary><ul class="muted">{caps}</ul></details>' if caps else "")
            + f'<div class="build-wrap">{"".join(groups)}</div>' + timers
            + ('<input type="hidden" name="admin" value="1">' if admin else "") +
            '<div style="margin-top:12px"><button class="ghost" type="submit">Calculate price</button></div></div></form>')
    summary = ""
    if amounts and not error:
        from cogs.webshop import CheckoutError
        try:
            q, picked = shop.price_custom(amounts, int(timer) if str(timer or "").isdigit() else None)
        except CheckoutError as exc:
            error = str(exc)
        else:
            rows = "".join(f'<tr><td>{esc(item_name(line["item"]))}</td><td class="r">×{line["amount"]:,}</td>'
                           f'<td class="r">{esc(shop.fmt(line["price"]) if line["price"] else "included")}</td></tr>'
                           for line in q.lines)
            if q.timer_hours:
                rows += (f'<tr><td>Claim every {q.timer_hours} h</td><td></td><td class="r">'
                         f'{esc(shop.fmt(q.timer_price) if q.timer_price else "included")}</td></tr>')
            hidden = "".join(f'<input type="hidden" name="q.{esc(i["short"])}" value="{i["amount"]}">' for i in picked)
            if q.timer_hours:
                hidden += f'<input type="hidden" name="timer" value="{q.timer_hours}">'
            summary = (f'<div class="card"><h2>Your kit – <span class="price">{esc(shop.fmt(q.total))}</span></h2>'
                       f'{site.item_tiles([(i["short"], i["amount"]) for i in picked])}<table>{rows}</table>'
                       f'<form method="post" action="{base}/store/checkout"><input type="hidden" name="kind" '
                       f'value="custom">{hidden}{buyer_fields(values=query, admin=admin)}'
                       f'{pay_button(admin, f"Buy for {shop.fmt(q.total)} with PayPal")}</form></div>')
    return wrap(site, shop, base, "store/build", "Build your own kit", notice(error, "bad") + summary + form, admin)


def giftcards_page(site, shop, base: str, query, ip: str, error: str = "", admin: bool = False) -> str:
    from cogs.webshop import CheckoutError
    body = notice(error, "bad")
    code = str(query.get("code", "")).strip()
    if code:
        try:
            row = shop.gift_card(code, ip)
        except CheckoutError as exc:
            body += notice(str(exc), "bad")
        else:
            until = (f" · valid until {datetime.fromtimestamp(row['expires_at']).strftime('%Y-%m-%d')}"
                     if row["expires_at"] else "")
            body += notice(f"Balance: {pricing.format_money(row['balance'], row['currency'])} of "
                           f"{pricing.format_money(row['initial'], row['currency'])}{until}", "ok")
    values = shop.giftcard_values()
    if values and (shop.paypal or admin):
        cards = "".join(
            f'<div class="card"><h2>Gift card</h2><div class="price">{esc(shop.fmt(v))}</div>'
            f'<p class="muted">A code worth {esc(shop.fmt(v))} for this store – a perfect gift for a friend. '
            f'You get the code right after paying.</p><form method="post" action="{base}/store/checkout">'
            f'<input type="hidden" name="kind" value="giftcard"><input type="hidden" name="value" value="{v}">'
            f'{buyer_fields(player=False, gift=False, admin=admin)}{pay_button(admin)}</form></div>' for v in values)
        body += f'<div class="store-grid">{cards}</div>'
    body += (f'<div class="card"><h2>Check a gift card</h2><form class="search" method="get" '
             f'action="{base}/store/giftcards"><input name="code" maxlength="40" placeholder="XXXX-XXXX-XXXX-XXXX" '
             f'required><button type="submit">Check</button></form><p class="muted">Use a gift card at checkout: '
             f'enter its code in the "Gift card code" field. If it covers the whole price, no PayPal is needed.</p></div>')
    return wrap(site, shop, base, "store/giftcards", "Gift cards", body, admin)


def order_page(site, shop, base: str, order) -> str:
    if order is None:
        return wrap(site, shop, base, "", "Order", '<p class="muted">Order not found.</p>')
    head, sub = STATUS_TEXT.get(order["status"], (order["status"], ""))
    kind = {"delivered": "ok", "failed": "bad", "review": "bad"}.get(order["status"], "")
    body = notice(f"{head}. {sub}".strip(), kind)
    pay = []
    if order["gift_amount"]:
        pay.append(f"gift card {pricing.format_money(order['gift_amount'], order['currency'])}")
    if order["paypal_amount"]:
        pay.append(f"PayPal {pricing.format_money(order['paypal_amount'], order['currency'])}")
    if order["admin_key"]:
        pay.append("free – admin key")
    body += (f'<div class="card"><h2>Order #{order["id"]} – {esc(order["title"])}</h2>'
             f'<p><span class="price">{esc(pricing.format_money(order["total"], order["currency"]))}</span> '
             f'<span class="muted">({esc(" + ".join(pay))})</span></p>'
             + (f'<p>For: <b>{esc(order["player"])}</b></p>' if order["player"] else ""))
    if order["status"] == "delivered":
        if order["kind"] == "giftcard":
            code = (order["result"] or "").removeprefix("Gift card ").strip()
            body += (f'<p>Your gift card code:</p><p class="code">{esc(code)}</p><p class="muted">Save it now – '
                     f'whoever has the code can use it. Check its balance any time under Gift cards.</p>')
        elif not order["request_id"]:           # in-game kit (no kit role to claim)
            body += f'<p>The kit <b>{esc(order["result"])}</b> was given to you in-game.</p>'
        else:
            body += (f'<p>Your kit <b>{esc(order["result"])}</b> is ready. Claim it in-game with '
                     f'<b>/customkit claim {esc(order["result"])}</b> on our Discord while you\'re online.</p>')
    body += f'<p class="muted">Keep this page\'s address if you need help with your order (order #{order["id"]}).</p></div>'
    if order["status"] == "pending" and time.time() - order["created_at"] < 3 * 3600:
        body += f'<p><a href="{base}/store/order/{esc(order["token"])}">Refresh</a></p>'
    return wrap(site, shop, base, "", "Order", body)


# ------------------------------------------------------------ Requests

def _base_url(request, base: str) -> str:
    """Absolute address of the website for PayPal's return link (works through the Cloudflare tunnel too)."""
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() if request.remote in (
        "127.0.0.1", "::1") else ""
    return f"{proto or request.scheme}://{request.host}{base}"


async def handle(site, shop, request, base: str, path: str, ip: str):
    """Returns (status, html) or a web.Response (redirect)."""
    from cogs.webshop import CheckoutError
    sub = path.removeprefix("store").strip("/")
    query = request.query
    admin_on = bool(shop.bot.config.store_admin_key)
    if request.method == "POST":
        if sub != "checkout":
            return 404, site.not_found(base)
        form = await request.post()
        kind = str(form.get("kind", ""))
        admin = admin_on and form.get("admin") == "1"
        amounts = {k[2:]: v for k, v in form.items() if k.startswith("q.")}
        timer = str(form.get("timer", ""))
        try:
            result = await shop.checkout(
                kind, base_url=_base_url(request, base), kit_id=form.get("kit"), items=amounts,
                timer_hours=int(timer) if timer.isdigit() else None, value=form.get("value"),
                player=str(form.get("player", "")), gift_code=str(form.get("gift", "")),
                admin_key=str(form.get("admin_key", "")) if admin else "", ip=ip)
        except CheckoutError as exc:
            values = {k: str(v) for k, v in form.items() if k != "admin_key"}
            if kind == "custom":
                return 400, build_page(site, shop, base, values, str(exc), admin)
            if kind == "giftcard":
                return 400, giftcards_page(site, shop, base, {}, ip, str(exc), admin)
            return 400, kits_page(site, shop, base, str(exc), values, admin)
        raise web.HTTPSeeOther(result.get("redirect") or f"{base}/store/order/{result['token']}")
    admin = admin_on and query.get("admin") == "1"
    if sub == "":
        return 200, kits_page(site, shop, base, admin=admin)
    if sub == "build":
        return 200, build_page(site, shop, base, query, admin=admin)
    if sub == "giftcards":
        return 200, giftcards_page(site, shop, base, query, ip, admin=admin)
    if sub in ("return", "cancel"):
        token = str(query.get("o", ""))[:64]
        order = await (shop.complete(token) if sub == "return" else shop.cancel(token))
        if order is None:
            return 404, order_page(site, shop, base, None)
        raise web.HTTPSeeOther(f"{base}/store/order/{quote(order['token'])}")
    if sub.startswith("order/"):
        order = shop.bot.db.store_order_by("token", sub[6:70])
        return (200 if order else 404), order_page(site, shop, base, order)
    return 404, site.not_found(base)
