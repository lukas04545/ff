"""Low-pop bonus: extra playtime points while only a few players are online – pays people to "seed" the server.

Nobody joins an empty server. While fewer than "Bonus below" players are online, linked players earn their
playtime points (POINTS_PER_HOUR) "Multiplier" times. The bonus starts and ends with an in-game message.
"""
from __future__ import annotations

import time
from collections import defaultdict

from discord.ext import tasks

from plugin_api import Plugin, Setting


class LowPopBonus(Plugin):
    NAME = "Low-pop bonus"
    ICON = "🌱"
    DESCRIPTION = ("Multiplies playtime points while the server is nearly empty, so players come online early and "
                   "fill it up. Announced in-game when it starts and ends.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("below", "Bonus while fewer than … players are online", "int", 10, min=2, max=500),
        Setting("multiplier", "Multiplier", "int", 2, min=2, max=10, help="2 = double playtime points."),
        Setting("announce", "Announce start/end in-game", "bool", True),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.active = False
        self._remainder: defaultdict[int, float] = defaultdict(float)
        self._last_tick: float | None = None
        self._last_announce: float | None = None

    def bonus_active(self, count: int) -> bool:
        return 1 <= count < self.setting("below")

    async def tick(self, elapsed: float) -> int:
        """Pays the extra points for `elapsed` seconds. Returns the points paid."""
        per_hour = self.bot.config.points_per_hour * (self.setting("multiplier") - 1)
        players = self.online_players()
        active = self.points_enabled and per_hour > 0 and self.bonus_active(len(players))
        if active != self.active:
            self.active = active
            await self._announce(active)
        if not active:
            return 0
        paid = 0
        for player in players:
            user_id = self.linked_user(player)
            if user_id is None:
                continue
            self._remainder[user_id] += elapsed * per_hour / 3600
            earned = int(self._remainder[user_id])
            if earned:
                self._remainder[user_id] -= earned
                self.bot.db.add_points(user_id, earned, "low-pop bonus", log=False)
                paid += earned
        if paid:
            self.data.set("paid", int(self.data.get("paid", 0)) + paid)
        return paid

    async def _announce(self, active: bool) -> None:
        if not self.setting("announce") or (self._last_announce is not None
                                            and time.monotonic() - self._last_announce < 600):
            return
        self._last_announce = time.monotonic()
        x = self.setting("multiplier")
        if active:
            await self.say(f"<color=#2ecc71>Low-pop bonus active:</color> {x}x playtime {self.bot.config.points_name} "
                           f"while fewer than {self.setting('below')} players are online! (link on Discord)")
        else:
            await self.say("Low-pop bonus over – the server is filling up, thanks for seeding!")

    @tasks.loop(seconds=60)
    async def bonus_loop(self) -> None:
        now = time.monotonic()
        elapsed, self._last_tick = (min(now - self._last_tick, 120) if self._last_tick else 0), now
        if elapsed and self.bot.rcon.connected:
            await self.tick(elapsed)

    def problems(self) -> list[str]:
        if not self.points_enabled:
            return ["Points & shop are switched off – the bonus pays points."]
        if not self.bot.config.points_per_hour:
            return ["Points per hour online (Settings → Points & shop) is 0 – there is nothing to multiply."]
        return []

    def status(self) -> dict:
        return {"Bonus active now": "yes" if self.active else "no",
                "Bonus points paid": int(self.data.get("paid", 0))}
