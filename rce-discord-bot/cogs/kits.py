"""Kit manager: list, give and edit kits, plus autokits.

Built on Console Edition's built-in kit commands (no plugin needed):
  kit list | kit info "Kit" | kit givetoplayer "Kit" "Player" | kit givetogroup "Kit" "Group"
  kit giveall "Kit" | kit add ... | kit remove "Kit" "ID"
The syntax of the modifying commands can be customized as templates in the .env (see README).

Autokits (driven by the bot, rules stored in the SQLite database):
  * Trigger "respawn":   player spawns ("<Name> [..] has entered the game") -> kit
  * Trigger "quickchat": player sends a specific quick chat phrase -> kit
    Easiest: /kit bind kit emote (one emote = one kit, binding again replaces it), /kit binds, /kit unbind
  Per rule: cooldown per player and an optional limit (e.g. 1x per wipe).
Kits given on RCE are one-off and are lost on death.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from rce import kits as kitparse
from rce import log_parser as lp
from rce import quickchat
from support import emote_binds
from support import teleports as tp
from permcheck import main_guild
from rce.rcon_client import RconError
from utils import (admin_only, format_duration, player_choices, safe_md, sanitize_ingame_text,
                   sanitize_player_name)

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.kits")

TRIGGER_LABELS = {"respawn": "🔄 Respawn", "quickchat": "💬 Quick chat"}
KIT_TYPE_LABELS = {"ingame": "In-game kit", "custom": "🔒 Custom kit (bot only)"}
ANNOUNCE_THROTTLE_SECONDS = 30  # max. one cooldown message per player within this period
ROLE_CACHE_SECONDS = 120        # how long a member's Discord roles are cached for role-locked autokits


class _CachedList:
    """Cache for RCON lists (kits, groups) – for fast autocomplete."""

    def __init__(self, bot: "RceBot", command: str, parser: Callable[[str], list[str]], ttl: float = 300):
        self.bot, self.command, self.parser, self.ttl = bot, command, parser, ttl
        self.items: list[str] = []
        self._fetched_at: float | None = None  # None = never loaded
        self._task: asyncio.Task | None = None

    def invalidate(self) -> None:
        self._fetched_at = None

    async def _refresh(self) -> None:
        try:
            raw = await self.bot.rcon.command(self.command)
        except RconError as exc:
            log.debug("'%s' failed: %s", self.command, exc)
            return
        self.items = self.parser(raw)
        self._fetched_at = time.monotonic()

    async def get(self, *, max_wait: float = 2.0, force: bool = False) -> list[str]:
        stale = force or self._fetched_at is None or time.monotonic() - self._fetched_at > self.ttl
        if stale and self.bot.rcon.connected and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._refresh())
        if self._task is not None and not self._task.done() and (force or not self.items):
            try:
                # shield: keeps running even if we stop waiting (autocomplete has < 3 s)
                await asyncio.wait_for(asyncio.shield(self._task), max_wait)
            except asyncio.TimeoutError:
                pass
        return self.items


class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Yes, give to everyone", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Giving …", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class Kits(commands.Cog):
    kit = app_commands.Group(name="kit", description="Kit manager", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.kit_names = _CachedList(bot, "kit list", kitparse.parse_kit_list)
        self.auth_groups = _CachedList(bot, "getauthlevels", kitparse.parse_auth_groups, ttl=900)
        self.phrases = quickchat.all_phrases()
        self._announced: dict[str, float] = {}
        self._role_cache: dict[int, tuple[float, frozenset[int]]] = {}  # user_id -> (time, role IDs)
        self.attempts: deque = deque(maxlen=150)   # recent autokit tries with their outcome (diagnosis)

    # ------------------------------------------------------------ Helpers

    def _audit(self, text: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(text)

    def _who(self, interaction: discord.Interaction) -> str:
        return f"{interaction.user.mention} ({safe_md(str(interaction.user))})"

    async def _say(self, text: str) -> None:
        try:
            await self.bot.rcon.command(f"say {text}", expect_response=False)
        except RconError:
            pass

    def _rule_text(self, rule) -> str:
        if rule["trigger"] == "quickchat":
            trigger = f"Quick chat \"{self.phrases.get(rule['phrase'], rule['phrase'])}\""
        else:
            trigger = "on every respawn"
        lock = "🔒 " if rule["kit_type"] == "custom" else ""
        parts = [f"`#{rule['id']}` {lock}**{safe_md(rule['kit'])}** – {trigger}"]
        if rule["cooldown_minutes"]:
            parts.append(f"Cooldown {format_duration(rule['cooldown_minutes'] * 60)}")
        if rule["max_claims"]:
            parts.append(f"max. {rule['max_claims']}× per player")
        if rule["required_role_id"]:
            parts.append(f"🎖️ only <@&{rule['required_role_id']}>")
        if not rule["enabled"]:
            parts.append("⏸️ paused")
        return " · ".join(parts)

    @staticmethod
    def _response_block(response: str) -> str:
        response = response.strip()
        if not response:
            return "(No output from the server – normal for kit commands on RCE.)"
        return f"```\n{response[:1500].replace('```', 'ˋˋˋ')}\n```"

    # ------------------------------------------------------------ Autocomplete

    async def _kit_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = await self.kit_names.get()
        current_l = current.lower()
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names if current_l in n.lower()][:25]

    async def _rule_kit_ac(self, interaction: discord.Interaction, current: str
                           ) -> list[app_commands.Choice[str]]:
        """For /kit autokit-add: in-game kits and/or custom kits, depending on the chosen type."""
        chosen = getattr(interaction.namespace, "kit_type", None)
        custom = [] if chosen == "ingame" else [
            app_commands.Choice(name=f"🔒 {r['name']}"[:100], value=r["name"][:100])
            for r in self.bot.db.custom_kits() if current.lower() in r["name"].lower()
        ]
        ingame = [] if chosen == "custom" else await self._kit_ac(interaction, current)
        return (custom + ingame)[:25]

    async def _player_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    async def _group_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        groups = await self.auth_groups.get()
        return [app_commands.Choice(name=g, value=g) for g in groups if current.lower() in g.lower()][:25]

    async def _phrase_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current_l = current.lower()
        hits = [(raw, text) for raw, text in self.phrases.items() if not tp.is_location(raw)
                and (current_l in text.lower() or current_l in raw.lower())]
        return [app_commands.Choice(name=text[:100], value=raw[:100]) for raw, text in hits[:25]]

    async def _rule_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        choices = []
        for rule in self.bot.db.kit_rules():
            label = f"#{rule['id']} {rule['kit']} ({TRIGGER_LABELS.get(rule['trigger'], rule['trigger'])})"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=int(rule["id"])))
        return choices[:25]

    # ------------------------------------------------------------ Display (everyone)

    @kit.command(name="list", description="Shows all kits on the server.")
    async def kit_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        names = await self.kit_names.get(force=True, max_wait=8)
        if not self.bot.rcon.connected and not names:
            raise RconError("No RCON connection to the Rust server.")
        if not names:
            await interaction.followup.send("No kits have been created on the server.")
            return
        auto = {r["kit"].lower() for r in self.bot.db.kit_rules(enabled_only=True) if r["kit_type"] == "ingame"}
        lines = [f"• **{safe_md(n)}**" + (" · 🤖 Autokit" if n.lower() in auto else "") for n in names]
        embed = discord.Embed(title=f"🎒 Kits ({len(names)})", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Details: /kit info · Autokits: /kit autokits")
        await interaction.followup.send(embed=embed)

    @kit.command(name="info", description="Shows the contents of a kit.")
    @app_commands.describe(kit="Kit name")
    @app_commands.autocomplete(kit=_kit_ac)
    async def kit_info(self, interaction: discord.Interaction, kit: str) -> None:
        name = kitparse.clean_arg(kit)
        await interaction.response.defer()
        raw = await self.bot.rcon.command(f'kit info "{name}"')
        items = kitparse.parse_kit_info(raw)
        embed = discord.Embed(title=f"🎒 Kit: {name}", colour=discord.Colour.dark_green())
        if not items:
            embed.description = "No items recognized. Server response:\n" + self._response_block(raw)
        for container in kitparse.CONTAINERS:
            lines = [
                f"`{i.short_name}` × {i.amount}" + (f" ({i.condition} %)" if i.condition != 100 else "")
                + (f" · ID {i.item_id}" if i.item_id is not None else "")
                for i in items if i.container == container
            ]
            if lines:
                value = "\n".join(lines)
                if len(value) > 1024:
                    value = value[:1000].rsplit("\n", 1)[0] + "\n…"
                embed.add_field(name=kitparse.CONTAINER_LABELS[container.lower()], value=value, inline=False)
        rules = [r for r in self.bot.db.kit_rules(enabled_only=True)
                 if r["kit_type"] == "ingame" and r["kit"].lower() == name.lower()]
        if rules:
            embed.add_field(name="🤖 Autokit", value="\n".join(self._rule_text(r) for r in rules)[:1024],
                            inline=False)
        await interaction.followup.send(embed=embed)

    @kit.command(name="autokits", description="Shows which kits are given out automatically.")
    async def autokit_list(self, interaction: discord.Interaction) -> None:
        rules = self.bot.db.kit_rules()
        if not rules:
            await interaction.response.send_message("No autokits are set up.", ephemeral=True)
            return
        embed = discord.Embed(title="🤖 Autokits", description="\n".join(self._rule_text(r) for r in rules)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Quick chat kits: send the phrase in game · kits are lost on death")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ Giving (admins)

    @kit.command(name="give", description="Gives a kit to a player.")
    @app_commands.describe(kit="Kit name", player="In-game name")
    @app_commands.autocomplete(kit=_kit_ac, player=_player_ac)
    @admin_only()
    async def kit_give(self, interaction: discord.Interaction, kit: str, player: str) -> None:
        name, player = kitparse.clean_arg(kit), sanitize_player_name(player)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_template.format(kit=name, name=player), expect_response=False)
        self.bot.db.record_kit_claim(None, name, player, f"discord:{interaction.user}")
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** to **{safe_md(player)}**")
        hint = "" if player in self.bot.online_players else "\n⚠️ Player is not online according to the player list."
        await interaction.followup.send(f"🎒 Kit **{safe_md(name)}** sent to **{safe_md(player)}**.{hint}\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="group", description="Gives a kit to all members of an auth group.")
    @app_commands.describe(kit="Kit name", group="Auth group (e.g. VIP, Admin)")
    @app_commands.autocomplete(kit=_kit_ac, group=_group_ac)
    @admin_only()
    async def kit_give_group(self, interaction: discord.Interaction, kit: str, group: str) -> None:
        name, group = kitparse.clean_arg(kit), kitparse.clean_arg(group)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_group_template.format(kit=name, group=group), expect_response=False)
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** to group `{group}`")
        await interaction.followup.send(f"🎒 Kit **{safe_md(name)}** sent to group `{group}`.\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="all", description="Gives a kit to ALL players on the server.")
    @app_commands.describe(kit="Kit name")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_give_all(self, interaction: discord.Interaction, kit: str) -> None:
        name = kitparse.clean_arg(kit)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Really give kit **{safe_md(name)}** to **all {len(self.bot.online_players)} online players**?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Timed out – cancelled.", view=None)
            return
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_all_template.format(kit=name), expect_response=False)
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** to **all players**")
        await interaction.edit_original_response(
            content=f"🎒 Kit **{safe_md(name)}** sent to everyone.\n{self._response_block(response)}")

    # ------------------------------------------------------------ Editing (admins)

    @kit.command(name="add-item", description="Adds an item to a kit (creates the kit if needed).")
    @app_commands.describe(kit="Kit name (new name = new kit)", item="Item shortname, e.g. rifle.ak",
                           amount="Amount", condition="Condition in percent", container="Where the item goes")
    @app_commands.choices(container=[app_commands.Choice(name=kitparse.CONTAINER_LABELS[c.lower()], value=c)
                                 for c in kitparse.CONTAINERS])
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_add_item(self, interaction: discord.Interaction, kit: str, item: str,
                           amount: app_commands.Range[int, 1, 100000] = 1,
                           condition: app_commands.Range[int, 1, 100] = 100,
                           container: app_commands.Choice[str] | None = None) -> None:
        name, short = kitparse.clean_arg(kit), kitparse.clean_arg(item)
        slot = container.value if container else "Main"
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_add_template.format(kit=name, item=short, amount=amount, condition=condition,
                                                    container=slot),
            expect_response=False)
        self.kit_names.invalidate()
        self._audit(f"🛠️ {self._who(interaction)}: Kit **{safe_md(name)}** + `{short}` × {amount} ({slot})")
        await interaction.followup.send(
            f"➕ Added `{short}` × {amount} to **{safe_md(name)}** (check with `/kit info`).\n"
            f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="remove-item", description="Removes an item from a kit (see /kit info for the ID).")
    @app_commands.describe(kit="Kit name", item_id="Item ID from the /kit info output")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_remove_item(self, interaction: discord.Interaction, kit: str,
                              item_id: app_commands.Range[int, 0, 1_000_000]) -> None:
        name = kitparse.clean_arg(kit)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_remove_template.format(kit=name, id=item_id), expect_response=False)
        self._audit(f"🛠️ {self._who(interaction)}: Kit **{safe_md(name)}** – removed item ID {item_id}")
        await interaction.followup.send(f"➖ Removed item {item_id} from **{safe_md(name)}**.\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    # ------------------------------------------------------------ Autokit rules (admins)

    @kit.command(name="autokit-add", description="Sets up an autokit (respawn or quick chat phrase).")
    @app_commands.describe(
        kit="Kit name",
        trigger="When the kit is given",
        kit_type="In-game kit (default) or custom kit from /customkit",
        phrase="Quick chat only: which phrase triggers the kit",
        cooldown_minutes="Wait time per player until the next claim (0 = none)",
        max_per_player="Max. claims per player until reset (0 = unlimited)",
        required_role="Only players whose linked Discord account has this role get the kit",
    )
    @app_commands.choices(trigger=[app_commands.Choice(name=v, value=k) for k, v in TRIGGER_LABELS.items()],
                          kit_type=[app_commands.Choice(name=v, value=k) for k, v in KIT_TYPE_LABELS.items()])
    @app_commands.autocomplete(kit=_rule_kit_ac, phrase=_phrase_ac)
    @admin_only()
    async def autokit_add(self, interaction: discord.Interaction, kit: str, trigger: app_commands.Choice[str],
                          phrase: str | None = None,
                          cooldown_minutes: app_commands.Range[int, 0, 525600] = 0,
                          max_per_player: app_commands.Range[int, 0, 10000] = 0,
                          kit_type: app_commands.Choice[str] | None = None,
                          required_role: discord.Role | None = None) -> None:
        name = kitparse.clean_arg(kit)
        trigger_key = trigger.value
        if kit_type is not None:
            ktype = kit_type.value
        else:
            # No type chosen: custom kit if it only exists as a custom kit
            ingame_names = {k.lower() for k in await self.kit_names.get(max_wait=3)}
            is_custom = self.bot.db.custom_kit(name) is not None and name.lower() not in ingame_names
            ktype = "custom" if is_custom else "ingame"
        if ktype == "custom":
            custom = self.bot.db.custom_kit(name)
            if custom is None:
                await interaction.response.send_message(
                    f"Custom kit **{safe_md(name)}** does not exist (`/customkit list`).", ephemeral=True)
                return
            name = custom["name"]
        notes = []
        if trigger_key == "quickchat":
            if not phrase:
                await interaction.response.send_message(
                    "Please pick a `phrase` for the quick chat trigger.", ephemeral=True)
                return
            phrase = phrase.strip()
            if tp.is_location(phrase) or tp.find(phrase):
                await interaction.response.send_message(
                    "📍 Location emotes are teleports only – set them with `/teleport set` or `/teleport here`.",
                    ephemeral=True)
                return
            if phrase not in self.phrases:
                notes.append("⚠️ Unknown phrase – it must appear exactly like this in the console log.")
            clash = [r for r in self.bot.db.kit_rules(trigger="quickchat") if r["phrase"] == phrase]
            if clash:
                notes.append(f"ℹ️ This phrase already triggers rule #{clash[0]['id']} – both kits will be given.")
        else:
            phrase = None
            if cooldown_minutes == 0 and max_per_player == 0:
                notes.append("ℹ️ Without a cooldown the kit is given on **every** respawn.")

        if ktype == "ingame":
            known = await self.kit_names.get(max_wait=3)
            if known and name.lower() not in (k.lower() for k in known):
                notes.append(f"⚠️ Kit **{safe_md(name)}** was not found in `kit list`.")
        elif not self.bot.db.custom_kit_items(name):
            notes.append("⚠️ The custom kit is still empty.")

        if required_role is not None:
            notes.append(f"🎖️ Only players with {required_role.mention} get this kit. They link their in-game "
                         "player once with `/customkit link`.")
        rule_id = self.bot.db.add_kit_rule(name, trigger_key, phrase, cooldown_minutes, max_per_player,
                                           str(interaction.user), ktype,
                                           required_role.id if required_role else None)
        rule = self.bot.db.kit_rule(rule_id)
        self._audit(f"🤖 {self._who(interaction)}: Autokit created – {self._rule_text(rule)}")
        text = f"✅ Autokit created:\n{self._rule_text(rule)}"
        if notes:
            text += "\n\n" + "\n".join(notes)
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ Emote binds (quick chat -> kit, one step)

    async def _emote_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        bound = {}
        for r in self.bot.db.kit_rules(trigger="quickchat"):
            bound.setdefault(r["phrase"], r["kit"])
        q = current.lower().strip()
        out = []
        for e in emote_binds.emotes():
            if q and q not in e["label"].lower():
                continue
            name = e["label"] + (f"  → {bound[e['raw']]}" if e["raw"] in bound else "")
            out.append(app_commands.Choice(name=name[:100], value=e["raw"][:100]))
        return out[:25]

    async def _bound_emote_ac(self, interaction: discord.Interaction, current: str
                              ) -> list[app_commands.Choice[str]]:
        q = current.lower().strip()
        return [app_commands.Choice(name=f"{b['label']}  → {b['kit']}"[:100], value=b["raw"][:100])
                for b in emote_binds.binds(self.bot.db) if not q or q in (b["label"] + b["kit"]).lower()][:25]

    async def _kit_type_for(self, name: str) -> tuple[str, str]:
        """(kit type, exact name): a custom kit if it only exists as one, else the in-game kit."""
        custom = self.bot.db.custom_kit(name)
        ingame = {k.lower() for k in await self.kit_names.get(max_wait=3)}
        if custom is not None and name.lower() not in ingame:
            return "custom", custom["name"]
        return "ingame", name

    def kit_own_role(self, guild, kit: str, kit_type: str = "ingame"):
        """The kit's own Discord role: the role linked to it (Kit roles / custom kit claim role), else a role named
        like the kit ("m4 spawn kit" -> "M4 Spawn"). None if there is none."""
        from support.kit_role_match import name_match
        if guild is None:
            return None
        ids: list[int] = []
        if kit_type == "custom":
            row = self.bot.db.custom_kit(kit)
            ids += self.bot.db.claim_roles(row) if row is not None else []
        linked = self.bot.db.ingame_kit_role(kit) or next(
            (r for r in self.bot.db.ingame_kit_roles() if r["kit"].lower() == kit.lower()), None)
        if linked is not None:
            ids.append(linked["role_id"])
        role = next((guild.get_role(i) for i in ids if guild.get_role(i)), None)
        if role is not None:
            return role
        candidates = [(r.id, r.name) for r in guild.roles if not r.is_default() and not getattr(r, "managed", False)]
        match = name_match(kit, candidates)
        return guild.get_role(match) if match else None

    def clear_bind_roles(self, *, emotes_only: bool = True, by: str = "") -> int:
        """Removes the required role from every emote bind (or every autokit): everyone can use them again."""
        rules = self.bot.db.kit_rules(trigger="quickchat") if emotes_only else self.bot.db.kit_rules()
        changed = [r for r in rules if r["required_role_id"]]
        for r in changed:
            self.bot.db.set_kit_rule_role(r["id"], None)
        if changed:
            self._audit(f"🎖️ {by or 'Bot'}: required role removed from {len(changed)} autokit(s) – everyone can use "
                        "them again")
        return len(changed)

    def roles_to_binds(self, *, emotes_only: bool = True, only_missing: bool = False, by: str = "") -> dict:
        """Every emote bind (or every autokit) gets its kit's own Discord role as the required role – players then
        only get the kit from the emote if their linked Discord account has that kit's role."""
        guild = main_guild(self.bot)
        if guild is None:
            return {"ok": False, "error": "The bot isn't on a Discord server."}
        rules = self.bot.db.kit_rules(trigger="quickchat") if emotes_only else self.bot.db.kit_rules()
        assigned, unchanged, missing = [], [], []
        for rule in rules:
            if only_missing and rule["required_role_id"]:
                continue
            role = self.kit_own_role(guild, rule["kit"], rule["kit_type"])
            if role is None:
                missing.append(rule["kit"])
                continue
            if rule["required_role_id"] == role.id:
                unchanged.append(rule["kit"])
                continue
            self.bot.db.set_kit_rule_role(rule["id"], role.id)
            assigned.append({"rule_id": rule["id"], "kit": rule["kit"], "role": role.name, "role_id": str(role.id)})
        if assigned:
            self._audit(f"🎖️ {by or 'Bot'}: kit roles set on {len(assigned)} autokit(s): "
                        + ", ".join(f"{safe_md(a['kit'])} → {safe_md(a['role'])}" for a in assigned[:25]))
        return {"ok": True, "assigned": assigned, "already_set": unchanged, "no_role_found": sorted(set(missing))}

    @kit.command(name="bind-roles", description="Each emote kit only for its own Discord role (e.g. Ak Pvp → @Ak Pvp).")
    @app_commands.describe(only_missing="Only binds that have no required role yet (default: all)",
                           all_autokits="Also respawn autokits, not just emote binds",
                           remove="Undo: remove the required role from the binds – everyone can use them again")
    @admin_only()
    async def kit_bind_roles(self, interaction: discord.Interaction, only_missing: bool = False,
                             all_autokits: bool = False, remove: bool = False) -> None:
        if remove:
            count = self.clear_bind_roles(emotes_only=not all_autokits, by=self._who(interaction))
            await interaction.response.send_message(f"✅ Role removed from {count} bind(s) – everyone can use them "
                                                    "again.", ephemeral=True)
            return
        result = self.roles_to_binds(emotes_only=not all_autokits, only_missing=only_missing,
                                     by=self._who(interaction))
        if not result["ok"]:
            await interaction.response.send_message(result["error"], ephemeral=True)
            return
        lines = [f"🎖️ **{safe_md(a['kit'])}** → <@&{a['role_id']}>" for a in result["assigned"]]
        text = (f"✅ {len(result['assigned'])} bind(s) now need their kit's role." if lines
                else "Nothing to change.") + ("\n" + "\n".join(lines) if lines else "")
        if result["already_set"]:
            text += f"\nAlready set: {len(result['already_set'])}"
        if result["no_role_found"]:
            text += ("\n⚠️ No role found for: " + ", ".join(f"**{safe_md(k)}**" for k in result["no_role_found"])
                     + " – link one under Kits → Kit roles, or set it with `/kit autokit-role`.")
        await interaction.response.send_message(text[:1990], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @kit.command(name="why", description="Why don't emote kits work? Checks every bind and shows recent tries.")
    @app_commands.describe(player="Only this player's tries (and their link/roles)")
    @app_commands.autocomplete(player=_player_ac)
    @admin_only()
    async def kit_why(self, interaction: discord.Interaction, player: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        d = await self.diagnose(player)
        lines = [f"RCON: {'✅ connected' if d['rcon_connected'] else '❌ not connected'} · kits in kit list: "
                 f"{d['kits_in_kit_list']}"]
        if "player" in d:
            info = d["player"]
            lines.append(f"👤 **{safe_md(info['player'])}**: " + (f"linked to <@{info['linked_discord_user']}>"
                         if info["linked_discord_user"] else "❌ not linked (`/customkit link`)"))
        for b in d["binds_with_notes"][:15]:
            lines.append(f"• #{b['rule_id']} **{safe_md(b['kit'])}** ({safe_md(b['emote'])}): "
                         + "; ".join(safe_md(i) for i in b["issues"]))
        if d["recent_tries"]:
            lines.append("**Recent tries:**")
            icons = {"given": "✅", "no_role": "🎖️", "cooldown": "⏱️", "limit": "🔢", "failed": "⚠️",
                     "kit_not_found": "❓", "disabled": "⏸️"}
            for t in d["recent_tries"][-12:]:
                lines.append(f"{icons.get(t['outcome'], '•')} {t['at']} {safe_md(t['player'])} → "
                             f"{safe_md(t['kit'])}: {t['outcome']}" + (f" ({safe_md(t['detail'])})" if t["detail"] else ""))
        elif d.get("note"):
            lines.append(f"ℹ️ {d['note']}")
        await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @kit.command(name="bind", description="Binds a kit to an emote: players get it when they use that emote in game.")
    @app_commands.describe(kit="In-game kit (or 🔒 custom kit)", emote="The quick chat emote/phrase – type to search",
                           cooldown_minutes="Wait time per player until the next claim (0 = none)",
                           max_per_player="Max. claims per player until the wipe reset (0 = unlimited)",
                           required_role="Only players whose linked Discord account has this role")
    @app_commands.autocomplete(kit=_rule_kit_ac, emote=_emote_ac)
    @admin_only()
    async def kit_bind(self, interaction: discord.Interaction, kit: str, emote: str,
                       cooldown_minutes: app_commands.Range[int, 0, 525600] = 0,
                       max_per_player: app_commands.Range[int, 0, 10000] = 0,
                       required_role: discord.Role | None = None) -> None:
        raw, suggestions = emote_binds.find(emote)
        if raw is None:
            hint = ("Did you mean: " + ", ".join(f"**{safe_md(x)}**" for x in suggestions)) if suggestions \
                else "Pick one from the list while typing."
            await interaction.response.send_message(f"Unknown emote **{safe_md(emote[:60])}**. {hint}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        kit_type, name = await self._kit_type_for(kitparse.clean_arg(kit))
        notes = []
        if kit_type == "ingame":
            known = await self.kit_names.get(max_wait=3)
            if known and name.lower() not in (k.lower() for k in known):
                notes.append(f"⚠️ Kit **{safe_md(name)}** was not found in `kit list`.")
        rule_id, replaced = emote_binds.bind(self.bot.db, raw, name, kit_type=kit_type,
                                             cooldown_minutes=cooldown_minutes, max_claims=max_per_player,
                                             required_role_id=required_role.id if required_role else None,
                                             by=str(interaction.user))
        rule = self.bot.db.kit_rule(rule_id)
        if replaced:
            notes.append("♻️ Replaced: " + ", ".join(f"**{safe_md(k)}**" for k in replaced))
        if not cooldown_minutes and not max_per_player:
            notes.append("ℹ️ No cooldown – players can use the emote again and again. Add `cooldown_minutes` to "
                         "limit it.")
        self._audit(f"🎭 {self._who(interaction)}: Emote **{safe_md(emote_binds.label(raw))}** → kit "
                    f"**{safe_md(name)}** (rule #{rule_id})")
        await interaction.followup.send(
            f"🎭 **{safe_md(emote_binds.label(raw))}** now gives {'🔒 ' if kit_type == 'custom' else ''}"
            f"**{safe_md(name)}**.\n{self._rule_text(rule)}" + ("\n\n" + "\n".join(notes) if notes else ""),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @kit.command(name="unbind", description="Removes the kit from an emote.")
    @app_commands.describe(emote="A bound emote")
    @app_commands.autocomplete(emote=_bound_emote_ac)
    @admin_only()
    async def kit_unbind(self, interaction: discord.Interaction, emote: str) -> None:
        raw, _ = emote_binds.find(emote)
        removed = emote_binds.unbind(self.bot.db, raw) if raw else []
        if not removed:
            await interaction.response.send_message("That emote has no kit bound.", ephemeral=True)
            return
        self._audit(f"🎭 {self._who(interaction)}: Emote **{safe_md(emote_binds.label(raw))}** unbound "
                    f"({', '.join(safe_md(k) for k in removed)})")
        await interaction.response.send_message(
            f"🗑️ **{safe_md(emote_binds.label(raw))}** no longer gives "
            + ", ".join(f"**{safe_md(k)}**" for k in removed) + ".", ephemeral=True)

    @kit.command(name="binds", description="Shows which emote gives which kit.")
    async def kit_binds(self, interaction: discord.Interaction) -> None:
        from cogs.binds_panel import binds_embed
        if not emote_binds.binds(self.bot.db):
            await interaction.response.send_message(
                "No emote binds yet. Admins: `/kit bind kit:<kit> emote:<emote>`.", ephemeral=True)
            return
        await interaction.response.send_message(embed=binds_embed(self.bot.db),
                                                allowed_mentions=discord.AllowedMentions.none())

    @kit.command(name="binds-panel", description="Posts a live list of all emote binds that updates by itself.")
    @app_commands.describe(channel="Where to post it (default: here)")
    @admin_only()
    async def kit_binds_panel(self, interaction: discord.Interaction,
                              channel: discord.TextChannel | None = None) -> None:
        panel = self.bot.get_cog("BindsPanel")
        target = channel or interaction.channel
        if panel is None or target is None:
            await interaction.response.send_message("The binds panel module is not loaded.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        msg = await panel.post(target)
        self._audit(f"🎭 {self._who(interaction)}: live emote binds panel posted in {target.mention}")
        await interaction.followup.send(f"✅ Live panel posted: {msg.jump_url}\nIt updates by itself when binds "
                                        "change. Delete the message to stop it.", ephemeral=True)

    @kit.command(name="autokit-role", description="Sets the Discord role required for an autokit (e.g. spawn kit).")
    @app_commands.describe(rule="Autokit rule", role="Required role (empty = everyone gets the kit again)")
    @app_commands.autocomplete(rule=_rule_ac)
    @admin_only()
    async def autokit_role(self, interaction: discord.Interaction, rule: int,
                           role: discord.Role | None = None) -> None:
        if not self.bot.db.set_kit_rule_role(rule, role.id if role else None):
            await interaction.response.send_message(f"Rule #{rule} does not exist.", ephemeral=True)
            return
        row = self.bot.db.kit_rule(rule)
        self._audit(f"🎖️ {self._who(interaction)}: Autokit #{rule} – required role "
                    f"{role.mention if role else 'removed'}")
        hint = ("\nPlayers get it only if their in-game player is linked (`/customkit link`) to a Discord "
                "account with this role." if role else "")
        await interaction.response.send_message(f"✅ {self._rule_text(row)}{hint}", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @kit.command(name="autokit-toggle", description="Pauses or enables an autokit.")
    @app_commands.describe(rule="Autokit rule", enabled="On or off")
    @app_commands.autocomplete(rule=_rule_ac)
    @admin_only()
    async def autokit_toggle(self, interaction: discord.Interaction, rule: int, enabled: bool) -> None:
        if not self.bot.db.set_kit_rule_enabled(rule, enabled):
            await interaction.response.send_message(f"Rule #{rule} does not exist.", ephemeral=True)
            return
        state = "enabled" if enabled else "paused"
        self._audit(f"🤖 {self._who(interaction)}: Autokit #{rule} {state}")
        await interaction.response.send_message(f"Autokit #{rule} {state}.", ephemeral=True)

    @kit.command(name="autokit-delete", description="Deletes an autokit.")
    @app_commands.describe(rule="Autokit rule")
    @app_commands.autocomplete(rule=_rule_ac)
    @admin_only()
    async def autokit_delete(self, interaction: discord.Interaction, rule: int) -> None:
        row = self.bot.db.kit_rule(rule)
        if row is None or not self.bot.db.delete_kit_rule(rule):
            await interaction.response.send_message(f"Rule #{rule} does not exist.", ephemeral=True)
            return
        self._audit(f"🗑️ {self._who(interaction)}: Autokit deleted – {self._rule_text(row)}")
        await interaction.response.send_message(f"🗑️ Autokit #{rule} deleted.", ephemeral=True)

    @kit.command(name="wipe-reset", description="Resets autokit cooldowns/limits (e.g. after a wipe).")
    @app_commands.describe(rule="Only reset this rule (empty = all)")
    @app_commands.autocomplete(rule=_rule_ac)
    @admin_only()
    async def autokit_reset(self, interaction: discord.Interaction, rule: int | None = None) -> None:
        count = self.bot.db.reset_kit_claims(rule)
        scope = f"rule #{rule}" if rule is not None else "all autokits"
        self._audit(f"♻️ {self._who(interaction)}: Autokit history reset ({scope}, {count} entries)")
        await interaction.response.send_message(f"♻️ Reset {count} claims for {scope}.", ephemeral=True)

    @kit.command(name="history", description="Shows the latest kit claims.")
    @app_commands.describe(player="Only claims by this player")
    @app_commands.autocomplete(player=_player_ac)
    @admin_only()
    async def kit_history(self, interaction: discord.Interaction, player: str | None = None) -> None:
        rows = self.bot.db.kit_claim_history(player.strip() if player else None, limit=20)
        if not rows:
            await interaction.response.send_message("No kit claims found.", ephemeral=True)
            return
        lines = []
        for r in rows:
            source = f"Autokit #{r['rule_id']} ({r['source']})" if r["rule_id"] is not None else r["source"]
            lines.append(f"`{r['claimed_at']}` **{safe_md(r['kit'])}** → {safe_md(r['player'])} · {safe_md(source)}")
        embed = discord.Embed(title="📜 Kit history", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Times in UTC")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Autokit logic

    @commands.Cog.listener()
    async def on_rce_respawn(self, ev: lp.RespawnEvent) -> None:
        for rule in self.bot.db.kit_rules(enabled_only=True, trigger="respawn"):
            await self._try_claim(rule, ev.player, "respawn")

    @commands.Cog.listener()
    async def on_rce_chat(self, ev: lp.ChatEvent) -> None:
        if ev.channel == "SERVER":
            return
        if tp.is_location(ev.raw):
            return                       # Location emotes are teleports (cogs/teleports.py), never kits
        for rule in self.bot.db.kit_rules(enabled_only=True, trigger="quickchat"):
            if rule["phrase"] == ev.raw:
                await self._try_claim(rule, ev.player, "quickchat")

    async def player_has_role(self, player: str, role_id: int) -> bool:
        """Does the Discord account linked to this in-game player have the role? (unlinked = no)"""
        user_id = self.bot.db.player_owner(player)
        guild = main_guild(self.bot)
        if user_id is None or guild is None:
            return False
        cached = self._role_cache.get(user_id)
        if cached is None or time.monotonic() - cached[0] > ROLE_CACHE_SECONDS:
            member = guild.get_member(user_id)
            if member is None:
                # Works without the Server Members Intent; cached so respawns don't spam the API
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    member = None  # left the Discord server
                except discord.HTTPException as exc:
                    log.warning("Could not load roles of %s: %s", user_id, exc)
                    return False
            roles = frozenset(r.id for r in member.roles) if member else frozenset()
            cached = (time.monotonic(), roles)
            self._role_cache[user_id] = cached
        return role_id in cached[1]

    def _attempt(self, rule, player: str, source: str, outcome: str, detail: str = "") -> None:
        self.attempts.append({"at": int(time.time()), "player": player, "kit": rule["kit"], "rule_id": rule["id"],
                              "source": source, "outcome": outcome, "detail": detail})

    async def _exact_ingame_name(self, rule) -> str | None:
        """The kit's exact name in `kit list` (the server's kit commands are picky about spelling). A name that
        only differs in upper/lower case is fixed in the rule. None = the server has no such kit."""
        names = await self.kit_names.get(max_wait=2)
        if not names or rule["kit"] in names:
            return rule["kit"]
        exact = next((n for n in names if n.lower() == rule["kit"].lower().strip()), None)
        if exact is not None:
            self.bot.db.set_kit_rule_kit(rule["id"], exact)
            log.info("Autokit #%s: kit name %r corrected to %r", rule["id"], rule["kit"], exact)
        return exact

    async def _try_claim(self, rule, player: str, source: str) -> None:
        kit = rule["kit"]
        if rule["required_role_id"] and not await self.player_has_role(player, rule["required_role_id"]):
            guild = main_guild(self.bot)
            role = guild.get_role(rule["required_role_id"]) if guild else None
            linked = self.bot.db.player_owner(player) is not None
            self._attempt(rule, player, source, "no_role",
                          f"needs role {role.name if role else rule['required_role_id']}"
                          + ("" if linked else " – player is not linked (/customkit link)"))
            await self._announce_denied(
                player, f"Kit <b>{sanitize_ingame_text(kit, 40)}</b> needs the Discord role "
                        f"<b>{sanitize_ingame_text(role.name if role else 'required role', 32)}</b> "
                        + ("." if linked else "(link your account on Discord with /customkit link)."), source)
            return
        count, minutes_since = self.bot.db.kit_claim_state(rule["id"], player)

        if rule["max_claims"] and count >= rule["max_claims"]:
            self._attempt(rule, player, source, "limit", f"{count}/{rule['max_claims']} claims used")
            await self._announce_denied(player, f"Limit reached for kit <b>{sanitize_ingame_text(kit, 40)}</b>.",
                                        source)
            return
        if rule["cooldown_minutes"] and minutes_since is not None and minutes_since < rule["cooldown_minutes"]:
            remaining = max(1, math.ceil(rule["cooldown_minutes"] - minutes_since))
            self._attempt(rule, player, source, "cooldown", f"{remaining} min left")
            await self._announce_denied(
                player, f"Kit <b>{sanitize_ingame_text(kit, 40)}</b> available again in {format_duration(remaining * 60)}.",
                source)
            return

        if rule["kit_type"] == "custom":                       # a switched-off custom kit isn't given automatically
            row = self.bot.db.custom_kit(kit)
            if row is not None and "disabled" in row.keys() and row["disabled"]:
                self._attempt(rule, player, source, "disabled", "custom kit is switched off")
                return
        else:
            exact = await self._exact_ingame_name(rule)
            if exact is None:
                self._attempt(rule, player, source, "kit_not_found", f"no in-game kit '{kit}' in kit list")
                self._audit(f"⚠️ Autokit #{rule['id']}: the server has no kit **{safe_md(kit)}** – "
                            f"{safe_md(player)} got nothing. Fix the bind (`/kit bind`) or the kit name.")
                return
            kit = exact
        # Record first, then send: prevents double claims from log lines in quick succession
        claim_id = self.bot.db.record_kit_claim(rule["id"], kit, player, source)
        if source == "respawn" and self.bot.config.autokit_delay:
            await asyncio.sleep(self.bot.config.autokit_delay)  # let the player finish spawning first
        try:
            if rule["kit_type"] == "custom":
                custom = self.bot.get_cog("CustomKits")
                if custom is None:
                    raise RconError("Custom kit module is not loaded.")
                sent, total = await custom.deliver(kit, player)
                if total == 0:
                    raise RconError("Custom kit is empty or was deleted.")
            else:
                await self.bot.rcon.command(self.bot.config.kit_give_template.format(kit=kit, name=player),
                                            expect_response=False)
        except RconError as exc:
            self.bot.db.delete_kit_claim(claim_id)
            self._attempt(rule, player, source, "failed", str(exc)[:120])
            self._audit(f"⚠️ Autokit **{safe_md(kit)}** for **{safe_md(player)}** failed: {exc}")
            return

        self._attempt(rule, player, source, "given")
        self._audit(f"🤖 Autokit **{safe_md(kit)}** to **{safe_md(player)}** "
                    f"({TRIGGER_LABELS.get(source, source)}, rule #{rule['id']})")
        if source == "quickchat" and self.bot.config.kit_claim_announce:
            await self._say(f"<color=#7CFC00>{sanitize_ingame_text(player, 32)}</color> received the kit "
                            f"<b>{sanitize_ingame_text(kit, 40)}</b>.")

    async def diagnose(self, player: str | None = None) -> dict:
        """Why autokits/emote binds may not work: problems per bind, the player's link and roles, recent tries."""
        db, guild = self.bot.db, main_guild(self.bot)
        names = await self.kit_names.get(max_wait=5, force=True)
        problems = []
        for r in db.kit_rules():
            issues = []
            if not r["enabled"]:
                issues.append("paused")
            if r["kit_type"] == "ingame" and names and r["kit"] not in names:
                close = next((n for n in names if n.lower() == r["kit"].lower()), None)
                issues.append(f"kit name differs in case from '{close}' (fixed on the next claim)" if close
                              else "no in-game kit with this name in kit list")
            if r["kit_type"] == "custom":
                row = db.custom_kit(r["kit"])
                if row is None:
                    issues.append("custom kit doesn't exist")
                elif not db.custom_kit_items(row["name"]):
                    issues.append("custom kit is empty")
                elif "disabled" in row.keys() and row["disabled"]:
                    issues.append("custom kit is switched off")
            if r["required_role_id"]:
                role = guild.get_role(r["required_role_id"]) if guild else None
                if role is None:
                    issues.append("its required role no longer exists – nobody can get it")
                else:
                    issues.append(f"only linked players with the role {role.name} "
                                  f"({len(getattr(role, 'members', []) or [])} member(s) have it)")
            if issues:
                problems.append({"rule_id": r["id"], "kit": r["kit"],
                                 "emote": emote_binds.label(r["phrase"]) if r["trigger"] == "quickchat" else "respawn",
                                 "issues": issues})
        out = {"rcon_connected": self.bot.rcon.connected, "kits_in_kit_list": len(names),
               "in_game_messages": bool(self.bot.config.kit_claim_announce), "binds_with_notes": problems}
        tries = list(self.attempts)
        if player:
            p = player.strip().lower()
            tries = [t for t in tries if t["player"].lower() == p]
            owner = db.player_owner(player)
            info: dict = {"player": player, "linked_discord_user": str(owner) if owner else None}
            if owner and guild is not None:
                member = guild.get_member(owner)
                info["discord_roles"] = [r.name for r in getattr(member, "roles", [])
                                         if not r.is_default()] if member else "not on the Discord server"
            out["player"] = info
        out["recent_tries"] = [{**t, "at": f"<t:{t['at']}:R>"} for t in tries[-25:]]
        if not tries:
            out["note"] = ("No emote/respawn kit tries since the bot started" + (f" from {player}" if player else "")
                           + " – if players used the emotes, the bot didn't see them in the server log.")
        return out

    async def _announce_denied(self, player: str, text: str, source: str) -> None:
        # Only for quick chat (the player actively asked) and throttled – otherwise chat spam
        if source != "quickchat" or not self.bot.config.kit_claim_announce:
            return
        now = time.monotonic()
        if now - self._announced.get(player.lower(), 0) < ANNOUNCE_THROTTLE_SECONDS:
            return
        self._announced[player.lower()] = now
        await self._say(f"<color=orange>{sanitize_ingame_text(player, 32)}</color>: {text}")


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Kits(bot))
