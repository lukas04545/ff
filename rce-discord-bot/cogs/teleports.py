"""Location teleports: a player uses a Location quick chat emote (North, Northeast … Northwest) in game and is
teleported to the coordinates an admin set for it.

  /teleport set location:North x y z   coordinates typed in (name, cooldown, required role optional)
  /teleport here location:North        captures where YOU stand in game (your linked player – or player:<name>)
  /teleport list | clear | toggle
Also in the control panel (Kits → 📍 Teleports, with a "📍 Capture position" button) and via the management AI.

Commands (settings): TELEPORT_COMMAND (default global.teleportpos ({x},{y},{z}) "{name}") and
TELEPORT_POSITION_COMMAND (default server.printpos "{name}" – the server answers with the player's position).
Location emotes never give kits (support/teleports.py).
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from permcheck import main_guild
from rce import log_parser as lp
from rce.rcon_client import RconError
from support import teleports as tp
from utils import admin_only, format_duration, player_choices, safe_md, sanitize_ingame_text, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.teleports")
ANNOUNCE_THROTTLE = 20          # seconds: max one "wait" message per player


class TeleportError(Exception):
    """Readable reason (for Discord, the panel and the AI)."""


class Teleports(commands.Cog):
    teleport = app_commands.Group(name="teleport", description="Location emote teleports", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._announced: dict[str, float] = {}

    async def cog_load(self) -> None:
        # Location emotes are teleports only: kit binds left on them from before are removed (they'd never fire)
        removed = [r for r in self.bot.db.kit_rules(trigger="quickchat") if tp.is_location(r["phrase"])]
        for rule in removed:
            self.bot.db.delete_kit_rule(rule["id"])
        if removed:
            log.info("Removed %d kit bind(s) from Location emotes – those are teleports now: %s", len(removed),
                     ", ".join(f"#{r['id']} {r['kit']}" for r in removed))

    # ------------------------------------------------------------ Shared (Discord, panel, AI)

    def _cfg(self, key: str, default: str) -> str:
        return (getattr(self.bot.config, key, "") or default).strip()

    def online_name(self, player: str) -> str | None:
        return next((p for p in self.bot.online_players if p.lower() == player.strip().lower()), None)

    async def capture(self, player: str) -> tuple[str, tuple[float, float, float]]:
        """(online name, x/y/z) of a player standing in game – raises TeleportError."""
        name = self.online_name(sanitize_player_name(player or ""))
        if not player or name is None:
            raise TeleportError(f"**{safe_md(player or '?')}** is not online – join the server, stand on the spot, "
                                "then capture.")
        try:
            reply = await self.bot.rcon.command(self._cfg("teleport_position_command", tp.POSITION_DEFAULT)
                                                .format(name=name))
        except RconError as exc:
            raise TeleportError(f"The server didn't answer: {exc}") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise TeleportError("The position command setting is broken (placeholders: {name}).") from exc
        pos = tp.parse_position(reply)
        if pos is None:
            raise TeleportError("Couldn't read the position from the server's answer: "
                                f"`{(reply or '(nothing)').strip()[:120]}`")
        return name, pos

    def set(self, raw: str, x, y, z, *, name: str | None = None, cooldown_minutes: int | None = None,
            required_role_id: int | None | bool = False, by: str = "") -> dict:
        if not tp.is_location(raw):
            raise TeleportError("Only Location emotes (North … Northwest) can be teleports.")
        try:
            x, y, z = tp.check_coords(x, y, z)
        except ValueError as exc:
            raise TeleportError(str(exc)) from exc
        self.bot.db.set_teleport(raw, x, y, z, name=name, cooldown_minutes=cooldown_minutes,
                                 required_role_id=required_role_id, by=by)
        return self.info(raw)

    def info(self, raw: str) -> dict:
        row = self.bot.db.teleport(raw)
        out = {"raw": raw, "location": tp.label(raw), "set": row is not None}
        if row is not None:
            out.update(name=row["name"], x=row["x"], y=row["y"], z=row["z"], cooldown_minutes=row["cooldown_minutes"],
                       required_role_id=str(row["required_role_id"]) if row["required_role_id"] else None,
                       enabled=bool(row["enabled"]), updated_by=row["updated_by"], updated_at=row["updated_at"])
        return out

    def all(self) -> list[dict]:
        return [self.info(raw) for raw in tp.LOCATIONS]

    def _audit(self, text: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(text)

    # ------------------------------------------------------------ In game

    @commands.Cog.listener()
    async def on_rce_chat(self, ev: lp.ChatEvent) -> None:
        if ev.channel == "SERVER" or not tp.is_location(ev.raw):
            return
        row = self.bot.db.teleport(ev.raw)
        if row is None or not row["enabled"]:
            return
        await self.use(row, ev.player)

    async def use(self, row, player: str) -> bool:
        place = row["name"] or tp.label(row["phrase"])
        if row["required_role_id"]:
            kits = self.bot.get_cog("Kits")
            if kits is None or not await kits.player_has_role(player, row["required_role_id"]):
                guild = main_guild(self.bot)
                role = guild.get_role(row["required_role_id"]) if guild else None
                await self._tell(player, f"Teleport <b>{sanitize_ingame_text(place, 30)}</b> needs the Discord role "
                                         f"<b>{sanitize_ingame_text(role.name if role else 'required role', 30)}</b> "
                                         "(link with /customkit link).")
                return False
        now = time.time()
        last = self.bot.db.teleport_last_use(row["phrase"], player)
        if row["cooldown_minutes"] and last is not None and now - last < row["cooldown_minutes"] * 60:
            left = max(60, math.ceil(row["cooldown_minutes"] * 60 - (now - last)))
            await self._tell(player, f"Teleport <b>{sanitize_ingame_text(place, 30)}</b> again in "
                                     f"{format_duration(left)}.")
            return False
        command = self._cfg("teleport_command", tp.TELEPORT_DEFAULT)
        try:
            await self.bot.rcon.command(command.format(x=row["x"], y=row["y"], z=row["z"], name=player),
                                        expect_response=False)
        except (RconError, KeyError, IndexError, ValueError) as exc:
            log.warning("Teleport of %s to %s failed: %s", player, place, exc)
            return False
        self.bot.db.record_teleport_use(row["phrase"], player, now)
        self._audit(f"📍 **{safe_md(player)}** teleported to **{safe_md(place)}** {tp.fmt(row['x'], row['y'], row['z'])}")
        if getattr(self.bot.config, "kit_claim_announce", True):
            await self._say(f"<color=#7CFC00>{sanitize_ingame_text(player, 32)}</color> teleported to "
                            f"<b>{sanitize_ingame_text(place, 30)}</b>.")
        return True

    async def _say(self, text: str) -> None:
        try:
            await self.bot.rcon.command(f"say {text}", expect_response=False)
        except RconError:
            pass

    async def _tell(self, player: str, text: str) -> None:
        if not getattr(self.bot.config, "kit_claim_announce", True):
            return
        now = time.monotonic()
        if now - self._announced.get(player.lower(), 0) < ANNOUNCE_THROTTLE:
            return
        self._announced[player.lower()] = now
        await self._say(f"<color=orange>{sanitize_ingame_text(player, 32)}</color>: {text}")

    # ------------------------------------------------------------ Discord

    async def _location_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        q = current.lower().strip()
        out = []
        for t in self.all():
            name = f"📍 {t['location']}" + (f" → {t['name'] or tp.fmt(t['x'], t['y'], t['z'])}" if t["set"]
                                           else " (not set)")
            if not q or q in name.lower():
                out.append(app_commands.Choice(name=name[:100], value=t["raw"]))
        return out

    async def _player_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    def _raw(self, location: str) -> str:
        raw = tp.find(location)
        if raw is None:
            raise TeleportError("Pick a Location emote: North, Northeast, East, Southeast, South, Southwest, West or "
                                "Northwest.")
        return raw

    async def _fail(self, interaction: discord.Interaction, exc: TeleportError) -> None:
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(f"⚠️ {exc}", ephemeral=True)

    def _who(self, interaction: discord.Interaction) -> str:
        return f"{interaction.user.mention} ({safe_md(str(interaction.user))})"

    def _line(self, t: dict) -> str:
        if not t["set"]:
            return f"📍 **{t['location']}** – not set"
        extra = []
        if t["cooldown_minutes"]:
            extra.append(f"⏱️ {format_duration(t['cooldown_minutes'] * 60)}")
        if t["required_role_id"]:
            extra.append(f"🎖️ <@&{t['required_role_id']}>")
        if not t["enabled"]:
            extra.append("⏸️ off")
        name = f" · {safe_md(t['name'])}" if t["name"] else ""
        return (f"📍 **{t['location']}**{name} → `{tp.fmt(t['x'], t['y'], t['z'])}`"
                + (" · " + " · ".join(extra) if extra else ""))

    @teleport.command(name="set", description="Sets where a Location emote teleports players (coordinates).")
    @app_commands.describe(location="The Location emote", x="X", y="Y (height)", z="Z", name="A name, e.g. Outpost",
                           cooldown_minutes="Wait time per player (0 = none)",
                           required_role="Only players whose linked Discord account has this role")
    @app_commands.autocomplete(location=_location_ac)
    @admin_only()
    async def set_cmd(self, interaction: discord.Interaction, location: str, x: float, y: float, z: float,
                      name: str | None = None, cooldown_minutes: app_commands.Range[int, 0, 525600] | None = None,
                      required_role: discord.Role | None = None) -> None:
        try:
            t = self.set(self._raw(location), x, y, z, name=name, cooldown_minutes=cooldown_minutes,
                         required_role_id=required_role.id if required_role else False, by=str(interaction.user))
        except TeleportError as exc:
            await self._fail(interaction, exc)
            return
        self._audit(f"📍 {self._who(interaction)}: teleport {self._line(t)}")
        await interaction.response.send_message(f"✅ {self._line(t)}\nPlayers use the **{t['location']}** emote in "
                                                "game to go there.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @teleport.command(name="here", description="Sets a Location emote's teleport to where you stand in game now.")
    @app_commands.describe(location="The Location emote",
                           player="Whose position (default: your linked in-game player)", name="A name, e.g. Outpost")
    @app_commands.autocomplete(location=_location_ac, player=_player_ac)
    @admin_only()
    async def here_cmd(self, interaction: discord.Interaction, location: str, player: str | None = None,
                       name: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            raw = self._raw(location)
            who = player or self.bot.db.linked_player(interaction.user.id)
            if not who:
                raise TeleportError("Your Discord account isn't linked to an in-game player – pick `player:` (you, "
                                    "online) or link with `/customkit link`.")
            who, pos = await self.capture(who)
            t = self.set(raw, *pos, name=name, by=str(interaction.user))
        except TeleportError as exc:
            await self._fail(interaction, exc)
            return
        self._audit(f"📍 {self._who(interaction)}: teleport captured from **{safe_md(who)}** – {self._line(t)}")
        await interaction.followup.send(f"📍 Captured **{safe_md(who)}**'s position.\n✅ {self._line(t)}",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @teleport.command(name="clear", description="Removes a Location emote's teleport.")
    @app_commands.autocomplete(location=_location_ac)
    @admin_only()
    async def clear_cmd(self, interaction: discord.Interaction, location: str) -> None:
        try:
            raw = self._raw(location)
        except TeleportError as exc:
            await self._fail(interaction, exc)
            return
        if not self.bot.db.delete_teleport(raw):
            await interaction.response.send_message("That location has no teleport.", ephemeral=True)
            return
        self._audit(f"📍 {self._who(interaction)}: teleport **{tp.label(raw)}** removed")
        await interaction.response.send_message(f"🗑️ **{tp.label(raw)}** no longer teleports.", ephemeral=True)

    @teleport.command(name="toggle", description="Switches a Location emote's teleport on or off.")
    @app_commands.autocomplete(location=_location_ac)
    @admin_only()
    async def toggle_cmd(self, interaction: discord.Interaction, location: str, enabled: bool) -> None:
        try:
            raw = self._raw(location)
        except TeleportError as exc:
            await self._fail(interaction, exc)
            return
        if not self.bot.db.update_teleport(raw, enabled=enabled, by=str(interaction.user)):
            await interaction.response.send_message("That location has no teleport yet.", ephemeral=True)
            return
        self._audit(f"📍 {self._who(interaction)}: teleport **{tp.label(raw)}** {'on' if enabled else 'off'}")
        await interaction.response.send_message(f"📍 **{tp.label(raw)}** {'on' if enabled else 'off'}.", ephemeral=True)

    @teleport.command(name="list", description="Shows where each Location emote teleports.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="📍 Location teleports", colour=discord.Colour.dark_teal(),
                              description="\n".join(self._line(t) for t in self.all()))
        embed.set_footer(text="Use the Location emote (quick chat) in game to teleport there")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Teleports(bot))
