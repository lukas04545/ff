"""Custom Kits: Kits, die nur im Bot existieren.

Sie werden NICHT im Kit-System des Servers angelegt, sondern Item für Item per
"inventory.giveto" vergeben. Dadurch sind sie im Ingame-Kitmanager unsichtbar und
können dort weder eingesehen noch beansprucht oder bearbeitet werden.

Einschränkung: "inventory.giveto" kennt weder Zustand noch Platz (Hotbar/Kleidung) –
die Items landen im Inventar des Spielers (bei vollem Inventar fallen sie ggf. auf den Boden).
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
        description="Bot-eigene Kits (im Ingame-Kitmanager unsichtbar)",
        guild_only=True,
    )

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------ Vergabe (auch für Autokits)

    async def deliver(self, kit: str, player: str) -> tuple[int, int]:
        """Gibt alle Items eines Custom Kits an einen Spieler. Rückgabe: (gesendet, gesamt).

        Wirft RconNotConnected, wenn gar nichts gesendet werden konnte.
        """
        items = self.bot.db.custom_kit_items(kit)
        if not items:
            return 0, 0
        template = self.bot.config.item_give_template
        # Alle Befehle gleichzeitig senden: der Server arbeitet sie der Reihe nach ab,
        # der Bot muss aber nicht pro Item auf eine (evtl. ausbleibende) Antwort warten.
        results = await asyncio.gather(
            *(self.bot.rcon.command(template.format(name=player, item=i["shortname"], amount=i["amount"]),
                                    expect_response=False) for i in items),
            return_exceptions=True,
        )
        sent = sum(1 for r in results if not isinstance(r, BaseException))
        if sent == 0:
            first = next((r for r in results if isinstance(r, RconError)), None)
            raise first or RconNotConnected("Keine Items gesendet.")
        return sent, len(items)

    # ------------------------------------------------------------ Hilfen

    def _audit(self, interaction: discord.Interaction, text: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"🔒 {interaction.user.mention} ({safe_md(str(interaction.user))}): {text}")

    def _resolve(self, kit: str):
        return self.bot.db.custom_kit(kitparse.clean_arg(kit))

    async def _missing(self, interaction: discord.Interaction, kit: str) -> None:
        await interaction.response.send_message(
            f"Custom Kit **{safe_md(kit)}** gibt es nicht. Übersicht: `/customkit liste`", ephemeral=True)

    @staticmethod
    def _delivery_text(sent: int, total: int) -> str:
        if sent == total:
            return f"{total} Items gesendet"
        return f"⚠️ nur {sent} von {total} Items gesendet"

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

    # ------------------------------------------------------------ Verwalten

    @customkit.command(name="erstellen", description="Legt ein neues Custom Kit an.")
    @app_commands.describe(name="Name des Kits", beschreibung="Optionale Notiz (nur im Discord sichtbar)")
    @admin_only()
    async def create(self, interaction: discord.Interaction, name: str, beschreibung: str | None = None) -> None:
        kit = kitparse.clean_arg(name, limit=40)
        if not kit:
            await interaction.response.send_message("Bitte einen gültigen Namen angeben.", ephemeral=True)
            return
        if not self.bot.db.create_custom_kit(kit, beschreibung, str(interaction.user)):
            await interaction.response.send_message(f"Custom Kit **{safe_md(kit)}** gibt es schon.", ephemeral=True)
            return
        note = ""
        kits_cog = self.bot.get_cog("Kits")
        if kits_cog and kit.lower() in (k.lower() for k in kits_cog.kit_names.items):
            note = "\nℹ️ Ein Ingame-Kit heißt genauso – beide bleiben getrennt."
        self._audit(interaction, f"Custom Kit **{safe_md(kit)}** erstellt")
        await interaction.response.send_message(
            f"✅ Custom Kit **{safe_md(kit)}** erstellt. Items hinzufügen mit `/customkit item-hinzufuegen`.{note}",
            ephemeral=True)

    @customkit.command(name="item-hinzufuegen", description="Fügt einem Custom Kit ein Item hinzu.")
    @app_commands.describe(kit="Custom Kit", item="Item-Shortname, z. B. rifle.ak", menge="Anzahl")
    @app_commands.autocomplete(kit=_kit_ac, item=_item_ac)
    @admin_only()
    async def add_item(self, interaction: discord.Interaction, kit: str, item: str,
                       menge: app_commands.Range[int, 1, 100000] = 1) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        short = kitparse.clean_arg(item).replace(" ", "").lower()
        if not short:
            await interaction.response.send_message("Bitte einen Item-Shortnamen angeben.", ephemeral=True)
            return
        if len(self.bot.db.custom_kit_items(row["name"])) >= MAX_ITEMS_PER_KIT:
            await interaction.response.send_message(
                f"Ein Custom Kit kann höchstens {MAX_ITEMS_PER_KIT} Items enthalten.", ephemeral=True)
            return
        self.bot.db.add_custom_kit_item(row["name"], short, menge)
        hint = "" if short in itemlist.COMMON_ITEMS else \
            "\nℹ️ Unbekannter Shortname – bitte einmal mit `/customkit geben` an dich selbst testen."
        self._audit(interaction, f"Custom Kit **{safe_md(row['name'])}** + `{short}` × {menge}")
        await interaction.response.send_message(
            f"➕ `{short}` × {menge} zu **{safe_md(row['name'])}** hinzugefügt.{hint}", ephemeral=True)

    @customkit.command(name="item-entfernen", description="Entfernt ein Item aus einem Custom Kit.")
    @app_commands.describe(kit="Custom Kit", item="Item aus dem Kit")
    @app_commands.autocomplete(kit=_kit_ac, item=_kit_item_ac)
    @admin_only()
    async def remove_item(self, interaction: discord.Interaction, kit: str, item: int) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        if not self.bot.db.remove_custom_kit_item(row["name"], item):
            await interaction.response.send_message(f"Item #{item} ist nicht in diesem Kit.", ephemeral=True)
            return
        self._audit(interaction, f"Custom Kit **{safe_md(row['name'])}** – Item #{item} entfernt")
        await interaction.response.send_message(f"➖ Item #{item} entfernt.", ephemeral=True)

    @customkit.command(name="loeschen", description="Löscht ein Custom Kit samt Autokit-Regeln.")
    @app_commands.describe(kit="Custom Kit")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def delete(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        view = ConfirmView(interaction.user.id)
        view.confirm.label = "Ja, löschen"
        await interaction.response.send_message(
            f"Custom Kit **{safe_md(row['name'])}** wirklich löschen? Zugehörige Autokits werden mitgelöscht.",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Zeit abgelaufen – abgebrochen.", view=None)
            return
        rules = self.bot.db.delete_custom_kit(row["name"])
        self._audit(interaction, f"Custom Kit **{safe_md(row['name'])}** gelöscht ({rules} Autokit-Regeln)")
        await interaction.edit_original_response(
            content=f"🗑️ Custom Kit **{safe_md(row['name'])}** gelöscht ({rules} Autokit-Regeln entfernt).")

    # ------------------------------------------------------------ Anzeigen

    @customkit.command(name="liste", description="Zeigt alle Custom Kits.")
    @admin_only()
    async def list_kits(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.custom_kits()
        if not rows:
            await interaction.response.send_message("Noch keine Custom Kits. Anlegen mit `/customkit erstellen`.",
                                                    ephemeral=True)
            return
        auto = {r["kit"].lower() for r in self.bot.db.kit_rules(enabled_only=True) if r["kit_type"] == "custom"}
        lines = [f"• **{safe_md(r['name'])}** – {r['item_count']} Items"
                 + (" · 🤖 Autokit" if r["name"].lower() in auto else "")
                 + (f"\n  _{safe_md(r['description'])}_" if r["description"] else "")
                 for r in rows]
        embed = discord.Embed(title=f"🔒 Custom Kits ({len(rows)})", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_purple())
        embed.set_footer(text="Nur im Bot gespeichert – im Ingame-Kitmanager unsichtbar")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @customkit.command(name="info", description="Zeigt den Inhalt eines Custom Kits.")
    @app_commands.describe(kit="Custom Kit")
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
        embed = discord.Embed(title=f"🔒 Custom Kit: {row['name']}",
                              description="\n".join(lines)[:4000] or "_Noch leer._",
                              colour=discord.Colour.dark_purple())
        if row["description"]:
            embed.add_field(name="Notiz", value=row["description"][:1024], inline=False)
        rules = [r for r in self.bot.db.kit_rules()
                 if r["kit_type"] == "custom" and r["kit"].lower() == row["name"].lower()]
        if rules:
            embed.add_field(name="🤖 Autokit-Regeln", value=", ".join(f"#{r['id']}" for r in rules), inline=False)
        embed.set_footer(text=f"Erstellt von {row['created_by']} · {row['created_at']} UTC")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Vergeben

    @customkit.command(name="geben", description="Gibt einem Spieler ein Custom Kit.")
    @app_commands.describe(kit="Custom Kit", spieler="Ingame-Name")
    @app_commands.autocomplete(kit=_kit_ac, spieler=_player_ac)
    @admin_only()
    async def give(self, interaction: discord.Interaction, kit: str, spieler: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        player = sanitize_player_name(spieler)
        await interaction.response.defer(ephemeral=True)
        sent, total = await self.deliver(row["name"], player)
        if total == 0:
            await interaction.followup.send("Das Kit ist leer.", ephemeral=True)
            return
        self.bot.db.record_kit_claim(None, row["name"], player, f"discord:{interaction.user} (custom)")
        self._audit(interaction, f"Custom Kit **{safe_md(row['name'])}** an **{safe_md(player)}** "
                                 f"({self._delivery_text(sent, total)})")
        hint = "" if player in self.bot.online_players else "\n⚠️ Spieler ist laut Spielerliste nicht online."
        await interaction.followup.send(
            f"🔒 Custom Kit **{safe_md(row['name'])}** an **{safe_md(player)}**: "
            f"{self._delivery_text(sent, total)}.{hint}", ephemeral=True)

    @customkit.command(name="alle", description="Gibt allen Online-Spielern ein Custom Kit.")
    @app_commands.describe(kit="Custom Kit")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def give_all(self, interaction: discord.Interaction, kit: str) -> None:
        row = self._resolve(kit)
        if row is None:
            await self._missing(interaction, kit)
            return
        players = sorted(self.bot.online_players)
        if not players:
            await interaction.response.send_message("Niemand online.", ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Custom Kit **{safe_md(row['name'])}** wirklich an **{len(players)} Online-Spieler** vergeben?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Zeit abgelaufen – abgebrochen.", view=None)
            return
        ok = 0
        for player in players:
            try:
                sent, total = await self.deliver(row["name"], player)
            except RconError:
                continue
            if sent:
                ok += 1
                self.bot.db.record_kit_claim(None, row["name"], player, f"discord:{interaction.user} (custom, alle)")
        self._audit(interaction, f"Custom Kit **{safe_md(row['name'])}** an alle ({ok}/{len(players)} Spieler)")
        await interaction.edit_original_response(
            content=f"🔒 Custom Kit **{safe_md(row['name'])}** an {ok} von {len(players)} Spielern gesendet.")


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(CustomKits(bot))
