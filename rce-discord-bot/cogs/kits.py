"""Kitmanager: Kits anzeigen, vergeben, bearbeiten und Autokits.

Grundlage sind die eingebauten Kit-Befehle der Console Edition (kein Plugin nötig):
  kit list | kit info "Kit" | kit givetoplayer "Kit" "Spieler" | kit givetogroup "Kit" "Gruppe"
  kit giveall "Kit" | kit add ... | kit remove "Kit" "ID"
Die Syntax der verändernden Befehle ist als Vorlage in der .env anpassbar (siehe README).

Autokits (vom Bot gesteuert, Regeln in der SQLite-Datenbank):
  * Auslöser "respawn":   Spieler spawnt ("<Name> [..] has entered the game") -> Kit
  * Auslöser "quickchat": Spieler sendet eine bestimmte Quick-Chat-Phrase -> Kit
  Je Regel: Cooldown pro Spieler und optionales Limit (z. B. 1x pro Wipe).
Vergebene Kits sind auf RCE einmalig und gehen beim Tod verloren.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from rce import kits as kitparse
from rce import log_parser as lp
from rce import quickchat
from rce.rcon_client import RconError
from utils import (admin_only, format_duration, player_choices, safe_md, sanitize_ingame_text,
                   sanitize_player_name)

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.kits")

TRIGGER_LABELS = {"respawn": "🔄 Respawn", "quickchat": "💬 Quick-Chat"}
KIT_TYPE_LABELS = {"ingame": "Ingame-Kit", "custom": "🔒 Custom Kit (nur Bot)"}
ANNOUNCE_THROTTLE_SECONDS = 30  # max. eine Cooldown-Meldung pro Spieler in diesem Zeitraum


class _CachedList:
    """Zwischenspeicher für RCON-Listen (Kits, Gruppen) – für schnelle Autovervollständigung."""

    def __init__(self, bot: "RceBot", command: str, parser: Callable[[str], list[str]], ttl: float = 300):
        self.bot, self.command, self.parser, self.ttl = bot, command, parser, ttl
        self.items: list[str] = []
        self._fetched_at: float | None = None  # None = noch nie geladen
        self._task: asyncio.Task | None = None

    def invalidate(self) -> None:
        self._fetched_at = None

    async def _refresh(self) -> None:
        try:
            raw = await self.bot.rcon.command(self.command)
        except RconError as exc:
            log.debug("'%s' fehlgeschlagen: %s", self.command, exc)
            return
        self.items = self.parser(raw)
        self._fetched_at = time.monotonic()

    async def get(self, *, max_wait: float = 2.0, force: bool = False) -> list[str]:
        stale = force or self._fetched_at is None or time.monotonic() - self._fetched_at > self.ttl
        if stale and self.bot.rcon.connected and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._refresh())
        if self._task is not None and not self._task.done() and (force or not self.items):
            try:
                # shield: läuft weiter, auch wenn wir nicht länger warten (Autocomplete hat < 3 s)
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

    @discord.ui.button(label="Ja, an alle vergeben", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Wird vergeben …", view=None)
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Abgebrochen.", view=None)
        self.stop()


class Kits(commands.Cog):
    kit = app_commands.Group(name="kit", description="Kitmanager", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.kit_names = _CachedList(bot, "kit list", kitparse.parse_kit_list)
        self.auth_groups = _CachedList(bot, "getauthlevels", kitparse.parse_auth_groups, ttl=900)
        self.phrases = quickchat.all_phrases()
        self._announced: dict[str, float] = {}

    # ------------------------------------------------------------ Hilfen

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
            trigger = f"Quick-Chat „{self.phrases.get(rule['phrase'], rule['phrase'])}“"
        else:
            trigger = "bei jedem Respawn"
        lock = "🔒 " if rule["kit_type"] == "custom" else ""
        parts = [f"`#{rule['id']}` {lock}**{safe_md(rule['kit'])}** – {trigger}"]
        if rule["cooldown_minutes"]:
            parts.append(f"Cooldown {format_duration(rule['cooldown_minutes'] * 60)}")
        if rule["max_claims"]:
            parts.append(f"max. {rule['max_claims']}× pro Spieler")
        if not rule["enabled"]:
            parts.append("⏸️ pausiert")
        return " · ".join(parts)

    @staticmethod
    def _response_block(response: str) -> str:
        response = response.strip()
        if not response:
            return "(Keine Ausgabe vom Server – bei RCE für Kit-Befehle normal.)"
        return f"```\n{response[:1500].replace('```', 'ˋˋˋ')}\n```"

    # ------------------------------------------------------------ Autocomplete

    async def _kit_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = await self.kit_names.get()
        current_l = current.lower()
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names if current_l in n.lower()][:25]

    async def _rule_kit_ac(self, interaction: discord.Interaction, current: str
                           ) -> list[app_commands.Choice[str]]:
        """Für /kit autokit-neu: Ingame-Kits und/oder Custom Kits, je nach gewählter Art."""
        art = getattr(interaction.namespace, "art", None)
        custom = [] if art == "ingame" else [
            app_commands.Choice(name=f"🔒 {r['name']}"[:100], value=r["name"][:100])
            for r in self.bot.db.custom_kits() if current.lower() in r["name"].lower()
        ]
        ingame = [] if art == "custom" else await self._kit_ac(interaction, current)
        return (custom + ingame)[:25]

    async def _player_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    async def _group_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        groups = await self.auth_groups.get()
        return [app_commands.Choice(name=g, value=g) for g in groups if current.lower() in g.lower()][:25]

    async def _phrase_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current_l = current.lower()
        hits = [(raw, text) for raw, text in self.phrases.items()
                if current_l in text.lower() or current_l in raw.lower()]
        return [app_commands.Choice(name=text[:100], value=raw[:100]) for raw, text in hits[:25]]

    async def _rule_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        choices = []
        for rule in self.bot.db.kit_rules():
            label = f"#{rule['id']} {rule['kit']} ({TRIGGER_LABELS.get(rule['trigger'], rule['trigger'])})"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=int(rule["id"])))
        return choices[:25]

    # ------------------------------------------------------------ Anzeigen (alle)

    @kit.command(name="liste", description="Zeigt alle Kits auf dem Server.")
    async def kit_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        names = await self.kit_names.get(force=True, max_wait=8)
        if not self.bot.rcon.connected and not names:
            raise RconError("Keine RCON-Verbindung zum Rust-Server.")
        if not names:
            await interaction.followup.send("Auf dem Server sind keine Kits angelegt.")
            return
        auto = {r["kit"].lower() for r in self.bot.db.kit_rules(enabled_only=True) if r["kit_type"] == "ingame"}
        lines = [f"• **{safe_md(n)}**" + (" · 🤖 Autokit" if n.lower() in auto else "") for n in names]
        embed = discord.Embed(title=f"🎒 Kits ({len(names)})", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Details: /kit info · Autokits: /kit autokits")
        await interaction.followup.send(embed=embed)

    @kit.command(name="info", description="Zeigt den Inhalt eines Kits.")
    @app_commands.describe(kit="Name des Kits")
    @app_commands.autocomplete(kit=_kit_ac)
    async def kit_info(self, interaction: discord.Interaction, kit: str) -> None:
        name = kitparse.clean_arg(kit)
        await interaction.response.defer()
        raw = await self.bot.rcon.command(f'kit info "{name}"')
        items = kitparse.parse_kit_info(raw)
        embed = discord.Embed(title=f"🎒 Kit: {name}", colour=discord.Colour.dark_green())
        if not items:
            embed.description = "Keine Items erkannt. Antwort des Servers:\n" + self._response_block(raw)
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

    @kit.command(name="autokits", description="Zeigt, welche Kits automatisch vergeben werden.")
    async def autokit_list(self, interaction: discord.Interaction) -> None:
        rules = self.bot.db.kit_rules()
        if not rules:
            await interaction.response.send_message("Es sind keine Autokits eingerichtet.", ephemeral=True)
            return
        embed = discord.Embed(title="🤖 Autokits", description="\n".join(self._rule_text(r) for r in rules)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Quick-Chat-Kits: Phrase ingame senden · Kits gehen beim Tod verloren")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ Vergeben (Admins)

    @kit.command(name="geben", description="Gibt einem Spieler ein Kit.")
    @app_commands.describe(kit="Name des Kits", spieler="Ingame-Name")
    @app_commands.autocomplete(kit=_kit_ac, spieler=_player_ac)
    @admin_only()
    async def kit_give(self, interaction: discord.Interaction, kit: str, spieler: str) -> None:
        name, player = kitparse.clean_arg(kit), sanitize_player_name(spieler)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_template.format(kit=name, name=player), expect_response=False)
        self.bot.db.record_kit_claim(None, name, player, f"discord:{interaction.user}")
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** an **{safe_md(player)}**")
        hint = "" if player in self.bot.online_players else "\n⚠️ Spieler ist laut Spielerliste nicht online."
        await interaction.followup.send(f"🎒 Kit **{safe_md(name)}** an **{safe_md(player)}** gesendet.{hint}\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="gruppe", description="Gibt allen Mitgliedern einer Auth-Gruppe ein Kit.")
    @app_commands.describe(kit="Name des Kits", gruppe="Auth-Gruppe (z. B. VIP, Admin)")
    @app_commands.autocomplete(kit=_kit_ac, gruppe=_group_ac)
    @admin_only()
    async def kit_give_group(self, interaction: discord.Interaction, kit: str, gruppe: str) -> None:
        name, group = kitparse.clean_arg(kit), kitparse.clean_arg(gruppe)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_group_template.format(kit=name, group=group), expect_response=False)
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** an Gruppe `{group}`")
        await interaction.followup.send(f"🎒 Kit **{safe_md(name)}** an Gruppe `{group}` gesendet.\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="alle", description="Gibt ALLEN Spielern auf dem Server ein Kit.")
    @app_commands.describe(kit="Name des Kits")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_give_all(self, interaction: discord.Interaction, kit: str) -> None:
        name = kitparse.clean_arg(kit)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Kit **{safe_md(name)}** wirklich an **alle {len(self.bot.online_players)} Online-Spieler** vergeben?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            if view.confirmed is None:
                await interaction.edit_original_response(content="Zeit abgelaufen – abgebrochen.", view=None)
            return
        response = await self.bot.rcon.command(
            self.bot.config.kit_give_all_template.format(kit=name), expect_response=False)
        self._audit(f"🎒 {self._who(interaction)}: Kit **{safe_md(name)}** an **alle Spieler**")
        await interaction.edit_original_response(
            content=f"🎒 Kit **{safe_md(name)}** an alle gesendet.\n{self._response_block(response)}")

    # ------------------------------------------------------------ Bearbeiten (Admins)

    @kit.command(name="item-hinzufuegen", description="Fügt einem Kit ein Item hinzu (legt das Kit ggf. neu an).")
    @app_commands.describe(kit="Kit-Name (neuer Name = neues Kit)", item="Item-Shortname, z. B. rifle.ak",
                           menge="Anzahl", zustand="Zustand in Prozent", platz="Wohin das Item kommt")
    @app_commands.choices(platz=[app_commands.Choice(name=kitparse.CONTAINER_LABELS[c.lower()], value=c)
                                 for c in kitparse.CONTAINERS])
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_add_item(self, interaction: discord.Interaction, kit: str, item: str,
                           menge: app_commands.Range[int, 1, 100000] = 1,
                           zustand: app_commands.Range[int, 1, 100] = 100,
                           platz: app_commands.Choice[str] | None = None) -> None:
        name, short = kitparse.clean_arg(kit), kitparse.clean_arg(item)
        container = platz.value if platz else "Main"
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_add_template.format(kit=name, item=short, amount=menge, condition=zustand,
                                                    container=container),
            expect_response=False)
        self.kit_names.invalidate()
        self._audit(f"🛠️ {self._who(interaction)}: Kit **{safe_md(name)}** + `{short}` × {menge} ({container})")
        await interaction.followup.send(
            f"➕ `{short}` × {menge} zu **{safe_md(name)}** hinzugefügt (Prüfen mit `/kit info`).\n"
            f"{self._response_block(response)}", ephemeral=True)

    @kit.command(name="item-entfernen", description="Entfernt ein Item aus einem Kit (ID siehe /kit info).")
    @app_commands.describe(kit="Name des Kits", item_id="Item-ID aus der Ausgabe von /kit info")
    @app_commands.autocomplete(kit=_kit_ac)
    @admin_only()
    async def kit_remove_item(self, interaction: discord.Interaction, kit: str,
                              item_id: app_commands.Range[int, 0, 1_000_000]) -> None:
        name = kitparse.clean_arg(kit)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(
            self.bot.config.kit_remove_template.format(kit=name, id=item_id), expect_response=False)
        self._audit(f"🛠️ {self._who(interaction)}: Kit **{safe_md(name)}** – Item-ID {item_id} entfernt")
        await interaction.followup.send(f"➖ Item {item_id} aus **{safe_md(name)}** entfernt.\n"
                                        f"{self._response_block(response)}", ephemeral=True)

    # ------------------------------------------------------------ Autokit-Regeln (Admins)

    @kit.command(name="autokit-neu", description="Richtet ein Autokit ein (Respawn oder Quick-Chat-Phrase).")
    @app_commands.describe(
        kit="Name des Kits",
        ausloeser="Wann das Kit vergeben wird",
        art="Ingame-Kit (Standard) oder Custom Kit aus /customkit",
        phrase="Nur bei Quick-Chat: welche Phrase das Kit auslöst",
        cooldown_minuten="Wartezeit pro Spieler bis zur nächsten Vergabe (0 = keine)",
        max_pro_spieler="Höchstzahl Vergaben pro Spieler bis zum Reset (0 = unbegrenzt)",
    )
    @app_commands.choices(ausloeser=[app_commands.Choice(name=v, value=k) for k, v in TRIGGER_LABELS.items()],
                          art=[app_commands.Choice(name=v, value=k) for k, v in KIT_TYPE_LABELS.items()])
    @app_commands.autocomplete(kit=_rule_kit_ac, phrase=_phrase_ac)
    @admin_only()
    async def autokit_add(self, interaction: discord.Interaction, kit: str, ausloeser: app_commands.Choice[str],
                          phrase: str | None = None,
                          cooldown_minuten: app_commands.Range[int, 0, 525600] = 0,
                          max_pro_spieler: app_commands.Range[int, 0, 10000] = 0,
                          art: app_commands.Choice[str] | None = None) -> None:
        name = kitparse.clean_arg(kit)
        trigger = ausloeser.value
        if art is not None:
            kit_type = art.value
        else:
            # Art nicht gewählt: Custom Kit, wenn es nur als Custom Kit existiert
            ingame_names = {k.lower() for k in await self.kit_names.get(max_wait=3)}
            is_custom = self.bot.db.custom_kit(name) is not None and name.lower() not in ingame_names
            kit_type = "custom" if is_custom else "ingame"
        if kit_type == "custom":
            custom = self.bot.db.custom_kit(name)
            if custom is None:
                await interaction.response.send_message(
                    f"Custom Kit **{safe_md(name)}** gibt es nicht (`/customkit liste`).", ephemeral=True)
                return
            name = custom["name"]
        notes = []
        if trigger == "quickchat":
            if not phrase:
                await interaction.response.send_message(
                    "Für den Auslöser Quick-Chat bitte eine `phrase` auswählen.", ephemeral=True)
                return
            phrase = phrase.strip()
            if phrase not in self.phrases:
                notes.append("⚠️ Unbekannte Phrase – sie muss exakt so im Konsolen-Log stehen.")
            clash = [r for r in self.bot.db.kit_rules(trigger="quickchat") if r["phrase"] == phrase]
            if clash:
                notes.append(f"ℹ️ Diese Phrase löst bereits Regel #{clash[0]['id']} aus – beide Kits werden vergeben.")
        else:
            phrase = None
            if cooldown_minuten == 0 and max_pro_spieler == 0:
                notes.append("ℹ️ Ohne Cooldown gibt es das Kit bei **jedem** Respawn.")

        if kit_type == "ingame":
            known = await self.kit_names.get(max_wait=3)
            if known and name.lower() not in (k.lower() for k in known):
                notes.append(f"⚠️ Kit **{safe_md(name)}** wurde in `kit list` nicht gefunden.")
        elif not self.bot.db.custom_kit_items(name):
            notes.append("⚠️ Das Custom Kit ist noch leer.")

        rule_id = self.bot.db.add_kit_rule(name, trigger, phrase, cooldown_minuten, max_pro_spieler,
                                           str(interaction.user), kit_type)
        rule = self.bot.db.kit_rule(rule_id)
        self._audit(f"🤖 {self._who(interaction)}: Autokit angelegt – {self._rule_text(rule)}")
        text = f"✅ Autokit angelegt:\n{self._rule_text(rule)}"
        if notes:
            text += "\n\n" + "\n".join(notes)
        await interaction.response.send_message(text, ephemeral=True)

    @kit.command(name="autokit-status", description="Pausiert oder aktiviert ein Autokit.")
    @app_commands.describe(regel="Autokit-Regel", aktiv="An oder aus")
    @app_commands.autocomplete(regel=_rule_ac)
    @admin_only()
    async def autokit_toggle(self, interaction: discord.Interaction, regel: int, aktiv: bool) -> None:
        if not self.bot.db.set_kit_rule_enabled(regel, aktiv):
            await interaction.response.send_message(f"Regel #{regel} gibt es nicht.", ephemeral=True)
            return
        state = "aktiviert" if aktiv else "pausiert"
        self._audit(f"🤖 {self._who(interaction)}: Autokit #{regel} {state}")
        await interaction.response.send_message(f"Autokit #{regel} {state}.", ephemeral=True)

    @kit.command(name="autokit-loeschen", description="Löscht ein Autokit.")
    @app_commands.describe(regel="Autokit-Regel")
    @app_commands.autocomplete(regel=_rule_ac)
    @admin_only()
    async def autokit_delete(self, interaction: discord.Interaction, regel: int) -> None:
        rule = self.bot.db.kit_rule(regel)
        if rule is None or not self.bot.db.delete_kit_rule(regel):
            await interaction.response.send_message(f"Regel #{regel} gibt es nicht.", ephemeral=True)
            return
        self._audit(f"🗑️ {self._who(interaction)}: Autokit gelöscht – {self._rule_text(rule)}")
        await interaction.response.send_message(f"🗑️ Autokit #{regel} gelöscht.", ephemeral=True)

    @kit.command(name="wipe-reset", description="Setzt Cooldowns/Limits der Autokits zurück (z. B. nach Wipe).")
    @app_commands.describe(regel="Nur diese Regel zurücksetzen (leer = alle)")
    @app_commands.autocomplete(regel=_rule_ac)
    @admin_only()
    async def autokit_reset(self, interaction: discord.Interaction, regel: int | None = None) -> None:
        count = self.bot.db.reset_kit_claims(regel)
        scope = f"Regel #{regel}" if regel is not None else "alle Autokits"
        self._audit(f"♻️ {self._who(interaction)}: Autokit-Verlauf zurückgesetzt ({scope}, {count} Einträge)")
        await interaction.response.send_message(f"♻️ {count} Vergaben für {scope} zurückgesetzt.", ephemeral=True)

    @kit.command(name="verlauf", description="Zeigt die letzten Kit-Vergaben.")
    @app_commands.describe(spieler="Nur Vergaben an diesen Spieler")
    @app_commands.autocomplete(spieler=_player_ac)
    @admin_only()
    async def kit_history(self, interaction: discord.Interaction, spieler: str | None = None) -> None:
        rows = self.bot.db.kit_claim_history(spieler.strip() if spieler else None, limit=20)
        if not rows:
            await interaction.response.send_message("Keine Kit-Vergaben gefunden.", ephemeral=True)
            return
        lines = []
        for r in rows:
            source = f"Autokit #{r['rule_id']} ({r['source']})" if r["rule_id"] is not None else r["source"]
            lines.append(f"`{r['claimed_at']}` **{safe_md(r['kit'])}** → {safe_md(r['player'])} · {safe_md(source)}")
        embed = discord.Embed(title="📜 Kit-Verlauf", description="\n".join(lines)[:4000],
                              colour=discord.Colour.dark_green())
        embed.set_footer(text="Zeiten in UTC")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Autokit-Logik

    @commands.Cog.listener()
    async def on_rce_respawn(self, ev: lp.RespawnEvent) -> None:
        for rule in self.bot.db.kit_rules(enabled_only=True, trigger="respawn"):
            await self._try_claim(rule, ev.player, "respawn")

    @commands.Cog.listener()
    async def on_rce_chat(self, ev: lp.ChatEvent) -> None:
        if ev.channel == "SERVER":
            return
        for rule in self.bot.db.kit_rules(enabled_only=True, trigger="quickchat"):
            if rule["phrase"] == ev.raw:
                await self._try_claim(rule, ev.player, "quickchat")

    async def _try_claim(self, rule, player: str, source: str) -> None:
        kit = rule["kit"]
        count, minutes_since = self.bot.db.kit_claim_state(rule["id"], player)

        if rule["max_claims"] and count >= rule["max_claims"]:
            await self._announce_denied(player, f"Limit für Kit <b>{sanitize_ingame_text(kit, 40)}</b> erreicht.",
                                        source)
            return
        if rule["cooldown_minutes"] and minutes_since is not None and minutes_since < rule["cooldown_minutes"]:
            remaining = max(1, math.ceil(rule["cooldown_minutes"] - minutes_since))
            await self._announce_denied(
                player, f"Kit <b>{sanitize_ingame_text(kit, 40)}</b> wieder in {format_duration(remaining * 60)}.",
                source)
            return

        # Erst eintragen, dann senden: verhindert Doppelvergabe bei schnell aufeinanderfolgenden Zeilen
        claim_id = self.bot.db.record_kit_claim(rule["id"], kit, player, source)
        if source == "respawn" and self.bot.config.autokit_delay:
            await asyncio.sleep(self.bot.config.autokit_delay)  # Spieler erst vollständig spawnen lassen
        try:
            if rule["kit_type"] == "custom":
                custom = self.bot.get_cog("CustomKits")
                if custom is None:
                    raise RconError("Custom-Kit-Modul ist nicht geladen.")
                sent, total = await custom.deliver(kit, player)
                if total == 0:
                    raise RconError("Custom Kit ist leer oder wurde gelöscht.")
            else:
                await self.bot.rcon.command(self.bot.config.kit_give_template.format(kit=kit, name=player),
                                            expect_response=False)
        except RconError as exc:
            self.bot.db.delete_kit_claim(claim_id)
            self._audit(f"⚠️ Autokit **{safe_md(kit)}** an **{safe_md(player)}** fehlgeschlagen: {exc}")
            return

        self._audit(f"🤖 Autokit **{safe_md(kit)}** an **{safe_md(player)}** "
                    f"({TRIGGER_LABELS.get(source, source)}, Regel #{rule['id']})")
        if source == "quickchat" and self.bot.config.kit_claim_announce:
            await self._say(f"<color=#7CFC00>{sanitize_ingame_text(player, 32)}</color> hat das Kit "
                            f"<b>{sanitize_ingame_text(kit, 40)}</b> erhalten.")

    async def _announce_denied(self, player: str, text: str, source: str) -> None:
        # Nur bei Quick-Chat (der Spieler hat aktiv angefragt) und gedrosselt – sonst Spam im Chat
        if source != "quickchat" or not self.bot.config.kit_claim_announce:
            return
        now = time.monotonic()
        if now - self._announced.get(player.lower(), 0) < ANNOUNCE_THROTTLE_SECONDS:
            return
        self._announced[player.lower()] = now
        await self._say(f"<color=orange>{sanitize_ingame_text(player, 32)}</color>: {text}")


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Kits(bot))
