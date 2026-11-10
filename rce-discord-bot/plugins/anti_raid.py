"""Discord anti-raid and anti-bot protection.

  Join wave: more than N members joining within M seconds = a raid. Staff get an alert (admin log or your channel),
  and for the next minutes new accounts younger than the minimum age are kicked or timed out (your choice).
  Minimum account age (always, optional): brand-new accounts (a typical bot or alt) are kicked / timed out at join.

Needs the Server Members intent (Discord Developer Portal → Bot → Privileged Gateway Intents).
"""
from __future__ import annotations

import datetime
import time
from collections import deque

import discord

from plugin_api import Plugin, Setting

ACTIONS = ("alert", "timeout", "kick")
VERB = {"alert": "only flagged", "timeout": "timed out", "kick": "kicked"}


class AntiRaid(Plugin):
    NAME = "Anti-raid"
    ICON = "🛡️"
    DESCRIPTION = ("Spots join waves (raids) on your Discord and alerts staff; keeps brand-new accounts (bots, alts) out "
                   "while a raid is going on – or always.")
    INTENTS = ("members",)
    SETTINGS = (
        Setting("joins", "Raid = more than … joins", "int", 8, min=3, max=200),
        Setting("seconds", "… within seconds", "int", 30, min=5, max=600),
        Setting("raid_minutes", "Raid mode lasts … minutes", "int", 15, min=1, max=240),
        Setting("raid_action", "During a raid, new young accounts are …", "choice", "timeout", choices=ACTIONS),
        Setting("min_age_days", "Young account = younger than … days", "int", 7, min=0, max=365),
        Setting("always_action", "Young accounts outside a raid are …", "choice", "alert", choices=ACTIONS,
                help="alert = only a log line; timeout / kick = always keep brand-new accounts out."),
        Setting("timeout_minutes", "Timeout length (minutes)", "int", 60, min=1, max=40320),
        Setting("channel", "Alert channel", "channel", help="Empty = the admin log."),
    )

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.joins: deque = deque(maxlen=500)
        self.raid_until = 0.0

    def young(self, member, now: float | None = None) -> bool:
        days = self.setting("min_age_days")
        if not days or getattr(member, "created_at", None) is None:
            return False
        age = (now or time.time()) - member.created_at.timestamp()
        return age < days * 86400

    async def _act(self, member, action: str, why: str) -> str:
        try:
            if action == "kick":
                await member.kick(reason=f"Anti-raid: {why}")
                return "kicked"
            if action == "timeout":
                await member.timeout(datetime.timedelta(minutes=self.setting("timeout_minutes")),
                                     reason=f"Anti-raid: {why}")
                return "timed out"
        except discord.HTTPException as exc:
            return f"couldn't act ({exc})"
        return "flagged"

    async def _alert(self, text: str) -> None:
        ch = self.channel(self.setting("channel"))
        if ch is not None:
            await self.post(ch, f"🛡️ {text}")
        else:
            self.audit(text)

    @Plugin.listener()
    async def on_member_join(self, member) -> None:
        if getattr(member, "bot", False):
            return
        now = time.time()
        self.joins.append(now)
        window = self.setting("seconds")
        recent = [t for t in self.joins if now - t <= window]
        if len(recent) > self.setting("joins") and now >= self.raid_until:
            self.raid_until = now + self.setting("raid_minutes") * 60
            self.data.set("raids", int(self.data.get("raids", 0)) + 1)
            await self._alert(f"**Raid detected:** {len(recent)} joins in {window} s – raid mode for "
                              f"{self.setting('raid_minutes')} min (new accounts are {VERB[self.setting('raid_action')]}).")
        if not self.young(member, now):
            return
        in_raid = now < self.raid_until
        action = self.setting("raid_action") if in_raid else self.setting("always_action")
        result = await self._act(member, action, "raid" if in_raid else "account too new")
        self.data.set("caught", int(self.data.get("caught", 0)) + (result in ("kicked", "timed out")))
        age = max(0, int((now - member.created_at.timestamp()) // 86400))
        await self._alert(f"New account {member.mention} ({self.md(str(member))}, {age} day(s) old) "
                          f"{'during a raid ' if in_raid else ''}– {result}.")

    def status(self) -> dict:
        return {"Raid mode": "ON" if time.time() < self.raid_until else "off",
                "Raids detected": int(self.data.get("raids", 0)), "Accounts kept out": int(self.data.get("caught", 0))}
