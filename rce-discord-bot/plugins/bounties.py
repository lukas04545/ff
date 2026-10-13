"""Bounties: members put points on a player's head – whoever kills that player collects them (via the killfeed).

/bounty place <player> <amount>   points are taken right away
/bounty list                      open bounties
Rules that stop abuse: the killer needs a linked player (/customkit link) and can't be the one who placed it, a
teammate of the target or the target's own account. Bounties nobody claims are refunded after "Expires after".
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import player_choices

COLOUR = discord.Colour(0xC0392B)


class Bounties(Plugin):
    NAME = "Bounties"
    ICON = "🎯"
    DESCRIPTION = ("Members spend points to put a bounty on a player; whoever kills that player collects it "
                   "automatically. Creates rivalries and stories – unclaimed bounties are refunded.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("min_amount", "Smallest bounty", "int", 50, min=1, max=1_000_000),
        Setting("max_amount", "Biggest bounty", "int", 100_000, min=1, max=10_000_000),
        Setting("days", "Expires after (days)", "int", 7, min=1, max=60,
                help="Unclaimed bounties go back to whoever placed them."),
        Setting("channel", "Announcement channel", "channel",
                help="Empty = killfeed channel, then events channel."),
        Setting("ingame", "Announce in-game", "bool", True),
    )

    bounty = app_commands.Group(name="bounty", description="Put points on a player's head", guild_only=True)

    # ------------------------------------------------------------ Data

    def bounties(self) -> list[dict]:
        return list(self.data.get("bounties", []))

    def _save(self, rows: list[dict]) -> None:
        self.data.set("bounties", rows)

    def total_on(self, player: str) -> int:
        return sum(b["amount"] for b in self.bounties() if b["target"].lower() == player.lower())

    def claimed_count(self, user_id: int) -> int:
        return int(self.data.get(f"claimed:{user_id}", 0))

    def _announce_channel(self):
        cfg = self.bot.config
        return self.channel(self.setting("channel"), cfg.killfeed_channel_id, cfg.events_channel_id)

    # ------------------------------------------------------------ Placing

    def place(self, user_id: int, target: str, amount: int) -> tuple[bool, str]:
        target = " ".join(str(target).split())[:64]
        if not self.points_enabled:
            return False, "Points & shop are switched off."
        lo, hi = self.setting("min_amount"), self.setting("max_amount")
        if not lo <= amount <= hi:
            return False, f"A bounty must be between {self.fmt_points(lo)} and {self.fmt_points(hi)}."
        if not target:
            return False, "Which player?"
        known = self.bot.db.player(target)
        if known is None:
            return False, f"**{self.md(target)}** has never been seen on the server – check the spelling."
        target = known["name"]
        mine = self.linked_player(user_id)
        if (mine and mine.lower() == target.lower()) or self.linked_user(target) == user_id:
            return False, "You can't put a bounty on yourself."
        if self.take_points(user_id, amount, f"bounty on {target}") is None:
            have = self.bot.db.wallet(user_id)["points"]
            return False, f"Not enough {self.bot.config.points_name}: you have {self.fmt_points(have)}."
        now = self.now()
        rows = self.bounties()
        next_id = int(self.data.get("next_id", 1))
        self.data.set("next_id", next_id + 1)
        rows.append({"id": next_id, "target": target, "amount": amount, "by": user_id, "at": now,
                     "expires": now + self.setting("days") * 86400})
        self._save(rows)
        return True, target

    @bounty.command(name="place", description="Put points on a player's head – their killer gets them.")
    @app_commands.describe(player="In-game name", amount="How many points")
    async def place_cmd(self, interaction: discord.Interaction, player: str,
                        amount: app_commands.Range[int, 1, 10_000_000]) -> None:
        ok, result = self.place(interaction.user.id, player, amount)
        if not ok:
            await interaction.response.send_message(f"⚠️ {result}", ephemeral=True)
            return
        target, total = result, self.total_on(result)
        await interaction.response.send_message(
            f"🎯 Bounty placed: **{self.md(target)}** is now worth {self.fmt_points(total)}. Expires <t:"
            f"{self.now() + self.setting('days') * 86400}:R> (refunded if nobody claims it).", ephemeral=True)
        embed = discord.Embed(title=f"🎯 Bounty on {target}", colour=COLOUR,
                              description=f"{interaction.user.mention} added {self.fmt_points(amount)}.\n"
                                          f"Total: **{self.fmt_points(total)}** for whoever kills "
                                          f"**{self.md(target)}**!")
        await self.post(self._announce_channel(), embed=embed)
        if self.setting("ingame"):
            await self.say(f"<color=#e74c3c>Bounty!</color> {target} is worth {self.fmt_points(total)} – kill them "
                           "to collect (Discord /bounty).")
        self.audit(f"{interaction.user} placed a bounty of {amount} on **{self.md(target)}**")

    @place_cmd.autocomplete("player")
    async def _player_ac(self, interaction: discord.Interaction, current: str):
        return player_choices(self.bot, current)

    @bounty.command(name="list", description="Open bounties.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        totals: dict[str, list] = {}
        for b in self.bounties():
            entry = totals.setdefault(b["target"].lower(), [b["target"], 0, 0])
            entry[1] += b["amount"]
            entry[2] += 1
        rows = sorted(totals.values(), key=lambda r: -r[1])[:20]
        lines = [f"🎯 **{self.md(name)}** – {self.fmt_points(total)}" + (f" ({n} bounties)" if n > 1 else "")
                 + (" 🟢" if self.online_name(name) else "") for name, total, n in rows]
        embed = discord.Embed(title="🎯 Open bounties", colour=COLOUR,
                              description="\n".join(lines) or "No bounties right now – place one with `/bounty place`.")
        embed.set_footer(text="Kill the player to collect · needs a linked player (/customkit link)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ Claiming

    def _teammates(self, a: str, b: str) -> bool:
        found = self.bot.db.team_of(a)
        return bool(found and any(m.lower() == b.lower() for m in found[1]))

    def claim(self, killer: str, victim: str) -> tuple[int, int | None, str]:
        """Kill of `victim` by `killer`: pays eligible bounties. (amount, user_id, why-not)"""
        rows = self.bounties()
        on_victim = [b for b in rows if b["target"].lower() == victim.lower()]
        if not on_victim:
            return 0, None, ""
        user_id = self.linked_user(killer)
        if user_id is None:
            return 0, None, "not linked"
        if user_id == self.linked_user(victim) or self._teammates(victim, killer):
            return 0, user_id, "teammate"
        paid = [b for b in on_victim if b["by"] != user_id]
        if not paid:
            return 0, user_id, "own bounty"
        amount = sum(b["amount"] for b in paid)
        ids = {b["id"] for b in paid}
        self._save([b for b in rows if b["id"] not in ids])
        self.give_points(user_id, amount, f"bounty on {victim}")
        self.data.set(f"claimed:{user_id}", self.claimed_count(user_id) + 1)
        self.data.set("claimed_total", int(self.data.get("claimed_total", 0)) + 1)
        return amount, user_id, ""

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        if not ev.is_pvp or ev.killer.lower() == ev.victim.lower():
            return
        amount, user_id, why = self.claim(ev.killer, ev.victim)
        if why == "not linked":
            if self.setting("ingame"):
                await self.say(f"{ev.killer} killed {ev.victim} – link your account on Discord to collect bounties!")
            return
        if not amount:
            return
        embed = discord.Embed(title="💰 Bounty collected!", colour=COLOUR,
                              description=f"**{self.md(ev.killer)}** (<@{user_id}>) killed **{self.md(ev.victim)}** "
                                          f"and collected {self.fmt_points(amount)}.")
        await self.post(self._announce_channel(), embed=embed)
        if self.setting("ingame"):
            await self.say(f"<color=#e74c3c>{ev.killer}</color> collected the bounty on {ev.victim}: "
                           f"{self.fmt_points(amount)}!")
        self.audit(f"<@{user_id}> ({self.md(ev.killer)}) collected {amount} for killing **{self.md(ev.victim)}**")

    # ------------------------------------------------------------ Expiry

    def expire(self, now: int | None = None) -> list[dict]:
        now = self.now() if now is None else now
        rows = self.bounties()
        expired = [b for b in rows if b["expires"] <= now]
        if expired:
            self._save([b for b in rows if b["expires"] > now])
            for b in expired:
                self.bot.db.add_points(b["by"], b["amount"], f"{self.NAME}: refund, nobody killed {b['target']}")
        return expired

    @tasks.loop(minutes=10)
    async def expire_loop(self) -> None:
        for b in self.expire():
            self.log.info("Bounty #%s on %s expired – %s refunded to %s", b["id"], b["target"], b["amount"], b["by"])

    # ------------------------------------------------------------ Web / profile

    def status(self) -> dict:
        rows = self.bounties()
        return {"Open bounties": len(rows), "Points on heads": sum(b["amount"] for b in rows),
                "Collected so far": int(self.data.get("claimed_total", 0))}

    def problems(self) -> list[str]:
        return [] if self.points_enabled else ["Points & shop are switched off – bounties need points."]

    def profile_fields(self, member) -> list[tuple[str, str]]:
        out = []
        player = self.linked_player(member.id)
        if player and (total := self.total_on(player)):
            out.append(("🎯 Bounty on them", self.fmt_points(total)))
        if n := self.claimed_count(member.id):
            out.append(("🎯 Bounties collected", str(n)))
        return out
