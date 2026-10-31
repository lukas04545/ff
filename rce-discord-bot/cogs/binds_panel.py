"""Live emote binds panel: one Discord message that lists every emote bind (emote -> kit, cooldown, limit, role) and
the Location teleports, and is edited by the bot as soon as anything changes – no matter where it was changed
(/kit bind, the control panel, the management AI …). The bot checks every PANEL_CHECK_SECONDS.

  /kit binds-panel channel:#kits     posts it (admins); the management AI can post it too ("post_binds_panel")
Delete the message to stop it. Panels are remembered across restarts (kv "binds_panels").
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from support import emote_binds
from support import teleports as tp
from utils import format_duration, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.binds_panel")

KV_KEY = "binds_panels"
PANEL_CHECK_SECONDS = 15
MAX_PANELS = 10


def _chunks(lines: list[str], limit: int = 1024) -> list[str]:
    out, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line[:limit]
    return out + ([cur] if cur else [])


def panel_state(db) -> list:
    """Everything the panel shows – the panel is edited when this changes."""
    binds = [(b["raw"], b["kit"], b["kit_type"], b["cooldown_minutes"], b["max_claims"], b["required_role_id"],
              b["enabled"]) for b in emote_binds.binds(db)]
    tele = [(r["phrase"], r["name"], r["x"], r["y"], r["z"], r["cooldown_minutes"], r["required_role_id"],
             r["enabled"]) for r in db.teleports()]
    return [binds, tele]


def state_hash(db) -> str:
    return hashlib.sha1(json.dumps(panel_state(db), default=str).encode()).hexdigest()


def binds_embed(db, *, server: str = "") -> discord.Embed:
    by_cat: dict[str, list[str]] = {}
    for b in emote_binds.binds(db):
        cat, _, text = b["label"].partition(" › ")
        extra = []
        if b["cooldown_minutes"]:
            extra.append(f"⏱️ {format_duration(b['cooldown_minutes'] * 60)}")
        if b["max_claims"]:
            extra.append(f"max {b['max_claims']}×")
        if b["required_role_id"]:
            extra.append(f"🎖️ <@&{b['required_role_id']}>")
        if not b["enabled"]:
            extra.append("⏸️ paused")
        by_cat.setdefault(cat, []).append(
            f"💬 **{safe_md(text)}** → {'🔒 ' if b['kit_type'] == 'custom' else ''}{safe_md(b['kit'])}"
            + (f" · {' · '.join(extra)}" if extra else ""))
    total = sum(len(v) for v in by_cat.values())
    embed = discord.Embed(title="🎭 Emote kits" + (f" – {server}" if server else ""), colour=discord.Colour.dark_green(),
                          description="Use the emote in game (quick chat) to get the kit. "
                                      + (f"**{total}** emote kit(s)." if total else "No emote kits yet."))
    for cat, lines in by_cat.items():
        for n, chunk in enumerate(_chunks(lines)):
            if len(embed.fields) < 24:
                embed.add_field(name=cat if n == 0 else f"{cat} (more)", value=chunk, inline=False)
    tele = []
    for row in db.teleports():
        bits = [f"📍 **{tp.label(row['phrase'])}**" + (f" → {safe_md(row['name'])}" if row["name"] else "")]
        if row["cooldown_minutes"]:
            bits.append(f"⏱️ {format_duration(row['cooldown_minutes'] * 60)}")
        if row["required_role_id"]:
            bits.append(f"🎖️ <@&{row['required_role_id']}>")
        if not row["enabled"]:
            bits.append("⏸️ off")
        tele.append(" · ".join(bits))
    if tele and len(embed.fields) < 25:
        embed.add_field(name="📍 Location teleports", value=_chunks(tele)[0], inline=False)
    embed.set_footer(text="Updates live when binds change · last update")
    embed.timestamp = discord.utils.utcnow()
    return embed


class BindsPanel(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._last = ""

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.refresh_loop.is_running():
            self.refresh_loop.start()

    def panels(self) -> list[dict]:
        try:
            data = json.loads(self.bot.db.get_value(KV_KEY) or "[]")
        except json.JSONDecodeError:
            return []
        return [p for p in data if isinstance(p, dict) and p.get("channel_id") and p.get("message_id")]

    def _save(self, panels: list[dict]) -> None:
        self.bot.db.set_value(KV_KEY, json.dumps(panels[-MAX_PANELS:]))

    def embed(self) -> discord.Embed:
        return binds_embed(self.bot.db, server=self.bot.config.server_display_name or "")

    async def post(self, channel) -> discord.Message:
        """Posts a live panel in this channel (an older panel in the same channel is replaced)."""
        msg = await channel.send(embed=self.embed(), allowed_mentions=discord.AllowedMentions.none())
        panels = [p for p in self.panels() if p["channel_id"] != channel.id]
        for old in [p for p in self.panels() if p["channel_id"] == channel.id]:
            try:
                await (await channel.fetch_message(old["message_id"])).delete()
            except (discord.HTTPException, AttributeError):
                pass
        panels.append({"channel_id": channel.id, "message_id": msg.id})
        self._save(panels)
        self._last = state_hash(self.bot.db)
        return msg

    async def refresh(self, *, force: bool = False) -> int:
        """Edits every panel if the binds changed. Returns how many were edited. Deleted panels are forgotten."""
        current = state_hash(self.bot.db)
        if current == self._last and not force:
            return 0
        panels, keep, edited = self.panels(), [], 0
        embed = self.embed()
        for p in panels:
            channel = self.bot.get_channel(p["channel_id"])
            if channel is None:
                keep.append(p)                                 # not cached yet – try again later
                continue
            try:
                msg = channel.get_partial_message(p["message_id"]) if hasattr(channel, "get_partial_message") \
                    else await channel.fetch_message(p["message_id"])
                await msg.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                edited += 1
                keep.append(p)
            except discord.NotFound:
                log.info("Emote binds panel in #%s was deleted – no longer updated", getattr(channel, "name", "?"))
            except discord.HTTPException as exc:
                log.warning("Emote binds panel: %s", exc)
                keep.append(p)
        if len(keep) != len(panels):
            self._save(keep)
        self._last = current
        return edited

    @tasks.loop(seconds=PANEL_CHECK_SECONDS)
    async def refresh_loop(self) -> None:
        if not self.panels():
            return
        try:
            await self.refresh()
        except Exception:
            log.exception("Emote binds panel refresh failed")


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(BindsPanel(bot))
