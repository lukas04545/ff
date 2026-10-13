"""Invite rewards: members get points when someone they invited to the Discord really plays on the server.

How it works: the bot remembers which invite link a new member used. The inviter is rewarded only once the new
member has linked their in-game player (/customkit link) and played "Hours to play" on the server – so alt
accounts and invite farming earn nothing. People who were on the Discord before, accounts younger than "Minimum
account age" and self-invites never count.

Needs: the Server Members intent (Developer Portal → Bot) and the bot permission "Manage Server" (to read the
invite links).
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0x2ECC71)


class InviteRewards(Plugin):
    NAME = "Invite rewards"
    ICON = "🤝"
    DESCRIPTION = ("Rewards members for inviting friends: when someone they invited links their player and plays a "
                   "few hours, both get points. Alts, rejoins and brand-new accounts don't count.")
    INTENTS = ("members",)
    SETTINGS = (
        Setting("inviter_points", "Points for the inviter", "int", 200, min=0, max=1_000_000),
        Setting("invitee_points", "Welcome points for the new player", "int", 100, min=0, max=1_000_000),
        Setting("hours", "Hours the new player must play", "int", 3, min=1, max=200,
                help="Counted while their linked player is online on the server."),
        Setting("min_account_days", "Minimum Discord account age (days)", "int", 7, min=0, max=365,
                help="Younger accounts (typical alts) are not counted."),
        Setting("expire_days", "Invite counts if they play within (days)", "int", 30, min=1, max=365),
        Setting("channel", "Announcement channel", "channel", help="Empty = no announcement (only the admin log)."),
        Setting("role", "Recruiter role", "role", help="Optional: given after the number of invites below."),
        Setting("role_count", "Invites for the recruiter role", "int", 5, min=1, max=1000),
    )

    invites = app_commands.Group(name="invites", description="Invite friends and earn points", guild_only=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.uses: dict[str, int] = {}
        self.inviters: dict[str, int] = {}
        self.cache_ok = False
        self._last_tick: float | None = None

    # ------------------------------------------------------------ Invite cache

    async def refresh_invites(self) -> bool:
        guild = self.guild
        if guild is None:
            return False
        try:
            invites = await guild.invites()
        except discord.HTTPException as exc:    # Forbidden: no "Manage Server" permission
            if self.cache_ok or not self.uses:
                self.log.warning("Can't read the invite links (%s) – give the bot \"Manage Server\".", exc)
            self.cache_ok = False
            return False
        self.uses = {i.code: i.uses or 0 for i in invites}
        self.inviters = {i.code: i.inviter.id for i in invites if i.inviter}
        self.cache_ok = True
        if not self.data.get("seeded") and self.bot.intents.members:
            # Everyone already on the Discord counts as known – a rejoin never earns anything
            self.data.set("known", sorted({m.id for m in guild.members}))
            self.data.set("seeded", True)
        return True

    @tasks.loop(minutes=30)
    async def refresh_loop(self) -> None:
        await self.refresh_invites()

    @Plugin.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        self.uses[invite.code] = invite.uses or 0
        if invite.inviter:
            self.inviters[invite.code] = invite.inviter.id

    @Plugin.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        self.uses.pop(invite.code, None)

    def used_invite(self, before: dict[str, int], after: dict[str, int]) -> str | None:
        """The invite code whose use count went up (None if unclear)."""
        grown = [code for code, uses in after.items() if uses > before.get(code, 0)]
        gone = [code for code in before if code not in after]
        if len(grown) == 1:
            return grown[0]
        if not grown and len(gone) == 1:     # a one-use invite disappears when it is used
            return gone[0]
        return None

    # ------------------------------------------------------------ Joins

    @Plugin.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = self.guild
        if member.bot or guild is None or member.guild.id != guild.id:
            return
        before, inviters = dict(self.uses), dict(self.inviters)
        if not await self.refresh_invites():
            return
        code = self.used_invite(before, self.uses)
        inviter = (self.inviters.get(code) or inviters.get(code)) if code else None
        self.record_join(member.id, inviter, member.created_at.timestamp(), code)

    def record_join(self, member_id: int, inviter: int | None, created_at: float, code: str | None = None) -> dict | None:
        known = set(self.data.get("known", []))
        rejoin = member_id in known
        if not rejoin:
            known.add(member_id)
            self.data.set("known", sorted(known))
        if inviter is None or self.data.get(f"ref:{member_id}") is not None:
            return None
        reason = None
        if rejoin:
            reason = "was on the Discord before"
        elif inviter == member_id:
            reason = "invited themselves"
        elif time.time() - created_at < self.setting("min_account_days") * 86400:
            reason = "Discord account too new"
        ref = {"inviter": inviter, "at": self.now(), "code": code, "status": "invalid" if reason else "pending",
               "reason": reason, "played": 0}
        self.data.set(f"ref:{member_id}", ref)
        return ref

    # ------------------------------------------------------------ Rewards

    def rewarded_count(self, user_id: int) -> int:
        return int(self.data.get(f"count:{user_id}", 0))

    def pending_for(self, user_id: int) -> list[int]:
        return [int(k[4:]) for k, r in self.data.items("ref:").items()
                if r.get("inviter") == user_id and r.get("status") == "pending"]

    async def tick(self, elapsed: float) -> None:
        now = self.now()
        need = self.setting("hours") * 3600
        expire = self.setting("expire_days") * 86400
        for key, ref in self.data.items("ref:").items():
            if ref.get("status") != "pending":
                continue
            invitee = int(key[4:])
            if now - ref["at"] > expire:
                ref["status"], ref["reason"] = "expired", "didn't play in time"
                self.data.set(key, ref)
                continue
            player = self.linked_player(invitee)
            if not player or not self.online_name(player):
                continue
            ref["played"] = int(ref.get("played", 0) + elapsed)
            if ref["played"] >= need:
                ref["status"] = "rewarded"
                self.data.set(key, ref)
                await self.reward(ref["inviter"], invitee, player)
            else:
                self.data.set(key, ref)

    async def reward(self, inviter: int, invitee: int, player: str) -> None:
        count = self.rewarded_count(inviter) + 1
        self.data.set(f"count:{inviter}", count)
        self.data.set("rewarded_total", int(self.data.get("rewarded_total", 0)) + 1)
        p1, p2 = self.setting("inviter_points"), self.setting("invitee_points")
        self.give_points(inviter, p1, f"invited <@{invitee}>")
        self.give_points(invitee, p2, "welcome bonus (invited)")
        self.audit(f"<@{inviter}> invited <@{invitee}> ({self.md(player)}) – reward paid ({count} invites)")
        if self.points_enabled and (p1 or p2):
            text = (f"🤝 <@{inviter}> invited <@{invitee}> (**{self.md(player)}**) – "
                    f"+{self.fmt_points(p1)} for the invite, +{self.fmt_points(p2)} welcome bonus!")
        else:
            text = f"🤝 <@{inviter}> invited <@{invitee}> (**{self.md(player)}**) – thanks for growing the server!"
        await self.post(self.channel(self.setting("channel")),
                        embed=discord.Embed(colour=COLOUR, description=text + f"\nInvites so far: **{count}**"))
        role_id, guild = self.setting("role"), self.guild
        if role_id and guild is not None and count >= self.setting("role_count"):
            member, role = guild.get_member(inviter), guild.get_role(role_id)
            if member is not None and role is not None and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"{count} invites")
                except discord.HTTPException as exc:
                    self.log.warning("Recruiter role for %s failed: %s", inviter, exc)

    @tasks.loop(minutes=2)
    async def play_loop(self) -> None:
        now = time.monotonic()
        elapsed, self._last_tick = (min(now - self._last_tick, 300) if self._last_tick else 0), now
        if elapsed and self.bot.rcon.connected:
            await self.tick(elapsed)

    # ------------------------------------------------------------ Commands

    @invites.command(name="stats", description="Your invites (or someone else's).")
    @app_commands.describe(member="Member (default: you)")
    async def stats(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        pending = self.pending_for(member.id)
        embed = discord.Embed(title=f"🤝 Invites – {member.display_name}", colour=COLOUR)
        embed.add_field(name="Rewarded", value=str(self.rewarded_count(member.id)))
        embed.add_field(name="Waiting", value=str(len(pending)))
        hours = self.setting("hours")
        embed.description = (f"Invite friends with your own Discord invite link. You get "
                             f"{self.fmt_points(self.setting('inviter_points'))} when they link their player "
                             f"(`/customkit link`) and play {hours} hour{'s' if hours != 1 else ''} on the server.")
        if pending:
            embed.add_field(name="Still need to play", inline=False,
                            value="\n".join(f"<@{uid}>" for uid in pending[:15]))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @invites.command(name="top", description="Who brought the most players?")
    async def top(self, interaction: discord.Interaction) -> None:
        counts = sorted(((int(k[6:]), v) for k, v in self.data.items("count:").items() if v), key=lambda x: -x[1])
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> – {n} invite{'s' if n != 1 else ''}"
                 for i, (uid, n) in enumerate(counts[:10])]
        await interaction.response.send_message(
            embed=discord.Embed(title="🤝 Top inviters", colour=COLOUR,
                                description="\n".join(lines) or "Nobody has been rewarded yet."),
            allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ Web / profile

    def problems(self) -> list[str]:
        out = []
        guild = self.guild
        if guild is not None and not guild.me.guild_permissions.manage_guild:
            out.append('The bot needs the Discord permission "Manage Server" to see which invite link was used.')
        elif not self.cache_ok and self.bot.is_ready():
            out.append("The invite links couldn't be read yet (see the bot log).")
        return out

    def status(self) -> dict:
        refs = self.data.items("ref:").values()
        return {"Rewarded invites": int(self.data.get("rewarded_total", 0)),
                "Waiting (still need to play)": sum(1 for r in refs if r.get("status") == "pending")}

    def profile_fields(self, member) -> list[tuple[str, str]]:
        n = self.rewarded_count(member.id)
        return [("🤝 Invites", str(n))] if n else []
