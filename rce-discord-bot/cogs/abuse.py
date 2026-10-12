"""Admin abuse protection: someone who isn't allowed spawns raid items (rockets, C4) -> admin removed, killed.

Only items spawned with the in-game admin panel count – the server logs them as
"[ServerVar] giving <player> <amount> x <item>". Gives via RCON/console (the bot's shop and kits, /rcon) look
different ("[ServerVar] SERVER giving …" or without "[ServerVar]") and are ignored. When the item matches
ABUSE_ITEMS and the player is not allowed, the bot runs ABUSE_REMOVE_ADMIN_COMMANDS and ABUSE_KILL_COMMAND
(global.killplayer "<player>") and pings staff.

Allowed: players linked (/customkit link) to a Discord member with an ABUSE_EXEMPT_ROLE_IDS role (owner, co-owner)
and the names in ABUSE_EXEMPT_PLAYERS. Items the bot gives itself (shop, custom kits, rewards) never count.
The protection only runs once at least one of the two is set – otherwise the owner could be hit.
Limit: the log line doesn't say who gave the item. If an admin gives rockets to someone else, the log shows that
player – the alert says so, staff should check it.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from rce.rcon_client import RconError
from utils import safe_md, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.abuse")

OWN_GIVE_WINDOW = 60         # seconds: an item line this soon after the bot's own give to that player is the bot's
PUNISH_COOLDOWN = 120        # one punishment per player (a spawn of several stacks makes several lines)
IGNORED_WORDS = ("launcher",)   # "Rocket Launcher" is not a rocket


class AbuseGuard(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._punished: dict[str, float] = {}
        self._warned = False

    @property
    def active(self) -> bool:
        cfg = self.bot.config
        return cfg.abuse_protection and bool(cfg.abuse_exempt_role_ids or cfg.abuse_exempt_players)

    def forbidden(self, item: str) -> bool:
        name = item.lower()
        return any(k in name for k in self.bot.config.abuse_items) and not any(w in name for w in IGNORED_WORDS)

    def bot_gave(self, player: str) -> bool:
        """Did the bot itself just send this player something (shop, kit, reward, /rcon)?"""
        quoted = f'"{player.lower()}"'
        cutoff = time.monotonic() - OWN_GIVE_WINDOW
        return any(ts >= cutoff and quoted in cmd.lower() for ts, cmd in reversed(self.bot.rcon.recent_commands))

    async def exempt(self, player: str) -> str | None:
        """Why this player may spawn (or None)."""
        cfg = self.bot.config
        if player.lower() in cfg.abuse_exempt_players:
            return "allowed in-game name"
        user_id = self.bot.db.player_owner(player)
        if user_id is None or not cfg.abuse_exempt_role_ids:
            return None
        guild = (self.bot.get_guild(cfg.guild_id) if cfg.guild_id else None) or \
            (self.bot.guilds[0] if self.bot.guilds else None)
        member = guild.get_member(user_id) if guild else None
        if guild is not None and member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        roles = {r.id for r in getattr(member, "roles", [])}
        allowed = roles & set(cfg.abuse_exempt_role_ids)
        if allowed:
            role = guild.get_role(next(iter(allowed)))
            return f"Discord role {role.name if role else next(iter(allowed))}"
        return None

    @commands.Cog.listener()
    async def on_rce_admin_action(self, ev) -> None:
        if ev.kind != "item" or not ev.admin_panel or not ev.player or not ev.item or not self.forbidden(ev.item):
            return
        if not self.active:
            if self.bot.config.abuse_protection and not self._warned:
                self._warned = True
                log.warning("Admin abuse protection is not active: set ABUSE_EXEMPT_ROLE_IDS or ABUSE_EXEMPT_PLAYERS "
                            "(who may spawn rockets/C4) in the settings.")
            return
        player = ev.player
        if self.bot_gave(player):
            return
        reason = await self.exempt(player)
        if reason:
            log.info("%s spawned %s × %s – allowed (%s).", player, ev.amount, ev.item, reason)
            return
        now = time.monotonic()
        if now - self._punished.get(player.lower(), -PUNISH_COOLDOWN) < PUNISH_COOLDOWN:
            return
        self._punished[player.lower()] = now
        await self.punish(player, ev.item, ev.amount)

    async def punish(self, player: str, item: str, amount: float | None) -> list[tuple[str, str]]:
        """Removes admin and kills the player. Returns (command, server reply) for the alert."""
        cfg = self.bot.config
        name = sanitize_player_name(player)
        results: list[tuple[str, str]] = []
        for template in (*cfg.abuse_remove_admin_commands, cfg.abuse_kill_command):
            cmd = template.format(name=name)
            try:
                reply = (await self.bot.rcon.command(cmd, timeout=8)).strip() or "ok (no reply)"
            except RconError as exc:
                reply = f"failed: {exc}"
            results.append((cmd, reply))
        what = f"{amount:g} × {item}" if amount else item
        log.warning("Admin abuse: %s spawned %s – admin removed and killed.", player, what)
        self.bot.audit(f"🚨 **{safe_md(player)}** spawned **{safe_md(what)}** without permission – admin removed, "
                       "killed in-game")
        await self._alert(player, what, results)
        return results

    async def _alert(self, player: str, what: str, results: list[tuple[str, str]]) -> None:
        cfg = self.bot.config
        channel_id = cfg.alert_channel_id or cfg.admin_log_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        embed = discord.Embed(title="🚨 Raid items spawned without permission", colour=discord.Colour.dark_red(),
                              description=f"**{safe_md(player)}** received **{safe_md(what)}**.")
        embed.add_field(name="Done automatically", inline=False, value="\n".join(
            f"`{safe_md(cmd)}` → {safe_md(reply[:150])}" for cmd, reply in results)[:1024])
        owner = self.bot.db.player_owner(player)
        if owner:
            embed.add_field(name="Linked Discord account", value=f"<@{owner}>")
        embed.set_footer(text="The server log doesn't say who gave the item – if an admin gave it to someone else, "
                              "check who it was. Allowed: ⚙️ Settings → 🛡️ Admin abuse protection.")
        roles = [discord.Object(r) for r in cfg.alert_role_ids]
        try:
            await channel.send(content=" ".join(f"<@&{r.id}>" for r in roles) or None, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=roles))
        except discord.HTTPException as exc:
            log.warning("Abuse alert failed: %s", exc)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(AbuseGuard(bot))
