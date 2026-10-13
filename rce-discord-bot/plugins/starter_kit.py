"""Starter kit: the first time a player is ever seen on the server, the bot welcomes them in-game and gives a kit.

"New" means the bot has never seen the name before (not in the player stats, which keep every name ever online
since the bot runs – also after a wipe reset). Each name gets it only once.
"""
from __future__ import annotations

import asyncio

from plugin_api import Plugin, Setting


class StarterKit(Plugin):
    NAME = "Starter kit for new players"
    ICON = "🎁"
    DESCRIPTION = ("Welcomes brand-new players in-game and gives them a one-time starter kit, so new players "
                   "don't quit after dying naked five times.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("kit", "Starter kit", "kit", "",
                help="Custom kit (bot kit) or in-game kit name. Empty = only the welcome message."),
        Setting("message", "Welcome message (in-game)", "text",
                "Welcome to {server}, {player}! Join our Discord for kits, events and giveaways.",
                help="{player} and {server} are replaced."),
        Setting("delay", "Wait after joining (seconds)", "int", 60, min=5, max=900,
                help="Players are still loading in right after joining."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending: set[str] = set()

    def is_new(self, player: str) -> bool:
        seen = self.data.get(f"seen:{player.lower()}")
        return seen == 0 or (seen is None and self.bot.db.player(player) is None)   # 0 = left before the kit

    @Plugin.listener()
    async def on_rce_player_join(self, player: str) -> None:
        if not self.is_new(player) or player.lower() in self._pending:
            return
        self.data.set(f"seen:{player.lower()}", 0)
        self._pending.add(player.lower())
        try:
            await asyncio.sleep(self.setting("delay"))
            await self.welcome(player)
        finally:
            self._pending.discard(player.lower())

    async def welcome(self, player: str) -> bool:
        name = self.online_name(player)
        if name is None:
            return False        # left again before the kit – stays "new" until the next join
        self.data.set(f"seen:{name.lower()}", self.now())
        message = self.setting("message")
        if message:
            await self.say(message.replace("{player}", name).replace("{server}", self.server_name))
        kit = self.setting("kit")
        given = await self.give_kit(name, kit) if kit else False
        if given:
            self.data.set("given", int(self.data.get("given", 0)) + 1)
        self.audit(f"New player **{self.md(name)}** welcomed" + (f" with the kit {self.md(kit)}" if given else ""))
        return True

    def status(self) -> dict:
        return {"New players welcomed": sum(1 for v in self.data.items("seen:").values() if v),
                "Starter kits given": int(self.data.get("given", 0))}
