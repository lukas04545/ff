"""Bought custom kits: the ticket AI files a request, staff approves it with a button, the buyer gets the kit role.

Flow
  1. A player writes in their ticket that they bought a custom kit (store, order ID …).
  2. The AI calls request_kit_access -> the bot posts "🛒 Kit purchase – staff approval needed" in the ticket with
     ✅ Approve / ✖️ Reject buttons and pings the support roles. Nothing is given yet.
  3. Staff clicks Approve -> the kit gets a Discord role (its existing claim role, else an existing role with the
     kit's name, else a newly created one, linked as claim role), the buyer gets that role (for N days if the
     purchase was time-limited) and can claim the kit with /customkit claim.
Requests survive restarts (database); each can only be decided once.

Tip4Serv (optional, TIP4SERV_KEY): purchases arrive as "rcebot ..." commands (cogs/store.py). A request for a NEW
custom kit is priced with the price list (prices.txt); when an unused payment of the buyer covers the price, the
bot approves it by itself (TIP4SERV_AUTO_APPROVE) and the payment can't be used again. Giveaway prizes
(won_in_giveaway) are never checked against payments – staff always approves them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from permcheck import explain_forbidden
from rce import items as itemlist
from rce import kits as kitparse
from storage import raw_values
from support import pricing
from utils import is_admin, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.kit_requests")

COLOUR = discord.Colour(0xE0A23B)
MAX_NEW_KIT_ITEMS = 40
MAX_AMOUNT = 1_000_000
STATUS_TEXT = {"pending": "⏳ Waiting for staff", "approved": "✅ Approved", "rejected": "✖️ Rejected"}
AUTO_BY = "Tip4Serv (automatic)"
PAYMENT_MAX_AGE_DAYS = 30        # only payments from the last 30 days can confirm a request


class KitRequestError(Exception):
    """Request can't be filed / approved – message for the AI or staff."""


_DATE = re.compile(r"\d{1,4}([-./]\d{1,2}){1,2}([-./]\d{2,4})?")


def order_ids(text: str) -> set[str]:
    """Order / transaction IDs in what the player wrote (e.g. tbx-1a2b3c4d, 5RX12345AB678901C): at least 6
    characters with letters and digits, or 8+ digits. Dates and amounts don't count."""
    found = set()
    for tok in re.findall(r"[A-Za-z0-9-]{6,}", text or ""):
        if _DATE.fullmatch(tok):
            continue
        has_digit, has_alpha = any(c.isdigit() for c in tok), any(c.isalpha() for c in tok)
        if (has_digit and has_alpha) or (tok.isdigit() and len(tok) >= 8):
            found.add(tok.lower())
    return found


def resolve_items(items: list) -> tuple[list[dict], list[dict]]:
    """Buyer's item list [{item, amount}] -> (resolved [{short, label, amount}], problems for the AI)."""
    resolved, unknown = [], []
    for entry in items[:MAX_NEW_KIT_ITEMS]:
        text = str((entry or {}).get("item") or "").strip() if isinstance(entry, dict) else str(entry)
        try:
            amount = int(entry.get("amount", 1)) if isinstance(entry, dict) else 1
        except (TypeError, ValueError):
            amount = 0
        short, candidates = itemlist.resolve(text)
        if short is None or not 1 <= amount <= MAX_AMOUNT:
            unknown.append({"item": text, "amount": amount,
                            "problem": "amount must be 1–1,000,000" if short else "unknown item",
                            "candidates": [f"{s} ({label})" for s, label in candidates]})
            continue
        resolved.append({"short": short, "label": itemlist.COMMON_ITEMS[short], "amount": amount})
    if len(items) > MAX_NEW_KIT_ITEMS:
        unknown.append({"item": f"{len(items) - MAX_NEW_KIT_ITEMS} more entries",
                        "problem": f"at most {MAX_NEW_KIT_ITEMS} items per kit"})
    return resolved, unknown


class KitRequestView(discord.ui.View):
    """Persistent Approve / Reject buttons – the request is found via the message."""

    def __init__(self, cog: "KitRequests") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _decide(self, interaction: discord.Interaction, approve: bool) -> None:
        if not self.cog.can_decide(interaction.user):
            roles = " ".join(f"<@&{r}>" for r in self.cog.approver_role_ids()) or "staff"
            await interaction.response.send_message(f"⛔ Only {roles} can decide kit purchases.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        row = self.cog.bot.db.kit_request_by_message(interaction.message.id) if interaction.message else None
        if row is None:
            await interaction.response.send_message("Request not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await self.cog.decide(row["id"], approve, interaction.user)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + text[:1900], ephemeral=True)

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, custom_id="rce:kitreq:approve")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._decide(interaction, True)

    @discord.ui.button(label="Reject", emoji="✖️", style=discord.ButtonStyle.secondary, custom_id="rce:kitreq:reject")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._decide(interaction, False)


class KitRequests(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._prices: tuple[float, pricing.PriceList | None] | None = None

    async def cog_load(self) -> None:
        self.bot.add_view(KitRequestView(self))

    def price_list(self) -> pricing.PriceList | None:
        """prices.txt (re-read when it changes); None = no price list."""
        path = self.bot.config.price_list_file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if self._prices is None or self._prices[0] != mtime:
            self._prices = (mtime, pricing.load(path))
        return self._prices[1]

    def quote(self, resolved: list[dict], timer_hours: int | None,
              existing: list[dict] | None = None) -> pricing.Quote | None:
        prices = self.price_list()
        return pricing.quote(prices, resolved, timer_hours, existing) if prices else None

    def _kit_contents(self, kit: str) -> list[dict]:
        return [{"short": i["shortname"], "amount": i["amount"]} for i in self.bot.db.custom_kit_items(kit)]

    @staticmethod
    def quote_for_ai(q: pricing.Quote) -> dict:
        money = lambda v: pricing.format_money(v, q.currency)  # noqa: E731
        return {"total_price": money(q.total),
                "claim_timer": f"{q.timer_hours}h" + (f" (+{money(q.timer_price)})" if q.timer_price else "")
                if q.timer_hours else None,
                "lines": [f"{line['item']} ×{line['amount']:,}: " + ("no price – staff decides" if line["price"] is None
                          else "included" if line.get("note") else money(line["price"])) for line in q.lines],
                "items_without_price": q.unpriced, "over_limits": q.over_cap}

    def is_staff(self, user: discord.abc.User) -> bool:
        tickets = self.bot.get_cog("Tickets")
        if tickets is not None:
            return tickets.is_staff(user)
        return is_admin(self.bot, user)

    def approver_role_ids(self) -> frozenset[int]:
        cfg = self.bot.config
        return cfg.kit_approver_role_ids or cfg.support_role_ids

    def can_decide(self, user: discord.abc.User) -> bool:
        """Approve/Reject: the chosen approver roles (KIT_APPROVER_ROLE_IDS) + bot admins; otherwise staff."""
        if not self.bot.config.kit_approver_role_ids:
            return self.is_staff(user)
        roles = {r.id for r in getattr(user, "roles", [])}
        return bool(roles & self.bot.config.kit_approver_role_ids) or is_admin(self.bot, user)

    # ------------------------------------------------------------ Filing (ticket AI)

    def _order_reused(self, proof: str) -> dict | None:
        """One purchase = one kit: an order ID that another pending/approved purchase request already used."""
        ids = order_ids(proof)
        if not ids:
            return None
        for r in self.bot.db.kit_requests(limit=1000):
            if r["status"] in ("pending", "approved") and r["source"] == "purchase" and ids & order_ids(r["proof"]):
                same = sorted(ids & order_ids(r["proof"]))[0]
                return {"ok": False, "result": "order_already_used", "order_id": same, "used_for_request": r["id"],
                        "message": f"This order ID was already used for request #{r['id']} ({r['kit']}, "
                                   f"{r['status']}). One purchase gives one kit – tell the player; if it's a "
                                   "different purchase they need its own order ID, otherwise call staff."}
        return None

    async def file(self, channel: discord.abc.Messageable, ticket, member: discord.Member, kit_name: str,
                   proof: str, days: int = 0, *, giveaway: bool = False, checkout_name: str | None = None,
                   staff_ok: bool = False) -> dict:
        """Creates a pending request and posts it with buttons in the ticket. Result dict for the AI."""
        db = self.bot.db
        row = db.custom_kit(kit_name.strip())
        if row is None:
            return {"ok": False, "result": "unknown_kit", "available_custom_kits": [r["name"] for r in db.custom_kits()],
                    "message": "There is no custom kit with that name – ask which one they bought."}
        kit = row["name"]
        custom = self.bot.get_cog("CustomKits")
        if custom is not None and custom.can_claim(member, row) and not days:
            return {"ok": False, "result": "already_has_access", "kit": kit,
                    "message": "They already have access – they can claim it with /customkit claim."}
        if staff_ok:
            proof = f"{proof} [allowed by staff via /aiconfirm]"
        elif not giveaway and (reused := self._order_reused(proof)):
            return reused
        open_req = [r for r in db.kit_requests(status="pending", user_id=member.id) if r["kit"].lower() == kit.lower()]
        if open_req:
            return {"ok": True, "result": "already_waiting_for_staff", "request_id": open_req[0]["id"], "kit": kit}
        request_id = db.add_kit_request(ticket["id"] if ticket else None, member.id, kit, max(0, int(days)),
                                        proof.strip()[:500], source="giveaway" if giveaway else "purchase",
                                        checkout_name=(checkout_name or "").strip()[:64] or None)
        await self._post(channel, request_id)
        self.bot.audit(f"{'🎉 Giveaway prize' if giveaway else '🛒 Kit purchase'} request #{request_id}: "
                       f"<@{member.id}> → **{safe_md(kit)}** ({safe_md(proof[:150])}) – waiting for staff")
        result = {"ok": True, "result": "waiting_for_staff", "request_id": request_id, "kit": kit,
                  "message": "Posted for staff approval. Tell the player staff will check it and confirm."}
        result.update(await self.check_payment(request_id))
        return result

    async def file_new(self, channel: discord.abc.Messageable, ticket, member: discord.Member, kit_name: str,
                       items: list, proof: str, days: int = 0, *, timer_hours: int | None = None,
                       giveaway: bool = False, checkout_name: str | None = None, staff_ok: bool = False) -> dict:
        """Request for a NEW custom kit with the buyer's item list. Unknown items go back to the AI first."""
        db = self.bot.db
        resolved, unknown = resolve_items(items)
        if unknown or not resolved:
            return {"ok": False, "result": "fix_items", "resolved": resolved, "unknown": unknown,
                    "message": "Not filed yet: fix or ask about these entries (use find_items), then call again "
                               "with the complete list."}
        waiting = [r for r in db.kit_requests(status="pending", user_id=member.id)
                   if r["items"] and not r["addon"] and r["source"] == ("giveaway" if giveaway else "purchase")]
        if staff_ok:      # staff confirmed this exception with /aiconfirm
            waiting, proof = [], f"{proof} [allowed by staff via /aiconfirm]"
        if waiting:   # one purchase = one kit: never two open new-kit requests that one payment could stand for
            return {"ok": False, "result": "already_waiting_for_staff", "request_id": waiting[0]["id"],
                    "kit": waiting[0]["kit"],
                    "message": "They already have a new custom kit waiting for approval – it has to be decided "
                               "first. If they bought a second kit, staff handles it."}
        if not giveaway and not staff_ok and (reused := self._order_reused(proof)):
            return reused
        q = self.quote(resolved, timer_hours)
        if q is not None and q.over_cap:
            return {"ok": False, "result": "over_limits", "over_limits": q.over_cap, "price": self.quote_for_ai(q),
                    "message": "Not filed: the server's custom kit limits are exceeded – tell the player the limits "
                               "and ask them to lower the amounts (or pick an available claim timer)."}
        name = kitparse.clean_arg(kit_name or "")[:40] or f"{member.display_name} kit"
        base, n = name, 2
        while db.custom_kit(name) is not None or any(r["kit"].lower() == name.lower()
                                                     for r in db.kit_requests(status="pending")):
            name, n = f"{base} {n}", n + 1
        request_id = db.add_kit_request(
            ticket["id"] if ticket else None, member.id, name, max(0, int(days)), proof.strip()[:500],
            json.dumps(resolved), source="giveaway" if giveaway else "purchase",
            price=q.total if q else None, currency=q.currency if q else None,
            cooldown_minutes=q.timer_hours * 60 if q and q.timer_hours else None,
            checkout_name=(checkout_name or "").strip()[:64] or None)
        await self._post(channel, request_id)
        self.bot.audit(f"{'🎉 Giveaway prize' if giveaway else '🛒 New custom kit'} request #{request_id}: "
                       f"<@{member.id}> → **{safe_md(name)}** ({len(resolved)} items"
                       + (f", {pricing.format_money(q.total, q.currency)}" if q else "")
                       + f", {safe_md(proof[:150])}) – waiting for staff")
        result = {"ok": True, "result": "waiting_for_staff", "request_id": request_id, "kit": name, "items": resolved,
                  "message": "Posted for staff approval with the item list. Tell the player staff will check it; "
                             "after approval the kit is created and they get its role."}
        if q is not None:
            result["price"] = self.quote_for_ai(q)
        result.update(await self.check_payment(request_id))
        return result

    async def file_addon(self, channel: discord.abc.Messageable, ticket, member: discord.Member, kit_name: str,
                         items: list, proof: str, *, giveaway: bool = False,
                         checkout_name: str | None = None, staff_ok: bool = False) -> dict:
        """Request to ADD items to an existing custom kit – only for the member who originally bought it."""
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        db = self.bot.db
        mine = db.kits_bought_by(member.id)
        row = db.custom_kit(kit_name.strip())
        if row is None:
            return {"ok": False, "result": "unknown_kit", "kits_you_bought": mine,
                    "message": "There is no custom kit with that name – ask which of their kits they mean."}
        kit = row["name"]
        if db.kit_buyer(kit) != member.id:
            return {"ok": False, "result": "not_your_kit", "kits_you_bought": mine,
                    "message": "Only the member who originally bought this kit can buy add-ons for it. Tell them "
                               "that – staff can still change kits by hand."}
        resolved, unknown = resolve_items(items)
        if unknown or not resolved:
            return {"ok": False, "result": "fix_items", "resolved": resolved, "unknown": unknown,
                    "message": "Not filed yet: fix or ask about these entries (use find_items), then call again "
                               "with the complete list of items to ADD."}
        if staff_ok:
            proof = f"{proof} [allowed by staff via /aiconfirm]"
        elif not giveaway and (reused := self._order_reused(proof)):
            return reused
        current = self._kit_contents(kit)
        if len({i["short"] for i in current} | {i["short"] for i in resolved}) > MAX_ITEMS_PER_KIT:
            return {"ok": False, "result": "too_many_items",
                    "message": f"A custom kit can hold at most {MAX_ITEMS_PER_KIT} different items."}
        q = self.quote(resolved, None, existing=current)       # add-ons keep the kit's claim timer
        if q is not None and q.over_cap:
            return {"ok": False, "result": "over_limits", "over_limits": q.over_cap, "price": self.quote_for_ai(q),
                    "message": "Not filed: with these items the kit would exceed the server's custom kit limits – "
                               "tell the player the limits and ask them to lower the amounts."}
        if any(r["kit"].lower() == kit.lower() and r["addon"]
               for r in db.kit_requests(status="pending", user_id=member.id)):
            return {"ok": False, "result": "already_waiting_for_staff", "kit": kit,
                    "message": "An add-on for this kit is already waiting for approval – wait until it is decided."}
        request_id = db.add_kit_request(
            ticket["id"] if ticket else None, member.id, kit, 0, proof.strip()[:500], json.dumps(resolved),
            source="giveaway" if giveaway else "purchase", price=q.total if q else None,
            currency=q.currency if q else None, checkout_name=(checkout_name or "").strip()[:64] or None, addon=True)
        await self._post(channel, request_id)
        self.bot.audit(f"➕ Kit add-on request #{request_id}: <@{member.id}> → **{safe_md(kit)}** (+{len(resolved)} "
                       "items" + (f", {pricing.format_money(q.total, q.currency)}" if q else "")
                       + f"{', giveaway prize' if giveaway else ''}) – waiting for staff")
        result = {"ok": True, "result": "waiting_for_staff", "request_id": request_id, "kit": kit, "items": resolved,
                  "message": "Posted for staff approval. After approval the items are added to their kit."}
        if q is not None:
            result["price"] = self.quote_for_ai(q)
        result.update(await self.check_payment(request_id))
        return result

    # ------------------------------------------------------------ Tip4Serv payments

    @staticmethod
    def _order_tokens(proof: str | None) -> set[str]:
        """Order/transaction IDs the player gave ("#1110065", "tbx-…") – compared with every value Tip4Serv sent."""
        return {t.lower() for t in re.findall(r"[A-Za-z0-9-]{4,}", proof or "") if any(c.isdigit() for c in t)}

    def _matching_payments(self, req) -> list:
        """Unused store payments that belong to the request's buyer (oldest first)."""
        db, cfg = self.bot.db, self.bot.config
        names: set[str] = set()
        if cfg.tip4serv_name_match:
            linked = db.linked_player(req["user_id"])
            names |= {n.casefold() for n in (linked, req["checkout_name"]) if n}
        tokens = self._order_tokens(req["proof"])
        since = int(time.time()) - PAYMENT_MAX_AGE_DAYS * 86400
        found = []
        for p in db.store_payments(unused=True, since=since, limit=500):
            if p["discord_id"]:                       # the store knows the buyer's Discord account: must be theirs
                ok = p["discord_id"] == req["user_id"]
            else:
                ok = cfg.tip4serv_name_match and (
                    any((p[f] or "").casefold() in names for f in ("player", "username", "customer") if p[f])
                    or p["payment_id"].lower().lstrip("#") in tokens
                    or bool({t for t in tokens if len(t) >= 5} & raw_values(p["raw"])))
            if ok and (not req["currency"] or not p["currency"] or p["currency"] == req["currency"]):
                found.append(p)
        return sorted(found, key=lambda p: p["received_at"])

    def _request_quote(self, req) -> pricing.Quote | None:
        new = self.new_items(req)
        if not new:
            return None
        if req["addon"]:
            current = self._kit_contents(req["kit"])
            return self.quote(new, None, existing=current)
        return self.quote(new, req["cooldown_minutes"] // 60 if req["cooldown_minutes"] else None)

    async def check_payment(self, request_id: int) -> dict:
        """Looks for the buyer's Tip4Serv payment; approves the request by itself when it covers the price.
        Returns info for the AI ({} when Tip4Serv isn't used or the request is a giveaway prize)."""
        db, cfg = self.bot.db, self.bot.config
        req = db.kit_request(request_id)
        if req is None or req["status"] != "pending" or req["source"] != "purchase" or not cfg.tip4serv_key:
            return {}
        payments = self._matching_payments(req)
        money = lambda v: pricing.format_money(v, req["currency"] or payments[0]["currency"] or "EUR")  # noqa: E731
        q = self._request_quote(req)
        price = q.total if q else None
        if not payments:
            store = self.bot.get_cog("Store")
            problem = store.problem() if store is not None else "the store connection is not loaded"
            if problem:            # purchases don't reach the bot at all – say so instead of "not yet"
                note = f"⚠️ Can't check Tip4Serv: {problem}. Staff, please check the purchase by hand."
                db.set_kit_request_payment(request_id, [], note)
                await self._update(request_id)
                return {"payment": "store_unavailable", "payment_note": note,
                        "tell_player": "The automatic payment check isn't available right now – the team checks "
                                       "the purchase by hand and approves it. Don't promise an automatic approval."}
            auto = cfg.tip4serv_auto_approve and q is not None and q.complete
            given = sorted(self._order_tokens(req["proof"]))[:3]
            note = ("⏳ No matching Tip4Serv payment found yet"
                    + (f" (order {', '.join(given)} not received from Tip4Serv)" if given else "") + " – "
                    + ("approved automatically as soon as it arrives." if auto else "staff checks the purchase."))
            db.set_kit_request_payment(request_id, [], note)
            await self._update(request_id)
            return {"payment": "not_found_yet", "payment_note": note,
                    "tell_player": "No matching store payment found yet – Tip4Serv can take a few minutes. " + (
                        "If it arrives (and matches their Discord account, linked player, checkout name or order "
                        "ID) the kit is approved automatically; otherwise the team checks it." if auto else
                        "Staff will check the purchase.") + " Ask for the name/gamertag used at checkout if unsure."}
        paid_total = sum(p["amount"] or 0 for p in payments)
        chosen = payments
        if price is not None:
            single = [p for p in payments if (p["amount"] or 0) + 0.005 >= price]
            chosen = [min(single, key=lambda p: p["amount"])] if single else payments
        paid = sum(p["amount"] or 0 for p in chosen)
        ids = [p["id"] for p in chosen]
        if price is None:
            note = f"💳 Paid {money(paid_total)} on Tip4Serv – no price list price for this kit, staff decides."
        elif paid + 0.005 < price:
            note = f"⚠️ Paid {money(paid)} on Tip4Serv – price {money(price)} – {money(price - paid)} missing."
        elif not q.complete:
            note = (f"💳 Paid {money(paid)} – price {money(price)} without the items that have no price "
                    f"({', '.join(q.unpriced)}) – staff decides.")
        elif not cfg.tip4serv_auto_approve:
            note = f"✅ Paid {money(paid)} on Tip4Serv – price {money(price)} – covered."
        else:
            note = f"✅ Paid {money(paid)} on Tip4Serv – price {money(price)} – approved automatically."
            if db.use_store_payments(ids, request_id):
                db.set_kit_request_payment(request_id, ids, note)
                ok, text = await self.decide(request_id, True, AUTO_BY)
                if ok:
                    return {"payment": "approved_automatically", "paid": money(paid), "price": money(price),
                            "tell_player": ("Payment found and it covers the price – the items were added to their "
                                            "kit." if req["addon"] else "Payment found and it covers the price – "
                                            "the kit is approved and created, they got its role.")
                                           + " They claim it with /customkit claim."}
                db.release_store_payments(request_id)
                note = f"⚠️ Paid {money(paid)} – covers the price, but approving failed: {text[:300]} – staff, " \
                       "please fix it and approve."
        db.set_kit_request_payment(request_id, ids, note)
        await self._update(request_id)
        return {"payment": "found", "paid": money(paid), "price": money(price) if price is not None else None,
                "payment_note": note, "tell_player": "Staff will check the purchase and confirm."}

    async def on_new_payments(self) -> None:
        """New Tip4Serv payments arrived: re-check the open purchase requests."""
        for req in reversed(self.bot.db.kit_requests(status="pending", limit=200)):
            if req["source"] == "purchase":
                try:
                    await self.check_payment(req["id"])
                except discord.HTTPException as exc:
                    log.warning("Payment check for request #%s failed: %s", req["id"], exc)

    async def _post(self, channel, request_id: int) -> None:
        guild = getattr(channel, "guild", None)
        roles = [r for rid in sorted(self.approver_role_ids()) if guild and (r := guild.get_role(rid))]
        overwrites = getattr(channel, "overwrites", None)
        for role in roles if overwrites is not None else []:       # approvers must be able to see the ticket
            if role not in overwrites:
                try:
                    await channel.set_permissions(role, view_channel=True, send_messages=True,
                                                  read_message_history=True, reason="Kit purchase approvers")
                except discord.HTTPException as exc:
                    log.warning("Could not add approver role %s to %s: %s", role.id, channel.id, exc)
        msg = await channel.send(content=" ".join(r.mention for r in roles) or None,
                                 embed=self.embed(self.bot.db.kit_request(request_id)), view=KitRequestView(self),
                                 allowed_mentions=discord.AllowedMentions(roles=roles))
        self.bot.db.set_kit_request_message(request_id, msg.channel.id, msg.id)

    @staticmethod
    def new_items(req) -> list[dict] | None:
        try:
            return json.loads(req["items"]) if req["items"] else None
        except (ValueError, TypeError):
            return None

    def embed(self, req) -> discord.Embed:
        status = req["status"]
        new = self.new_items(req)
        addon = bool(req["addon"])
        what = (f"an **add-on** for their custom kit **{safe_md(req['kit'])}**" if addon
                else f"a **new custom kit** – it will be created as **{safe_md(req['kit'])}**" if new
                else f"**{safe_md(req['kit'])}**")
        prize = req["source"] == "giveaway"
        kind = "🎉 Giveaway prize" if prize else "➕ Kit add-on" if addon else "🛒 Kit purchase"
        embed = discord.Embed(title=f"{kind} #{req['id']} – {STATUS_TEXT[status]}",
                              colour=COLOUR if status == "pending" else discord.Colour.dark_grey(),
                              description=f"<@{req['user_id']}> says they " + (f"won {what} in a giveaway." if prize
                                                                               else f"bought {what}."))
        embed.add_field(name="Duration", value=f"{req['days']} days" if req["days"] else "permanent")
        if req["cooldown_minutes"]:
            embed.add_field(name="Claim timer", value=f"{req['cooldown_minutes'] // 60}h")
        if req["price"] is not None:
            embed.add_field(name="Price (price list)", value=pricing.format_money(req["price"], req["currency"] or "EUR"))
        if req["checkout_name"]:
            embed.add_field(name="Name on the store", value=safe_md(req["checkout_name"]))
        if req["pay_note"]:
            embed.add_field(name="💳 Tip4Serv", value=safe_md(req["pay_note"])[:1024], inline=False)
        if req["proof"]:
            embed.add_field(name="Their info (check it!)", value=safe_md(req["proof"])[:1024], inline=False)
        if new:
            chunks, chunk = [], ""       # every item must be visible to staff: split into fields of ≤1024 chars
            for i in new:
                line = f"• {safe_md(i['label'])} (`{i['short']}`) ×{i['amount']:,}"
                if chunk and len(chunk) + len(line) + 1 > 1024:
                    chunks.append(chunk)
                    chunk = ""
                chunk = f"{chunk}\n{line}" if chunk else line
            chunks.append(chunk)
            for n, value in enumerate(chunks):
                embed.add_field(name=(f"Items to add ({len(new)}) – check them!" if addon else
                                      f"Items of the new kit ({len(new)}) – check them!") if n == 0 else "​",
                                value=value, inline=False)
        else:
            items = self.bot.db.custom_kit_items(req["kit"])
            if items:
                embed.add_field(name="Kit contents", inline=False, value=", ".join(
                    f"{i['shortname']} ×{i['amount']}" for i in items[:15])[:1024])
        if status == "pending":
            embed.set_footer(text=("Approve = these items are added to the kit (amounts of items it already has "
                                   "go up)." if addon else
                                   "Approve = the kit is created with these items (edit it later in the web interface → "
                                   "Kits), gets a role and the buyer gets it." if new else
                                   "Approve = the buyer gets the kit's role and can claim it with /customkit claim.")
                             + (" Giveaway prizes are always approved by staff." if prize else ""))
        else:
            role = f" · role <@&{req['role_id']}>" if req["role_id"] else ""
            embed.add_field(name=STATUS_TEXT[status], value=f"by {safe_md(req['decided_by'] or '?')}{role}",
                            inline=False)
        return embed

    # ------------------------------------------------------------ Deciding (staff)

    async def kit_role(self, guild: discord.Guild, kit: str) -> discord.Role:
        """The kit's Discord role: its claim role, else a role with the kit's name, else a new one – and links it."""
        db = self.bot.db
        row = db.custom_kit(kit)
        claim = db.claim_roles(row)
        for rid in claim:
            if (role := guild.get_role(rid)) is not None:
                return role
        role = discord.utils.find(lambda r: r.name.lower() == kit.lower(), guild.roles)
        if role is None:
            role = await guild.create_role(name=kit[:100], reason=f"Role for the custom kit {kit} (kit purchase)")
            log.info("Created role %s for custom kit %s", role.name, kit)
        db.set_custom_kit_roles(row["name"], [*claim, role.id])
        self.bot.audit(f"🛒 Custom kit **{safe_md(row['name'])}** is now claimable with the role {role.mention}")
        return role

    async def decide(self, request_id: int, approve: bool, by: discord.abc.User | str) -> tuple[bool, str]:
        db = self.bot.db
        req = db.kit_request(request_id)
        if req is None:
            return False, "Request not found."
        by_name = str(by)
        if approve and by_name != AUTO_BY and req["status"] == "pending" and req["payment_ids"]:
            taken = db.store_payments_used_elsewhere(req["payment_ids"].split(","), request_id)
            if taken:          # one purchase = one kit: that payment already paid for another request
                db.set_kit_request_payment(request_id, [], "⚠️ The payment found earlier was used for request "
                                           + ", ".join(f"#{p['used_by']}" for p in taken) + " in the meantime.")
                await self.check_payment(request_id)
                return False, (f"Not approved: the Tip4Serv payment for this request was already used for request "
                               f"#{taken[0]['used_by']} – one purchase gives one kit. Ask the player for their payment "
                               "or reject.")
        if not db.decide_kit_request(request_id, "approved" if approve else "rejected", by_name):
            return False, f"Request #{request_id} was already {req['status']} by {req['decided_by']}."
        channel = self.bot.get_channel(req["channel_id"]) if req["channel_id"] else None
        if not approve:
            await self._update(req["id"])
            await self._say(channel, f"✖️ The purchase of **{safe_md(req['kit'])}** was not confirmed by the staff "
                                     f"({safe_md(by_name)}). Please reply here if you think that's a mistake.")
            self.bot.audit(f"🛒 Kit purchase #{request_id} rejected by {safe_md(by_name)}")
            return True, f"Request #{request_id} rejected."
        try:
            text = await self._grant(req, by_name)
        except KitRequestError as exc:
            db.reopen_kit_request(request_id)          # nothing given – staff can fix it and approve again
            return False, str(exc)
        if req["payment_ids"] and by_name != AUTO_BY:     # staff approved: the found payment is used up too
            db.use_store_payments(req["payment_ids"].split(","), req["id"])
        await self._update(req["id"])
        what = "Prize" if req["source"] == "giveaway" else "Add-on" if req["addon"] else "Purchase"
        await self._say(channel, f"✅ 🎁 {what} confirmed by {safe_md(by_name)}: {text} – claim it with "
                                 f"`/customkit claim {req['kit']}` while you're online in-game.")
        return True, f"Request #{request_id} approved: {text}"

    def _add_items(self, req, items: list[dict], by: str) -> str:
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        db = self.bot.db
        if db.custom_kit(req["kit"]) is None:
            raise KitRequestError("The kit no longer exists.")
        have = {i["short"] for i in self._kit_contents(req["kit"])}
        if len(have | {i["short"] for i in items}) > MAX_ITEMS_PER_KIT:
            raise KitRequestError(f"The kit would hold more than {MAX_ITEMS_PER_KIT} different items.")
        for item in items:
            db.increase_custom_kit_item(req["kit"], item["short"], int(item["amount"]))
        self.bot.audit(f"➕ {len(items)} items added to custom kit **{safe_md(req['kit'])}** (add-on #{req['id']}, "
                       f"approved by {safe_md(by)})")
        return f"{len(items)} items were added to **{safe_md(req['kit'])}** for <@{req['user_id']}>"

    async def _grant(self, req, by: str) -> str:
        channel = self.bot.get_channel(req["channel_id"]) if req["channel_id"] else None
        guild = getattr(channel, "guild", None) or (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None:
            raise KitRequestError("The Discord server is not available.")
        new = self.new_items(req)
        if req["addon"]:
            return self._add_items(req, new or [], by)
        if new is not None:
            marker = f"Bought by user {req['user_id']} (request #{req['id']})"
            existing = self.bot.db.custom_kit(req["kit"])
            if existing is None:
                self.bot.db.create_custom_kit(req["kit"], marker, by)
                for item in new:
                    self.bot.db.add_custom_kit_item(req["kit"], item["short"], int(item["amount"]))
                if req["cooldown_minutes"]:
                    self.bot.db.set_custom_kit_cooldown(req["kit"], int(req["cooldown_minutes"]))
                self.bot.audit(f"🛒 Custom kit **{safe_md(req['kit'])}** created ({len(new)} items, request "
                               f"#{req['id']}, approved by {safe_md(by)})")
            elif existing["description"] != marker:   # same marker = created by an earlier, failed approval: reuse
                raise KitRequestError(f"A custom kit called {req['kit']} exists by now – reject this request and ask "
                                      "the AI to file it again.")
        elif self.bot.db.custom_kit(req["kit"]) is None:
            raise KitRequestError("The kit no longer exists.")
        member = guild.get_member(req["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(req["user_id"])
            except discord.HTTPException:
                raise KitRequestError("The buyer is no longer on the Discord server.") from None
        try:
            role = await self.kit_role(guild, req["kit"])
        except discord.Forbidden as exc:
            raise KitRequestError("Could not create the kit role: " + explain_forbidden(exc, self.bot)) from exc
        self.bot.db.set_kit_request_role(req["id"], role.id)
        reason = f"Bought custom kit {req['kit']} (request #{req['id']}, approved by {by})"
        if req["days"]:
            timed = self.bot.get_cog("TimedRoles")
            if timed is None:
                raise KitRequestError("Timed roles are not available.")
            from cogs.timed_roles import TimedRoleError
            try:
                until = await timed.grant(member, role, req["days"] * 1440, reason=reason, by=by)
            except TimedRoleError as exc:
                raise KitRequestError(str(exc)) from exc
            text = f"{member.mention} has {role.mention} until <t:{until}:f>"
        else:
            if role >= guild.me.top_role:
                raise KitRequestError(f"The role {role.name} is above the bot's role – drag the bot role higher.")
            try:
                await member.add_roles(role, reason=reason[:500])
            except discord.Forbidden as exc:
                raise KitRequestError(explain_forbidden(exc, self.bot)) from exc
            text = f"{member.mention} now has {role.mention}"
        self.bot.audit(f"🛒 Kit purchase #{req['id']} approved by {safe_md(by)}: {text} (kit **{safe_md(req['kit'])}**)")
        return text

    async def _update(self, request_id: int) -> None:
        req = self.bot.db.kit_request(request_id)
        channel = self.bot.get_channel(req["channel_id"]) if req["channel_id"] else None
        if channel is None or not req["message_id"]:
            return
        try:
            msg = await channel.fetch_message(req["message_id"])
            await msg.edit(embed=self.embed(req), view=None if req["status"] != "pending" else KitRequestView(self))
        except discord.HTTPException as exc:
            log.debug("Kit request message not updated: %s", exc)

    async def _say(self, channel, text: str) -> None:
        if channel is None:
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(KitRequests(bot))
