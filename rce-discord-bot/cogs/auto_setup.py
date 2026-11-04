"""First-run setup in the server's Discord, done by the bot itself.

Channels: every core channel that isn't set (or was deleted) is created – reusing one with the same name if it
exists – and saved in the settings:
  🎮 <server name>          status (read only), events (read only), killfeed (read only), game chat
  🔒 <server name> staff     admin log, console, ticket log    (admins + support roles only)
  🎫 Tickets                 the ticket category                (admins + support roles only)
Channels you set yourself are never touched. On by default for hosted servers; AUTO_CHANNELS=true/false decides.

Knowledge base: when the support knowledge file is missing or empty, a default one is written from what the bot
knows – the server's name, Discord, website, rules, kits, the store, the wipe schedule and the commands players use.
Edit it freely (web interface → Knowledge base); it's only written while the file is empty.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from permcheck import main_guild

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.auto_setup")

# (setting, config attribute, channel name, group, mode) – mode: "read" = members read only, "chat", "staff"
CHANNELS = (
    ("STATUS_CHANNEL_ID", "status_channel_id", "📊・status", "public", "read"),
    ("EVENTS_CHANNEL_ID", "events_channel_id", "🎉・events", "public", "read"),
    ("KILLFEED_CHANNEL_ID", "killfeed_channel_id", "💀・killfeed", "public", "read"),
    ("CHAT_CHANNEL_ID", "chat_channel_id", "💬・game-chat", "public", "chat"),
    ("ADMIN_LOG_CHANNEL_ID", "admin_log_channel_id", "🛡・admin-log", "staff", "staff"),
    ("CONSOLE_LOG_CHANNEL_ID", "console_log_channel_id", "🖥・console", "staff", "staff"),
    ("TICKET_LOG_CHANNEL_ID", "ticket_log_channel_id", "📁・ticket-log", "staff", "staff"),
)
TICKET_CATEGORY = ("TICKET_CATEGORY_ID", "ticket_category_id", "🎫 Tickets")
MANAGER_CHANNEL = ("MANAGER_AI_CHANNEL_IDS", "manager_ai_channel_ids", "🧠・bot-setup")


def manager_ai_here() -> bool:
    import saas_mode
    return saas_mode.manager_ai_in_discord()


def enabled() -> bool:
    import saas_mode
    raw = os.environ.get("AUTO_CHANNELS", "").strip().lower()
    if not raw:
        return saas_mode.enabled()          # hosted: yes; an own install keeps its channels as they are
    return raw not in ("0", "false", "no", "off")


class AutoSetup(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._done_for: int | None = None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = main_guild(self.bot)
        if guild is None or self._done_for == guild.id:
            return
        self._done_for = guild.id
        try:
            self.write_default_knowledge(guild)
        except OSError as exc:
            log.warning("Default knowledge base: %s", exc)
        if enabled():
            try:
                created = await self.ensure_channels(guild)
            except discord.HTTPException as exc:
                log.warning("Creating the bot's channels failed: %s", exc)
                return
            if created:
                self.bot.audit("🧱 Created the missing channels: " + ", ".join(created))

    # ------------------------------------------------------------ channels

    def _staff_roles(self, guild) -> list:
        cfg = self.bot.config
        ids = set(cfg.admin_role_ids) | set(getattr(cfg, "support_role_ids", ()) or ())
        return [r for r in (guild.get_role(i) for i in ids) if r is not None]

    def _overwrites(self, guild, mode: str) -> dict:
        everyone = {"read": discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                        add_reactions=False),
                    "chat": discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    "staff": discord.PermissionOverwrite(view_channel=False)}[mode]
        ow = {guild.default_role: everyone,
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    attach_files=True, read_message_history=True,
                                                    manage_channels=True, manage_messages=True)}
        if mode == "staff":
            for role in self._staff_roles(guild):
                ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       read_message_history=True)
        return ow

    def _exists(self, guild, channel_id) -> bool:
        return bool(channel_id) and guild.get_channel(int(channel_id)) is not None

    async def _category(self, guild, name: str, mode: str):
        found = discord.utils.get(guild.categories, name=name)
        if found is not None:
            return found
        return await guild.create_category(name, overwrites=self._overwrites(guild, mode),
                                           reason="RCE bot: first-run setup")

    async def ensure_channels(self, guild) -> list[str]:
        """Creates the core channels that aren't set up, saves them, applies the settings. Returns their names."""
        cfg = self.bot.config
        name = (cfg.server_display_name or guild.name or "Rust server").strip()[:80]
        groups = {"public": (f"🎮 {name}", "read"), "staff": (f"🔒 {name} staff", "staff")}
        updates: dict[str, str] = {}
        made: list[str] = []
        cats: dict[str, object] = {}
        for key, attr, ch_name, group, mode in CHANNELS:
            if self._exists(guild, getattr(cfg, attr, None)):
                continue
            if group not in cats:
                cats[group] = await self._category(guild, *groups[group])
            category = cats[group]
            channel = discord.utils.get(getattr(category, "text_channels", []), name=ch_name)
            if channel is None:
                channel = await guild.create_text_channel(ch_name, category=category,
                                                          overwrites=self._overwrites(guild, mode),
                                                          reason="RCE bot: first-run setup")
                made.append(f"#{ch_name}")
            updates[key] = str(channel.id)
        key, attr, ch_name = MANAGER_CHANNEL                # the management AI's channel (staff only)
        if manager_ai_here() and not any(self._exists(guild, i) for i in (getattr(cfg, attr, None) or ())):
            if "staff" not in cats:
                cats["staff"] = await self._category(guild, *groups["staff"])
            channel = discord.utils.get(getattr(cats["staff"], "text_channels", []), name=ch_name)
            if channel is None:
                channel = await guild.create_text_channel(ch_name, category=cats["staff"],
                                                          overwrites=self._overwrites(guild, "staff"),
                                                          topic="Tell the management AI what to set up – kits, "
                                                                "settings, roles, channels …",
                                                          reason="RCE bot: management AI channel")
                made.append(f"#{ch_name}")
                try:
                    await channel.send("🧠 **Management AI** – tell me what to set up, e.g. *\"make a VIP role and a "
                                       "read-only #announcements channel\"*, *\"bind the Starter kit to the Thanks "
                                       "emote\"* or *\"set the wipe to every Thursday 19:00\"*. Changes to settings "
                                       "ask for a ✅ first. Only admins can use this channel.")
                except discord.HTTPException:
                    pass
            updates[key] = str(channel.id)
        key, attr, cat_name = TICKET_CATEGORY
        if not self._exists(guild, getattr(cfg, attr, None)):
            cat = discord.utils.get(guild.categories, name=cat_name)
            if cat is None:
                cat = await guild.create_category(cat_name, overwrites=self._overwrites(guild, "staff"),
                                                  reason="RCE bot: ticket category")
                made.append(cat_name)
            updates[key] = str(cat.id)
        if updates:
            await self._save(updates)
        return made

    async def _save(self, updates: dict[str, str]) -> None:
        from config import ConfigError, load_config
        from envfile import update_env
        web = getattr(self.bot, "web", None)
        env_path = getattr(web, "env_path", None) or Path(".env")
        update_env(env_path, updates)
        try:
            new = load_config(env_path)
        except ConfigError as exc:
            log.warning("Saved the new channels, but the settings didn't load: %s", exc)
            return
        await self.bot.apply_config(new)

    # ------------------------------------------------------------ knowledge base

    def write_default_knowledge(self, guild) -> bool:
        path: Path = self.bot.config.support_knowledge_file
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip():
                return False                              # theirs: never overwritten
        except OSError:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.default_knowledge(guild), encoding="utf-8")
        log.info("Wrote a default knowledge base to %s", path)
        return True

    def default_knowledge(self, guild) -> str:
        """A starting knowledge base for the support AI, from what the bot knows about this server."""
        cfg, db = self.bot.config, self.bot.db
        name = (cfg.server_display_name or getattr(guild, "name", "") or "our Rust server").strip()
        out = [f"# {name} – support knowledge",
               "_Made by the bot from the server's settings – edit it freely (web interface → Knowledge base)._", "",
               "## About the server",
               f"- **{name}** is a Rust Console Edition server (PlayStation / Xbox).",
               f"- Discord: {getattr(guild, 'name', '') or name}."]
        web = getattr(self.bot, "web", None)
        url = None
        try:
            url = web.public_url if web is not None else None
        except Exception:
            url = None
        if url:
            out.append(f"- Website (store, leaderboards, rules): {url}")
        wipe = self.bot.get_cog("Wipe")
        if wipe is not None:
            try:
                sched = wipe.schedule()
                if sched.get("next"):
                    out.append(f"- Next wipe: <t:{sched['next']}:F> ({wipe.repeat_text(sched['repeat'], sched['next'])}).")
            except Exception:
                pass
        out += ["", "## Linking your account",
                "- Link your in-game name to Discord with `/customkit link` – needed for kits, the store, points and "
                "your profile.", "",
                "## Commands players use",
                "- `/profile` – your stats, points and kits", "- `/customkit claim` – claim a kit you own",
                "- `/shop list` and `/shop buy` – spend points" if cfg.economy_enabled else "",
                "- `/report` – report a player to the admins", "- Open a ticket for anything else – staff answer here.",
                ""]
        kits = [r["name"] for r in db.custom_kits()][:30]
        if kits:
            out += ["## Kits", "- Kits on this server: " + ", ".join(kits) + ".",
                    "- Kits you bought or won are claimed with `/customkit claim` while you're online in game.", ""]
        try:
            store = [r for r in db.store_kits(enabled_only=True)][:20]
        except Exception:
            store = []
        if store:
            cur = getattr(cfg, "store_currency", "") or ""
            out += ["## Store"] + [f"- {r['name']}: {r['price']} {cur}".rstrip()
                                   for r in store]
            out += ["- Buy on the website; you get the kit (and its Discord role) after paying.", ""]
        rules = ""
        try:
            if cfg.rules_file.exists():
                rules = cfg.rules_file.read_text(encoding="utf-8").strip()
        except OSError:
            rules = ""
        if rules:
            out += ["## Rules", rules[:4000], ""]
        out += ["## When to call staff",
                "- Bans, refunds, payment problems, bugs and anything the bot can't do: a staff member takes over "
                "the ticket.", "",
                f"_Generated {time.strftime('%Y-%m-%d')}._"]
        return "\n".join(line for line in out if line is not None).replace("\n\n\n", "\n\n") + "\n"


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(AutoSetup(bot))
