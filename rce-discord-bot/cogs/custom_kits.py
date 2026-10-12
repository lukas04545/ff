"""Custom kits: kits that exist only in the bot.

They are NOT created in the server's kit system but given item by item via
"inventory.giveto". This makes them invisible in the in-game kit manager, where
they can't be viewed, claimed or edited.

Limitation: "inventory.giveto" supports neither condition nor slot (hotbar/clothing) –
the items go into the player's inventory (if it is full, they may drop on the ground).

Self-service: admins pick roles per kit (/customkit access). Members first link their Discord
account to their in-game player (/customkit link) – verified by sending two requested quick chat
phrases in-game, so nobody can claim for someone else's account. Members with one of the roles then
claim the kit with /customkit claim; it always goes to their own linked player. The cooldown
(/customkit cooldown) is per kit and per player.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.kits import ConfirmView
from rce import items as itemlist
from rce import kits as kitparse
from rce import quickchat
from rce.rcon_client import RconError, RconNotConnected
from utils import (admin_only, format_minutes, parse_duration, player_choices, safe_md,
                   sanitize_player_name)

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.customkits")

MAX_ITEMS_PER_KIT = 60
MAX_IMPORT_BYTES = 200_000
MAX_COOLDOWN_MINUTES = 365 * 1440
VERIFY_SECONDS = 300                       # time to send the verification quick chats in-game
VERIFY_SKIP_CATEGORIES = ("_responses_",)  # "Yes", "Hello", ... are sent too often to prove anything


@dataclass
class PendingLink:
    user_id: int
    phrases: list[str]
    interaction: discord.Interaction
    expires: float
    step: int = 0


def _category(raw: str) -> str:
    """'d11_quick_chat_orders_slot_6' -> 'Orders' (menu where players find the phrase)."""
    return raw.removeprefix("d11_quick_chat_").rsplit("_slot_", 1)[0].replace("_", " ").title()


def import_kit_commands(db, text: str, *, kit_name: str | None, replace: bool, created_by: str) -> dict:
    """Creates/fills custom kits from kit builder output ('kit add "Kit" "item" amount ...').

    kit_name: put all items into this kit instead of the names in the commands.
    replace:  existing items of the kit are removed first (the builder output is the whole kit).
    Returns a summary: {"kits": [{name, created, replaced, added, skipped}], "errors": [...]}.
    """
    lines, errors = kitparse.parse_kit_add_commands(text)
    override = kitparse.clean_arg(kit_name or "", limit=40)
    by_kit: dict[str, list] = {}
    for line in lines:
        name = override or line.kit
        key = next((k for k in by_kit if k.lower() == name.lower()), name)  # kit names are case-insensitive
        by_kit.setdefault(key, []).append(line)

    kits = []
    for name, items in by_kit.items():
        created = db.create_custom_kit(name, None, created_by)
        name = db.custom_kit(name)["name"]
        replaced = db.clear_custom_kit_items(name) if replace and not created else 0
        room = MAX_ITEMS_PER_KIT - len(db.custom_kit_items(name))
        for line in items[:max(room, 0)]:
            db.add_custom_kit_item(name, line.item, line.amount)
        added = min(len(items), max(room, 0))
        unknown = sorted({i.item for i in items[:added] if i.item not in itemlist.COMMON_ITEMS})
        kits.append({"name": name, "created": created, "replaced": replaced, "added": added,
                     "skipped": len(items) - added, "unknown": unknown})
    return {"kits": kits, "errors": errors}


def import_summary(result: dict) -> str:
    """Discord text for the result of import_kit_commands()."""
    if not result["kits"]:
        return ("⚠️ No `kit add` commands found. Expected lines like\n"
                '`kit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main`')
    lines = []
    for k in result["kits"]:
        status = "new kit" if k["created"] else (f"replaced {k['replaced']} old items" if k["replaced"]
                                                 else "added to the existing kit")
        lines.append(f"📥 **{safe_md(k['name'])}**: {k['added']} items ({status})")
        if k["skipped"]:
            lines.append(f"   ⚠️ {k['skipped']} items skipped – max. {MAX_ITEMS_PER_KIT} items per kit")
        if k["unknown"]:
            lines.append("   ℹ️ Unknown shortnames (check by testing): "
                         + ", ".join(f"`{u}`" for u in k["unknown"][:15]))
    if result["errors"]:
        lines.append(f"⚠️ {len(result['errors'])} commands could not be read, e.g. `{result['errors'][0][:100]}`")
    lines.append("Container and condition from the builder are ignored – custom kit items go into the inventory.")
    return "\n".join(lines)[:1990]


class ImportModal(discord.ui.Modal, title="Import from kit builder"):
    commands_text = discord.ui.TextInput(
        label="Kit builder commands", style=discord.TextStyle.paragraph, max_length=4000,
        placeholder='kit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main\n'
                    'kit add "Custom kit 1" "rifle.ak" 1 100 belt')

    def __init__(self, cog: "CustomKits", kit_name: str | None, replace: bool) -> None:
        super().__init__()
        self.cog, self.kit_name, self.replace = cog, kit_name, replace

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.run_import(interaction, str(self.commands_text.value), self.kit_name, self.replace)


class CustomKits(commands.Cog):
    customkit = app_commands.Group(
        name="customkit",
        description="Bot-only kits (invisible in the in-game kit manager)",
        guild_only=True,
    )

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._claim_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, PendingLink] = {}  # player (lowercase) -> open ownership check

    # ------------------------------------------------------------ Delivery (also used by autokits)

    async def deliver(self, kit: str, player: str) -> tuple[int, int]:
        """Gives all items of a custom kit to a player. Returns: (sent, total).

        Raises RconNotConnected if nothing at all could be sent.
        """
        items = self.bot.db.custom_kit_items(kit)
        if not items:
            return 0, 0
        template = self.bot.config.item_give_template
        # Send all commands at once: the server processes them in order, but the bot
        # doesn't have to wait for a (possibly missing) response for each item.
        results = await asyncio.gather(
            *(self.bot.rcon.command(template.format(name=player, item=i["shortname"], amount=i["amount"]),
                                    expect_response=False) for i in items),
            return_exceptions=True,
        )
        sent = sum(1 for r in results if not isinstance(r, BaseException))
        if sent == 0:
            first = next((r for r in results if isinstance(r, RconError)), None)
            raise first or RconNotConnected("No items sent.")
        return sent, len(items)

    # ------------------------------------------------------------ Helpers

    def _audit(self, interaction: discord.Interaction, text: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"🔒 {interaction.user.mention} ({safe_md(str(interaction.user))}): {text}")

    def _resolve(self, kit: str):
        return self.bot.db.custom_kit(kitparse.clean_arg(kit))

    async def _missing(self, interaction: discord.Interaction, kit: str) -> None:
        await interaction.response.send_message(
            f"Custom kit **{safe_md(kit)}** does not exist. Overview: `/customkit list`", ephemeral=True)

    def can_claim(self, member: discord.abc.User, row) -> bool:
        """May this member claim the kit themselves? (has one of the kit's claim roles)"""
        allowed = set(self.bot.db.claim_roles(row))
        return bool(allowed) and any(r.id in allowed for r in getattr(member, "roles", []))

    def _access_text(self, guild: discord.Guild | None, row) -> str:
        roles = self.bot.db.claim_roles(row)
        if not roles:
            return "admins only"
        role_obj = guild.get_role if guild else (lambda _id: None)
        return ", ".join(role_obj(r).mention if role_obj(r) else f"<@&{r}>" for r in roles)

    @staticmethod
    def _delivery_text(sent: int, total: int) -> str:
        if sent == total:
            return f"{total} items sent"
        return f"⚠️ only {sent} of {total} items sent"

    # ------------------------------------------------------------ Autocomplete

    async def _kit_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = [r["name"] for r in self.bot.db.custom_kits()]
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in names if current.lower() in n.lower()][:25]

    async def _claimable_ac(self, interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
        names = [r["name"] for r in self.bot.db.custom_kits() if self.can_claim(interaction.user, r)]
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in names if current.lower() in n.lower()][:25]

    async def _item_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=f"{label} ({short})"[:100], value=short)
                for short, label in itemlist.suggestions(current)]

    async def _kit_item_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        kit = getattr(interaction.namespace, "kit", None)
        if not kit:
            return []
        choices = []
        for row in self.bot.db.custom_kit_items(kitparse.clean_arg(kit)):
            label = f"#{row['id']} {row['shortname']} × {row['amount']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=int(row["id"])))
        return choices[:25]

    async def _player_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    # ------------------------------------------------------------ Manage

    @customkit.command(name="create", description="Creates a new custom kit.")
    @app_commands.describe(name="Kit name", description="Optional note (only visible in Discord)")
    @admin_only()
    async def create(self, interaction: discord.Interaction, name: str, description: str | None = None) -> None:
        kit = kitparse.clean_arg(name, limit=40)
        if not kit:
            await interaction.response.send_message("Please enter a valid name.", ephemeral=True)
            return
        if not self.bot.db.create_custom_kit(kit, description, str(interaction.user)):
            await interaction.response.send_message(f"Custom kit **{safe_md(kit)}** already exists.", ephemeral=True)
            return
        note = ""
        kits_cog = self.bot.get_cog("Kits")
        if kits_cog and kit.lower() in (k.lower() for k in kits_cog.kit_names.items):
            note = "\nℹ️ An in-game kit has the same name – the two stay separate."
        self._audit(interaction, f"Custom kit **{safe_md(kit)}** created")
        await interaction.response.send_message(
            f"✅ Custom kit **{safe_md(kit)}** created. Add items with `/customkit add-item`.{note}",
            ephemeral=True)

    @customkit.command(name="add-item", description="Adds an item to a custom kit.")
    @app_commands.describe(kit="Custom kit", item="Item shortname, e.g. rifle.ak", amount="Amount")
    @app_commands.autocomplete(kit=_kit_ac, item=_item_ac)
    @admin_only()
    async def add_item(self, interaction: discord.Interaction, kit: str, item: str,
                       amount: app_commands.Range[int, 1, 100000] = 1) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        short = itemlist.normalize_shortname(kitparse.clean_arg(item))
        if not short:
            await interaction.response.send_message("Please enter an item shortname.", ephemeral=True)
            return
        if len(self.bot.db.custom_kit_items(row["name"])) >= MAX_ITEMS_PER_KIT:
            await interaction.response.send_message(
                f"A custom kit can hold at most {MAX_ITEMS_PER_KIT} items.", ephemeral=True)
            return
        self.bot.db.add_custom_kit_item(row["name"], short, amount)
        hint = "" if short in itemlist.COMMON_ITEMS else \
            "\nℹ️ Unknown shortname – test it once with `/customkit give` on yourself."
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** + `{short}` × {amount}")
        await interaction.response.send_message(
            f"➕ Added `{short}` × {amount} to **{safe_md(row['name'])}**.{hint}", ephemeral=True)

    @customkit.command(name="import", description="Paste kit builder commands (kit add ...) to create/fill custom kits.")
    @app_commands.describe(kit="Optional: put everything into this kit instead of the names in the commands",
                           replace="Replace the existing items of the kit (default: yes)")
    @admin_only()
    async def import_cmd(self, interaction: discord.Interaction, kit: str | None = None,
                         replace: bool = True) -> None:
        # Opens a text box – Discord slash options can't hold multi-line text
        await interaction.response.send_modal(ImportModal(self, kit, replace))

    @customkit.command(name="import-file", description="Upload a .txt file with kit builder commands (for big kits).")
    @app_commands.describe(file=".txt file with the kit add commands",
                           kit="Optional: put everything into this kit instead of the names in the commands",
                           replace="Replace the existing items of the kit (default: yes)")
    @admin_only()
    async def import_file(self, interaction: discord.Interaction, file: discord.Attachment,
                          kit: str | None = None, replace: bool = True) -> None:
        if file.size > MAX_IMPORT_BYTES:
            await interaction.response.send_message("The file is too big (max. 200 KB).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            await interaction.followup.send("Please upload a plain text file (.txt).", ephemeral=True)
            return
        await self.run_import(interaction, text, kit, replace)

    async def run_import(self, interaction: discord.Interaction, text: str, kit: str | None, replace: bool) -> None:
        result = import_kit_commands(self.bot.db, text, kit_name=kit, replace=replace,
                                     created_by=str(interaction.user))
        for k in result["kits"]:
            self._audit(interaction, f"Custom kit **{safe_md(k['name'])}** imported from kit builder "
                                     f"({k['added']} items{', replaced' if k['replaced'] else ''})")
        text = import_summary(result)
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @customkit.command(name="remove-item", description="Removes an item from a custom kit.")
    @app_commands.describe(kit="Custom kit", item="Item in the kit")
    @app_commands.autocomplete(kit=_kit_ac, item=_kit_item_ac)
    @admin_only()
    async def remove_item(self, interaction: discord.Interaction, kit: str, item: int) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        if not self.bot.db.remove_custom_kit_item(row["name"], item):
            await interaction.response.send_message(f"Item #{item} is not in this kit.", ephemeral=True)
            return
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** – removed item #{item}")
        await interaction.response.send_message(f"➖ Removed item #{item}.", ephemeral=True)

    @customkit.command(name="delete", description="Deletes a custom kit along with its autokit rules.")
    @app_commands.describe(kit="Custom kit")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def delete(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        view = ConfirmView(interaction.user.id)
        view.confirm.label = "Yes, delete"
        await interaction.response.send_message(
            f"Really delete custom kit **{safe_md(row['name'])}**? Its autokits will be deleted too.",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Timed out – cancelled.", view=None)
            return
        rules = self.bot.db.delete_custom_kit(row["name"])
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** deleted ({rules} autokit rules)")
        await interaction.edit_original_response(
            content=f"🗑️ Custom kit **{safe_md(row['name'])}** deleted ({rules} autokit rules removed).")

    # ------------------------------------------------------------ Display

    @customkit.command(name="list", description="Shows all custom kits.")
    @admin_only()
    async def list_kits(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.custom_kits()
        if not rows:
            await interaction.response.send_message("No custom kits yet. Create one with `/customkit create`.",
                                                    ephemeral=True)
            return
        auto = {r["kit"].lower() for r in self.bot.db.kit_rules(enabled_only=True) if r["kit_type"] == "custom"}
        lines = [f"• **{safe_md(r['name'])}** – {r['item_count']} items"
                 + (" · 🤖 Autokit" if r["name"].lower() in auto else "")
                 + (f" · 🙋 claim: {self._access_text(interaction.guild, r)}, "
                    f"every {format_minutes(r['cooldown_minutes'])}" if self.bot.db.claim_roles(r) else "")
                 + (f"\n  _{safe_md(r['description'])}_" if r["description"] else "")
                 for r in rows]
        embed = discord.Embed(title=f"🔒 Custom kits ({len(rows)})", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_purple())
        embed.set_footer(text="Stored only in the bot – invisible in the in-game kit manager")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customkit.command(name="info", description="Shows the contents of a custom kit.")
    @app_commands.describe(kit="Custom kit")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def info(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        items = self.bot.db.custom_kit_items(row["name"])
        lines = [f"`#{i['id']}` `{i['shortname']}` × {i['amount']}"
                 + (f" – {itemlist.COMMON_ITEMS[i['shortname']]}" if i["shortname"] in itemlist.COMMON_ITEMS else "")
                 for i in items]
        embed = discord.Embed(title=f"🔒 Custom kit: {row['name']}",
                              description="\n".join(lines)[:4000] or "_Still empty._",
                              colour=discord.Colour.dark_purple())
        if row["description"]:
            embed.add_field(name="Note", value=row["description"][:1024], inline=False)
        embed.add_field(name="🙋 Who can claim", value=self._access_text(interaction.guild, row)[:1024])
        embed.add_field(name="⏱️ Claim cooldown", value=format_minutes(row["cooldown_minutes"]))
        rules = [r for r in self.bot.db.kit_rules()
                 if r["kit_type"] == "custom" and r["kit"].lower() == row["name"].lower()]
        if rules:
            embed.add_field(name="🤖 Autokit rules", value=", ".join(f"#{r['id']}" for r in rules), inline=False)
        embed.set_footer(text=f"Created by {row['created_by']} · {row['created_at']} UTC")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Give

    @customkit.command(name="give", description="Gives a custom kit to a player.")
    @app_commands.describe(kit="Custom kit", player="In-game name")
    @app_commands.autocomplete(kit=_kit_ac, player=_player_ac)
    @admin_only()
    async def give(self, interaction: discord.Interaction, kit: str, player: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        player = sanitize_player_name(player)
        await interaction.response.defer(ephemeral=True)
        sent, total = await self.deliver(row["name"], player)
        if total == 0:
            await interaction.followup.send("The kit is empty.", ephemeral=True)
            return
        self.bot.db.record_kit_claim(None, row["name"], player, f"discord:{interaction.user} (custom)")
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** to **{safe_md(player)}** "
                                 f"({self._delivery_text(sent, total)})")
        hint = "" if player in self.bot.online_players else "\n⚠️ Player is not online according to the player list."
        await interaction.followup.send(
            f"🔒 Custom kit **{safe_md(row['name'])}** to **{safe_md(player)}**: "
            f"{self._delivery_text(sent, total)}.{hint}", ephemeral=True)

    @customkit.command(name="all", description="Gives a custom kit to all online players.")
    @app_commands.describe(kit="Custom kit")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def give_all(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        players = sorted(self.bot.online_players)
        if not players:
            await interaction.response.send_message("Nobody online.", ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Really give custom kit **{safe_md(row['name'])}** to **{len(players)} online players**?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Timed out – cancelled.", view=None)
            return
        ok = 0
        for player in players:
            try:
                sent, total = await self.deliver(row["name"], player)
            except RconError:
                continue
            if sent:
                ok += 1
                self.bot.db.record_kit_claim(None, row["name"], player, f"discord:{interaction.user} (custom, all)")
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** to everyone ({ok}/{len(players)} players)")
        await interaction.edit_original_response(
            content=f"🔒 Custom kit **{safe_md(row['name'])}** sent to {ok} of {len(players)} players.")

    # ------------------------------------------------------------ Link Discord account <-> player

    def _verification_phrases(self) -> list[str]:
        """Two random quick chat phrases for the ownership check (not used as autokit triggers)."""
        triggers = {r["phrase"] for r in self.bot.db.kit_rules(trigger="quickchat")}
        pool = [raw for raw in quickchat.all_phrases()
                if "_slot_" in raw and raw not in triggers and not any(c in raw for c in VERIFY_SKIP_CATEGORIES)]
        return random.sample(pool, 2)

    @commands.Cog.listener()
    async def on_rce_chat(self, ev) -> None:
        """Ownership check: the player sends the requested quick chats in order in-game."""
        pending = self._pending.get(ev.player.lower())
        if pending is None or ev.channel == "SERVER":
            return
        if time.monotonic() > pending.expires:
            self._pending.pop(ev.player.lower(), None)
            return
        if ev.raw == pending.phrases[pending.step]:
            pending.step += 1
        elif ev.raw == pending.phrases[0]:
            pending.step = 1  # started over
        if pending.step < len(pending.phrases):
            return
        self._pending.pop(ev.player.lower(), None)
        if not self.bot.db.link_player(pending.user_id, ev.player):
            text = f"⛔ **{safe_md(ev.player)}** was linked to another Discord account in the meantime."
        else:
            text = (f"✅ Verified! Your Discord account is now linked to **{safe_md(ev.player)}**. "
                    "You can claim kits with `/customkit claim`.")
            feed = self.bot.feed(self.bot.config.admin_log_channel_id)
            if feed:
                feed.add_line(f"🔗 <@{pending.user_id}> linked to in-game player **{safe_md(ev.player)}**")
        try:
            await pending.interaction.edit_original_response(content=text)
        except discord.HTTPException:
            pass

    @customkit.command(name="link",
                       description="Link your Discord account to your in-game player (needed to claim kits).")
    @app_commands.describe(player="Your in-game name (you must be online)")
    @app_commands.autocomplete(player=_player_ac)
    async def link(self, interaction: discord.Interaction, player: str) -> None:
        name = sanitize_player_name(player)
        online = next((n for n in self.bot.online_players if n.lower() == name.lower()), None)
        if online is None:
            await interaction.response.send_message(
                f"🎮 **{safe_md(name)}** is not online right now. Join the game first – you have to confirm "
                "in-game that this is your account.", ephemeral=True)
            return
        owner = self.bot.db.player_owner(online)
        if owner == interaction.user.id:
            await interaction.response.send_message(f"✅ You are already linked to **{safe_md(online)}**.",
                                                    ephemeral=True)
            return
        if owner is not None:
            await interaction.response.send_message(
                f"⛔ **{safe_md(online)}** is already linked to another Discord account. If that's wrong, "
                "ask an admin (`/customkit unlink`).", ephemeral=True)
            return
        phrases = self._verification_phrases()
        for key, other in list(self._pending.items()):  # one open verification per Discord account
            if other.user_id == interaction.user.id:
                self._pending.pop(key)
        self._pending[online.lower()] = PendingLink(interaction.user.id, phrases, interaction,
                                                    time.monotonic() + VERIFY_SECONDS)
        steps = "\n".join(f"{i}. **{quickchat.translate(raw)}**  _(quick chat → {_category(raw)})_"
                          for i, raw in enumerate(phrases, 1))
        await interaction.response.send_message(
            f"🔗 To prove that **{safe_md(online)}** is your account, send these **quick chat** messages "
            f"in-game, in this order, within {VERIFY_SECONDS // 60} minutes:\n{steps}\n"
            "This message updates as soon as it worked.", ephemeral=True)

    @customkit.command(name="unlink", description="Removes the link between a member and their in-game player.")
    @app_commands.describe(member="Discord member")
    @admin_only()
    async def unlink(self, interaction: discord.Interaction, member: discord.Member) -> None:
        player = self.bot.db.unlink_player(member.id)
        if player is None:
            await interaction.response.send_message(f"{member.mention} is not linked.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        self._audit(interaction, f"unlinked {member.mention} from **{safe_md(player)}**")
        await interaction.response.send_message(
            f"🔓 {member.mention} is no longer linked to **{safe_md(player)}**. They can link again with "
            "`/customkit link`.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ Self-service claim

    @customkit.command(name="claim", description="Claim a custom kit your role gives you access to.")
    @app_commands.describe(kit="Custom kit")
    @app_commands.autocomplete(kit=_claimable_ac)
    async def claim(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None or not self.can_claim(interaction.user, row):
            claimable = [r["name"] for r in self.bot.db.custom_kits() if self.can_claim(interaction.user, r)]
            hint = (f"Kits you can claim: {', '.join(f'**{safe_md(n)}**' for n in claimable)}" if claimable
                    else "Your roles don't give you access to any custom kit.")
            await interaction.response.send_message(
                f"⛔ You can't claim **{safe_md(kit)}**.\n{hint}", ephemeral=True)
            return
        # The kit only ever goes to the player linked (and verified) for this Discord account
        player = self.bot.db.linked_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message(
                "🔗 First link your in-game player with `/customkit link` – the kit can only be sent to "
                "your own account.", ephemeral=True)
            return

        async with self._claim_locks[player.lower()]:  # no double claims by clicking twice
            cooldown = int(row["cooldown_minutes"])
            since = self.bot.db.minutes_since_custom_claim(row["name"], player)
            if cooldown and since is not None and since < cooldown:
                ready_at = int(time.time() + (cooldown - since) * 60)
                await interaction.response.send_message(
                    f"⏱️ **{safe_md(player)}** already claimed **{safe_md(row['name'])}**. You can claim it again "
                    f"<t:{ready_at}:R> (cooldown: {format_minutes(cooldown)}).", ephemeral=True)
                return
            # Must be online: items given to an offline player are lost
            online = next((n for n in self.bot.online_players if n.lower() == player.lower()), None)
            if online is None:
                await interaction.response.send_message(
                    f"🎮 **{safe_md(player)}** is not online on the server right now. Join the game first, "
                    "then claim the kit (the player list updates every minute).", ephemeral=True)
                return
            if not self.bot.db.custom_kit_items(row["name"]):
                await interaction.response.send_message("This kit is empty – please tell an admin.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            sent, total = await self.deliver(row["name"], online)
            self.bot.db.record_custom_claim(row["name"], player, interaction.user.id, str(interaction.user))

        self._audit(interaction, f"claimed custom kit **{safe_md(row['name'])}** as **{safe_md(online)}** "
                                 f"({self._delivery_text(sent, total)})")
        next_claim = f" Next claim in {format_minutes(cooldown)}." if cooldown else ""
        await interaction.followup.send(
            f"🎁 **{safe_md(row['name'])}** sent to **{safe_md(online)}** in-game "
            f"({self._delivery_text(sent, total)}).{next_claim}", ephemeral=True)

    @customkit.command(name="access", description="Allows or removes a role that may claim a custom kit.")
    @app_commands.describe(kit="Custom kit", role="Role whose members may claim the kit",
                           allow="True = role may claim, False = remove the role")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def access(self, interaction: discord.Interaction, kit: str, role: discord.Role, allow: bool = True) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        roles = self.bot.db.claim_roles(row)
        roles = roles + [role.id] if allow else [r for r in roles if r != role.id]
        self.bot.db.set_custom_kit_roles(row["name"], roles)
        row = self._resolve(row["name"])
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** – {role.mention} "
                                 f"{'may claim' if allow else 'removed'}")
        await interaction.response.send_message(
            f"✅ **{safe_md(row['name'])}** can be claimed by: {self._access_text(interaction.guild, row)} "
            f"(cooldown {format_minutes(row['cooldown_minutes'])}).", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @customkit.command(name="cooldown", description="Sets how often a custom kit can be claimed.")
    @app_commands.describe(kit="Custom kit", duration="e.g. 30m, 12h, 1d, 1d 12h – or 0 for no cooldown")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def cooldown(self, interaction: discord.Interaction, kit: str, duration: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        minutes = parse_duration(duration)
        if minutes is None or minutes > MAX_COOLDOWN_MINUTES:
            await interaction.response.send_message(
                "Invalid duration. Examples: `30m`, `12h`, `1d`, `1d 12h`, `0` (no cooldown). Max. 365 days.",
                ephemeral=True)
            return
        self.bot.db.set_custom_kit_cooldown(row["name"], minutes)
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** – cooldown {format_minutes(minutes)}")
        await interaction.response.send_message(
            f"⏱️ Cooldown for **{safe_md(row['name'])}**: {format_minutes(minutes)}. "
            "Applies immediately, also to earlier claims.", ephemeral=True)

    @customkit.command(name="reset-cooldown", description="Lets players claim a custom kit again right away.")
    @app_commands.describe(kit="Custom kit", player="Only this in-game player (empty = everyone)")
    @app_commands.autocomplete(kit=_kit_ac, player=_player_ac)
    @admin_only()
    async def reset_cooldown(self, interaction: discord.Interaction, kit: str, player: str | None = None) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        player = sanitize_player_name(player) if player else None
        count = self.bot.db.reset_custom_claims(row["name"], player)
        who = f"**{safe_md(player)}**" if player else "everyone"
        self._audit(interaction, f"Custom kit **{safe_md(row['name'])}** – cooldown reset for {who}")
        await interaction.response.send_message(
            f"🔄 Cooldown of **{safe_md(row['name'])}** reset for {who} ({count} claims).", ephemeral=True)

async def setup(bot: "RceBot") -> None:
    await bot.add_cog(CustomKits(bot))
