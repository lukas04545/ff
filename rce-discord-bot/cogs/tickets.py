"""Support-Tickets mit KI-Erstantwort (DeepSeek) und Staff-Benachrichtigung.

Ablauf:
  1. Nutzer klickt im Ticket-Panel auf "Ticket öffnen" (oder /ticket oeffnen) und beschreibt sein Anliegen.
  2. Der Bot legt einen privaten Channel in TICKET_CATEGORY_ID an (Nutzer + Support-Rollen + Bot).
  3. Die KI antwortet auf jede Nachricht des Nutzers (mit kurzer Sammelpause für mehrere Nachrichten).
  4. Braucht es einen Menschen, pingt der Bot die Support-Rollen – ausgelöst von der KI,
     über den Button "Staff rufen", bei KI-Fehlern oder wenn das Antwortlimit erreicht ist.
  5. Schreibt ein Staff-Mitglied im Ticket, pausiert die KI automatisch.
  6. Beim Schließen wird ein Transkript in TICKET_LOG_CHANNEL_ID gespeichert und der Channel gelöscht.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.kits import ConfirmView
from support.deepseek import PLATFORM_KEYS_URL, PLATFORM_USAGE_URL, DeepSeekClient, DeepSeekError
from support.prompt import build_messages, build_system_prompt, parse_answer
from utils import admin_only, format_duration, is_admin, safe_md, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.tickets")

AI_PREFIX = "🤖 "             # Markiert KI-Antworten (wichtig für den Gesprächsverlauf)
DEBOUNCE_SECONDS = 3.0        # mehrere schnelle Nachrichten gesammelt beantworten
HISTORY_LIMIT = 40            # so viele Nachrichten bekommt die KI als Verlauf
STAFF_REPING_SECONDS = 600    # "Staff rufen" erneut erst nach 10 Minuten
KNOWLEDGE_MAX_CHARS = 12000
DELETE_DELAY_SECONDS = 5


class TicketModal(discord.ui.Modal, title="Support-Ticket öffnen"):
    ingame = discord.ui.TextInput(label="Ingame-Name (Gamertag/PSN)", required=False, max_length=32)
    anliegen = discord.ui.TextInput(label="Worum geht es?", style=discord.TextStyle.paragraph,
                                    min_length=10, max_length=1500,
                                    placeholder="Beschreibe dein Problem möglichst genau.")

    def __init__(self, cog: "Tickets") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(interaction, str(self.anliegen).strip(), str(self.ingame).strip() or None)


class TicketPanelView(discord.ui.View):
    """Dauerhafter Button im Panel-Channel (funktioniert auch nach einem Bot-Neustart)."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Ticket öffnen", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="rce:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_ticket_flow(interaction)


class TicketControlView(discord.ui.View):
    """Dauerhafte Buttons in jedem Ticket-Channel."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Staff rufen", emoji="🔔", style=discord.ButtonStyle.secondary,
                       custom_id="rce:ticket:staff")
    async def call_staff(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.request_staff(interaction)

    @discord.ui.button(label="Schließen", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="rce:ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.close_flow(interaction, reason=None)


class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Support-Tickets", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        cfg = bot.config
        self.ai = DeepSeekClient(cfg.deepseek_api_key, model=cfg.deepseek_model, base_url=cfg.deepseek_base_url)
        self._generation: dict[int, int] = defaultdict(int)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_ping: dict[int, float] = {}
        self._knowledge_cache: tuple[float, str] | None = None

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self))
        if self.bot.config.ticket_category_id and not self.ai.enabled:
            log.warning("Tickets aktiv, aber kein DEEPSEEK_API_KEY – Tickets gehen direkt an den Staff. "
                        "API-Key anlegen: %s", PLATFORM_KEYS_URL)
        elif self.ai.enabled:
            asyncio.create_task(self._check_balance_on_start())

    async def _check_balance_on_start(self) -> None:
        try:
            data = await self.ai.balance()
        except DeepSeekError as exc:
            log.warning("DeepSeek-Plattform nicht erreichbar oder Key ungültig: %s", exc)
            return
        if not data.get("is_available", True):
            log.warning("DeepSeek-Guthaben reicht nicht für API-Aufrufe – aufladen: %s", PLATFORM_USAGE_URL)
        else:
            log.info("DeepSeek-Plattform verbunden (Modell %s).", self.ai.model)

    async def cog_unload(self) -> None:
        await self.ai.close()

    # ------------------------------------------------------------ Hilfen

    @property
    def enabled(self) -> bool:
        return bool(self.bot.config.ticket_category_id)

    def is_staff(self, member: discord.abc.User) -> bool:
        roles = getattr(member, "roles", [])
        return any(r.id in self.bot.config.support_role_ids for r in roles) or is_admin(self.bot, member)

    def _staff_roles(self, guild: discord.Guild) -> list[discord.Role]:
        return [r for rid in self.bot.config.support_role_ids if (r := guild.get_role(rid))]

    def _knowledge(self) -> str:
        path: Path = self.bot.config.support_knowledge_file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if self._knowledge_cache is None or self._knowledge_cache[0] != mtime:
            text = path.read_text(encoding="utf-8")[:KNOWLEDGE_MAX_CHARS]
            self._knowledge_cache = (mtime, text)
        return self._knowledge_cache[1]

    def _status_text(self) -> str:
        info = self.bot.server_info
        if not self.bot.rcon.connected or not info:
            return "Server-Status gerade unbekannt (keine RCON-Verbindung)."
        parts = [
            f"Server online, {info.get('Players', '?')}/{info.get('MaxPlayers', '?')} Spieler",
        ]
        if info.get("Queued"):
            parts.append(f"{info['Queued']} in der Warteschlange")
        if "Uptime" in info:
            parts.append(f"Uptime {format_duration(float(info['Uptime']))}")
        if info.get("Map"):
            parts.append(f"Map: {info['Map']}")
        return ", ".join(parts) + "."

    def _server_name(self) -> str:
        info = self.bot.server_info or {}
        return self.bot.config.server_display_name or strip_rich_text(str(info.get("Hostname", "")))

    async def _log(self, guild: discord.Guild, *, embed: discord.Embed, file: discord.File | None = None) -> None:
        channel_id = self.bot.config.ticket_log_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                kwargs = {"embed": embed}
                if file is not None:
                    kwargs["file"] = file
                await channel.send(**kwargs)
            except discord.HTTPException as exc:
                log.warning("Ticket-Log konnte nicht gesendet werden: %s", exc)

    async def _not_enabled(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Tickets sind nicht eingerichtet (TICKET_CATEGORY_ID fehlt in der .env).", ephemeral=True)

    # ------------------------------------------------------------ Ticket öffnen

    async def start_ticket_flow(self, interaction: discord.Interaction) -> None:
        if not self.enabled or interaction.guild is None:
            await self._not_enabled(interaction)
            return
        existing = self.bot.db.open_ticket_of_user(interaction.user.id)
        if existing and interaction.guild.get_channel(existing["channel_id"]):
            await interaction.response.send_message(
                f"Du hast bereits ein offenes Ticket: <#{existing['channel_id']}>", ephemeral=True)
            return
        await interaction.response.send_modal(TicketModal(self))

    async def create_ticket(self, interaction: discord.Interaction, topic: str, ingame: str | None) -> None:
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        category = guild.get_channel(self.bot.config.ticket_category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("⚠️ Die Ticket-Kategorie wurde nicht gefunden (TICKET_CATEGORY_ID).",
                                            ephemeral=True)
            return

        user = interaction.user
        ticket_id = self.bot.db.create_ticket(user.id, topic, ingame)
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                  manage_channels=True, embed_links=True, attach_files=True),
        }
        for role in self._staff_roles(guild):
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True, attach_files=True)
        slug = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-")[:20] or "user"
        try:
            channel = await guild.create_text_channel(
                f"ticket-{ticket_id:04d}-{slug}", category=category, overwrites=overwrites,
                topic=f"Ticket #{ticket_id} von {user} ({user.id})", reason=f"Support-Ticket #{ticket_id}")
        except discord.HTTPException as exc:
            self.bot.db.delete_ticket(ticket_id)
            log.error("Ticket-Channel konnte nicht erstellt werden: %s", exc)
            await interaction.followup.send(
                "⚠️ Ticket konnte nicht erstellt werden. Der Bot braucht die Berechtigung „Kanäle verwalten“ "
                "in der Ticket-Kategorie.", ephemeral=True)
            return
        self.bot.db.update_ticket(ticket_id, channel_id=channel.id)

        embed = discord.Embed(title=f"🎫 Ticket #{ticket_id}", description=topic[:4000], colour=discord.Colour.blurple())
        embed.add_field(name="Ingame-Name", value=safe_md(ingame) if ingame else "–")
        if self.ai.enabled:
            embed.add_field(name="Wie geht's weiter?", inline=False, value=(
                "Unser KI-Assistent antwortet gleich. Wenn er nicht weiterhelfen kann, holt er das Team dazu – "
                "oder du klickst auf **Staff rufen**."))
        else:
            embed.add_field(name="Wie geht's weiter?", value="Das Team wurde benachrichtigt.", inline=False)
        embed.set_footer(text="Bitte keine Passwörter oder Zahlungsdaten posten.")
        await channel.send(content=user.mention, embed=embed, view=TicketControlView(self),
                           allowed_mentions=discord.AllowedMentions(users=[user]))
        await interaction.followup.send(f"✅ Dein Ticket wurde erstellt: {channel.mention}", ephemeral=True)

        log_embed = discord.Embed(title=f"🎫 Ticket #{ticket_id} geöffnet", description=topic[:1000],
                                  colour=discord.Colour.blurple())
        log_embed.add_field(name="Von", value=f"{user.mention} ({safe_md(str(user))})")
        log_embed.add_field(name="Channel", value=channel.mention)
        await self._log(guild, embed=log_embed)

        ticket = self.bot.db.ticket(ticket_id)
        if self.ai.enabled:
            self._schedule_ai(channel, delay=0)
        else:
            await self._escalate(channel, ticket, "Neues Ticket (keine KI konfiguriert)", source="System")

    # ------------------------------------------------------------ Nachrichten & KI

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.enabled:
            return
        ticket = self.bot.db.ticket_by_channel(message.channel.id)
        if ticket is None:
            return
        if message.author.id != ticket["user_id"] and self.is_staff(message.author):
            if not ticket["staff_joined"]:
                self.bot.db.update_ticket(ticket["id"], staff_joined=1)
                self._generation[message.channel.id] += 1  # geplante KI-Antwort verwerfen
                await message.channel.send(
                    f"👋 **{safe_md(message.author.display_name)}** vom Team hat übernommen – "
                    "der KI-Assistent pausiert in diesem Ticket.",
                    allowed_mentions=discord.AllowedMentions.none())
            return
        if ticket["ai_enabled"] and not ticket["staff_joined"] and self.ai.enabled:
            self._schedule_ai(message.channel)

    def _schedule_ai(self, channel: discord.TextChannel, delay: float | None = None) -> None:
        self._generation[channel.id] += 1
        delay = DEBOUNCE_SECONDS if delay is None else delay
        asyncio.create_task(self._debounced_reply(channel, self._generation[channel.id], delay))

    async def _debounced_reply(self, channel: discord.TextChannel, generation: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._generation[channel.id] != generation:
            return  # inzwischen kam eine neuere Nachricht – die wird gesammelt beantwortet
        async with self._locks[channel.id]:
            try:
                await self._ai_reply(channel)
            except discord.HTTPException as exc:
                log.warning("KI-Antwort in %s fehlgeschlagen: %s", channel.id, exc)

    async def _history(self, channel: discord.TextChannel, ticket) -> list[tuple[str, str]]:
        owner_name = "Nutzer"
        messages: list[discord.Message] = [m async for m in channel.history(limit=HISTORY_LIMIT)]
        history: list[tuple[str, str]] = []
        for m in reversed(messages):
            if m.author.id == ticket["user_id"]:
                owner_name = m.author.display_name
            if self.bot.user and m.author.id == self.bot.user.id:
                if m.content.startswith(AI_PREFIX):
                    history.append(("assistant", m.content[len(AI_PREFIX):].strip()))
                continue
            if m.author.bot:
                continue
            text = m.content.strip()
            if m.attachments:
                text += " " + " ".join(f"[Anhang: {a.filename}]" for a in m.attachments)
            if not text.strip():
                continue
            staff = m.author.id != ticket["user_id"] and self.is_staff(m.author)
            label = "[Staff] " if staff else ""
            history.append(("user", f"{label}{m.author.display_name}: {text[:1500]}"))
        # Die Ticket-Beschreibung steht immer am Anfang des Gesprächs
        history.insert(0, ("user", f"{owner_name} (Ticket-Beschreibung): {ticket['topic']}"))
        return history

    async def _ai_reply(self, channel: discord.TextChannel) -> None:
        ticket = self.bot.db.ticket_by_channel(channel.id)
        if ticket is None or not ticket["ai_enabled"] or ticket["staff_joined"]:
            return
        if ticket["ai_replies"] >= self.bot.config.ticket_ai_max_replies:
            await self._escalate(channel, ticket, "Antwortlimit der KI in diesem Ticket erreicht", source="System")
            return

        system = build_system_prompt(server_name=self._server_name(), topic=ticket["topic"],
                                     ingame_name=ticket["ingame_name"], status=self._status_text(),
                                     knowledge=self._knowledge())
        messages = build_messages(system, await self._history(channel, ticket))
        try:
            async with channel.typing():
                raw = await self.ai.chat_json(messages)
        except DeepSeekError as exc:
            log.warning("DeepSeek-Fehler in Ticket #%s: %s", ticket["id"], exc)
            if not ticket["escalated"]:
                await channel.send("⚠️ Der KI-Assistent ist gerade nicht erreichbar – ich habe das Team benachrichtigt.")
            await self._escalate(channel, ticket, f"KI-Fehler: {exc}", source="System")
            return

        answer = parse_answer(raw)
        # Nach dem Warten nochmal prüfen: hat inzwischen Staff übernommen?
        ticket = self.bot.db.ticket_by_channel(channel.id)
        if ticket is None or ticket["staff_joined"] or not ticket["ai_enabled"]:
            return
        if answer.reply:
            await channel.send(AI_PREFIX + answer.reply, allowed_mentions=discord.AllowedMentions.none())
            self.bot.db.increment_ticket_ai_replies(ticket["id"])
        if answer.escalate:
            await self._escalate(channel, ticket, answer.reason, source="KI")

    # ------------------------------------------------------------ Staff pingen

    async def _escalate(self, channel: discord.TextChannel, ticket, reason: str, *, source: str,
                        force: bool = False) -> bool:
        """Pingt die Support-Rollen. Ohne force nur einmal pro Ticket."""
        if ticket["escalated"] and not force:
            return False
        self.bot.db.update_ticket(ticket["id"], escalated=1)
        self._last_ping[channel.id] = time.monotonic()

        roles = self._staff_roles(channel.guild)
        embed = discord.Embed(title="🔔 Staff wird benötigt", description=safe_md(reason)[:1000],
                              colour=discord.Colour.orange())
        embed.add_field(name="Ticket", value=f"#{ticket['id']}")
        embed.add_field(name="Nutzer", value=f"<@{ticket['user_id']}>")
        embed.add_field(name="Ausgelöst durch", value=source)
        await channel.send(content=" ".join(r.mention for r in roles) or "Staff",
                           embed=embed, allowed_mentions=discord.AllowedMentions(roles=roles))

        log_embed = embed.copy()
        log_embed.add_field(name="Channel", value=channel.mention)
        await self._log(channel.guild, embed=log_embed)
        return True

    async def request_staff(self, interaction: discord.Interaction) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("Dieses Ticket ist nicht mehr offen.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not self.is_staff(interaction.user):
            await interaction.response.send_message("Nur der Ticket-Ersteller kann Staff rufen.", ephemeral=True)
            return
        since = time.monotonic() - self._last_ping.get(interaction.channel_id, -1e9)
        if ticket["escalated"] and since < STAFF_REPING_SECONDS:
            wait = format_duration(STAFF_REPING_SECONDS - since)
            await interaction.response.send_message(
                f"Das Team wurde bereits benachrichtigt. Erneut rufen kannst du in ca. {wait}.", ephemeral=True)
            return
        await interaction.response.send_message("🔔 Das Team wird benachrichtigt.", ephemeral=True)
        await self._escalate(interaction.channel, ticket, "Nutzer hat über den Button Staff angefordert",
                             source="Nutzer", force=True)

    # ------------------------------------------------------------ Schließen

    async def close_flow(self, interaction: discord.Interaction, reason: str | None) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("Das ist kein offenes Ticket.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not self.is_staff(interaction.user):
            await interaction.response.send_message("Nur Ersteller oder Staff können das Ticket schließen.",
                                                    ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        view.confirm.label = "Ja, schließen"
        await interaction.response.send_message("Ticket wirklich schließen?", view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Zeit abgelaufen – abgebrochen.", view=None)
            return
        await interaction.edit_original_response(content="🔒 Ticket wird geschlossen …", view=None)
        await self._close(interaction.channel, ticket, interaction.user, reason)

    async def _transcript(self, channel: discord.TextChannel) -> str:
        lines = []
        async for m in channel.history(limit=2000, oldest_first=True):
            stamp = m.created_at.strftime("%Y-%m-%d %H:%M")
            text = m.content
            for e in m.embeds:
                text += f" [Embed: {e.title or ''} {e.description or ''}]".rstrip()
            for a in m.attachments:
                text += f" [Anhang: {a.url}]"
            lines.append(f"[{stamp} UTC] {m.author} : {text.strip()}")
        return "\n".join(lines)

    async def _close(self, channel: discord.TextChannel, ticket, closer: discord.abc.User,
                     reason: str | None) -> None:
        transcript = await self._transcript(channel)
        self.bot.db.close_ticket(ticket["id"], str(closer), reason)

        embed = discord.Embed(title=f"🔒 Ticket #{ticket['id']} geschlossen", colour=discord.Colour.dark_grey())
        embed.add_field(name="Ersteller", value=f"<@{ticket['user_id']}>")
        embed.add_field(name="Geschlossen von", value=f"{closer.mention}")
        embed.add_field(name="KI-Antworten", value=str(ticket["ai_replies"]))
        embed.add_field(name="Staff gerufen", value="ja" if ticket["escalated"] else "nein")
        if reason:
            embed.add_field(name="Grund", value=safe_md(reason)[:1000], inline=False)
        embed.add_field(name="Thema", value=safe_md(ticket["topic"])[:1000], inline=False)
        file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"ticket-{ticket['id']:04d}.txt")
        await self._log(channel.guild, embed=embed, file=file)

        owner = channel.guild.get_member(ticket["user_id"])
        if owner is not None:
            try:
                await owner.send(f"Dein Support-Ticket #{ticket['id']} auf **{safe_md(channel.guild.name)}** "
                                 f"wurde geschlossen." + (f"\nGrund: {reason}" if reason else ""))
            except discord.HTTPException:
                pass  # DMs deaktiviert

        await channel.send(f"🔒 Ticket geschlossen – der Channel wird in {DELETE_DELAY_SECONDS} Sekunden gelöscht.")
        await asyncio.sleep(DELETE_DELAY_SECONDS)
        try:
            await channel.delete(reason=f"Ticket #{ticket['id']} geschlossen von {closer}")
        except discord.HTTPException as exc:
            log.warning("Ticket-Channel konnte nicht gelöscht werden: %s", exc)
        self._generation.pop(channel.id, None)
        self._locks.pop(channel.id, None)
        self._last_ping.pop(channel.id, None)

    # ------------------------------------------------------------ Slash-Commands

    @ticket.command(name="panel", description="Postet das Ticket-Panel mit Button in diesen Channel.")
    @admin_only()
    async def panel(self, interaction: discord.Interaction) -> None:
        if not self.enabled:
            await self._not_enabled(interaction)
            return
        embed = discord.Embed(
            title="🎫 Support",
            description=("Du brauchst Hilfe mit dem Server? Klicke auf **Ticket öffnen** und beschreibe dein "
                         "Anliegen. Unser KI-Assistent antwortet sofort und holt bei Bedarf das Team dazu.\n\n"
                         "-# Hinweis: Nachrichten in Tickets werden zur Beantwortung an den KI-Dienst DeepSeek "
                         "übermittelt. Bitte keine persönlichen Daten posten."
                         if self.ai.enabled else
                         "Du brauchst Hilfe? Klicke auf **Ticket öffnen** – das Team meldet sich bei dir."),
            colour=discord.Colour.blurple())
        await interaction.channel.send(embed=embed, view=TicketPanelView(self))
        await interaction.response.send_message("✅ Panel gepostet.", ephemeral=True)

    @ticket.command(name="oeffnen", description="Öffnet ein neues Support-Ticket.")
    async def open_cmd(self, interaction: discord.Interaction) -> None:
        await self.start_ticket_flow(interaction)

    @ticket.command(name="schliessen", description="Schließt dieses Ticket.")
    @app_commands.describe(grund="Optionaler Grund (steht im Log und in der DM an den Nutzer)")
    async def close_cmd(self, interaction: discord.Interaction, grund: str | None = None) -> None:
        await self.close_flow(interaction, reason=grund)

    @ticket.command(name="ki", description="Schaltet den KI-Assistenten in diesem Ticket an oder aus (Staff).")
    @app_commands.describe(aktiv="KI antwortet in diesem Ticket")
    async def ai_toggle(self, interaction: discord.Interaction, aktiv: bool) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None or not self.is_staff(interaction.user):
            await interaction.response.send_message("Nur Staff, nur in offenen Tickets.", ephemeral=True)
            return
        self.bot.db.update_ticket(ticket["id"], ai_enabled=int(aktiv), staff_joined=0 if aktiv else 1)
        await interaction.response.send_message(
            "🤖 KI-Assistent ist in diesem Ticket wieder aktiv." if aktiv else "⏸️ KI-Assistent pausiert.")

    @ticket.command(name="hinzufuegen", description="Fügt ein Mitglied zu diesem Ticket hinzu (Staff).")
    @app_commands.describe(mitglied="Wer soll das Ticket sehen?")
    async def add_member(self, interaction: discord.Interaction, mitglied: discord.Member) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None or not self.is_staff(interaction.user):
            await interaction.response.send_message("Nur Staff, nur in offenen Tickets.", ephemeral=True)
            return
        await interaction.channel.set_permissions(mitglied, view_channel=True, send_messages=True,
                                                  read_message_history=True, attach_files=True)
        await interaction.response.send_message(f"➕ {mitglied.mention} wurde hinzugefügt.",
                                                allowed_mentions=discord.AllowedMentions(users=[mitglied]))

    @ticket.command(name="ki-status", description="Zeigt Modell und Guthaben des DeepSeek-Kontos (Admins).")
    @admin_only()
    async def ai_status(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🤖 DeepSeek-Plattform", colour=discord.Colour.blurple(), url=PLATFORM_USAGE_URL)
        embed.add_field(name="Modell", value=f"`{self.ai.model}`")
        embed.add_field(name="API", value=f"`{self.ai.base_url}`")
        if not self.ai.enabled:
            embed.description = f"⚠️ Kein `DEEPSEEK_API_KEY` gesetzt. Key anlegen: {PLATFORM_KEYS_URL}"
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.ai.balance()
        except DeepSeekError as exc:
            embed.description = f"⚠️ {exc}"
            embed.colour = discord.Colour.red()
        else:
            ok = bool(data.get("is_available"))
            embed.description = "✅ Guthaben reicht für API-Aufrufe." if ok else \
                "⚠️ Guthaben reicht **nicht** – Tickets gehen dann direkt an den Staff."
            embed.colour = discord.Colour.green() if ok else discord.Colour.red()
            for info in data.get("balance_infos") or []:
                embed.add_field(
                    name=f"Guthaben {info.get('currency', '?')}",
                    value=(f"**{info.get('total_balance', '?')}** gesamt\n"
                           f"{info.get('topped_up_balance', '?')} aufgeladen · "
                           f"{info.get('granted_balance', '?')} geschenkt"),
                    inline=False)
        embed.add_field(name="Verbrauch & Aufladen", value=PLATFORM_USAGE_URL, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ticket.command(name="ki-test", description="Testet den KI-Assistenten mit einer Frage (Admins).")
    @app_commands.describe(frage="Beispielfrage, wie sie ein Spieler stellen würde")
    @admin_only()
    async def ai_test(self, interaction: discord.Interaction, frage: str) -> None:
        if not self.ai.enabled:
            await interaction.response.send_message("Kein DEEPSEEK_API_KEY konfiguriert.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        system = build_system_prompt(server_name=self._server_name(), topic=frage, ingame_name=None,
                                     status=self._status_text(), knowledge=self._knowledge())
        try:
            raw = await self.ai.chat_json(build_messages(system, [("user", f"Testnutzer: {frage}")]))
        except DeepSeekError as exc:
            await interaction.followup.send(f"⚠️ DeepSeek-Fehler: {exc}", ephemeral=True)
            return
        answer = parse_answer(raw)
        knowledge = "geladen" if self._knowledge() else "⚠️ nicht gefunden"
        await interaction.followup.send(
            f"**Antwort:**\n{answer.reply or '–'}\n\n**Staff rufen:** {'ja – ' + answer.reason if answer.escalate else 'nein'}"
            f"\n-# Modell: {self.ai.model} · Wissensbasis {knowledge}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Tickets(bot))
