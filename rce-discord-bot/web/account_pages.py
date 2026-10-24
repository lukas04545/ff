"""Player account pages (/account …): log in with a one-time Discord code, then manage your own custom kits –
add items, buy team slots per month and pick which teammate sits on each slot.

Server-rendered HTML forms, no scripts. The login cookie holds a random token; only its hash is stored. Buying
items/slots reuses the web store checkout (PayPal); assigning slots is free and happens right here.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from aiohttp import web

from cogs.player_accounts import SESSION_TTL
from web.store_pages import STORE_CSS, esc, item_name, notice, picker_groups

COOKIE = "rce_player"
ACCOUNT_CSS = STORE_CSS + """
.slot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px;border:1px solid var(--line);
background:rgba(0,0,0,.3);margin:6px 0}.slot .who{flex:1;min-width:120px}.slot form{margin:0}
.slot .free{color:var(--muted)}.kit-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.mini{display:inline-flex;gap:6px;align-items:center}.mini input{width:120px;padding:7px 9px;border:1px solid var(--line);
background:rgba(0,0,0,.5);color:var(--text);font:inherit}
table.sum{width:100%;border-collapse:collapse;margin:8px 0}table.sum td{padding:3px 6px;border-bottom:1px solid rgba(255,255,255,.07)}
table.sum td.r{text-align:right}
"""


def _page(site, base: str, active: str, title: str, body: str) -> str:
    page = site.page(base, active, title, f"<h1>{esc(title)}</h1>{body}")
    return page.replace("</style>", ACCOUNT_CSS + "</style>", 1)


def _secure(request) -> bool:
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    return (proto or request.scheme) == "https"


def _base_url(request, base: str) -> str:
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() if request.remote in (
        "127.0.0.1", "::1") else ""
    return f"{proto or request.scheme}://{request.host}{base}"


def _resp(site, base: str, active: str, title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(status=status, text=_page(site, base, active, title, body), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------ Pages

def login_page(site, base: str, *, error: str = "", player: str = "", stage: str = "name") -> web.Response:
    if stage == "code":
        body = (notice(error, "bad") + notice(f"A 6-digit code was sent to the Discord account linked to "
                f"{esc(player)}. Enter it below (valid 10 minutes).")
                + f'<form method="post" action="{base}/account/verify" class="card">'
                f'<input type="hidden" name="player" value="{esc(player)}">'
                '<label class="field"><span>Login code</span><input name="code" inputmode="numeric" maxlength="6" '
                'autocomplete="one-time-code" required></label>'
                '<button class="pay" type="submit">Log in</button></form>'
                f'<form method="post" action="{base}/account/login"><input type="hidden" name="player" '
                f'value="{esc(player)}"><button class="ghost" type="submit">Send a new code</button></form>')
        return _resp(site, base, "account", "Log in", body)
    body = (notice(error, "bad") + '<div class="card"><p>Log in to manage your own custom kits: add items, and buy '
            'team slots so your mates can use your kit. Enter your in-game name – the bot sends a one-time code to '
            'the Discord account it\'s linked to (link it with <b>/customkit link</b>).</p>'
            f'<form method="post" action="{base}/account/login">'
            f'<label class="field"><span>Your in-game name</span><input name="player" maxlength="64" required '
            f'value="{esc(player)}"></label><button class="pay" type="submit">Send me a code</button></form></div>')
    return _resp(site, base, "account", "Log in", body)


def _slot_rows(base: str, kit: str, slots: list[dict]) -> str:
    rows = []
    for s in slots:
        if s["member_id"]:
            left = max(0, int(s["expires_at"]) - int(time.time()))
            days = left // 86400
            when = f"{days} day{'s' if days != 1 else ''} left" if days else "less than a day left"
            who = f'<span class="who">👤 <b>{esc(s["player"] or s["member_id"])}</b> · <span class="days">{when}</span></span>'
            action = (f'<form method="post" action="{base}/account/slot"><input type="hidden" name="slot" '
                      f'value="{s["id"]}"><input type="hidden" name="action" value="clear">'
                      '<button class="ghost" type="submit">Remove</button></form>')
        else:
            left = max(0, int(s["expires_at"]) - int(time.time()))
            days = left // 86400
            who = (f'<span class="who free">Empty slot · {days} day{"s" if days != 1 else ""} left</span>'
                   if left > 0 else '<span class="who free">Empty slot · expired</span>')
            action = (f'<form method="post" action="{base}/account/slot" class="mini">'
                      f'<input type="hidden" name="slot" value="{s["id"]}">'
                      '<input type="hidden" name="action" value="assign">'
                      '<input name="teammate" placeholder="teammate in-game name" maxlength="64" required>'
                      '<button class="ghost" type="submit">Add</button></form>') if left > 0 else ""
        rows.append(f'<div class="slot">{who}{action}</div>')
    return "".join(rows)


def account_page(site, shop, accounts, base: str, user_id: int, name: str, *, error: str = "",
                 ok: str = "") -> web.Response:
    kits = accounts.my_kits(user_id)
    slot_price = shop.bot.config.store_slot_price if shop is not None else 0     # (no store set up: no buying)
    cards = []
    for kit in kits:
        tiles = site.item_tiles([(i["short"], i["amount"]) for i in kit["items"]])
        slots_html = _slot_rows(base, kit["name"], kit["slots"]) or '<p class="days">No team slots yet.</p>'
        buy = ""
        if slot_price > 0:
            buy = (f'<details><summary>Buy team slots</summary>'
                   f'<form method="post" action="{base}/account/checkout"><input type="hidden" name="kind" '
                   f'value="slots"><input type="hidden" name="kit" value="{esc(kit["name"])}">'
                   f'<div class="mini" style="margin:8px 0">Slots <input type="number" name="slots" min="1" '
                   f'max="{shop.bot.config.store_slot_max}" value="1" required> for <input type="number" '
                   f'name="months" min="1" max="12" value="1" required> month(s)</div>'
                   f'<p class="days">{esc(shop.fmt(slot_price))} per slot per month.</p>'
                   f'<button class="pay" type="submit">Checkout</button></form></details>')
        add = (f'<a class="ghost" href="{base}/account/additems?kit={quote(kit["name"])}" '
               'style="display:inline-block;margin-top:8px">Add items</a>') \
            if shop is not None and shop.builder_problem() is None else ""
        cards.append(f'<div class="card"><div class="kit-head"><h2>{esc(kit["name"])}</h2>{add}</div>{tiles}'
                     f'<h3 style="margin:14px 0 4px">Team slots</h3>{slots_html}{buy}</div>')
    head = (notice(error, "bad") + notice(ok, "ok")
            + f'<div class="card"><div class="kit-head"><div>Logged in as <b>{esc(name)}</b></div>'
            f'<form method="post" action="{base}/account/logout"><button class="ghost" type="submit">Log out'
            '</button></form></div></div>')
    store_hint = (' Buy one in the <a href="' + base + '/store">store</a> or build your own.') if shop is not None else ""
    body = head + ("".join(cards) if cards else '<div class="card"><p class="muted">You don\'t own any custom kits '
                   'yet.' + store_hint + '</p></div>')
    return _resp(site, base, "account", "My account", body)


def additems_page(site, shop, base: str, kit: str, query, *, error: str = "") -> web.Response:
    from cogs.webshop import CheckoutError
    pl = shop.price_list()
    if pl is None:
        return _resp(site, base, "account", "Add items", notice("Building items isn't available right now.", "bad"))
    amounts = {k[2:]: v for k, v in query.items() if k.startswith("q.") and str(v).strip() not in ("", "0")}
    groups_html = picker_groups(shop, amounts)
    form = (f'<form method="get" action="{base}/account/additems"><input type="hidden" name="kit" value="{esc(kit)}">'
            f'<div class="card"><h2>Add items to {esc(kit)}</h2><p class="muted">Pick items to add. They are added to '
            f'your kit after payment.</p><div class="build-wrap">{groups_html}</div>'
            '<div style="margin-top:12px"><button class="ghost" type="submit">Calculate price</button></div></div></form>')
    summary = ""
    if amounts and not error:
        try:
            q, picked = shop.price_custom(amounts, None)
        except CheckoutError as exc:
            error = str(exc)
        else:
            rows = "".join(f'<tr><td>{esc(item_name(line["item"]))}</td><td class="r">×{line["amount"]:,}</td>'
                           f'<td class="r">{esc(shop.fmt(line["price"]) if line["price"] else "included")}</td></tr>'
                           for line in q.lines)
            hidden = "".join(f'<input type="hidden" name="q.{esc(i["short"])}" value="{i["amount"]}">' for i in picked)
            summary = (f'<div class="card"><h2>To add – <span class="price">{esc(shop.fmt(q.total))}</span></h2>'
                       f'<table class="sum">{rows}</table>'
                       f'<form method="post" action="{base}/account/checkout"><input type="hidden" name="kind" '
                       f'value="addon"><input type="hidden" name="kit" value="{esc(kit)}">{hidden}'
                       f'<button class="pay" type="submit">Buy and add for {esc(shop.fmt(q.total))}</button></form></div>')
    return _resp(site, base, "account", "Add items", notice(error, "bad") + summary + form)


# ------------------------------------------------------------ Routing

async def handle(site, accounts, shop, request, base: str, path: str, ip: str):
    """Returns a web.Response. `accounts` is the PlayerAccounts cog, `shop` the WebShop cog (may be None)."""
    from cogs.player_accounts import AccountError
    from cogs.webshop import CheckoutError
    if not accounts.enabled:
        return _resp(site, base, "account", "My account",
                     notice("Player login is switched off.", "bad"), status=404)
    sub = path.removeprefix("account").strip("/")
    token = request.cookies.get(COOKIE)
    sess = accounts.session(token)

    if request.method == "POST":
        form = await request.post()
        if sub == "login":
            player = str(form.get("player", ""))
            try:
                name = await accounts.send_code(player, ip)
            except AccountError as exc:
                return login_page(site, base, error=str(exc), player=player, stage="name")
            return login_page(site, base, player=name, stage="code")
        if sub == "verify":
            player = str(form.get("player", ""))
            try:
                new_token, uid, name = accounts.verify(player, str(form.get("code", "")), ip)
            except AccountError as exc:
                return login_page(site, base, error=str(exc), player=player, stage="code")
            resp = web.HTTPSeeOther(f"{base}/account")
            resp.set_cookie(COOKIE, new_token, max_age=SESSION_TTL, httponly=True, samesite="Lax",
                            secure=_secure(request), path=base or "/")
            raise resp
        if sess is None:
            raise web.HTTPSeeOther(f"{base}/account")
        if sub == "logout":
            accounts.logout(token)
            resp = web.HTTPSeeOther(f"{base}/account")
            resp.del_cookie(COOKIE, path=base or "/")
            raise resp
        if sub == "slot":
            action = str(form.get("action", ""))
            try:
                slot_id = int(str(form.get("slot", "0")) or 0)
                if action == "assign":
                    who = await accounts.assign_slot(sess["user_id"], slot_id, str(form.get("teammate", "")))
                    ok = f"{who} is on the slot."
                else:
                    await accounts.clear_slot(sess["user_id"], slot_id)
                    ok = "Slot freed."
            except AccountError as exc:
                return account_page(site, shop, accounts, base, sess["user_id"], sess["name"], error=str(exc))
            return account_page(site, shop, accounts, base, sess["user_id"], sess["name"], ok=ok)
        if sub == "checkout" and shop is not None:
            kind = str(form.get("kind", ""))
            amounts = {k[2:]: v for k, v in form.items() if k.startswith("q.")}
            try:
                result = await shop.checkout(
                    kind, base_url=_base_url(request, base), user_id=sess["user_id"], name=str(form.get("kit", "")),
                    items=amounts, slot_count=form.get("slots"), months=form.get("months"), ip=ip)
            except CheckoutError as exc:
                if kind == "addon":
                    return additems_page(site, shop, base, str(form.get("kit", "")), {}, error=str(exc))
                return account_page(site, shop, accounts, base, sess["user_id"], sess["name"], error=str(exc))
            raise web.HTTPSeeOther(result.get("redirect") or f"{base}/store/order/{result['token']}")
        raise web.HTTPSeeOther(f"{base}/account")

    # GET
    if sess is None:
        return login_page(site, base)
    if sub == "additems" and shop is not None:
        return additems_page(site, shop, base, str(request.query.get("kit", ""))[:40], request.query)
    return account_page(site, shop, accounts, base, sess["user_id"], sess["name"])
