# Writing a plugin

A plugin adds a feature to the bot without changing the bot's own code. It is **one Python file** with one class
derived from `Plugin`. Put it in `data/plugins/` (or upload it in the web interface → 🧩 Plugins), switch it on,
done. Updates never overwrite your plugins, and they are included in backups.

> A plugin runs inside the bot with full access (Discord, RCON, database). Only install plugins you wrote yourself
> or got from someone you trust.

**Don't want to write Python?** Open the **✨ Create plugin** page in the web interface: describe the feature in
plain words and the integrated AI writes the plugin from the API below. You review (and can edit) the code in the
window before it is created, and it is saved switched off like any plugin. Because the generated code runs inside
the bot, read it before you create it.

## The smallest plugin

`data/plugins/hello.py` – the file name (a–z, 0–9, `_`) is the plugin's ID:

```python
from plugin_api import Plugin, Setting


class Hello(Plugin):
    NAME = "Hello"
    ICON = "👋"
    DESCRIPTION = "Greets players in-game when they join."
    SETTINGS = (
        Setting("text", "Greeting", default="Welcome back, {player}!", help="{player} is replaced."),
    )

    @Plugin.listener()
    async def on_rce_player_join(self, player: str):
        await self.say(self.setting("text").replace("{player}", player))
```

On the Plugins page it shows up with an on/off switch and a form for its settings. Change the file and click
**↻ Reload** to load the new version – no restart.

## What a plugin can use

A plugin is a [discord.py Cog](https://discordpy.readthedocs.io/en/stable/ext/commands/cogs.html), so slash
commands (`@app_commands.command`, `app_commands.Group`), listeners and loops work as usual:

```python
from discord import app_commands
from discord.ext import tasks

class Example(Plugin):
    NAME = "Example"

    @app_commands.command(name="hello", description="Says hi")
    async def hello(self, interaction):
        await interaction.response.send_message("Hi!", ephemeral=True)

    @tasks.loop(minutes=10)          # started when the plugin is switched on, stopped when it's switched off
    async def every_ten_minutes(self):
        ...
```

Slash commands are registered with Discord automatically when the plugin is switched on or off.

### Class attributes

| Attribute | Meaning |
|---|---|
| `NAME`, `ICON`, `DESCRIPTION`, `VERSION`, `AUTHOR` | Shown on the Plugins page |
| `SETTINGS` | Tuple of `Setting(key, label, kind, default, help=…, min=…, max=…, choices=…)`. Kinds: `text`, `textarea`, `int`, `bool`, `choice`, `channel`, `role`, `kit`, `image` (uploaded on the Plugins page) |
| `INTENTS` | Privileged intents it needs: `("members",)` and/or `("message_content",)` – the page tells the owner what to switch on |

### Events

Game events from the server log (`ev` objects from `rce/log_parser.py`):

| Listener | Called when |
|---|---|
| `on_rce_player_join(name)`, `on_rce_player_leave(name)` | A player joins/leaves (player list) |
| `on_rce_kill(ev)` | Kill line – `ev.killer`, `ev.victim`, `ev.killer_type`, `ev.is_pvp` |
| `on_rce_suicide(ev)`, `on_rce_respawn(ev)` | Suicide / respawn |
| `on_rce_chat(ev)` | Quick chat – `ev.player`, `ev.text` |
| `on_rce_team(ev)`, `on_rce_ban(ev)`, `on_rce_role(ev)` | Team changes, bans, admin/moderator changes |
| `on_rce_admin_action(ev)`, `on_rce_server_event(ev)` | Item spawns/kits/commands, airdrop/heli/cargo … |
| `on_rce_console(msg)` | Every raw console message (`msg.text`) |
| `on_rce_connection(connected)` | RCON connected/disconnected |
| `on_rce_wiped(next_wipe)` | The wipe manager marked a wipe (`next_wipe` = Unix time or `None`) |

…plus every discord.py event (`on_member_join`, `on_message`, …).

### Helpers (`self.…`)

| Helper | |
|---|---|
| `setting(key)` | Current value of a setting (or its default) |
| `file(key)` | Path of the picture uploaded for an `image` setting (or `None`) |
| `data.get(key, default)`, `data.set(key, value)`, `data.delete(key)`, `data.items(prefix)` | The plugin's own storage (any JSON value, survives restarts and updates) |
| `await say(text)` | In-game chat message |
| `await give_kit(player, kit)` | Custom kit (bot kit) or in-game kit |
| `give_points(user_id, n, reason)`, `take_points(user_id, n, reason)`, `fmt_points(n)` | Points (if Points & shop is on) |
| `online_players()`, `online_name(player)` | Who is online |
| `linked_user(player)`, `linked_player(user_id)` | Discord account ↔ in-game player (`/customkit link`) |
| `channel(id, *fallbacks)`, `await post(channel, text, embed=…, ping_role=…)` | Send a message (only the given role is pinged) |
| `audit(text)` | Line in the admin log channel |
| `guild`, `server_name`, `next_wipe()`, `tz`, `now()`, `md(text)` | Discord server, server name, next wipe, wipe time zone, Unix time, escape Markdown |
| `plugin(id)` | Another running plugin (or `None`) |
| `bot` | Everything else: `bot.db`, `bot.rcon.command(...)`, `bot.config`, `bot.get_cog(...)` |
| `log` | Logger (`logs/bot.log`) |

### Optional methods

| Method | |
|---|---|
| `async on_load()` / `async on_unload()` | Switched on / off (also at bot start and stop) |
| `async on_settings_changed()` | Settings saved in the web interface |
| `problems() -> list[str]` | Setup hints shown on the card ("Choose a role") |
| `status() -> dict` | A few numbers shown on the card |
| `profile_fields(member) -> [(name, value)]` | Extra fields in `/profile` |
| `async web_public(request, path)` | Public web page at `/p/<plugin id>/<path>` (no login – never show secrets) |

The built-in plugins in `plugins/` are complete examples – `plugins/starter_kit.py` is a short one to start from.
