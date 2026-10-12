"""Support tickets with a first AI reply (DeepSeek) and staff notification.

Flow:
  1. The user clicks "Open ticket" in the ticket panel (or /ticket open) and describes their issue.
  2. The bot creates a private channel in TICKET_CATEGORY_ID (user + support roles + bot).
  3. The AI replies to every user message (with a short pause to batch several messages).
  4. When a human is needed, the bot pings the support roles – triggered by the AI,
     via the "Call staff" button, on AI errors or when the reply limit is reached.
  5. As soon as a staff member writes in the ticket, the AI pauses automatically.
  6. On close, a transcript is saved to TICKET_LOG_CHANNEL_ID and the channel is deleted.
"""
from __future__ import annotations

import asyncio
import io
import json
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
from permcheck import explain_forbidden
from support.deepseek import PLATFORM_KEYS_URL, PLATFORM_USAGE_URL, DeepSeekClient, DeepSeekError
from support.giveaway_wins import GiveawayWin, match_kit, parse_win, prize_duration_minutes
from support.prompt import build_messages, build_system_prompt, parse_answer
from utils import admin_only, format_duration, is_admin, safe_md, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.tickets")

AI_PREFIX = "🤖 "             # Marks AI replies (important for the conversation history)
CLAIM_PREFIX = "✅ 🎁 "        # Kit claimed by the AI tool (also part of the history)
PRIZE_PREFIX = "🎉 Giveaway won: "   # Prize notice in an already open ticket (also part of the history)
DEBOUNCE_SECONDS = 3.0        # answer several quick messages in one go
HISTORY_LIMIT = 40            # number of messages the AI gets as history
STAFF_REPING_SECONDS = 600    # "Call staff" again only after 10 minutes
KNOWLEDGE_MAX_CHARS = 12000
DELETE_DELAY_SECONDS = 5
MAX_TOOL_ROUNDS = 5           # AI tool calls per reply (look up kits, claim, …)
GIVEAWAY_SCAN_LIMIT = 500     # winner messages read from the giveaway channel
GIVEAWAY_CACHE_SECONDS = 60

# Tools for the AI – they always act for the ticket owner (never a user chosen by the model)
KIT_TOOLS = [
    {"type": "function", "function": {
        "name": "get_my_kits",
        "description": "Kits the ticket owner may claim (from their Discord roles) with contents, last claim, "
                       "cooldown and whether they can claim now; their linked in-game player and online status; "
                       "role-locked autokits (e.g. spawn kits) and whether they have the needed role.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "claim_kit",
        "description": "Claims one custom kit for the ticket owner – sent in-game to their linked player. "
                       "Enforces role, cooldown, linked player and online status. Only when they asked for it.",
        "parameters": {"type": "object", "properties": {
            "kit": {"type": "string", "description": "Exact kit name from get_my_kits"}}, "required": ["kit"]}}},
]

PURCHASE_TOOLS = [
    {"type": "function", "function": {
        "name": "request_kit_access",
        "description": "The ticket owner says they BOUGHT a custom kit: files a request that staff approves or rejects "
                       "with a button. On approval they get the kit's Discord role and can claim the kit with "
                       "/customkit claim. Nothing is given before staff approves. Use once per kit.",
        "parameters": {"type": "object", "properties": {
            "kit": {"type": "string", "description": "Name of the custom kit they bought"},
            "proof": {"type": "string", "description": "What they told you: store, order/transaction ID, date, price"},
            "days": {"type": "integer", "description": "Only if the purchase is time-limited: number of days. "
                                                       "0 = permanent (default)"}},
            "required": ["kit", "proof"]}}},
]

GIVEAWAY_TOOLS = [
    {"type": "function", "function": {
        "name": "get_my_giveaway_wins",
        "description": "Giveaways the ticket owner won (this server's /giveaway and the winners channel): prize, "
                       "date, whether it is a lifetime prize, the matching kit and its Discord role, and whether it "
                       "was already redeemed.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "redeem_giveaway_win",
        "description": "Redeems one of the ticket owner's wins: for a kit prize it gives them the kit's Discord "
                       "role (once per win) – for good if lifetime, else for the prize's duration (e.g. 7 days). "
                       "Other prizes return needs_staff.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "message_id of the win from get_my_giveaway_wins"}},
            "required": ["message_id"]}}},
]


class TicketError(Exception):
    """Ticket could not be opened – message for the user."""


class TicketModal(discord.ui.Modal, title="Open support ticket"):
    ingame = discord.ui.TextInput(label="In-game name (Gamertag/PSN)", required=False, max_length=32)
    anliegen = discord.ui.TextInput(label="What is it about?", style=discord.TextStyle.paragraph,
                                    min_length=10, max_length=1500,
                                    placeholder="Describe your problem as precisely as possible.")

    def __init__(self, cog: "Tickets") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(interaction, str(self.anliegen).strip(), str(self.ingame).strip() or None)


class TicketPanelView(discord.ui.View):
    """Persistent button in the panel channel (keeps working after a bot restart)."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open ticket", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="rce:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_ticket_flow(interaction)


class TicketControlView(discord.ui.View):
    """Persistent buttons in every ticket channel."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Call staff", emoji="🔔", style=discord.ButtonStyle.secondary,
                       custom_id="rce:ticket:staff")
    async def call_staff(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.request_staff(interaction)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="rce:ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.close_flow(interaction, reason=None)


class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)

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
            log.warning("Tickets enabled, but no DEEPSEEK_API_KEY – tickets go straight to staff. "
                        "Create an API key: %s", PLATFORM_KEYS_URL)
        elif self.ai.enabled:
            asyncio.create_task(self._check_balance_on_start())

    async def on_config_changed(self, old, new) -> None:
        if (old.deepseek_api_key, old.deepseek_model, old.deepseek_base_url) != \
                (new.deepseek_api_key, new.deepseek_model, new.deepseek_base_url):
            await self.ai.close()
            self.ai = DeepSeekClient(new.deepseek_api_key, model=new.deepseek_model, base_url=new.deepseek_base_url)
        if old.support_knowledge_file != new.support_knowledge_file:
            self._knowledge_cache = None

    async def _check_balance_on_start(self) -> None:
        try:
            data = await self.ai.balance()
        except DeepSeekError as exc:
            log.warning("DeepSeek platform unreachable or key invalid: %s", exc)
            return
        if not data.get("is_available", True):
            log.warning("DeepSeek balance too low for API calls – top up: %s", PLATFORM_USAGE_URL)
        else:
            log.info("DeepSeek platform connected (model %s).", self.ai.model)

    async def cog_unload(self) -> None:
        await self.ai.close()

    # ------------------------------------------------------------ Helpers

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
            return "Server status currently unknown (no RCON connection)."
        parts = [
            f"Server online, {info.get('Players', '?')}/{info.get('MaxPlayers', '?')} players",
        ]
        if info.get("Queued"):
            parts.append(f"{info['Queued']} in queue")
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
                log.warning("Could not send ticket log: %s", exc)

    async def _not_enabled(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Tickets are not set up (TICKET_CATEGORY_ID is missing in .env).", ephemeral=True)

    # ------------------------------------------------------------ Open ticket

    async def start_ticket_flow(self, interaction: discord.Interaction) -> None:
        if not self.enabled or interaction.guild is None:
            await self._not_enabled(interaction)
            return
        existing = self.bot.db.open_ticket_of_user(interaction.user.id)
        if existing and interaction.guild.get_channel(existing["channel_id"]):
            await interaction.response.send_message(
                f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
            return
        await interaction.response.send_modal(TicketModal(self))

    async def create_ticket(self, interaction: discord.Interaction, topic: str, ingame: str | None) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await self.open_ticket(interaction.guild, interaction.user, topic, ingame)
        except TicketError as exc:
            await interaction.followup.send(str(exc)[:2000], ephemeral=True)
            return
        await interaction.followup.send(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)

    async def open_ticket(self, guild: discord.Guild, user: discord.abc.User, topic: str, ingame: str | None, *,
                          title: str | None = None, opened_by: str | None = None) -> discord.TextChannel:
        """Creates a ticket channel for this user and starts the AI (also used for giveaway prize tickets)."""
        category = guild.get_channel(self.bot.config.ticket_category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise TicketError("⚠️ The ticket category was not found (TICKET_CATEGORY_ID).")

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
                topic=f"Ticket #{ticket_id} by {user} ({user.id})", reason=f"Support ticket #{ticket_id}")
        except discord.HTTPException as exc:
            self.bot.db.delete_ticket(ticket_id)
            log.error("Could not create ticket channel: %s", exc)
            text = "⚠️ Could not create the ticket."
            if isinstance(exc, discord.Forbidden):
                text += "\n" + explain_forbidden(exc, self.bot)
            raise TicketError(text) from exc
        self.bot.db.update_ticket(ticket_id, channel_id=channel.id)

        embed = discord.Embed(title=title or f"🎫 Ticket #{ticket_id}", description=topic[:4000],
                              colour=discord.Colour.blurple())
        embed.add_field(name="In-game name", value=safe_md(ingame) if ingame else "–")
        if self.ai.enabled:
            embed.add_field(name="What happens next?", inline=False, value=(
                "Our AI assistant will reply shortly. If it can't help, it will bring in the team – "
                "or you can click **Call staff**."))
        else:
            embed.add_field(name="What happens next?", value="The team has been notified.", inline=False)
        embed.set_footer(text="Please do not post passwords or payment details.")
        await channel.send(content=user.mention, embed=embed, view=TicketControlView(self),
                           allowed_mentions=discord.AllowedMentions(users=[user]))

        log_embed = discord.Embed(title=f"🎫 Ticket #{ticket_id} opened", description=topic[:1000],
                                  colour=discord.Colour.blurple())
        log_embed.add_field(name="By", value=opened_by or f"{user.mention} ({safe_md(str(user))})")
        log_embed.add_field(name="Channel", value=channel.mention)
        await self._log(guild, embed=log_embed)

        ticket = self.bot.db.ticket(ticket_id)
        if self.ai.enabled:
            self._schedule_ai(channel, delay=0)
        else:
            await self._escalate(channel, ticket, "New ticket (no AI configured)", source="System")
        return channel

    async def open_prize_ticket(self, guild: discord.Guild, winner_id: int, prize: str, link: str) -> None:
        """Giveaway won -> the winner gets a ticket in which the AI redeems the prize (or calls staff)."""
        member = guild.get_member(winner_id)
        if member is None:
            try:
                member = await guild.fetch_member(winner_id)
            except discord.HTTPException:
                return
        topic = (f"🎉 I won the giveaway \"{prize}\" ({link}). Please redeem my prize.")
        existing = self.bot.db.open_ticket_of_user(member.id)
        channel = guild.get_channel(existing["channel_id"]) if existing else None
        if channel is not None:
            # Already has an open ticket: add the prize there instead of opening a second one
            await channel.send(f"{PRIZE_PREFIX}{member.mention} won **{safe_md(prize)}** ({link}) – the prize is "
                               "handled in this ticket.", allowed_mentions=discord.AllowedMentions(users=[member]))
            ticket = self.bot.db.ticket_by_channel(channel.id)
            if self.ai.enabled:
                self._schedule_ai(channel, delay=1)
            elif ticket is not None:
                await self._escalate(channel, ticket, f"Giveaway prize to hand out: {prize}", source="Giveaway",
                                     force=True)
            return
        try:
            await self.open_ticket(guild, member, topic, self.bot.db.linked_player(member.id),
                                   title="🎉 Giveaway prize", opened_by="Giveaway (automatic)")
        except TicketError as exc:
            log.warning("Prize ticket for %s failed: %s", member, exc)

    # ------------------------------------------------------------ Messages & AI

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
                self._generation[message.channel.id] += 1  # discard pending AI reply
                await message.channel.send(
                    f"👋 **{safe_md(message.author.display_name)}** from the team has taken over – "
                    "the AI assistant is paused in this ticket.",
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
            return  # a newer message arrived meanwhile – it will be answered together
        async with self._locks[channel.id]:
            try:
                await self._ai_reply(channel)
            except discord.HTTPException as exc:
                log.warning("AI reply in %s failed: %s", channel.id, exc)

    async def _history(self, channel: discord.TextChannel, ticket) -> list[tuple[str, str]]:
        owner_name = "User"
        messages: list[discord.Message] = [m async for m in channel.history(limit=HISTORY_LIMIT)]
        history: list[tuple[str, str]] = []
        for m in reversed(messages):
            if m.author.id == ticket["user_id"]:
                owner_name = m.author.display_name
            if self.bot.user and m.author.id == self.bot.user.id:
                if m.content.startswith(AI_PREFIX):
                    history.append(("assistant", m.content[len(AI_PREFIX):].strip()))
                elif m.content.startswith(CLAIM_PREFIX):
                    history.append(("assistant", "[Kit claimed via claim_kit] " + m.content[len(CLAIM_PREFIX):]))
                elif m.content.startswith(PRIZE_PREFIX):
                    history.append(("user", "[System] I won the giveaway: " + m.content[len(PRIZE_PREFIX):]
                                    + " Please redeem my prize."))
                continue
            if m.author.bot:
                continue
            text = m.content.strip()
            if m.attachments:
                text += " " + " ".join(f"[Attachment: {a.filename}]" for a in m.attachments)
            if not text.strip():
                continue
            staff = m.author.id != ticket["user_id"] and self.is_staff(m.author)
            label = "[Staff] " if staff else ""
            history.append(("user", f"{label}{m.author.display_name}: {text[:1500]}"))
        # The ticket description always comes first in the conversation
        history.insert(0, ("user", f"{owner_name} (ticket description): {ticket['topic']}"))
        return history

    async def _ai_reply(self, channel: discord.TextChannel) -> None:
        ticket = self.bot.db.ticket_by_channel(channel.id)
        if ticket is None or not ticket["ai_enabled"] or ticket["staff_joined"]:
            return
        if ticket["ai_replies"] >= self.bot.config.ticket_ai_max_replies:
            await self._escalate(channel, ticket, "AI reply limit reached in this ticket", source="System")
            return

        tools = self._ai_tools()
        system = build_system_prompt(server_name=self._server_name(), topic=ticket["topic"],
                                     ingame_name=ticket["ingame_name"], status=self._status_text(),
                                     knowledge=self._knowledge(), kit_tools=bool(tools),
                                     giveaway_tools=any(t in GIVEAWAY_TOOLS for t in tools),
                purchase_tools=any(t in PURCHASE_TOOLS for t in tools))
        messages = build_messages(system, await self._history(channel, ticket))
        try:
            async with channel.typing():
                raw = None
                if tools:
                    try:
                        raw = await self._chat_with_tools(list(messages), ticket, channel, tools)
                    except DeepSeekError as exc:
                        if "HTTP 400" not in str(exc) and "HTTP 422" not in str(exc):
                            raise
                        log.warning("DeepSeek rejected the kit tools (%s) – answering without them.", exc)
                if raw is None:
                    raw = await self.ai.chat_json(messages)
        except DeepSeekError as exc:
            log.warning("DeepSeek error in ticket #%s: %s", ticket["id"], exc)
            if not ticket["escalated"]:
                await channel.send("⚠️ The AI assistant is currently unavailable – I have notified the team.")
            await self._escalate(channel, ticket, f"AI error: {exc}", source="System")
            return

        answer = parse_answer(raw)
        # Check again after waiting: has staff taken over in the meantime?
        ticket = self.bot.db.ticket_by_channel(channel.id)
        if ticket is None or ticket["staff_joined"] or not ticket["ai_enabled"]:
            return
        if answer.reply:
            await channel.send(AI_PREFIX + answer.reply, allowed_mentions=discord.AllowedMentions.none())
            self.bot.db.increment_ticket_ai_replies(ticket["id"])
        if answer.escalate:
            await self._escalate(channel, ticket, answer.reason, source="AI")

    # ------------------------------------------------------------ AI tools (kits of the ticket owner)

    def _kit_tools_enabled(self) -> bool:
        return self.bot.config.ticket_ai_kit_tools and self.bot.get_cog("CustomKits") is not None

    def _ai_tools(self) -> list[dict]:
        if not self._kit_tools_enabled():
            return []
        # Giveaway tools always: wins of the bot's own /giveaway are in the database
        purchase = PURCHASE_TOOLS if self.bot.get_cog("KitRequests") is not None else []
        return KIT_TOOLS + GIVEAWAY_TOOLS + purchase

    async def _ticket_member(self, channel: discord.TextChannel, ticket) -> discord.Member | None:
        member = channel.guild.get_member(ticket["user_id"])
        if member is None:
            try:
                member = await channel.guild.fetch_member(ticket["user_id"])
            except discord.HTTPException:
                return None
        return member

    async def _chat_with_tools(self, messages: list[dict], ticket, channel: discord.TextChannel,
                               tools: list[dict] | None = None) -> str:
        """Lets the AI call the kit tools (several rounds), returns its final answer text."""
        tools = tools or self._ai_tools() or KIT_TOOLS
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await self.ai.chat(messages, tools=tools)
            calls = msg["tool_calls"]
            if not calls:
                return msg["content"]
            messages.append({"role": "assistant", "content": msg["content"], "tool_calls": calls})
            for call in calls:
                result = await self._run_tool(call, ticket, channel)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False)})
        # Too many tool rounds – final answer without tools
        return (await self.ai.chat(messages, json_mode=False))["content"]

    async def _run_tool(self, call: dict, ticket, channel: discord.TextChannel) -> dict:
        fn = call.get("function") or {}
        name = fn.get("name")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        custom = self.bot.get_cog("CustomKits")
        member = await self._ticket_member(channel, ticket)
        if custom is None or member is None:
            return {"error": "The ticket owner is not on this Discord server any more."}
        if name == "get_my_kits":
            overview = custom.kit_overview(member)
            kits_cog = self.bot.get_cog("Kits")
            roles = {r.id for r in getattr(member, "roles", [])}
            overview["role_locked_autokits"] = [
                {"kit": r["kit"], "trigger": "respawn" if r["trigger"] == "respawn" else "quick chat",
                 "required_role": (channel.guild.get_role(r["required_role_id"]).name
                                   if channel.guild.get_role(r["required_role_id"]) else "a role"),
                 "has_role": r["required_role_id"] in roles}
                for r in self.bot.db.kit_rules(enabled_only=True) if r["required_role_id"]] if kits_cog else []
            economy = self.bot.get_cog("Economy")
            if economy is not None and economy.enabled:
                overview["points"] = {"balance": self.bot.db.wallet(member.id)["points"],
                                      "name": self.bot.config.points_name,
                                      "how_to_spend": "/shop list and /shop buy in Discord", "daily": "/daily"}
            if overview["linked_player"] is None:
                overview["note"] = "Not linked yet: the player must use /customkit link while online in-game."
            return overview
        if name == "claim_kit":
            outcome = await custom.claim_for(member, str(args.get("kit") or ""), via=f"ticket #{ticket['id']} (AI)")
            if outcome.ok:  # visible confirmation in the ticket, independent of the AI's wording
                await channel.send(CLAIM_PREFIX + outcome.message.removeprefix("🎁 "),
                                   allowed_mentions=discord.AllowedMentions.none())
            return {"ok": outcome.ok, "result": outcome.code, "kit": outcome.kit, "player": outcome.player,
                    "can_claim_again_discord": f"<t:{outcome.ready_at}:R>" if outcome.ready_at else None,
                    "message": outcome.message.replace("**", "")}
        if name == "get_my_giveaway_wins":
            return await self._tool_giveaway_wins(channel.guild, member)
        if name == "redeem_giveaway_win":
            return await self._tool_redeem_win(channel, ticket, member, str(args.get("message_id") or ""))
        if name == "request_kit_access":
            requests = self.bot.get_cog("KitRequests")
            if requests is None:
                return {"error": "Kit purchases can't be handled here – call staff."}
            try:
                days = max(0, min(int(args.get("days") or 0), 3650))
            except (TypeError, ValueError):
                days = 0
            return await requests.file(channel, ticket, member, str(args.get("kit") or ""),
                                       str(args.get("proof") or "")[:500], days)
        return {"error": f"Unknown tool {name}"}

    # ------------------------------------------------------------ Giveaway wins (own /giveaway + Helios, Kryvon …)

    def _own_wins(self, guild: discord.Guild) -> list[GiveawayWin]:
        """Winners of this bot's own /giveaway (from the database – always up to date, no cache)."""
        return [GiveawayWin(message_id=row["message_id"], created_at=int(row["ends_at"]),
                            jump_url=f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}",
                            winner_ids=[int(w) for w in row["winner_ids"].split(",") if w],
                            winner_names=[], prize=row["prize"])
                for row in self.bot.db.ended_giveaways(guild.id)]

    async def _channel_wins(self, guild: discord.Guild) -> list[GiveawayWin]:
        """Winners announced by another bot (Helios, Kryvon …) in GIVEAWAY_WIN_CHANNEL_ID (cached briefly)."""
        channel_id = self.bot.config.giveaway_win_channel_id
        if not channel_id:
            return []
        cached = getattr(self, "_win_cache", None)
        if cached and time.monotonic() - cached[0] < GIVEAWAY_CACHE_SECONDS:
            return cached[1]
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise LookupError("The giveaway winner channel was not found (or the bot can't see it).")
        wins = []
        async for message in channel.history(limit=GIVEAWAY_SCAN_LIMIT):
            # Only announcements from bots (Helios, Kryvon …) – a player can't post a fake win there
            if message.author.bot and (win := parse_win(message)):
                wins.append(win)
        self._win_cache = (time.monotonic(), wins)
        return wins

    async def _all_wins(self, guild: discord.Guild) -> list[GiveawayWin]:
        own = self._own_wins(guild)
        try:
            other = await self._channel_wins(guild)
        except (LookupError, discord.HTTPException) as exc:
            if not own:
                raise
            log.warning("Giveaway winner channel unavailable: %s", exc)
            other = []
        known = {w.message_id for w in own}
        return own + [w for w in other if w.message_id not in known]

    def _prize_kits(self) -> dict[str, int | None]:
        """Kit name -> Discord role that unlocks it (custom kit claim role or role-locked autokit)."""
        kits: dict[str, int | None] = {}
        for rule in self.bot.db.kit_rules(enabled_only=True):
            if rule["required_role_id"]:
                kits.setdefault(rule["kit"], rule["required_role_id"])
        for row in self.bot.db.custom_kits():
            roles = self.bot.db.claim_roles(row)
            kits[row["name"]] = roles[0] if roles else kits.get(row["name"])
        return kits

    async def _owner_wins(self, guild: discord.Guild, member) -> list[GiveawayWin]:
        names = {n.lower() for n in (member.name, getattr(member, "global_name", None),
                                     getattr(member, "display_name", None)) if n}
        return [w for w in await self._all_wins(guild) if w.won_by(member.id, names)]

    def _describe_win(self, guild: discord.Guild, win: GiveawayWin, kits: dict[str, int | None],
                      user_id: int) -> dict:
        kit = match_kit(win.prize, list(kits))
        role = guild.get_role(kits[kit]) if kit and kits[kit] else None
        redeemed = self.bot.db.giveaway_redemption(win.message_id, user_id)
        minutes = self._prize_minutes(win)
        return {"message_id": str(win.message_id), "prize": win.prize, "won_at_discord": f"<t:{win.created_at}:R>",
                "lifetime": win.lifetime, "duration": "lifetime" if win.lifetime else
                (format_duration(minutes * 60) if minutes else None), "kit": kit,
                "kit_role": role.name if role else None, "already_redeemed": bool(redeemed), "link": win.jump_url}

    def _prize_minutes(self, win: GiveawayWin) -> int | None:
        """How long a non-lifetime prize lasts: from the prize text ("7 days"), else GIVEAWAY_PRIZE_DAYS."""
        if win.lifetime:
            return None
        return prize_duration_minutes(win.prize) or (self.bot.config.giveaway_prize_days * 1440 or None)

    async def _tool_giveaway_wins(self, guild: discord.Guild, member) -> dict:
        try:
            wins = await self._owner_wins(guild, member)
        except (LookupError, discord.HTTPException) as exc:
            return {"error": str(exc)}
        kits = self._prize_kits()
        return {"wins": [self._describe_win(guild, w, kits, member.id) for w in wins],
                "note": None if wins else "No giveaway win found for this user."}

    async def _tool_redeem_win(self, channel: discord.TextChannel, ticket, member, message_id: str) -> dict:
        try:
            wins = await self._owner_wins(channel.guild, member)
        except (LookupError, discord.HTTPException) as exc:
            return {"ok": False, "result": "error", "message": str(exc)}
        win = next((w for w in wins if str(w.message_id) == message_id.strip()), None)
        if win is None:
            return {"ok": False, "result": "not_your_win", "message": "This is not a win of the ticket owner."}
        info = self._describe_win(channel.guild, win, self._prize_kits(), member.id)
        if info["already_redeemed"]:
            return {"ok": False, "result": "already_redeemed", **info}
        if not info["kit"] or not info["kit_role"]:
            return {"ok": False, "result": "needs_staff", "reason": "Prize is not a kit with a Discord role", **info}
        minutes = self._prize_minutes(win)
        if not win.lifetime and not minutes:
            return {"ok": False, "result": "needs_staff",
                    "reason": "Kit prize is neither lifetime nor has a duration (e.g. '7 days')", **info}
        role = channel.guild.get_role(self._prize_kits()[info["kit"]])
        action, until = "role already owned", ""
        if minutes:
            timed = self.bot.get_cog("TimedRoles")
            if timed is None:
                return {"ok": False, "result": "needs_staff", "reason": "Timed roles are not available", **info}
            if timed.permanent(member, role):
                action = "role already owned permanently"
            else:
                from cogs.timed_roles import TimedRoleError
                try:
                    expires_at = await timed.grant(member, role, minutes, by=f"ticket #{ticket['id']} (AI)",
                                                   reason=f"Giveaway prize {win.prize}")
                except TimedRoleError as exc:
                    return {"ok": False, "result": "needs_staff", "reason": str(exc), **info}
                action = f"role {role.name} for {format_duration(minutes * 60)}"
                until = f" until <t:{expires_at}:f> (<t:{expires_at}:R>)"
        elif role not in getattr(member, "roles", []):
            try:
                await member.add_roles(role, reason=f"Giveaway win {win.jump_url} (ticket #{ticket['id']})")
            except discord.Forbidden as exc:
                return {"ok": False, "result": "needs_staff", "reason": explain_forbidden(exc, self.bot), **info}
            except discord.HTTPException as exc:
                return {"ok": False, "result": "needs_staff", "reason": f"Discord error: {exc}", **info}
            action = f"role {role.name} given"
        if not self.bot.db.record_giveaway_redemption(win.message_id, member.id, win.prize, action):
            return {"ok": False, "result": "already_redeemed", **info}
        await channel.send(f"{CLAIM_PREFIX}Giveaway prize **{safe_md(win.prize)}** redeemed: you now have the "
                           f"**{safe_md(role.name)}** role{until} (unlocks **{safe_md(info['kit'])}**).",
                           allowed_mentions=discord.AllowedMentions.none())
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"🎉 {member.mention} redeemed giveaway win \"{safe_md(win.prize)}\" ({win.jump_url}) "
                          f"via ticket #{ticket['id']} (AI): {action}")
        return {"ok": True, "result": "redeemed", "action": action, "until_discord": until.strip() or None,
                **info, "already_redeemed": True}

    # ------------------------------------------------------------ Ping staff

    async def _escalate(self, channel: discord.TextChannel, ticket, reason: str, *, source: str,
                        force: bool = False) -> bool:
        """Pings the support roles. Without force only once per ticket."""
        if ticket["escalated"] and not force:
            return False
        self.bot.db.update_ticket(ticket["id"], escalated=1)
        self._last_ping[channel.id] = time.monotonic()

        roles = self._staff_roles(channel.guild)
        embed = discord.Embed(title="🔔 Staff needed", description=safe_md(reason)[:1000],
                              colour=discord.Colour.orange())
        embed.add_field(name="Ticket", value=f"#{ticket['id']}")
        embed.add_field(name="User", value=f"<@{ticket['user_id']}>")
        embed.add_field(name="Triggered by", value=source)
        await channel.send(content=" ".join(r.mention for r in roles) or "Staff",
                           embed=embed, allowed_mentions=discord.AllowedMentions(roles=roles))

        log_embed = embed.copy()
        log_embed.add_field(name="Channel", value=channel.mention)
        await self._log(channel.guild, embed=log_embed)
        return True

    async def request_staff(self, interaction: discord.Interaction) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("This ticket is no longer open.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not self.is_staff(interaction.user):
            await interaction.response.send_message("Only the ticket creator can call staff.", ephemeral=True)
            return
        since = time.monotonic() - self._last_ping.get(interaction.channel_id, -1e9)
        if ticket["escalated"] and since < STAFF_REPING_SECONDS:
            wait = format_duration(STAFF_REPING_SECONDS - since)
            await interaction.response.send_message(
                f"The team has already been notified. You can call again in about {wait}.", ephemeral=True)
            return
        await interaction.response.send_message("🔔 Notifying the team.", ephemeral=True)
        await self._escalate(interaction.channel, ticket, "User requested staff via the button",
                             source="User", force=True)

    # ------------------------------------------------------------ Close

    async def close_flow(self, interaction: discord.Interaction, reason: str | None) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not self.is_staff(interaction.user):
            await interaction.response.send_message("Only the creator or staff can close the ticket.",
                                                    ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        view.confirm.label = "Yes, close"
        await interaction.response.send_message("Really close this ticket?", view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Timed out – cancelled.", view=None)
            return
        await interaction.edit_original_response(content="🔒 Closing ticket …", view=None)
        await self._close(interaction.channel, ticket, interaction.user, reason)

    async def _transcript(self, channel: discord.TextChannel) -> str:
        lines = []
        async for m in channel.history(limit=2000, oldest_first=True):
            stamp = m.created_at.strftime("%Y-%m-%d %H:%M")
            text = m.content
            for e in m.embeds:
                text += f" [Embed: {e.title or ''} {e.description or ''}]".rstrip()
            for a in m.attachments:
                text += f" [Attachment: {a.url}]"
            lines.append(f"[{stamp} UTC] {m.author} : {text.strip()}")
        return "\n".join(lines)

    async def _close(self, channel: discord.TextChannel, ticket, closer: discord.abc.User,
                     reason: str | None) -> None:
        transcript = await self._transcript(channel)
        self.bot.db.close_ticket(ticket["id"], str(closer), reason, transcript)

        embed = discord.Embed(title=f"🔒 Ticket #{ticket['id']} closed", colour=discord.Colour.dark_grey())
        embed.add_field(name="Creator", value=f"<@{ticket['user_id']}>")
        embed.add_field(name="Closed by", value=f"{closer.mention}")
        embed.add_field(name="AI replies", value=str(ticket["ai_replies"]))
        embed.add_field(name="Staff called", value="yes" if ticket["escalated"] else "no")
        if reason:
            embed.add_field(name="Reason", value=safe_md(reason)[:1000], inline=False)
        embed.add_field(name="Topic", value=safe_md(ticket["topic"])[:1000], inline=False)
        file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"ticket-{ticket['id']:04d}.txt")
        await self._log(channel.guild, embed=embed, file=file)

        owner = channel.guild.get_member(ticket["user_id"])
        if owner is not None:
            try:
                await owner.send(f"Your support ticket #{ticket['id']} on **{safe_md(channel.guild.name)}** "
                                 f"has been closed." + (f"\nReason: {reason}" if reason else ""))
            except discord.HTTPException:
                pass  # DMs disabled

        await channel.send(f"🔒 Ticket closed – this channel will be deleted in {DELETE_DELAY_SECONDS} seconds.")
        await asyncio.sleep(DELETE_DELAY_SECONDS)
        try:
            await channel.delete(reason=f"Ticket #{ticket['id']} closed by {closer}")
        except discord.HTTPException as exc:
            log.warning("Could not delete ticket channel: %s", exc)
        self._generation.pop(channel.id, None)
        self._locks.pop(channel.id, None)
        self._last_ping.pop(channel.id, None)

    # ------------------------------------------------------------ Slash commands

    @ticket.command(name="panel", description="Posts the ticket panel with its button in this channel.")
    @admin_only()
    async def panel(self, interaction: discord.Interaction) -> None:
        if not self.enabled:
            await self._not_enabled(interaction)
            return
        embed = discord.Embed(
            title="🎫 Support",
            description=("Need help with the server? Click **Open ticket** and describe your "
                         "issue. Our AI assistant replies right away and brings in the team when needed.\n\n"
                         "-# Note: messages in tickets are sent to the AI service DeepSeek to generate replies. "
                         "Please do not post personal data."
                         if self.ai.enabled else
                         "Need help? Click **Open ticket** – the team will get back to you."),
            colour=discord.Colour.blurple())
        await interaction.channel.send(embed=embed, view=TicketPanelView(self))
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @ticket.command(name="open", description="Opens a new support ticket.")
    async def open_cmd(self, interaction: discord.Interaction) -> None:
        await self.start_ticket_flow(interaction)

    @ticket.command(name="close", description="Closes this ticket.")
    @app_commands.describe(reason="Optional reason (shown in the log and in the DM to the user)")
    async def close_cmd(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self.close_flow(interaction, reason=reason)

    @ticket.command(name="ai", description="Turns the AI assistant on or off in this ticket (staff).")
    @app_commands.describe(enabled="AI replies in this ticket")
    async def ai_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None or not self.is_staff(interaction.user):
            await interaction.response.send_message("Staff only, and only in open tickets.", ephemeral=True)
            return
        self.bot.db.update_ticket(ticket["id"], ai_enabled=int(enabled), staff_joined=0 if enabled else 1)
        await interaction.response.send_message(
            "🤖 AI assistant is active again in this ticket." if enabled else "⏸️ AI assistant paused.")

    @ticket.command(name="add", description="Adds a member to this ticket (staff).")
    @app_commands.describe(member="Who should be able to see the ticket?")
    async def add_member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        ticket = self.bot.db.ticket_by_channel(interaction.channel_id)
        if ticket is None or not self.is_staff(interaction.user):
            await interaction.response.send_message("Staff only, and only in open tickets.", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,
                                                  read_message_history=True, attach_files=True)
        await interaction.response.send_message(f"➕ {member.mention} has been added.",
                                                allowed_mentions=discord.AllowedMentions(users=[member]))

    @ticket.command(name="ai-status", description="Shows the model and balance of the DeepSeek account (admins).")
    @admin_only()
    async def ai_status(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🤖 DeepSeek platform", colour=discord.Colour.blurple(), url=PLATFORM_USAGE_URL)
        embed.add_field(name="Model", value=f"`{self.ai.model}`")
        embed.add_field(name="API", value=f"`{self.ai.base_url}`")
        if not self.ai.enabled:
            embed.description = f"⚠️ No `DEEPSEEK_API_KEY` set. Create a key: {PLATFORM_KEYS_URL}"
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
            embed.description = "✅ Balance is sufficient for API calls." if ok else \
                "⚠️ Balance is **not** sufficient – tickets will go straight to staff."
            embed.colour = discord.Colour.green() if ok else discord.Colour.red()
            for info in data.get("balance_infos") or []:
                embed.add_field(
                    name=f"Balance {info.get('currency', '?')}",
                    value=(f"**{info.get('total_balance', '?')}** total\n"
                           f"{info.get('topped_up_balance', '?')} topped up · "
                           f"{info.get('granted_balance', '?')} granted"),
                    inline=False)
        embed.add_field(name="Usage & top-up", value=PLATFORM_USAGE_URL, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ticket.command(name="ai-test", description="Tests the AI assistant with a question (admins).")
    @app_commands.describe(question="Sample question, as a player would ask it")
    @admin_only()
    async def ai_test(self, interaction: discord.Interaction, question: str) -> None:
        if not self.ai.enabled:
            await interaction.response.send_message("No DEEPSEEK_API_KEY configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        system = build_system_prompt(server_name=self._server_name(), topic=question, ingame_name=None,
                                     status=self._status_text(), knowledge=self._knowledge())
        try:
            raw = await self.ai.chat_json(build_messages(system, [("user", f"Test user: {question}")]))
        except DeepSeekError as exc:
            await interaction.followup.send(f"⚠️ DeepSeek error: {exc}", ephemeral=True)
            return
        answer = parse_answer(raw)
        knowledge = "loaded" if self._knowledge() else "⚠️ not found"
        await interaction.followup.send(
            f"**Answer:**\n{answer.reply or '–'}\n\n**Call staff:** {'yes – ' + answer.reason if answer.escalate else 'no'}"
            f"\n-# Model: {self.ai.model} · Knowledge base {knowledge}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Tickets(bot))
