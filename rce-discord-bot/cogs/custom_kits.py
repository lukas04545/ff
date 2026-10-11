"""Custom kits: kits that exist only in the bot.

They are NOT created in the server's kit system but given item by item via
"inventory.giveto". This makes them invisible in the in-game kit manager, where
they can't be viewed, claimed or edited.

Limitation: "inventory.giveto" supports neither condition nor slot (hotbar/clothing) –
the items go into the player's inventory (if it is full, they may drop on the ground).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.kits import ConfirmView
from rce import items as itemlist
from rce import kits as kitparse
from rce.rcon_client import RconError, RconNotConnected
from utils import admin_only, player_choices, safe_md, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.customkits")

MAX_ITEMS_PER_KIT = 30


class CustomKits(commands.Cog):
    customkit = app_commands.Group(
        name="customkit",
        description="Bot-only kits (invisible in the in-game kit manager)",
        guild_only=True,
    )

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

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
        short = kitparse.clean_arg(item).replace(" ", "").lower()
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


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(CustomKits(bot))
