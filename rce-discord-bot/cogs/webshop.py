"""Web store: players buy kits on the player website (/play/store) with PayPal and/or gift cards.

What can be bought
  * Kits for sale – staff-made bot kits (kit builder) and the server's in-game kits are added automatically (hidden
    until an owner sets a price in the web interface → Store); owners can also add kits by hand. Bot kits give the
    kit's claim role (optionally for N days), in-game kits are given in-game once (right away if the buyer is
    online, otherwise when they next join).
  * Own custom kit – players pick items on the website, priced by the price list (prices.txt, STORE_CUSTOM_KITS).
  * Gift cards – values from STORE_GIFTCARD_VALUES; owners also create gift cards in the web interface.

Buyers enter their in-game name; it has to be linked to their Discord account (/customkit link) – that's who gets
the kit (its claim role, like an approved kit purchase: /customkit claim). Gift cards pay all or part of an order.

Discord: /giftcard create | list | disable (owners: server owner, Administrator permission or ADMIN_USER_IDS – the
codes are only shown to them) and /giftcard check (everyone).

Payment safety: the price is always calculated by the bot. PayPal orders are created for the exact amount and only
delivered after the bot captured the payment and PayPal reports COMPLETED for exactly that amount and currency.
Each order is paid and delivered once (database status change). Gift card amounts are held while an order waits
and returned if it is cancelled or never paid (after 3 hours).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rce import kits as kitparse
from rce.rcon_client import RconError
from support import pricing
from support.paypal import PayPalClient, PayPalError
from utils import safe_md
from web.ratelimit import RateLimiter

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.webshop")

STORE_BY = "Web store (paid)"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no 0/O, 1/I – easy to type
BOUGHT_MARKER = "Bought by user "    # description of custom kits created for a buyer (not offered in the store)
DISMISSED_KEY = "store_dismissed"     # kits an owner removed from the store – never added again automatically
JOIN_DELAY = 20                       # seconds after joining before an in-game kit is given (still loading)
ITEMS_REFRESH = 6 * 3600              # in-game kit contents are read again after this
ITEMS_PER_SYNC = 5                    # at most this many "kit info" commands per sync (keeps it quick)
PENDING_EXPIRE = 3 * 3600          # unpaid orders are cancelled after this (gift card amounts returned)
CHECK_AFTER = 10 * 60              # approved on PayPal but the buyer never came back: capture after this
MAX_AMOUNT = 100_000               # per item in a custom kit
COLOUR = discord.Colour(0x2ECC71)


class CheckoutError(Exception):
    """Message shown to the buyer."""


def normalize_code(text: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4)) if len(raw) == 16 else raw[:40]


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))      # 80 bits – can't be guessed
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


class WebShop(commands.Cog):
    giftcard = app_commands.Group(name="giftcard", description="Store gift cards", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._paypal: tuple[tuple, PayPalClient] | None = None
        self._gift_failures = RateLimiter(10, 900)      # wrong gift card codes per visitor
        self._checkouts = RateLimiter(10, 600)          # started orders per visitor (each one is a PayPal order)
        self._key_failures = RateLimiter(5, 900)        # wrong admin keys per visitor
        self._locks: dict[int, asyncio.Lock] = {}
        self._sync_task: asyncio.Task | None = None
        self._items_read: dict[int, float] = {}       # store kit id -> when its in-game contents were read

    async def cog_load(self) -> None:
        self.check_loop.start()

    async def cog_unload(self) -> None:
        self.check_loop.cancel()

    # ------------------------------------------------------------ Setup

    @property
    def paypal(self) -> PayPalClient | None:
        cfg = self.bot.config
        key = (cfg.paypal_client_id, cfg.paypal_secret, cfg.paypal_mode)
        if not key[0] or not key[1]:
            return None
        if self._paypal is None or self._paypal[0] != key:
            self._paypal = (key, PayPalClient(*key))
        return self._paypal[1]

    def price_list(self) -> pricing.PriceList | None:
        kr = self.bot.get_cog("KitRequests")
        return kr.price_list() if kr is not None else None

    @property
    def currency(self) -> str:
        pl = self.price_list()
        return self.bot.config.store_currency or (pl.currency if pl else "EUR")

    def fmt(self, amount: float) -> str:
        return pricing.format_money(amount, self.currency)

    def builder_problem(self) -> str | None:
        """Why players can't build their own kit right now (None = they can)."""
        if not self.bot.config.store_custom_kits:
            return "switched off"
        pl = self.price_list()
        if pl is None:
            return "no price list (prices.txt)"
        if pl.currency != self.currency:
            return f"the price list is in {pl.currency}, the store in {self.currency}"
        return None

    def giftcard_values(self) -> tuple[float, ...]:
        return self.bot.config.store_giftcard_values

    def status(self) -> dict:
        pp = self.paypal
        return {"paypal": bool(pp), "paypal_mode": self.bot.config.paypal_mode, "currency": self.currency,
                "admin_key": bool(self.bot.config.store_admin_key),
                "builder": self.builder_problem() is None, "builder_problem": self.builder_problem(),
                "giftcard_values": list(self.giftcard_values())}

    # ------------------------------------------------------------ Gift cards

    def create_gift_cards(self, amount: float, count: int, by: str, *, days: int = 0, note: str = "",
                          order_id: int | None = None) -> list[str]:
        amount = round(float(amount), 2)
        if not 0 < amount <= 10_000:
            raise CheckoutError("The value must be between 0.01 and 10 000.")
        if not 1 <= count <= 100:
            raise CheckoutError("You can create 1 to 100 gift cards at once.")
        expires = int(time.time()) + days * 86400 if days else None
        codes = []
        for _ in range(count):
            code = new_code()
            self.bot.db.add_gift_card(code, amount, self.currency, by, note=note.strip()[:200] or None,
                                      expires_at=expires, order_id=order_id)
            codes.append(code)
        return codes

    def check_admin_key(self, key: str, ip: str = "?") -> None:
        """Admin key for a free checkout (STORE_ADMIN_KEY). Wrong keys: 5 tries per 15 minutes per visitor."""
        from web.users import same_secret
        expected = self.bot.config.store_admin_key
        if not expected:
            raise CheckoutError("The admin key isn't enabled.")
        if self._key_failures.retry_after(ip) is not None:
            raise CheckoutError("Too many wrong admin keys – please wait 15 minutes.")
        if not same_secret(key.strip(), expected):
            self._key_failures.hit(ip)
            log.warning("Wrong store admin key from %s", ip)
            raise CheckoutError("Wrong admin key.")

    def gift_card(self, code: str, ip: str = "?"):
        """A usable gift card or CheckoutError (wrong codes are limited per visitor)."""
        if self._gift_failures.retry_after(ip) is not None:
            raise CheckoutError("Too many wrong gift card codes – please wait 15 minutes.")
        row = self.bot.db.gift_card(normalize_code(code))
        if row is None:
            self._gift_failures.hit(ip)
            raise CheckoutError("This gift card code doesn't exist.")
        if row["disabled"]:
            raise CheckoutError("This gift card was disabled.")
        if row["expires_at"] and row["expires_at"] <= time.time():
            raise CheckoutError("This gift card has expired.")
        if row["currency"] != self.currency:
            raise CheckoutError(f"This gift card is in {row['currency']}, the store uses {self.currency}.")
        if row["balance"] <= 0:
            raise CheckoutError("This gift card is used up.")
        return row

    # ------------------------------------------------------------ Prices

    def store_kit(self, kit_id) -> "object":
        try:
            row = self.bot.db.store_kit(int(kit_id))
        except (TypeError, ValueError):
            row = None
        if row is None or not row["enabled"] or row["price"] <= 0 or \
                (row["source"] == "custom" and self.bot.db.custom_kit(row["kit"]) is None):
            raise CheckoutError("This kit isn't for sale (any more).")
        return row

    def price_custom(self, amounts: dict[str, int], timer_hours: int | None = None
                     ) -> tuple[pricing.Quote, list[dict]]:
        """Price of a kit a player built (items checked against the price list). (quote, items)"""
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        if (problem := self.builder_problem()) is not None:
            raise CheckoutError(f"Building your own kit isn't available ({problem}).")
        pl = self.price_list()
        items = []
        for short, amount in amounts.items():
            short = str(short).strip().lower()
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            if pl.rule_for(short) is None and not any(short in r.extras for r in pl.rules):
                raise CheckoutError(f"{short} can't be bought.")
            items.append({"short": short, "amount": min(amount, MAX_AMOUNT)})
        if not items:
            raise CheckoutError("Pick at least one item.")
        if len(items) > MAX_ITEMS_PER_KIT:
            raise CheckoutError(f"A kit can hold at most {MAX_ITEMS_PER_KIT} different items.")
        q = pricing.quote(pl, items, timer_hours)
        if q.over_cap:
            raise CheckoutError("Over the server's limits: " + "; ".join(q.over_cap))
        if q.unpriced:
            extras = {e: r for r in pl.rules for e in r.extras}
            raise CheckoutError("; ".join(
                f"{s} is only included with {', '.join(sorted(extras[s].items))} (up to the same amount)"
                if s in extras else f"no price for {s}" for s in q.unpriced) + ".")
        if q.total <= 0:
            raise CheckoutError("This kit has no price – pick more items.")
        return q, items

    # ------------------------------------------------------------ Automatic kit list

    def _dismissed(self) -> set[str]:
        try:
            return set(json.loads(self.bot.db.get_value(DISMISSED_KEY) or "[]"))
        except ValueError:
            return set()

    def remove_kit(self, kit_id: int) -> bool:
        """Removes a kit from the store; automatically found kits are remembered so they don't come back."""
        db = self.bot.db
        row = db.store_kit(kit_id)
        if row is None or not db.delete_store_kit(kit_id):
            return False
        if row["auto"]:
            db.set_value(DISMISSED_KEY, json.dumps(sorted(self._dismissed() | {f"{row['source']}:{row['kit'].lower()}"})))
        return True

    def start_sync(self) -> bool:
        """Runs sync_kits in the background (the web interface doesn't wait). True while one is running."""
        if self._sync_task is None or self._sync_task.done():
            async def run() -> None:
                try:
                    await self.sync_kits()
                except Exception:
                    log.exception("Store kit sync failed")
            self._sync_task = asyncio.create_task(run(), name="store-kit-sync")
        return not self._sync_task.done()

    async def sync_kits(self) -> int:
        """Adds new staff-made bot kits and in-game kits to the store (hidden, no price). Returns how many."""
        db = self.bot.db
        have = {(r["source"], r["kit"].lower()) for r in db.store_kits()}
        dismissed = self._dismissed()
        added = 0

        def add(source: str, name: str) -> None:
            nonlocal added
            if (source, name.lower()) in have or f"{source}:{name.lower()}" in dismissed:
                return
            db.add_store_kit(name[:60], name, 0, 0, None, source=source, auto=True, enabled=False)
            have.add((source, name.lower()))
            added += 1

        for row in db.custom_kits():
            if not (row["description"] or "").startswith(BOUGHT_MARKER):
                add("custom", row["name"])
        kits_cog = self.bot.get_cog("Kits")
        if kits_cog is not None and self.bot.rcon.connected:
            try:
                names = await kits_cog.kit_names.get(max_wait=5)
            except RconError:
                names = []
            for name in names:
                add("ingame", name)
            await self._refresh_ingame_items()
        if added:
            await self._log(f"🛒 {added} kit(s) added to the store – hidden until you set a price (web interface → "
                            "Store → Kits for sale).")
        return added

    async def _refresh_ingame_items(self) -> None:
        """Keeps the contents of in-game kits for sale (shown with pictures on the website). Kits without contents
        first, then the oldest – a few per run, so a sync never takes long."""
        now = time.monotonic()
        rows = [r for r in self.bot.db.store_kits() if r["source"] == "ingame"
                and now - self._items_read.get(r["id"], -1e9) >= ITEMS_REFRESH]
        rows.sort(key=lambda r: (bool(r["items"]), self._items_read.get(r["id"], 0)))
        for row in rows[:ITEMS_PER_SYNC]:
            self._items_read[row["id"]] = now
            try:
                raw = await self.bot.rcon.command(f'kit info "{kitparse.clean_arg(row["kit"])}"', timeout=4)
            except RconError:
                return
            items = [{"short": i.short_name, "amount": i.amount} for i in kitparse.parse_kit_info(raw)]
            if items:
                self.bot.db.update_store_kit(row["id"], items=json.dumps(items))

    def kit_items(self, row) -> list[tuple[str, int]]:
        """(shortname, amount) of a kit for sale."""
        if row["source"] == "ingame":
            try:
                return [(i["short"], int(i["amount"])) for i in json.loads(row["items"] or "[]")]
            except (ValueError, KeyError, TypeError):
                return []
        return [(i["shortname"], i["amount"]) for i in self.bot.db.custom_kit_items(row["kit"])]

    # ------------------------------------------------------------ Checkout

    def _buyer(self, player: str) -> tuple[str, int]:
        player = " ".join(str(player or "").split())[:64]
        if not player:
            raise CheckoutError("Enter your in-game name.")
        user_id = self.bot.db.player_owner(player)
        if user_id is None:
            raise CheckoutError(f"{player} isn't linked to a Discord account yet. Join our Discord and link your "
                                "player with /customkit link first – the kit is given to that account.")
        return self.bot.db.linked_player(user_id) or player, user_id

    def _owned_kit(self, user_id: int, kit: str):
        """The custom kit `kit`, but only if `user_id` is the player who bought it. Raises otherwise."""
        name = kitparse.clean_arg(kit or "")[:40].strip()
        row = self.bot.db.custom_kit(name)
        if row is None:
            raise CheckoutError("That kit doesn't exist (any more).")
        if self.bot.db.kit_buyer(row["name"]) != user_id:
            raise CheckoutError("That isn't one of your kits.")
        return row

    async def checkout(self, kind: str, *, base_url: str, kit_id=None, items: dict | None = None,
                       timer_hours: int | None = None, value=None, player: str = "", gift_code: str = "",
                       name: str = "", admin_key: str = "", ip: str = "?", user_id: int | None = None,
                       slot_count: int | None = None, months: int | None = None) -> dict:
        """Starts an order. Returns {"token", "redirect"} (PayPal) or {"token"} (paid with a gift card or free with
        the admin key). `user_id` (a logged-in player) is required for the "addon" and "slots" kinds."""
        db = self.bot.db
        if self._checkouts.hit(ip) is not None:
            raise CheckoutError("Too many orders started – please wait a few minutes.")
        fields: dict = {"kind": kind, "currency": self.currency}
        if kind == "kit":
            row = self.store_kit(kit_id)
            fields.update(title=row["name"], store_kit_id=row["id"], total=round(float(row["price"]), 2))
        elif kind == "addon":
            if user_id is None:
                raise CheckoutError("Log in first.")
            kit_row = self._owned_kit(user_id, name)
            q, picked = self.price_custom(items or {}, None)
            fields.update(title=f"Add items to {kit_row['name']}", total=q.total, items=json.dumps(picked),
                          kit_name=kit_row["name"])
        elif kind == "slots":
            if user_id is None:
                raise CheckoutError("Log in first.")
            if self.bot.config.store_slot_price <= 0:
                raise CheckoutError("Team slots aren't for sale right now.")
            kit_row = self._owned_kit(user_id, name)
            try:
                slot_count = int(slot_count)
                months = int(months)
            except (TypeError, ValueError):
                raise CheckoutError("Pick how many slots and months.") from None
            if not 1 <= slot_count <= self.bot.config.store_slot_max:
                raise CheckoutError(f"Between 1 and {self.bot.config.store_slot_max} slots.")
            if not 1 <= months <= 12:
                raise CheckoutError("Between 1 and 12 months.")
            have = len(db.kit_slots(kit=kit_row["name"]))
            if have + slot_count > self.bot.config.store_slot_max:
                raise CheckoutError(f"That kit can have at most {self.bot.config.store_slot_max} team slots "
                                    f"(it already has {have}).")
            total = round(self.bot.config.store_slot_price * slot_count * months, 2)
            unit = f"{slot_count} slot{'s' if slot_count != 1 else ''} × {months} month{'s' if months != 1 else ''}"
            fields.update(title=f"{unit} on {kit_row['name']}", total=total, kit_name=kit_row["name"],
                          slot_count=slot_count, months=months)
        elif kind == "custom":
            q, picked = self.price_custom(items or {}, timer_hours)
            kit_name = kitparse.clean_arg(name or "")[:40].strip()
            count = f"{len(picked)} item{'s' if len(picked) != 1 else ''}"
            fields.update(title=f"Custom kit: {kit_name}" if kit_name else f"Own custom kit ({count})",
                          total=q.total, timer_hours=q.timer_hours, items=json.dumps(picked),
                          kit_name=kit_name or None)
        elif kind == "giftcard":
            try:
                value = round(float(value), 2)
            except (TypeError, ValueError):
                value = None
            if value not in self.giftcard_values():
                raise CheckoutError("This gift card value isn't available.")
            if gift_code:
                raise CheckoutError("Gift cards can't be paid with a gift card.")
            fields.update(title=f"Gift card {self.fmt(value)}", total=value)
        else:
            raise CheckoutError("Unknown product.")
        if kind in ("kit", "custom"):
            fields["player"], fields["user_id"] = self._buyer(player)
        elif kind in ("addon", "slots"):
            fields["user_id"] = user_id
            fields["player"] = db.linked_player(user_id) or ""
        if admin_key.strip():
            self.check_admin_key(admin_key, ip)
            token = secrets.token_urlsafe(18)
            order_id = db.add_store_order(token=token, admin_key=1, **fields)
            db.move_store_order(order_id, "pending", "paid", paid_at=int(time.time()))
            log.warning("Store order #%s taken for free with the admin key (from %s)", order_id, ip)
            await self.fulfil(order_id)
            return {"token": token}
        total = fields["total"]
        gift_row, gift_amount = None, 0.0
        if gift_code.strip():
            gift_row = self.gift_card(gift_code, ip)
            gift_amount = round(min(float(gift_row["balance"]), total), 2)
        paypal_amount = round(total - gift_amount, 2)
        if paypal_amount > 0 and self.paypal is None:
            raise CheckoutError("Online payment isn't set up yet – please try again later or use a gift card.")
        token = secrets.token_urlsafe(18)
        order_id = db.add_store_order(token=token, gift_code=gift_row["code"] if gift_row else None,
                                      gift_amount=gift_amount, paypal_amount=paypal_amount, **fields)
        if gift_amount and not db.take_gift_balance(gift_row["code"], gift_amount, int(time.time())):
            db.update_store_order(order_id, status="cancelled", result="gift card balance changed")
            raise CheckoutError("The gift card's balance just changed – please try again.")
        if paypal_amount <= 0:
            db.move_store_order(order_id, "pending", "paid", paid_at=int(time.time()))
            await self.fulfil(order_id)
            return {"token": token}
        try:
            pp_id, url = await self.paypal.create_order(
                amount=paypal_amount, currency=self.currency, description=f"{fields['title']} (order #{order_id})",
                reference=f"store-{order_id}", brand=self.bot.config.server_display_name or "Web store",
                return_url=f"{base_url}/store/return?o={token}", cancel_url=f"{base_url}/store/cancel?o={token}")
        except PayPalError as exc:
            self._release(order_id, "cancelled", f"PayPal error: {exc}")
            log.warning("PayPal order for store order #%s failed: %s", order_id, exc)
            raise CheckoutError("PayPal is not available right now – please try again in a few minutes.") from exc
        db.update_store_order(order_id, paypal_order=pp_id)
        return {"token": token, "redirect": url}

    def _release(self, order_id: int, status: str, result: str) -> bool:
        """Cancels/expires a pending order and gives the held gift card amount back."""
        order = self.bot.db.store_order(order_id)
        if order is None or not self.bot.db.move_store_order(order_id, "pending", status, result=result[:300]):
            return False
        if order["gift_code"] and order["gift_amount"]:
            self.bot.db.return_gift_balance(order["gift_code"], float(order["gift_amount"]))
        return True

    def _lock(self, order_id: int) -> asyncio.Lock:
        return self._locks.setdefault(order_id, asyncio.Lock())

    async def complete(self, token: str):
        """Buyer came back from PayPal: capture the payment and deliver. Returns the order row (or None)."""
        order = self.bot.db.store_order_by("token", str(token or "")[:64])
        if order is None or order["status"] != "pending" or not order["paypal_order"]:
            return order
        async with self._lock(order["id"]):
            order = self.bot.db.store_order(order["id"])
            if order["status"] != "pending" or self.paypal is None:
                return order
            try:
                cap = await self.paypal.capture(order["paypal_order"], amount=float(order["paypal_amount"]),
                                                currency=order["currency"])
            except PayPalError as exc:
                log.warning("Capture of store order #%s failed: %s", order["id"], exc)
                return order
            if cap.ok:
                if self.bot.db.move_store_order(order["id"], "pending", "paid", capture_id=cap.capture_id,
                                                payer_email=(cap.payer_email or "")[:200] or None,
                                                paid_at=int(time.time())):
                    await self.fulfil(order["id"])
            elif cap.capture_id or cap.status in ("PENDING", "DECLINED", "FAILED"):
                self.bot.db.move_store_order(order["id"], "pending", "review", capture_id=cap.capture_id,
                                             result=(cap.problem or cap.status)[:300])
                await self._log(f"⚠️ Store order #{order['id']} ({safe_md(order['title'])}) needs a check: "
                                f"{safe_md(cap.problem or cap.status)} – PayPal order {order['paypal_order']}",
                                alert=True)
            return self.bot.db.store_order(order["id"])

    async def cancel(self, token: str):
        """Buyer cancelled on PayPal. If they paid after all, it is completed instead."""
        order = self.bot.db.store_order_by("token", str(token or "")[:64])
        if order is None or order["status"] != "pending":
            return order
        if order["paypal_order"] and self.paypal is not None:
            try:
                state = (await self.paypal.get_order(order["paypal_order"])).get("status")
            except PayPalError:
                state = None
            if state in ("APPROVED", "COMPLETED"):
                return await self.complete(token)
        self._release(order["id"], "cancelled", "cancelled by the buyer")
        return self.bot.db.store_order(order["id"])

    # ------------------------------------------------------------ Delivery

    def _unique_kit_name(self, base: str) -> str:
        db = self.bot.db
        base = kitparse.clean_arg(base)[:40] or "Store kit"
        name, n = base, 2
        while db.custom_kit(name) is not None or any(r["kit"].lower() == name.lower()
                                                     for r in db.kit_requests(status="pending")):
            name, n = f"{base} {n}", n + 1
        return name

    async def fulfil(self, order_id: int) -> bool:
        """Delivers a paid order (once). False = failed – staff can retry it in the web interface."""
        db = self.bot.db
        order = db.store_order(order_id)
        if order is None or order["status"] != "paid":
            return False
        pay = (f"PayPal {order['capture_id']}" if order["capture_id"] else "") + \
              (f" gift card {order['gift_code'][:4]}…" if order["gift_code"] else "") + \
              (" FREE with the admin key" if order["admin_key"] else "")
        try:
            skit = db.store_kit(order["store_kit_id"]) if order["store_kit_id"] else None
            if order["kind"] == "giftcard":
                code = self.create_gift_cards(float(order["total"]), 1, f"store order #{order_id}",
                                              order_id=order_id)[0]
                result = f"Gift card {code}"
            elif order["kind"] == "slots":
                expires = int(time.time()) + int(order["months"]) * 30 * 86400
                db.add_kit_slots(order["kit_name"], order["user_id"], int(order["slot_count"]), expires)
                result = f"{order['slot_count']} team slot(s) on {order['kit_name']}"
            elif order["kind"] == "addon":
                kr = self.bot.get_cog("KitRequests")
                if kr is None or db.custom_kit(order["kit_name"]) is None:
                    raise CheckoutError("the kit no longer exists")
                proof = f"Web store order #{order_id} ({pay.strip()})"
                earlier = db.kit_request(order["request_id"]) if order["request_id"] else None
                rid = earlier["id"] if earlier is not None and earlier["status"] == "pending" else \
                    db.add_kit_request(None, order["user_id"], order["kit_name"], 0, proof, order["items"],
                                       source="store", price=order["total"], currency=order["currency"],
                                       checkout_name=order["player"], addon=True)
                db.update_store_order(order_id, request_id=rid)
                ok, text = await kr.decide(rid, True, STORE_BY)
                if not ok:
                    raise CheckoutError(text)
                result = f"items added to {order['kit_name']}"
            elif order["kind"] == "kit" and skit is not None and skit["source"] == "ingame":
                result = await self._give_ingame(order, skit)
                if result is None:            # buyer is offline: given when they join
                    return True
            else:
                kr = self.bot.get_cog("KitRequests")
                if kr is None:
                    raise CheckoutError("kit purchases are not available")
                proof = f"Web store order #{order_id} ({pay.strip()})"
                earlier = db.kit_request(order["request_id"]) if order["request_id"] else None
                if earlier is not None and earlier["status"] == "pending":     # retry: use the same request
                    rid = earlier["id"]
                elif order["kind"] == "kit":
                    if skit is None or db.custom_kit(skit["kit"]) is None:
                        raise CheckoutError("the kit for sale no longer exists")
                    rid = db.add_kit_request(None, order["user_id"], skit["kit"], int(skit["days"]), proof,
                                             source="store", price=order["total"], currency=order["currency"],
                                             checkout_name=order["player"])
                else:
                    name = self._unique_kit_name(order["kit_name"] or f"{order['player']} kit")
                    rid = db.add_kit_request(None, order["user_id"], name, 0, proof, order["items"], source="store",
                                             price=order["total"], currency=order["currency"],
                                             cooldown_minutes=order["timer_hours"] * 60 if order["timer_hours"] else None,
                                             checkout_name=order["player"])
                db.update_store_order(order_id, request_id=rid)
                ok, text = await kr.decide(rid, True, STORE_BY)
                if not ok:
                    raise CheckoutError(text)
                result = db.kit_request(rid)["kit"]
        except (CheckoutError, discord.HTTPException) as exc:
            db.move_store_order(order_id, "paid", "failed", result=str(exc)[:300])
            await self._log(f"❌ Store order #{order_id} is paid but could not be delivered: {safe_md(str(exc))} – "
                            "fix it and click Retry in the web interface (Store → Orders).", alert=True)
            return False
        db.move_store_order(order_id, "paid", "delivered", result=result[:300])
        await self._log(("🔑 " if order["admin_key"] else "") + f"💳 Store order #{order_id}: **{safe_md(order['title'])}** "
                        f"{self.fmt(float(order['total']))}"
                        + (f" for <@{order['user_id']}> ({safe_md(order['player'] or '')})" if order["user_id"] else "")
                        + f" – {safe_md(pay.strip())} – delivered", alert=bool(order["admin_key"]))
        if order["user_id"] and order["kind"] != "giftcard":
            if order["kind"] == "slots":
                text = (f"✅ Thanks! You added **{order['slot_count']} team slot(s)** to **{safe_md(order['kit_name'])}** "
                        f"for {order['months']} month(s). Put your teammates on them under **My account** on the website.")
            elif order["kind"] == "addon":
                text = (f"✅ Thanks! Your items were added to **{safe_md(order['kit_name'])}**. Claim it in-game with "
                        f"`/customkit claim {order['kit_name']}` while you're online.")
            elif skit is not None and skit["source"] == "ingame":
                text = f"✅ Thanks for your purchase! **{safe_md(order['title'])}** was given to you in-game."
            else:
                text = (f"✅ Thanks for your purchase! **{safe_md(order['title'])}** is yours: claim it in-game with "
                        f"`/customkit claim {result}` on our Discord while you're online.")
            await self._dm(order["user_id"], text)
        return True

    async def _give_ingame(self, order, skit) -> str | None:
        """Gives an in-game kit to the buyer. None = they're offline (order waits until they join)."""
        name = next((n for n in self.bot.online_players if n.lower() == (order["player"] or "").lower()), None)
        if name is None or not self.bot.rcon.connected:
            if self.bot.db.move_store_order(order["id"], "paid", "waiting",
                                            result="paid – given when the player is online"):
                await self._dm(order["user_id"], f"✅ Thanks for your purchase! **{safe_md(order['title'])}** is "
                                                 "paid – you get it in-game the next time you're on the server.")
            return None
        template = self.bot.config.kit_give_template
        try:
            await self.bot.rcon.command(template.format(kit=kitparse.clean_arg(skit["kit"]), name=name),
                                        expect_response=False)
        except RconError:
            self.bot.db.move_store_order(order["id"], "paid", "waiting", result="paid – server not reachable, retrying")
            return None
        return skit["kit"]

    async def deliver_waiting(self, player: str | None = None) -> int:
        """Gives in-game kits that wait for their buyer (only `player`, or everyone online). Returns how many."""
        done = 0
        online = {n.lower() for n in self.bot.online_players}
        for order in self.bot.db.store_orders(status="waiting", limit=200):
            buyer = (order["player"] or "").lower()
            if buyer not in online or (player is not None and buyer != player.lower()):
                continue
            if self.bot.db.move_store_order(order["id"], "waiting", "paid") and await self.fulfil(order["id"]):
                done += self.bot.db.store_order(order["id"])["status"] == "delivered"
        return done

    @commands.Cog.listener()
    async def on_rce_player_join(self, player: str) -> None:
        if any((o["player"] or "").lower() == player.lower() for o in self.bot.db.store_orders(status="waiting")):
            await asyncio.sleep(JOIN_DELAY)
            await self.deliver_waiting(player)

    async def retry(self, order_id: int) -> bool:
        if not self.bot.db.move_store_order(order_id, ("failed", "review", "waiting"), "paid"):
            return False
        return await self.fulfil(order_id)

    async def _dm(self, user_id: int, text: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=discord.Embed(colour=COLOUR, description=text))
        except discord.HTTPException:
            pass

    async def _log(self, text: str, *, alert: bool = False) -> None:
        cfg = self.bot.config
        channel = self.bot.get_channel(cfg.store_channel_id) if cfg.store_channel_id else None
        if channel is None:
            self.bot.audit(text)
            return
        roles = [discord.Object(r) for r in cfg.alert_role_ids] if alert else []
        try:
            await channel.send((" ".join(f"<@&{r.id}>" for r in roles) + " " if roles else "") + text,
                               allowed_mentions=discord.AllowedMentions(roles=roles, users=False))
        except discord.HTTPException as exc:
            log.warning("Store log message failed: %s", exc)

    # ------------------------------------------------------------ Discord: gift cards

    def is_owner(self, user) -> bool:
        """Owners may create and manage gift cards: the Discord server owner, Administrator permission or
        ADMIN_USER_IDS."""
        if user.id in self.bot.config.admin_user_ids:
            return True
        guild = getattr(user, "guild", None)
        if guild is not None and getattr(guild, "owner_id", None) == user.id:
            return True
        perms = getattr(user, "guild_permissions", None)
        return bool(perms is not None and perms.administrator)

    async def _owners_only(self, interaction: discord.Interaction) -> bool:
        if self.is_owner(interaction.user):
            return True
        await interaction.response.send_message("⛔ Only owners can do this (server owner or Administrator).",
                                                ephemeral=True)
        return False

    @staticmethod
    def _card_line(row) -> str:
        state = ("disabled" if row["disabled"] else "expired" if row["expires_at"] and row["expires_at"] <= time.time()
                 else "used up" if row["balance"] <= 0 else "active")
        until = f" · until <t:{row['expires_at']}:d>" if row["expires_at"] else ""
        return (f"`{row['code']}` – {pricing.format_money(row['balance'], row['currency'])} of "
                f"{pricing.format_money(row['initial'], row['currency'])} · {state}{until}"
                + (f" · {safe_md(row['note'])}" if row["note"] else ""))

    @giftcard.command(name="create", description="Create gift cards for the web store (owners).")
    @app_commands.describe(value="Value of each card (store currency)", count="How many (1-25)",
                           days="Valid for N days (0 = no end)", note="Note, e.g. giveaway prize")
    async def gc_create(self, interaction: discord.Interaction, value: app_commands.Range[float, 0.01, 10000.0],
                        count: app_commands.Range[int, 1, 25] = 1, days: app_commands.Range[int, 0, 3650] = 0,
                        note: str | None = None) -> None:
        if not await self._owners_only(interaction):
            return
        try:
            codes = self.create_gift_cards(value, count, str(interaction.user), days=days, note=note or "")
        except CheckoutError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        self.bot.audit(f"🎁 {safe_md(str(interaction.user))} created {len(codes)} gift card(s) of {self.fmt(value)} "
                       "in Discord")
        embed = discord.Embed(title=f"🎁 {len(codes)} gift card(s) – {self.fmt(value)} each", colour=COLOUR,
                              description="```\n" + "\n".join(codes) + "\n```")
        embed.set_footer(text=("Valid for " + f"{days} days" if days else "No end date") +
                         " · only you can see this · players use the code at checkout on the website")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giftcard.command(name="list", description="The newest gift cards and their balance (owners).")
    async def gc_list(self, interaction: discord.Interaction) -> None:
        if not await self._owners_only(interaction):
            return
        rows = self.bot.db.gift_cards(limit=15)
        embed = discord.Embed(title="🎁 Gift cards", colour=COLOUR,
                              description="\n".join(self._card_line(r) for r in rows)[:4000] or "None yet.")
        embed.set_footer(text="All of them: web interface → Store → Gift cards")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giftcard.command(name="disable", description="Disable (or enable again) a gift card (owners).")
    @app_commands.describe(code="The gift card code", disabled="True = can't be used any more")
    async def gc_disable(self, interaction: discord.Interaction, code: str, disabled: bool = True) -> None:
        if not await self._owners_only(interaction):
            return
        code = normalize_code(code)
        if not self.bot.db.set_gift_card_disabled(code, disabled):
            await interaction.response.send_message("⚠️ No gift card with that code.", ephemeral=True)
            return
        self.bot.audit(f"🎁 {safe_md(str(interaction.user))} {'disabled' if disabled else 'enabled'} gift card "
                       f"{code[:4]}…")
        await interaction.response.send_message(f"✅ `{code}` is {'disabled' if disabled else 'usable again'}.",
                                                ephemeral=True)

    @giftcard.command(name="check", description="How much is left on a gift card?")
    @app_commands.describe(code="The gift card code")
    async def gc_check(self, interaction: discord.Interaction, code: str) -> None:
        try:
            row = self.gift_card(code, f"discord:{interaction.user.id}")
        except CheckoutError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"🎁 {self._card_line(row)}", ephemeral=True)

    # ------------------------------------------------------------ Background check

    async def check_pending(self, now: int | None = None) -> None:
        """Buyers who paid but closed the page get their order anyway; never-paid orders expire."""
        now = int(time.time()) if now is None else now
        for order in self.bot.db.store_orders(status="pending", limit=200):
            age = now - order["created_at"]
            if order["paypal_order"] and age >= CHECK_AFTER and self.paypal is not None:
                try:
                    state = (await self.paypal.get_order(order["paypal_order"])).get("status")
                except PayPalError:
                    state = None
                if state in ("APPROVED", "COMPLETED"):
                    await self.complete(order["token"])
                    continue
            if age >= PENDING_EXPIRE:
                self._release(order["id"], "expired", "not paid within 3 hours")

    @tasks.loop(minutes=5)
    async def check_loop(self) -> None:
        await self.check_pending()
        await self.deliver_waiting()
        await self.sync_kits()

    @check_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(WebShop(bot))
