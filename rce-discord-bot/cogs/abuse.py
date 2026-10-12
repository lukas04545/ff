"""Admin abuse protection: someone who isn't allowed spawns raid items (rockets, C4) -> admin removed, killed.

Only items spawned with the in-game admin panel count – the server logs them as
"[ServerVar] giving <player> <amount> x <item>". Gives via RCON/console (the bot's shop and kits, /rcon) look
different ("[ServerVar] SERVER giving …" or without "[ServerVar]") and are ignored. When the item matches
ABUSE_ITEMS and the player is not allowed, the bot runs ABUSE_REMOVE_ADMIN_COMMANDS, ABUSE_KILL_COMMAND
(global.killplayer "<player>") and ABUSE_AFTER_KILL_COMMANDS (entity.deleteby "<player>") and pings staff.

Allowed: players linked (/customkit link) to a Discord member with an ABUSE_EXEMPT_ROLE_IDS role (owner, co-owner)
and the names in ABUSE_EXEMPT_PLAYERS. Items the bot gives itself (shop, custom kits, rewards) never count.
The protection only runs once at least one of the two is set – otherwise the owner could be hit.
Limit: the log line doesn't say who gave the item. If an admin gives rockets to someone else, the log shows that
player – the alert says so, staff should check it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from rce.rcon_client import RconError
from utils import is_admin, safe_md, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.abuse")

OWN_GIVE_WINDOW = 15         # seconds: an item line this soon after the bot's own give to that player is the bot's
RESPAWN_WAIT = 3600         # on-respawn commands only within 1 hour after the punishment
PUNISH_COOLDOWN = 10         # one spawn of several stacks makes several lines within a second – handle it once
IGNORED_WORDS = ("launcher",)   # "Rocket Launcher" is not a rocket


class AbuseGuard(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._punished: dict[str, float] = {}
        self._noted: dict[str, float] = {}      # spawns reported but not punished (allowed / not set up)
        self.recent: deque[dict] = deque(maxlen=15)   # last raid item spawns seen (web interface status box)
        self._await_respawn: dict[str, float] = {}     # punished players -> on-respawn commands still to run

    async def cog_load(self) -> None:
        self.bot.add_view(CaseView(self))

    @property
    def active(self) -> bool:
        cfg = self.bot.config
        return cfg.abuse_protection and bool(cfg.abuse_exempt_role_ids or cfg.abuse_exempt_players)

    def forbidden(self, item: str) -> bool:
        name = item.lower()
        return any(k in name for k in self.bot.config.abuse_items) and not any(w in name for w in IGNORED_WORDS)

    def bot_gave(self, player: str) -> bool:
        """Did the bot itself just give this player items (shop, kit, reward, /rcon)? Only give commands count –
        not e.g. the bot's own removeowner/killplayer/sleep for this player."""
        quoted = f'"{player.lower()}"'
        cutoff = time.monotonic() - OWN_GIVE_WINDOW
        return any(ts >= cutoff and quoted in cmd.lower() and "give" in cmd.lower().split(quoted)[0]
                   for ts, cmd in reversed(self.bot.rcon.recent_commands))

    async def exempt(self, player: str) -> str | None:
        """Why this player may spawn (or None)."""
        cfg = self.bot.config
        if player.lower() in cfg.abuse_exempt_players:
            return "allowed in-game name"
        user_id = self.bot.db.player_owner(player)
        if user_id is None or not cfg.abuse_exempt_role_ids:
            return None
        guild = (self.bot.get_guild(cfg.guild_id) if cfg.guild_id else None) or \
            (self.bot.guilds[0] if self.bot.guilds else None)
        member = guild.get_member(user_id) if guild else None
        if guild is not None and member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        roles = {r.id for r in getattr(member, "roles", [])}
        allowed = roles & set(cfg.abuse_exempt_role_ids)
        if allowed:
            role = guild.get_role(next(iter(allowed)))
            return f"Discord role {role.name if role else next(iter(allowed))}"
        return None

    @commands.Cog.listener()
    async def on_rce_admin_action(self, ev) -> None:
        if ev.kind != "item" or not ev.admin_panel or not ev.player or not ev.item or not self.forbidden(ev.item):
            return
        player = ev.player
        what = f"{ev.amount:g} × {ev.item}" if ev.amount else ev.item
        now = time.monotonic()
        if any(now - seen.get(player.lower(), -PUNISH_COOLDOWN) < PUNISH_COOLDOWN
               for seen in (self._punished, self._noted)):
            return   # already handled (one spawn = several lines)
        if not self.bot.config.abuse_protection:
            self._remember(player, what, "protection switched off")
            return
        if not self.active:
            self._remember(player, what, "not punished – waiting for setup (no allowed roles/names)")
            self._note(player, f"🛡️ **{safe_md(player)}** spawned **{safe_md(what)}** with the admin panel – "
                               "**not punished: the protection is waiting for setup.** Set the allowed Discord roles "
                               "or in-game names under ⚙️ Settings → 🛡️ Admin abuse protection.")
            return
        if self.bot_gave(player):
            self._remember(player, what, "ignored – the bot itself just gave this player items")
            return
        reason = await self.exempt(player)
        if reason:
            self._remember(player, what, f"allowed ({reason})")
            self._note(player, f"🛡️ **{safe_md(player)}** spawned **{safe_md(what)}** – allowed ({safe_md(reason)}).")
            return
        self._punished[player.lower()] = now
        results = await self.punish(player, ev.item, ev.amount)
        self._remember(player, what, "PUNISHED: " + " · ".join(f"{c} → {r}" for c, r in results))

    @commands.Cog.listener()
    async def on_rce_respawn(self, ev) -> None:
        """The punished player respawned: run the on-respawn commands (e.g. global.sleep) once."""
        key = ev.player.strip().lower()
        since = self._await_respawn.pop(key, None)
        if since is None or time.monotonic() - since > RESPAWN_WAIT:
            return
        name = sanitize_player_name(ev.player.strip())
        results = await self._run_all(self.bot.config.abuse_on_respawn_commands, name)
        text = " · ".join(f"{c} → {r}" for c, r in results)
        self._remember(ev.player.strip(), "respawned", "on respawn: " + text)
        log.warning("Admin abuse: %s respawned – %s", ev.player, text)
        self.bot.audit(f"🚨 **{safe_md(ev.player)}** respawned – {safe_md(text)}")

    def _remember(self, player: str, what: str, result: str) -> None:
        self.recent.appendleft({"ts": int(time.time()), "player": player, "what": what, "result": result})

    def status(self) -> dict:
        """For the web interface: is it active, who is allowed, what happened recently."""
        cfg = self.bot.config
        guild = (self.bot.get_guild(cfg.guild_id) if cfg.guild_id else None) or \
            (self.bot.guilds[0] if self.bot.guilds else None)
        roles = []
        member_list = bool(getattr(getattr(self.bot, "intents", None), "members", False))
        for rid in cfg.abuse_exempt_role_ids:
            role = guild.get_role(rid) if guild else None
            members = list(getattr(role, "members", [])) if role else []
            linked = [p for p in (self.bot.db.linked_player(m.id) for m in members) if p]
            roles.append({"name": role.name if role else str(rid), "members": len(members) if member_list else None,
                          "linked_players": linked})
        cases = [{"id": r["id"], "player": r["player"], "what": r["what"], "status": r["status"],
                  "decided_by": r["decided_by"], "result": r["result"], "created_at": r["created_at"]}
                 for r in self.bot.db.abuse_cases(limit=10)]
        return {"enabled": cfg.abuse_protection, "active": self.active, "roles": roles,
                "players": sorted(cfg.abuse_exempt_players), "items": list(cfg.abuse_items),
                "approval_commands": list(cfg.abuse_approval_commands), "cases": cases, "recent": list(self.recent)}

    def _note(self, player: str, text: str) -> None:
        """Admin log entry for a spawn that was NOT punished (once per player and spawn)."""
        self._noted[player.lower()] = time.monotonic()
        log.warning(text.replace("**", ""))
        self.bot.audit(text)

    async def _run(self, template: str, name: str) -> tuple[str, str]:
        cmd = template.format(name=name)
        try:
            # Console Edition often doesn't answer these commands – don't wait long (and don't count it as a
            # dead connection)
            reply = (await self.bot.rcon.command(cmd, timeout=3, expect_response=False)).strip() or "sent (no reply)"
        except RconError as exc:
            reply = f"failed: {exc}"
        return cmd, reply

    async def _run_all(self, templates, name: str) -> list[tuple[str, str]]:
        """Sends all commands right away, in this order (one RCON connection, sent one after another without
        waiting for replies in between) – then collects the replies."""
        return list(await asyncio.gather(*(self._run(t, name) for t in templates)))

    async def punish(self, player: str, item: str, amount: float | None) -> list[tuple[str, str]]:
        """Removes admin, god mode off, kill (+ after-kill commands) – automatically. The approval commands
        (entity.deleteby …) become a case that staff approves or rejects. Returns (command, reply) of what ran."""
        cfg = self.bot.config
        name = sanitize_player_name(player)
        results = await self._run_all((*cfg.abuse_remove_admin_commands, *cfg.abuse_before_kill_commands,
                                       cfg.abuse_kill_command), name)
        if cfg.abuse_after_kill_commands:
            await asyncio.sleep(1)       # let the kill go through first
            results += await self._run_all(cfg.abuse_after_kill_commands, name)
        if cfg.abuse_on_respawn_commands:
            self._await_respawn[name.lower()] = time.monotonic()
        what = f"{amount:g} × {item}" if amount else item
        done = "admin removed, killed in-game"
        if cfg.abuse_on_respawn_commands:
            done += " – on respawn: " + ", ".join(t.split(" ")[0] for t in cfg.abuse_on_respawn_commands)
        case_id = None
        if cfg.abuse_approval_commands:
            case_id = self.bot.db.add_abuse_case(player, what, "\n".join(f"{c} → {r}" for c, r in results))
            done += f" – case #{case_id} waits for staff approval: " + ", ".join(
                t.format(name=name) for t in cfg.abuse_approval_commands)
        log.warning("Admin abuse: %s spawned %s – %s.", player, what, done)
        self.bot.audit(f"🚨 **{safe_md(player)}** spawned **{safe_md(what)}** without permission – {safe_md(done)}")
        await self._alert(player, what, results, case_id)
        return results

    # ------------------------------------------------------------ Staff approval (Discord buttons + web interface)

    def case_embed(self, row) -> discord.Embed:
        cfg = self.bot.config
        status = row["status"]
        colour = {"pending": discord.Colour.dark_red(), "approved": discord.Colour.dark_grey(),
                  "rejected": discord.Colour.dark_grey()}[status]
        embed = discord.Embed(title=f"🚨 Raid items spawned without permission – case #{row['id']}", colour=colour,
                              description=f"**{safe_md(row['player'])}** received **{safe_md(row['what'])}**.")
        if row["done"]:
            embed.add_field(name="Done automatically", inline=False, value="\n".join(
                f"`{safe_md(line)}`" for line in row["done"].splitlines())[:1024])
        commands_text = ", ".join(f"`{t.format(name=sanitize_player_name(row['player']))}`"
                                  for t in cfg.abuse_approval_commands) or "–"
        if status == "pending":
            embed.add_field(name="⏳ Waiting for staff", inline=False,
                            value=f"Approve to run {commands_text} (deletes everything the player owns), or reject.")
        elif status == "approved":
            embed.add_field(name=f"✅ Approved by {row['decided_by']}", inline=False,
                            value=safe_md(row["result"] or "")[:1024] or "–")
        else:
            embed.add_field(name=f"✖️ Rejected by {row['decided_by']}", inline=False, value="Nothing was deleted.")
        owner = self.bot.db.player_owner(row["player"])
        if owner:
            embed.add_field(name="Linked Discord account", value=f"<@{owner}>")
        embed.set_footer(text="The server log doesn't say who gave the item – if an admin gave it to someone else, "
                              "check who it was before approving.")
        return embed

    async def decide(self, case_id: int, approve: bool, by: str) -> tuple[bool, str]:
        """Approve (run the approval commands) or reject a pending case. Used by the buttons and the web interface."""
        row = self.bot.db.abuse_case(case_id)
        if row is None:
            return False, "Case not found."
        if not self.bot.db.decide_abuse_case(case_id, "approved" if approve else "rejected", by):
            return False, f"Case #{case_id} was already {row['status']} by {row['decided_by']}."
        text = "rejected – nothing deleted"
        if approve:
            name = sanitize_player_name(row["player"])
            results = await self._run_all(self.bot.config.abuse_approval_commands, name)
            text = " · ".join(f"{c} → {r}" for c, r in results) or "nothing to run"
            self.bot.db.set_abuse_result(case_id, text)
        row = self.bot.db.abuse_case(case_id)
        self.bot.audit(f"🚨 Case #{case_id} ({safe_md(row['player'])}) {'approved' if approve else 'rejected'} by "
                       f"{safe_md(by)}: {safe_md(text)}")
        self._remember(row["player"], f"case #{case_id}", f"{'approved' if approve else 'rejected'} by {by}: {text}")
        await self._update_message(row)
        return True, f"Case #{case_id} {'approved' if approve else 'rejected'}: {text}"

    async def _update_message(self, row) -> None:
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if channel is None or not row["message_id"]:
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.edit(embed=self.case_embed(row), view=None if row["status"] != "pending" else CaseView(self))
        except discord.HTTPException as exc:
            log.debug("Case message not updated: %s", exc)

    async def _alert(self, player: str, what: str, results: list[tuple[str, str]], case_id: int | None) -> None:
        cfg = self.bot.config
        channel_id = cfg.alert_channel_id or cfg.admin_log_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        if case_id is not None:
            embed, view = self.case_embed(self.bot.db.abuse_case(case_id)), CaseView(self)
        else:
            embed, view = discord.Embed(title="🚨 Raid items spawned without permission", colour=discord.Colour.dark_red(),
                                        description=f"**{safe_md(player)}** received **{safe_md(what)}**."), None
            embed.add_field(name="Done automatically", inline=False, value="\n".join(
                f"`{safe_md(cmd)}` → {safe_md(reply[:150])}" for cmd, reply in results)[:1024])
        roles = [discord.Object(r) for r in cfg.alert_role_ids]
        kwargs = {"view": view} if view else {}
        try:
            msg = await channel.send(content=" ".join(f"<@&{r.id}>" for r in roles) or None, embed=embed,
                                     allowed_mentions=discord.AllowedMentions(roles=roles), **kwargs)
            if case_id is not None and msg is not None:
                self.bot.db.set_abuse_message(case_id, msg.channel.id, msg.id)
        except discord.HTTPException as exc:
            log.warning("Abuse alert failed: %s", exc)


class CaseView(discord.ui.View):
    """Persistent Approve / Reject buttons – one custom_id each, the case is found via the message."""

    def __init__(self, cog: "AbuseGuard") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _decide(self, interaction: discord.Interaction, approve: bool) -> None:
        if not is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("⛔ Only bot admins can decide this.", ephemeral=True)
            return
        row = self.cog.bot.db.abuse_case_by_message(interaction.message.id) if interaction.message else None
        if row is None:
            await interaction.response.send_message("Case not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await self.cog.decide(row["id"], approve, str(interaction.user))
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + text[:1900], ephemeral=True)

    @discord.ui.button(label="Approve deletion", emoji="🗑️", style=discord.ButtonStyle.danger,
                       custom_id="rce:abuse:approve")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._decide(interaction, True)

    @discord.ui.button(label="Reject", emoji="✖️", style=discord.ButtonStyle.secondary, custom_id="rce:abuse:reject")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._decide(interaction, False)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(AbuseGuard(bot))
