"""Early-bird wipe reward: the first linked players online after a wipe get points and/or a kit.

Starts when the wipe manager marks the wipe (scheduled wipe time or /wipe done). Only linked players
(/customkit link) count, each Discord account once per wipe – alt accounts can't take extra rewards.
"""
from __future__ import annotations

from discord.ext import tasks

from plugin_api import Plugin, Setting


class EarlyBird(Plugin):
    NAME = "Early-bird wipe reward"
    ICON = "🐦"
    DESCRIPTION = ("The first linked players online after a wipe get points and/or a kit – gives everyone a "
                   "reason to be there at wipe time.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("minutes", "Counts for … minutes after the wipe", "int", 60, min=5, max=1440),
        Setting("players", "Number of rewards", "int", 10, min=1, max=500),
        Setting("points", "Points", "int", 100, min=0, max=1_000_000),
        Setting("kit", "Kit", "kit", "", help="Optional: custom kit or in-game kit name."),
        Setting("announce", "Announce in-game", "bool", True),
    )

    def window_open(self, now: int | None = None) -> bool:
        started = self.data.get("wiped_at")
        now = self.now() if now is None else now
        return bool(started) and now - started <= self.setting("minutes") * 60

    def rewarded(self) -> list[int]:
        return list(self.data.get("rewarded", []))

    @Plugin.listener()
    async def on_rce_wiped(self, next_wipe) -> None:
        self.data.set("wiped_at", self.now())
        self.data.set("rewarded", [])
        if self.setting("announce"):
            await self.say(f"<color=#f1c40f>Wipe is live!</color> The first {self.setting('players')} linked players "
                           "online get an early-bird reward. Link your player on Discord: /customkit link")

    async def tick(self) -> list[str]:
        if not self.window_open():
            return []
        rewarded, limit = self.rewarded(), self.setting("players")
        got = []
        for player in self.online_players():
            if len(rewarded) >= limit:
                break
            user_id = self.linked_user(player)
            if user_id is None or user_id in rewarded:
                continue
            rewarded.append(user_id)
            self.data.set("rewarded", rewarded)
            parts = []
            if self.setting("points") and self.give_points(user_id, self.setting("points"), "early bird") is not None:
                parts.append(self.fmt_points(self.setting("points")))
            if self.setting("kit") and await self.give_kit(player, self.setting("kit")):
                parts.append(f"kit {self.setting('kit')}")
            got.append(player)
            if self.setting("announce"):
                await self.say(f"<color=#f1c40f>Early bird #{len(rewarded)}:</color> {player}"
                               + (f" gets {' + '.join(parts)}!" if parts else "!"))
            self.audit(f"Early bird #{len(rewarded)}: <@{user_id}> ({self.md(player)}) – {', '.join(parts) or 'no reward set'}")
        return got

    @tasks.loop(seconds=30)
    async def reward_loop(self) -> None:
        if self.bot.rcon.connected:
            await self.tick()

    def problems(self) -> list[str]:
        if not self.setting("kit") and not (self.setting("points") and self.points_enabled):
            return ["Set points (with points & shop on) or a kit as the reward."]
        return []

    def status(self) -> dict:
        return {"Window open now": "yes" if self.window_open() else "no",
                "Rewarded this wipe": len(self.rewarded())}
