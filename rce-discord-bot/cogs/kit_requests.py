"""Bought custom kits: the ticket AI files a request, staff approves it with a button, the buyer gets the kit role.

Flow
  1. A player writes in their ticket that they bought a custom kit (store, order ID …).
  2. The AI calls request_kit_access -> the bot posts "🛒 Kit purchase – staff approval needed" in the ticket with
     ✅ Approve / ✖️ Reject buttons and pings the support roles. Nothing is given yet.
  3. Staff clicks Approve -> the kit gets a Discord role (its existing claim role, else an existing role with the
     kit's name, else a newly created one, linked as claim role), the buyer gets that role (for N days if the
     purchase was time-limited) and can claim the kit with /customkit claim.
Requests survive restarts (database); each can only be decided once.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from permcheck import explain_forbidden
from utils import is_admin, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.kit_requests")

COLOUR = discord.Colour(0xE0A23B)
STATUS_TEXT = {"pending": "⏳ Waiting for staff", "approved": "✅ Approved", "rejected": "✖️ Rejected"}


class KitRequestError(Exception):
    """Request can't be filed / approved – message for the AI or staff."""


class KitRequestView(discord.ui.View):
    """Persistent Approve / Reject buttons – the request is found via the message."""

    def __init__(self, cog: "KitRequests") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _decide(self, interaction: discord.Interaction, approve: bool) -> None:
        if not self.cog.is_staff(interaction.user):
            await interaction.response.send_message("⛔ Only staff can decide kit purchases.", ephemeral=True)
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

    async def cog_load(self) -> None:
        self.bot.add_view(KitRequestView(self))

    def is_staff(self, user: discord.abc.User) -> bool:
        tickets = self.bot.get_cog("Tickets")
        if tickets is not None:
            return tickets.is_staff(user)
        return is_admin(self.bot, user)

    # ------------------------------------------------------------ Filing (ticket AI)

    async def file(self, channel: discord.abc.Messageable, ticket, member: discord.Member, kit_name: str,
                   proof: str, days: int = 0) -> dict:
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
        open_req = [r for r in db.kit_requests(status="pending", user_id=member.id) if r["kit"].lower() == kit.lower()]
        if open_req:
            return {"ok": True, "result": "already_waiting_for_staff", "request_id": open_req[0]["id"], "kit": kit}
        request_id = db.add_kit_request(ticket["id"] if ticket else None, member.id, kit, max(0, int(days)),
                                        proof.strip()[:500])
        req = db.kit_request(request_id)
        guild = getattr(channel, "guild", None)
        roles = [r for rid in self.bot.config.support_role_ids if guild and (r := guild.get_role(rid))]
        msg = await channel.send(content=" ".join(r.mention for r in roles) or None, embed=self.embed(req),
                                 view=KitRequestView(self), allowed_mentions=discord.AllowedMentions(roles=roles))
        db.set_kit_request_message(request_id, msg.channel.id, msg.id)
        self.bot.audit(f"🛒 Kit purchase request #{request_id}: <@{member.id}> → **{safe_md(kit)}** "
                       f"({safe_md(proof[:150])}) – waiting for staff")
        return {"ok": True, "result": "waiting_for_staff", "request_id": request_id, "kit": kit,
                "message": "Posted for staff approval. Tell the player staff will check the purchase and confirm."}

    def embed(self, req) -> discord.Embed:
        status = req["status"]
        embed = discord.Embed(title=f"🛒 Kit purchase #{req['id']} – {STATUS_TEXT[status]}", colour=COLOUR
                              if status == "pending" else discord.Colour.dark_grey(),
                              description=f"<@{req['user_id']}> says they bought **{safe_md(req['kit'])}**.")
        embed.add_field(name="Duration", value=f"{req['days']} days" if req["days"] else "permanent")
        if req["proof"]:
            embed.add_field(name="Their info (check it!)", value=safe_md(req["proof"])[:1024], inline=False)
        items = self.bot.db.custom_kit_items(req["kit"])
        if items:
            embed.add_field(name="Kit contents", inline=False, value=", ".join(
                f"{i['shortname']} ×{i['amount']}" for i in items[:15])[:1024])
        if status == "pending":
            embed.set_footer(text="Approve = the buyer gets the kit's role and can claim it with /customkit claim.")
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
        await self._update(req["id"])
        await self._say(channel, f"✅ 🎁 Purchase confirmed by {safe_md(by_name)}: {text} – claim it with "
                                 f"`/customkit claim {req['kit']}` while you're online in-game.")
        return True, f"Request #{request_id} approved: {text}"

    async def _grant(self, req, by: str) -> str:
        channel = self.bot.get_channel(req["channel_id"]) if req["channel_id"] else None
        guild = getattr(channel, "guild", None) or (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None or self.bot.db.custom_kit(req["kit"]) is None:
            raise KitRequestError("The kit or the Discord server is no longer available.")
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
