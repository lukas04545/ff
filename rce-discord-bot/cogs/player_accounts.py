"""Player accounts on the website (/account): players log in with a one-time code the bot DMs to their linked
Discord account, then manage their own custom kits – add items, buy team slots per month and pick who sits on them.

Login: the player enters their in-game name (linked with /customkit link). The bot DMs a 6-digit code to that
Discord account; entering it starts a session (a cookie; only a hash of it is stored). No password, no OAuth.

Team slots: a slot on a player's own custom kit lets one more player (a teammate) claim that kit, until the slot
runs out. Slots are bought per month in the web store (cogs/webshop.py); the owner assigns/clears them here. When a
slot expires the bot takes the kit role back (unless that member still has the kit another way) and DMs the owner.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from web.ratelimit import RateLimiter

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.accounts")

CODE_TTL = 600                 # a login code is valid for 10 minutes
CODE_TRIES = 5                 # wrong codes before a new one must be requested
SESSION_DAYS = 30
SESSION_TTL = SESSION_DAYS * 86400


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountError(Exception):
    """Message shown to the player."""


class PlayerAccounts(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._codes: dict[str, dict] = {}                 # player name (lower) -> {code, user_id, name, expires, tries}
        self._code_requests = RateLimiter(5, 900)         # codes requested per visitor IP
        self._codes_per_player = RateLimiter(3, 900)      # codes DMed per player (many IPs can't flood his DMs)
        self._logins = RateLimiter(10, 900)               # verify attempts per visitor IP

    async def cog_load(self) -> None:
        self.expire_slots.start()

    def cog_unload(self) -> None:
        self.expire_slots.cancel()

    @property
    def enabled(self) -> bool:
        return bool(self.bot.config.player_accounts)

    @property
    def guild(self) -> discord.Guild | None:
        gid = self.bot.config.guild_id
        return (self.bot.get_guild(gid) if gid else None) or (self.bot.guilds[0] if self.bot.guilds else None)

    # ------------------------------------------------------------ Login

    async def send_code(self, player: str, ip: str = "?") -> str:
        """DMs a one-time code to the linked Discord account. Returns the (masked) target, raises AccountError."""
        if not self.enabled:
            raise AccountError("Player login is switched off.")
        player = " ".join(str(player or "").split())[:64]
        if not player:
            raise AccountError("Enter your in-game name.")
        if self._code_requests.hit(ip) is not None:
            raise AccountError("Too many codes requested – please wait a few minutes.")
        user_id = self.bot.db.player_owner(player)
        if user_id is None:
            raise AccountError(f"{player} isn't linked to a Discord account yet. Join our Discord and link it with "
                               "/customkit link first.")
        name = self.bot.db.linked_player(user_id) or player
        if self._codes_per_player.hit(str(user_id)) is not None:
            raise AccountError("A code was sent several times already – check your Discord DMs or wait a few "
                               "minutes.")
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None
        if user is None:
            raise AccountError("Couldn't reach your Discord account. Are you still on the server?")
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            await user.send(embed=discord.Embed(
                title="🔑 Website login code", colour=discord.Colour.blurple(),
                description=(f"Your login code for **{name}** is:\n\n# {code}\n\nIt's valid for 10 minutes. "
                            "If you didn't ask for it, ignore this message – nobody can log in without it.")))
        except discord.HTTPException:
            raise AccountError("I couldn't DM you the code – turn on direct messages from server members and try "
                               "again.") from None
        self._codes[name.lower()] = {"code": code, "user_id": user_id, "name": name,
                                     "expires": time.time() + CODE_TTL, "tries": 0}
        log.info("Login code sent to %s (user %s) for %s", name, user_id, ip)
        return name

    def verify(self, player: str, code: str, ip: str = "?") -> tuple[str, int, str]:
        """Checks the code and starts a session. Returns (cookie token, user_id, name); raises AccountError."""
        if self._logins.hit(ip) is not None:
            raise AccountError("Too many attempts – please wait a few minutes.")
        player = " ".join(str(player or "").split())[:64]
        pending = self._codes.get(player.lower())
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        if pending is None or pending["expires"] < time.time():
            self._codes.pop(player.lower(), None)
            raise AccountError("No code for that name, or it expired. Request a new one.")
        pending["tries"] += 1
        if pending["tries"] > CODE_TRIES:
            self._codes.pop(player.lower(), None)
            raise AccountError("Too many wrong codes. Request a new one.")
        if not code or not secrets.compare_digest(code, pending["code"]):
            raise AccountError("Wrong code. Check your Discord DMs and try again.")
        self._codes.pop(player.lower(), None)
        token = secrets.token_urlsafe(32)
        self.bot.db.add_player_session(_hash(token), pending["user_id"], pending["name"],
                                       int(time.time()) + SESSION_TTL)
        return token, pending["user_id"], pending["name"]

    def session(self, token: str | None):
        """The logged-in player's session row (or None)."""
        if not token:
            return None
        return self.bot.db.player_session(_hash(token))

    def logout(self, token: str | None) -> None:
        if token:
            self.bot.db.delete_player_session(_hash(token))

    # ------------------------------------------------------------ Account data

    def my_kits(self, user_id: int) -> list[dict]:
        """The custom kits this player owns (bought), with items and their team slots."""
        db = self.bot.db
        out = []
        for row in db.custom_kits():
            kit = row["name"]
            if db.kit_buyer(kit) != user_id:
                continue
            items = [{"short": i["shortname"], "amount": i["amount"]} for i in db.custom_kit_items(kit)]
            slots = [{"id": s["id"], "member_id": s["member_id"], "player": s["player"],
                      "expires_at": s["expires_at"]} for s in db.kit_slots(kit=kit)]
            out.append({"name": kit, "items": items, "slots": slots})
        return out

    # ------------------------------------------------------------ Team slots

    async def assign_slot(self, user_id: int, slot_id: int, teammate: str) -> str:
        """Owner puts a teammate (by in-game name) on a free slot and gives them the kit role. Returns their name."""
        db = self.bot.db
        slot = db.kit_slot(slot_id)
        if slot is None or slot["owner_id"] != user_id:
            raise AccountError("That slot isn't yours.")
        if slot["expires_at"] <= time.time():
            raise AccountError("That slot has expired – buy more time first.")
        teammate = " ".join(str(teammate or "").split())[:64]
        member_id = db.player_owner(teammate)
        if member_id is None:
            raise AccountError(f"{teammate} isn't linked to a Discord account (they link it with /customkit link).")
        if member_id == user_id:
            raise AccountError("You already have your own kit – use a slot for a teammate.")
        name = db.linked_player(member_id) or teammate
        if any(s["member_id"] == member_id for s in db.kit_slots(kit=slot["kit"])):
            raise AccountError(f"{name} already has a slot on this kit.")
        await self._grant_role(slot["kit"], member_id)
        db.set_kit_slot_member(slot_id, member_id, name)
        self.bot.audit(f"🪑 Team slot on **{slot['kit']}**: <@{user_id}> put <@{member_id}> ({name}) on a slot")
        await self._dm(member_id, f"🎒 You're on a team slot for the kit **{slot['kit']}** – claim it in-game with "
                                  f"`/customkit claim {slot['kit']}` while you're online.")
        return name

    async def clear_slot(self, user_id: int, slot_id: int) -> None:
        """Owner frees a slot and takes the kit role back (if the member doesn't keep it another way)."""
        db = self.bot.db
        slot = db.kit_slot(slot_id)
        if slot is None or slot["owner_id"] != user_id:
            raise AccountError("That slot isn't yours.")
        if slot["member_id"]:
            await self._revoke_role(slot["kit"], slot["member_id"], ignore_slot=slot_id)
            self.bot.audit(f"🪑 Team slot on **{slot['kit']}**: <@{user_id}> removed <@{slot['member_id']}>")
        db.set_kit_slot_member(slot_id, None, None)

    async def _grant_role(self, kit: str, member_id: int) -> None:
        guild = self.guild
        kr = self.bot.get_cog("KitRequests")
        if guild is None or kr is None:
            raise AccountError("The Discord server isn't available right now – try again later.")
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except discord.HTTPException:
                raise AccountError("That teammate isn't on the Discord server.") from None
        try:
            role = await kr.kit_role(guild, kit)
            if role >= guild.me.top_role:
                raise AccountError(f"The role {role.name} is above the bot's role – move the bot's role higher.")
            await member.add_roles(role, reason=f"Team slot on custom kit {kit}")
        except discord.Forbidden:
            raise AccountError("I couldn't give the kit role (missing permission).") from None

    async def _revoke_role(self, kit: str, member_id: int, *, ignore_slot: int | None = None) -> None:
        """Takes the kit role back, unless the member still owns the kit or holds another active slot on it."""
        db = self.bot.db
        if db.has_kit_purchase(member_id, kit):
            return
        now = time.time()
        if any(s["id"] != ignore_slot and s["member_id"] == member_id and s["expires_at"] > now
               for s in db.kit_slots(kit=kit)):
            return
        guild = self.guild
        kr = self.bot.get_cog("KitRequests")
        if guild is None or kr is None:
            return
        member = guild.get_member(member_id)
        row = db.custom_kit(kit)
        if member is None or row is None:
            return
        for rid in db.claim_roles(row):
            role = guild.get_role(rid)
            if role is not None and role in getattr(member, "roles", []):
                try:
                    await member.remove_roles(role, reason=f"Team slot on {kit} ended")
                except discord.HTTPException as exc:
                    log.warning("Could not remove kit role %s from %s: %s", kit, member_id, exc)

    # ------------------------------------------------------------ Expiry

    @tasks.loop(minutes=5)
    async def expire_slots(self) -> None:
        try:
            rows = self.bot.db.expired_kit_slots()
        except Exception as exc:
            log.warning("Slot expiry query failed: %s", exc)
            return
        for slot in rows:
            try:
                await self._revoke_role(slot["kit"], slot["member_id"], ignore_slot=slot["id"])
                self.bot.db.set_kit_slot_member(slot["id"], None, None)
                self.bot.audit(f"🪑 Team slot on **{slot['kit']}** ran out – <@{slot['member_id']}> removed")
                await self._dm(slot["owner_id"], f"⌛ A team slot on your kit **{slot['kit']}** ran out and the "
                                                 "teammate lost it. Buy more time under My account to keep them on.")
            except Exception:
                log.exception("Expiring slot %s failed", slot["id"])

    @expire_slots.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _dm(self, user_id: int, text: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if user is not None:
                await user.send(text)
        except discord.HTTPException:
            pass


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(PlayerAccounts(bot))
