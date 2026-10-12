# RCE Discord Bot – Rust Console Edition (G-Portal) via WebRCON

A self-hosted Discord bot for **Rust Console Edition**. It connects to your G-Portal server using
nothing but **IP, RCON port and RCON password**, similar to Kaosbot or Helios.

> **Language:** The whole bot is in English: slash commands, Discord messages, the in-game chat
> prefix and the web interface.

> **Status: quick prototype.** The code is fully functional and has been tested against a
> simulated RCE server (`tools/mock_rcon_server.py`, `tests/`). It has **not** been tested against
> a **real** G-Portal server. What it assumes about one is listed in the section
> [Assumptions](#assumptions).

---

## Why Python + discord.py?

- **discord.py 2.x** is the most mature Python library for Discord. Slash commands,
  autocomplete, embeds and background tasks are built in.
- discord.py ships with **aiohttp**, which already contains a WebSocket client – exactly what
  WebRCON needs. No extra RCON library required.
- Python is easy to read and adapt. That matters, because the Console Edition log format can
  change with game updates.

---

## What is technically possible on Console Edition at G-Portal?

| Interface | Available? | Use in the bot |
|---|---|---|
| **WebRCON** (WebSocket, `ws://IP:PORT/PASSWORD`, JSON) | ✅ Yes. G-Portal itself points to Facepunch's WebRCON client. | The bot's only connection |
| Console commands via RCON (`serverinfo`, `playerlist`, `say`, `kick`, …) | ✅ Yes | Status, player list, admin commands |
| **Live console output** over the same WebSocket connection | ✅ Yes | Killfeed, quick chat, bans, teams, events |
| Oxide/uMod plugins, custom server hooks | ❌ No, there is no modding on console | – |
| Log files via FTP | ❌ Not available for RCE servers | – |
| G-Portal web API | ⚠️ Unofficial, needs your account login, can change at any time | deliberately **not** used |
| Free-text chat in-game | ❌ RCE has **only quick chat** (preset phrases) | Phrases are translated into readable text |

Research basis: the source code of the community library
[rce.js](https://www.npmjs.com/package/rce.js) (v4.6.1, Nov 2025). Kaosbot, Helios and other
RCE tools use exactly this WebRCON interface. Also G-Portal's wiki article
["How to connect to your Rust CE server via RCON"](https://www.g-portal.com/wiki/de/so-verbindest-du-dich-ueber-rcon-mit-deinem-rust-ce-server/)
(in German) and the [public Helios listing](https://top.gg/bot/1327703273410658324).

---

## Features

### ✅ Implemented (prototype)

| Feature | How | Notes |
|---|---|---|
| **Status embed**, edited live | `serverinfo` + `playerlist` | One message in the status channel, refreshed every `STATUS_INTERVAL` s. The message ID is stored and survives a restart. |
| **Bot activity** "Watching 12/50 players" | `serverinfo` | When the connection drops: "Server offline" (status "Do Not Disturb") |
| `/status`, `/players` | as above | Player list with time online and ping |
| **Joins/leaves** | Two consecutive `playerlist` queries are compared | Delay = `PLAYERLIST_INTERVAL`. After a (re)connect the list is synced silently, so there is no flood of join messages. |
| **Chat bridge game → Discord** | Log lines `[CHAT LOCAL/TEAM] Name : Phrase` | Quick-chat identifiers are turned into readable text ("I need scrap.") |
| **Chat bridge Discord → game** | `say` | With prefix, 3 s cooldown per user. Rich-text tags are stripped so nobody can inject formatting. |
| **Killfeed** | Log line `Victim was killed by Killer` | Distinguishes PvP, natural causes (hunger, fall …), NPCs (wolf, scientist, heli …) and traps/turrets |
| **Server events** | `[EVENT …]` lines | Airdrop, cargo ship, Chinook, patrol heli, seasonal events |
| **Team events** | Team log lines | Created, joined, left, leader changed |
| **Admin log** | Log lines + bot audit | Bans/unbans, group changes, kits/items, console commands, *who* used which admin command in Discord, RCON connection status |
| **Console log** | all live lines | Batched as a code block. The bot's own queries are filtered out. |
| **Admin commands** | `/kick`, `/ban`, `/unban`, `/say`, `/rcon` | Only for `ADMIN_ROLE_IDS`/`ADMIN_USER_IDS`. Player name autocomplete. Long `/rcon` output is sent as a file. |
| **Leaderboard & stats** | own SQLite database | `/leaderboard` (kills, deaths, K/D, playtime), `/stats <player>` incl. platform (Xbox/PlayStation from the respawn line) |
| **Kit manager** | Console Edition's built-in kit commands | List kits, give them to players, auth groups or everyone (with confirmation), add/remove items, history. See [Kit manager](#kit-manager). |
| **Custom kits** | Bot database → `inventory.giveto` per item | Kits that exist **only in the bot** and are invisible in the in-game kit manager. See [Custom kits](#custom-kits). |
| **Autokits** | Respawn log line or quick-chat phrase → `kit givetoplayer` or custom kit | Per-rule cooldown and per-player limit, wipe reset. Quick-chat kits confirm in-game via `say`. |
| **Support tickets with AI** | Discord + [DeepSeek platform](https://platform.deepseek.com) | Private ticket channels, the AI answers from your knowledge base and pings staff when needed. See [Support tickets](#support-tickets-with-ai-deepseek). |
| **Community** | Discord | Welcome message, auto role, `/ping`, `/rust`, `/rules` (rules from `rules.md`). See [Community & moderation](#community--moderation). |
| **Discord moderation** | Discord | `/mod timeout/untimeout/kick/ban/unban/purge` with permission and role-hierarchy checks |
| **Discord log** | Discord | Joins/leaves, deleted and edited messages, moderation, giveaways |
| **Giveaways** | Discord + database | Enter via button, automatic draw, multiple winners, winner role, reroll; **survive restarts** |
| **Points & shop** | Bot database + RCON | Points for playtime, PvP kills and `/daily` (streaks), playtime rewards, `/shop` with kits, items and (timed) roles, `/profile`. See [Points & shop](#points--shop). |
| **Wipe manager** | Bot + Discord + `say` | Next wipe with countdown (status message, `/rust`, `/wipe info`), posts 24 h / 1 h before, in-game warnings, optional resets. See [Wipe manager](#wipe-manager). |
| **Scheduled messages** | `say` + Discord | Messages every N minutes in-game and/or in a channel (`/schedule`, web) |
| **Reports & watchlist** | Discord + player list | `/report` for members, staff pinged with a ✅ Handled button; watchlist pings staff when a watched player joins |
| **Team leaderboard** | Team log lines | `/teams top`, `/teams of` – teams named after their leader, current members' stats summed up |
| **Population graph** | `serverinfo` every 5 min | Players online over 24 h / 7 / 30 days and the busiest hours (web interface → Overview) |
| **Admin abuse protection** | Item spawn log line + RCON | Someone not allowed spawns rockets/C4 → admin removed, killed with `global.killplayer`, staff pinged. See [Admin abuse protection](#admin-abuse-protection). |
| **Timed roles** | Discord + database | `/temprole`: roles that end by themselves (e.g. VIP for 7 days) – also for giveaway prizes. See [Timed roles](#timed-roles-eg-vip-for-7-days). |
| **Alerts & backups** | Bot | Staff ping when the server is offline, daily backups. See [Alerts & backups](#alerts--backups). |
| **Automatic updates** | GitHub | Installs new versions by itself. See [Automatic updates](#automatic-updates). |
| **Auto-reconnect** | exponential backoff 5 s → 120 s | A dead connection is detected after 3 unanswered commands. Pending commands get a clear error message. |

### 🔜 Technically possible, but not in this prototype

According to their listings, Helios and Kaosbot offer these features. They also run on RCE
console commands and log lines. The exact command syntax could not be verified reliably for this
prototype, so they are not built in:

- **More in-game "commands" via quick chat** (e.g. TP home): the principle is already in place
  with the quick-chat autokits and can be applied to other commands.
- **Shop/economy, teleports, zones**: via commands such as `teleport…` or `zones.…`. Try them with
  `/rcon` first, then add them as your own slash command.
- **Scheduled messages/events**: a simple `tasks.loop` with `say` or event commands.
- **Player count in the channel name**: Discord only allows 2 renames per 10 minutes, so the
  status embed is the better solution here.

### ❌ Not possible on Console Edition (with the next best alternative)

| Kaosbot/Helios-style feature or PC feature | Why not | Alternative in the bot |
|---|---|---|
| Free-text chat in-game ↔ Discord | RCE has no free chat | Quick-chat phrases are translated. Discord → game via `say` works. |
| Weapon, distance, hit zone in the killfeed | Not in the kill line | The killer type (player/NPC/trap/nature) is shown |
| Plugins (Oxide/uMod/Carbon), custom hooks, custom UI | No modding on console | Only built-in console commands and log lines |
| Stable player IDs (SteamID/XUID/PSN ID) | Logs and `playerlist` only provide the display name | Stats per name. A name change creates a new entry. |
| Real-time join/leave messages from the log | No reliable log line | Comparing player lists (delay ≤ `PLAYERLIST_INTERVAL`) |
| Stats from before the bot was started | RCON has no history | Counting starts when the bot starts |
| Live map, player positions, raid detection via server hooks | No map or hook API over RCON | – (Helios' raid alerts: data source can't be verified, so **not** rebuilt) |
| Wipe, restart, server settings | Done in the G-Portal panel, not via RCON | Use the G-Portal web interface |

---

## Assumptions

Every assumption can be adjusted without touching the rest of the code:

1. **Connection:** `ws://<RCON_HOST>:<RCON_PORT>/<RCON_PASSWORD>`. This is the standard Rust
   WebRCON format and is used the same way by rce.js.
2. **Log format** (file `rce/log_parser.py`, dictionary `PATTERNS`), e.g.:
   - `Victim was killed by Killer` / `Name was suicide by Suicide`
   - `[CHAT LOCAL] Name : d11_quick_chat_responses_slot_5`
   - `Name [SCARLETT] has entered the game` (SCARLETT = Xbox)
   - `[Admin] Added [Name] to [Banned]`, `[Admin] Added [Name] to Group [Role]`
   - `[Name] has joined [Leader]s team, ID: [123]`
   - `[EVENT] … event_cargoship …`

   These patterns come from rce.js as of November 2025. If Facepunch changes the format, you only
   need to adjust the affected regex. With `CONSOLE_LOG_CHANNEL_ID` you can see the real lines of
   your server.
3. **`playerlist` and `serverinfo` answer with JSON** (fields `DisplayName`, `Ping`,
   `ConnectedSeconds` and `Hostname`, `Players`, `MaxPlayers`, `Queued`, …).
4. **The kick/ban/unban syntax is NOT verified.** The defaults are `kick "{name}"`,
   `banid "{name}"` and `unbanid "{name}"`. Check the commands once via `/rcon` or in the
   G-Portal console and adjust `KICK_/BAN_/UNBAN_COMMAND_TEMPLATE` in the `.env` if needed.
   Whether a ban worked is shown by the log line `Added [Name] to [Banned]` in the admin log.
5. **Kit commands:** `kit list` and `kit info` and their output format come from rce.js.
   `kit givetogroup`, `kit giveall` and `kit remove "Kit" "ID"` are documented in the official
   [RCE community server docs](https://rust-console-edition.gitbook.io/community-servers/feature-guides/kit-management).
   `kit givetoplayer "Kit" "Player"` and the exact argument order of `kit add` come from a
   third-party guide and are **not verified**. That's why every kit command that changes something
   is configurable as `KIT_*_TEMPLATE` in the `.env`. It's unclear whether the item ID needed for
   `kit remove` appears in the output of `kit info`; the bot shows it if it finds it, otherwise the
   raw output.
6. **Item giving for custom kits:** `inventory.giveto` is listed in the RCE community docs. The
   argument order `"Player" "shortname" amount` is only taken from Rust PC and is **not
   verified**. Configurable via `ITEM_GIVE_TEMPLATE`. The log line `giving Player 1 x item`
   (confirmed by rce.js) shows up in the admin log and confirms every item given.
7. Some commands (`say`, `kick`, …) return no response on RCE. The bot does not treat that as an
   error.
8. Quick-chat identifiers and their readable texts live in `rce/quickchat.py`, the names of
   causes of death in `rce/kill_sources.py`. Both can be extended freely.

---

## Project structure

```
rce-discord-bot/
├── bot.py                 # Entry point: bot, event dispatch, error handling
├── config.py              # Loads and validates the .env
├── storage.py             # SQLite: leaderboard, playtime, status message ID
├── utils.py               # Permission checks, text sanitizing, batched sending
├── rce/
│   ├── rcon_client.py     # WebRCON client with auto-reconnect
│   ├── log_parser.py      # Regexes for console lines  ← adjust the log format here
│   ├── kill_sources.py    # Causes of death (nature/NPC/trap)
│   ├── kits.py            # Parsing of kit list / kit info / getauthlevels
│   ├── items.py           # Common item shortnames (autocomplete)
│   ├── diagnostics.py     # RCON connection test (web interface + rcon_test.bat)
│   └── quickchat.py       # Quick-chat translation
├── cogs/
│   ├── status.py          # Status embed, activity, /status, /players, join/leave
│   ├── feeds.py           # Chat bridge, killfeed, events, admin/console log
│   ├── admin.py           # /kick /ban /unban /say /rcon
│   ├── stats.py           # /leaderboard /stats
│   ├── kits.py            # Kit manager: /kit … and autokits
│   ├── custom_kits.py     # Custom kits: /customkit … (bot only)
│   ├── tickets.py         # Support tickets with AI: /ticket …
│   ├── community.py       # /ping /rust /rules, welcome, auto role, Discord log
│   ├── moderation.py      # /mod timeout/kick/ban/unban/purge (Discord members)
│   ├── giveaways.py       # /giveaway … (persisted in the database)
│   ├── timed_roles.py     # /temprole …: roles that end by themselves
│   ├── monitor.py         # Server-down alerts, daily backups
│   ├── economy.py         # Points, /shop, /daily, playtime rewards, /profile
│   ├── wipe.py            # /wipe: countdown, announcements, resets
│   ├── schedule.py        # /schedule: messages every N minutes
│   ├── reports.py         # /report and /watch
│   ├── abuse.py           # Admin abuse protection (raid items spawned without permission)
│   ├── kit_requests.py    # Bought custom kits: AI request → staff approve button → kit role
│   ├── store.py           # Tip4Serv purchases (polled every minute) → automatic approval
│   └── updates.py         # Automatic updates: checks GitHub regularly, restarts after an update
├── support/
│   ├── deepseek.py        # Client for the DeepSeek platform API
│   ├── pricing.py         # Price list for custom kits (prices.txt)
│   ├── prompt.py          # System prompt, history, parsing the AI reply
│   └── tip4serv.py        # Tip4Serv store connection (signed requests, rcebot commands)
├── support_knowledge.md   # Knowledge base for the AI  ← fill in yourself
├── rules.md               # Rules for /rules  ← adjust
├── prices.txt             # Custom kit price list (Tip4Serv auto-approve)  ← adjust
├── web/
│   ├── server.py          # Web interface: login + JSON API (runs inside the bot process)
│   └── static/            # Frontend (HTML/CSS/JS, no external files)
├── logbuffer.py           # Recent log lines for the web interface
├── settings_schema.py     # All settings for the form (label, type, help, validation)
├── envfile.py             # Read/write .env (comments are preserved)
├── updater.py             # Downloads + installs new versions from GitHub (ZIP install or git checkout)
├── backups.py             # Backup ZIPs in data/backups/
├── start.bat              # Windows: double-click = set up + start + auto-restart (also after updates)
├── start.sh               # Linux/macOS: the same
├── rcon_test.bat          # Windows: test the RCON connection step by step (tools/rcon_test.py)
├── tools/mock_rcon_server.py  # Simulated RCE server for testing
├── tests/                 # pytest: parser, RCON client, kits, tickets, web interface
├── logs/bot.log           # Log file (created automatically)
├── requirements.txt
└── .env.example
```

---

## Installation & startup

### 1. Requirements

- **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/); on Windows, tick
  "Add Python to PATH" during installation)
- A computer that stays on for as long as the bot should run – e.g. **your own PC** (see
  [Hosting on your PC](#hosting-on-your-pc)) or a VPS. The bot only makes **outgoing**
  connections to Discord, the G-Portal server and DeepSeek – you do **not** need to forward any
  ports on your router.

### 2. Create the Discord bot in the Developer Portal

1. Open <https://discord.com/developers/applications> → **New Application** → give it a name.
2. Click **Bot** on the left:
   - **Reset Token** → copy the token. This is your `DISCORD_TOKEN`. Never share it!
   - Under **Privileged Gateway Intents**, enable:
     - **Message Content Intent** – for the chat bridge, AI tickets and the message log.
     - **Server Members Intent** – for the welcome message, auto role and join/leave log.
     - Click **Save Changes**.

     If you don't use these features (channels left empty in the `.env`), the bot doesn't
     request the intents either.
3. Click **OAuth2 → URL Generator** on the left:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: *View Channels*, *Send Messages*, *Embed Links*, *Attach Files*,
     *Read Message History*, *Add Reactions*, *Manage Channels* (tickets), *Manage Roles*
     (auto role, winner role), *Timeout Members* (timeout), *Kick Members*, *Ban Members*,
     *Manage Messages* (purge), *Mention @everyone, @here and All Roles* (staff ping).
     All of these together are the permission integer **`1099780320342`** – the invite link the
     web interface generates (⚙️ Settings → "Check bot token") uses exactly this value.
   - In your server settings, drag the **bot role** above the roles it should assign or
     moderate.
   - Open the generated URL and invite the bot to your Discord server.
4. Copying IDs: Discord → User Settings → Advanced → turn on **Developer Mode**. Then
   right-click a server, channel or role → "Copy ID".

### 3. Find your RCON details at G-Portal

1. Log in at [g-portal.com](https://www.g-portal.com) → **My servers** → open your Rust Console
   server.
2. **IP and RCON port** are shown in the server overview or in the connection/port information.
   The RCON port is **not** the game port.
3. The **RCON password** is under **Settings → Basic settings** (field "RCON password"). Change
   it, save, and restart the server for it to take effect. Ideally use only letters and digits,
   because the password is part of the URL.
4. **Test the connection:** According to the G-Portal wiki, only a WebRCON client works with
   RCE. With the [Facepunch WebRCON client](http://facepunch.github.io/webrcon/) you can check
   `IP:port` and the password. If it works there, it works with the bot.

> The menu names in the G-Portal panel may change slightly. You're always looking for: IP, RCON port, RCON password.

### 4. Set up the bot

**Windows (easiest way):** Double-click **`start.bat`**. On first start the script creates the
Python environment and installs everything. Then your **browser opens with the setup page**:
enter the bot token, server IP, RCON port, RCON password and your Discord ID, click
"Test RCON", then **"Save & start bot"**. Done – no text editor needed.

If no browser opens, the bot window shows a link like
`http://localhost:8080/auth?token=…` – just open it in your browser.

**Linux/macOS:** Run `./start.sh` and open the link from the window in your browser.

> If required settings are missing or the Discord token is invalid, the bot no longer exits.
> Instead it starts in **setup mode** (settings page only). You can still edit the `.env` by
> hand – the web interface writes to the same file.

**Manual (all systems):**

```bash
cd rce-discord-bot
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# open .env and fill in: DISCORD_TOKEN, GUILD_ID, RCON_*, ADMIN_ROLE_IDS, channel IDs
```

Required: `DISCORD_TOKEN`, `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` and at least one of
`ADMIN_ROLE_IDS` or `ADMIN_USER_IDS`. Every channel is optional. If it's empty, the matching
feature is off. Recommended: create `CONSOLE_LOG_CHANNEL_ID` and `ADMIN_LOG_CHANNEL_ID` as
**private** channels.

### 5. Start

`start.bat` (Windows) or `./start.sh` (Linux/macOS) – or manually `python bot.py`.

The window should show `RCON connected.`, `Logged in as …` and `Web interface running:
http://localhost:8080`; the browser opens the web interface automatically. With `GUILD_ID` set,
the slash commands are available immediately. Without `GUILD_ID` they are registered globally,
which can take up to an hour.

The start scripts restart the bot 10 seconds after a crash. On configuration errors (wrong
token, missing settings) they stop instead and show the message – fix the `.env` first, then
start again.

---

## Hosting on your PC

The bot runs fine on a normal Windows, Linux or Mac PC:

- **No port forwarding needed.** The bot only makes outgoing connections (Discord, G-Portal
  RCON, DeepSeek). By default the web interface is only reachable on this PC.
- **The PC has to stay on.** If it's off or in **sleep/hibernate**, the bot is offline. On
  Windows: *Settings → System → Power & battery → Screen and sleep → put the device to sleep
  "Never"* (at least when plugged in). Turning the screen off is fine.
- **Keep the window open.** The black `start.bat` window *is* the bot. Close it = bot off.
  Minimizing is fine.
- **Start automatically with Windows:** `Win + R` → `shell:startup` → Enter. Put a **shortcut**
  to `start.bat` into the folder that opens (right-click `start.bat` → *Create shortcut* → move
  the shortcut there). The bot then starts every time you sign in.
  (Without signing in: *Task Scheduler* → *Create Basic Task* → trigger "When the computer
  starts" → program `start.bat`, "Start in" = bot folder.)
- **Linux:** `./start.sh` in a `tmux`/`screen` session or as a systemd service
  (`ExecStart=/path/rce-discord-bot/start.sh`, `Restart=always`).
- **Resources:** The bot only needs about 100–150 MB RAM and hardly any CPU – it runs fine in
  the background.
- **Logs:** everything is also written to `logs/bot.log` (rotated automatically, max. ~8 MB).
- **Backups:** the bot saves one by itself every day and before every automatic update (see
  [Alerts & backups](#alerts--backups)). For a full copy (e.g. to move to another PC) you need
  `.env`, `data/` (database, web logins, backups) and `support_knowledge.md`.

---

## Automatic updates

The bot keeps itself up to date: every 5 minutes it checks the `main` branch of
[lukas04545/ff](https://github.com/lukas04545/ff) on GitHub. When there is a new version it installs it and
restarts – about 10 seconds offline. The admin log channel shows `🔄 Update installed: <version> …`.

- **Never touched:** `.env`, `data/` (database, web logins), `logs/`, `bin/`, `.venv/` and
  `PUBLIC_LINK.txt`. `rules.md` and `support_knowledge.md` are only replaced if you never edited them.
- **Safe:** the new version is checked first (Python syntax; new packages are installed with pip). If
  anything fails, nothing is changed and the bot keeps running the old version – the reason is in the bot
  window and under ⚙️ Settings → 🔄 Updates.
- **ZIP install** (downloaded from GitHub): the changed files are replaced, files removed in the new version
  are deleted (only files that came with an earlier update, never your own).
- **Git checkout** (`git clone`): the bot runs `git fetch` + a fast-forward merge. If you changed bot files
  yourself, it doesn't touch them and asks you to update with `git pull`.
- **Restart:** start the bot with `start.bat` / `start.sh` – they start the new version right away. (Started
  with `python bot.py`, the bot replaces its own process instead.)
- **Settings** (⚙️ Settings → 🔄 Updates): turn it off (`AUTO_UPDATE=false`), change how often it checks
  (`AUTO_UPDATE_INTERVAL`, minutes) or the source (`AUTO_UPDATE_REPO`, `AUTO_UPDATE_BRANCH`, advanced).
  **Check now** shows the installed version and updates immediately.
- The public Cloudflare link **stays the same** across automatic updates: the bot leaves the tunnel running
  and the new version takes it over. It only changes when you close the bot window (or the PC restarts).

> **One-time step:** versions from before this feature can't update themselves. Update once by hand
> (close the bot window, copy the new files over the old ones – including `start.bat` – and start it again).
> From then on it updates by itself.

---

## Web interface

The web interface runs **inside the bot** – no extra program, no extra packages. After startup
your browser opens automatically; otherwise go to **<http://localhost:8080>**.

| Page | Features |
|---|---|
| 📊 Overview | Server status (players, map, uptime, FPS), **population graph** (24 h / 7 / 30 days, busiest hours), online players with **kick/ban**, unban, message to the game |
| ⌨️ Console | **Live server console** and running any RCON command (if `ALLOW_RAW_RCON=true`) |
| 🎒 Kits | Give kits (player/everyone), view in-game kits, create/edit **custom kits**, create/pause/delete **autokits**, wipe reset, give history |
| 🧰 Kit builder | **Visual kit builder** (like the Helios kit builder): all 566 RCE items by category with search, 7 wear / 24 inventory / 6 belt slots, click or drag items in, drag to move/swap, amount & condition per slot. Generates the `kit add` commands (copy), **creates the in-game kit on the server** or **saves it as a custom kit**. Load existing kits from the server, save kits in the browser, export/import share codes, import pasted `kit add` commands |
| 🪙 Shop | Shop items (custom kit, in-game kit, item, role – optionally for N days), prices, on sale/hidden; all point balances with ± correction; how members earn points |
| 🚩 Reports | Admin abuse cases (approve/reject deletion), player reports (open/all, mark handled) and the watchlist (add/remove) |
| 🎫 Tickets | All tickets with status, **transcripts of closed tickets**, DeepSeek balance |
| 🏆 Stats | Leaderboard (kills, deaths, K/D, playtime), platform, last seen; **team leaderboard** |
| 🎉 Community | **Wipe** (next date, repeat, wipe done), **scheduled messages**, timed roles, giveaways (end early, reroll), **edit rules**, overview of the community settings |
| 📚 Knowledge base | Edit `support_knowledge.md` right in the browser (applies immediately) |
| 🧾 Bot log | The bot's last 500 log lines |
| ⚙️ Settings | **All settings** from the `.env` as a form: channels and roles as dropdowns (once the bot is connected), **Test RCON**, **Check bot token** (incl. intent status and invite link). **Saved settings apply right away** – only the bot token, Discord server, database file, web interface address/port and a newly needed privileged intent need **Restart bot now** (the page says which) |

Everything you do in the web interface is posted to the admin log channel in Discord
("🌐 Web interface: …"). The interface also works on your phone.

**Settings in the `.env`:**

| Variable | Default | Meaning |
|---|---|---|
| `WEB_PASSWORD` | empty | Login password. **Empty = sign in via the link in the bot window** (or the random password shown there). Best to set your own – you can also do that under ⚙️ Settings. |
| `WEB_HOST` | `127.0.0.1` | `127.0.0.1` = this PC only. `0.0.0.0` = also phone/laptop on the **same home network** via `http://<your-PC-IP>:8080` (Windows then asks once for firewall access → allow "Private networks"). |
| `WEB_PORT` | `8080` | Port, in case 8080 is already in use |
| `WEB_OPEN_BROWSER` | `true` | Open the browser automatically on startup |
| `WEB_ENABLED` | `true` | Turn the web interface off completely |

**Security:** Password login (max. 5 attempts per minute), sessions expire after 12 hours,
protection against other websites (CSRF), all player and console texts are displayed as plain
text only. However, the web interface has **no HTTPS** – **never expose it to the internet via
port forwarding on your router**. For access on the go, use a VPN such as Tailscale or WireGuard
into your home network instead.

### Accounts for other admins

The server owner logs in with username **`owner`** and the `WEB_PASSWORD`. Under **👥 Users** (owners
only) you create accounts for your other admins – username + password (🎲 generates a strong one) +
role:

| Role | Can do |
|---|---|
| **Admin** | Everything (overview, console, kits, kit builder, tickets, stats, community, …) **except** Settings, bot restart and user management |
| **Owner** | Everything, including settings and managing accounts |

- Passwords are stored only as scrypt hashes in `data/web_users.json` (never in plain text).
- Admins change their own password via **🔑 My password** in the sidebar. The owner can reset
  passwords, change roles, disable or delete accounts – disabling/deleting/resetting logs that person
  out immediately.
- Every action in the web interface is written to the admin log channel **with the username**.
- Login protection: max. 5 attempts per minute per IP and per account.

**How other admins reach the web interface** (it runs on your PC):

- **From anywhere – built-in Cloudflare tunnel (on by default):** just start the bot. It sets up a free,
  encrypted Cloudflare tunnel by itself (downloads Cloudflare's `cloudflared` once into `bin/`, ~40 MB,
  from Cloudflare's official GitHub releases) and prints your link in a box in the bot window:
  ```
  ========================================================================
    🌍 YOUR WEB INTERFACE LINK – works from anywhere (phone, other PCs):
       https://quiet-fox-123.trycloudflare.com
    Login: owner / <your password>
  ========================================================================
  ```
  If you haven't set a `WEB_PASSWORD`, the bot creates a strong one on the first start, saves it in the
  `.env` and shows it in that box. Nothing to open on your router; the web interface itself still only
  listens on your PC. To turn it off: Settings → Web interface → *Reachable from the internet* → Off.
  - The link is also saved in **`PUBLIC_LINK.txt`** (next to `start.bat`), shown as a card on the
    **Overview** (with a copy button) and posted in your **admin log channel** – so your admins always find
    the current one.
  - It stays the same while the bot runs (also across restarts from the web interface) and **changes when
    you close the bot window**. For a **fixed address** (needs your own domain on Cloudflare): create a
    tunnel in the Cloudflare dashboard (Zero Trust → Networks → Tunnels), point its public hostname to
    `http://localhost:8080`, then paste the tunnel **token** into *Cloudflare tunnel token* and the
    address into *Fixed public address* (advanced settings).
  - The address is public, so give every admin their own account with a strong password (👥 Users) –
    logins are rate-limited per visitor IP and per account.
  - macOS: install it once with `brew install cloudflared`.
- **Same home network only:** Settings → Web interface → *Reachable from* → "Also home network
  (0.0.0.0)", restart the bot. Others open `http://<your PC's local IP>:8080` (find it with `ipconfig`).
- **Alternative:** [Tailscale](https://tailscale.com/) (private network between your devices).
- **Don't** forward the port on your router: the web interface speaks plain HTTP, so passwords would
  travel unencrypted over the internet. The tunnel gives you HTTPS instead.

### Testing without a real server

```bash
python tools/mock_rcon_server.py --port 28016 --password test123
# in .env: RCON_HOST=127.0.0.1  RCON_PORT=28016  RCON_PASSWORD=test123
python bot.py
```

The mock server generates sample kills, quick chats, events and joins/leaves every few seconds.
Run the automated tests with `pip install pytest && pytest`.

---

## Commands

| Command | Who | What it does |
|---|---|---|
| `/status` | everyone | Server status as an embed |
| `/players` | everyone | Online players with time online and ping |
| `/leaderboard [category]` | everyone | Top 10 by kills, deaths, K/D or playtime |
| `/stats <player>` | everyone | A player's stats |
| `/kick <player> [reason]` | Admins | Kick a player |
| `/ban <player> [reason]` | Admins | Ban a player |
| `/unban <player>` | Admins | Lift a ban |
| `/say <message>` | Admins | In-game server message. Rich text allowed, e.g. `<color=red>Wipe today!</color>` |
| `/rcon <command>` | Admins | Any console command. Can be disabled with `ALLOW_RAW_RCON=false`. |
| `/kit list`, `/kit info <kit>`, `/kit autokits` | everyone | Show kits and autokits |
| `/kit give`, `/kit group`, `/kit all` | Admins | Give a kit to a player, an auth group or everyone |
| `/kit add-item`, `/kit remove-item` | Admins | Edit kits |
| `/kit autokit-add`, `/kit autokit-role`, `/kit autokit-toggle`, `/kit autokit-delete`, `/kit wipe-reset`, `/kit history` | Admins | Manage autokits (also with custom kits, optionally only for a Discord role) |
| `/customkit create`, `add-item`, `remove-item`, `delete` | Admins | Create and edit custom kits |
| `/customkit import`, `/customkit import-file` | Admins | Create/fill custom kits from kit builder `kit add` commands (text box or .txt file) |
| `/customkit list`, `/customkit info` | Admins | Show custom kits (visible to admins only) |
| `/customkit give`, `/customkit all` | Admins | Give a custom kit to one player or to all online players |
| `/customkit link` | Everyone | Link your Discord account to your in-game player (verified via quick chat) |
| `/customkit claim` | Members with an allowed role | Claim a custom kit to your own linked player (cooldown per player) |
| `/customkit access`, `/customkit cooldown`, `/customkit reset-cooldown`, `/customkit unlink` | Admins | Who may claim, how often, reset cooldowns, remove links |
| `/ticket open`, `/ticket close` | everyone | Open a ticket or close your own ticket |
| `/ticket ai`, `/ticket add` | Staff | Turn the AI on/off in a ticket, add a member |
| `/ticket panel`, `/ticket ai-status`, `/ticket ai-test` | Admins | Post the ticket panel, show DeepSeek balance, test the AI |
| `/aiconfirm message [approve]` | Staff | Tell the ticket AI something (e.g. purchase verified) without pausing it; optionally approve the ticket's kit requests |
| `/ping`, `/rust`, `/rules` | everyone | Bot/RCON latency, server info (platform, wipe, live status), rules |
| `/mod timeout`, `/mod untimeout` | "Timeout Members" | Time out a Discord member (max. 28 days) or remove the timeout |
| `/mod kick`, `/mod ban`, `/mod unban` | "Kick Members"/"Ban Members" | Kick/ban a Discord member (with a DM to the user), unban by user ID |
| `/mod purge` | "Manage Messages" | Delete 1–100 messages, optionally only from one user |
| `/giveaway start/end/reroll/list` | "Manage Server" | Start, end early, reroll and list giveaways |
| `/daily` | Members with a linked player | Collect daily points (more for days in a row) |
| `/shop list`, `/shop buy <item>` | everyone | See the shop and buy with your points – delivered in-game to your linked player |
| `/points balance`, `/points top`, `/points history` | everyone | Your balance, the top 10, your last point changes |
| `/points give`, `/points take` | Admins | Give or take points |
| `/profile [member]` | everyone | Linked player, PvP stats, playtime, points, /daily, custom kits with cooldowns, timed roles |
| `/wipe info` | everyone | Next wipe with countdown |
| `/wipe set`, `/wipe clear`, `/wipe done` | Admins | Plan the next wipe (with repeat), clear it, or mark it as done now (runs the resets) |
| `/schedule add/list/toggle/remove` | Admins | Messages posted automatically every N minutes in-game and/or in Discord |
| `/report <player> <reason>` | everyone | Report an in-game player to the staff |
| `/watch add/remove/list` | Admins | Get pinged when a watched player joins |
| `/teams top [category]`, `/teams of <player>` | everyone | Team leaderboard and a player's team |
| `/temprole give/remove/list` | Admins | Give a Discord role for a limited time (`7d`, `12h`, `2w` …), take it away early, list running ones |

Admin rights for **server commands** (RCON, kits, ticket management) are given only to the
roles or users in `ADMIN_ROLE_IDS`/`ADMIN_USER_IDS`. Discord's own permissions such as
"Administrator" deliberately do **not** count automatically there. `/mod` and `/giveaway`, on the
other hand, only affect Discord and therefore use the normal **Discord permissions** (just like
Discord itself).

> **Heads-up, two kinds of kick/ban:** `/kick` and `/ban` act on **in-game players** (RCON).
> `/mod kick` and `/mod ban` act on **Discord members**. Every admin action is logged in the
> admin log together with the Discord user who ran it.

---

## Kit manager

The kit manager uses Console Edition's **built-in kit system**. No plugins needed. Kits you
created in the in-game kit UI or via the console show up in the bot right away.

**Managing kits**

- `/kit list` and `/kit info <kit>` show kits and their contents, split into hotbar, inventory
  and clothing.
- `/kit give <kit> <player>` gives a kit to a player, `/kit group <kit> <group>` to an auth group
  (groups come from `getauthlevels`). `/kit all <kit>` asks for confirmation via a button first.
- `/kit add-item <kit> <item> [amount] [condition] [container]` adds an item. A new kit name
  creates a new kit. `/kit remove-item <kit> <item_id>` removes an item.
- Deleting kits by command is not documented for Console Edition. Use the in-game kit UI for
  that.

**Autokits**

Autokits are given out by the bot itself. The rules are stored in the bot database:

| Trigger | Detection | Typical use |
|---|---|---|
| 🔄 Respawn | Log line `Name [..] has entered the game` | Starter kit after every death, e.g. with a 30 min cooldown |
| 💬 Quick chat | The player sends the chosen phrase, e.g. "Here, take this!" | Kit on request, e.g. once per wipe (`max_per_player=1`) |

Examples:

```
/kit autokit-add kit:starter trigger:"🔄 Respawn" cooldown_minutes:30
/kit autokit-add kit:raid trigger:"💬 Quick chat" phrase:"Here, take this!" max_per_player:1
/kit autokit-add kit:vipspawn trigger:"🔄 Respawn" required_role:@VIP   ← spawn kit only for a Discord role
/kit autokit-role rule:3 role:@VIP     ← set/change the required role of an existing autokit (empty = everyone)
/kit wipe-reset          ← after the wipe: reset cooldowns and limits
```

**Role-locked autokits (e.g. a VIP spawn kit):** an autokit can require a Discord role. The game only
knows the in-game name, so each player links it to their Discord account once with
`/customkit link` (pick their online player from a dropdown). On respawn the bot gives the kit
only if the linked Discord account has the role. Unlinked players and members without the role
don't get it (for quick chat kits they get a short in-game hint). Role changes on Discord apply
within 2 minutes. Works without the Server Members Intent. Also in the web interface: Kits →
Autokits → "Required role" column / new-autokit form.

- Since Console Edition has no free chat, quick chat is the only way for a player to request
  something in-game. The player name comes from the server log, so it can't be spoofed.
- For quick-chat kits the bot reports in-game via `say` whether the kit was given or how long the
  cooldown still runs (at most every 30 s per player). Turn this off with
  `KIT_CLAIM_ANNOUNCE=false`.
- On RCE, kits given by command are **one-off** and are lost on death. For permanent kits per
  group, use the in-game kit groups.
- Every kit given ends up in the admin log and in `/kit history`. If giving fails (e.g. because
  the connection is down), it doesn't count as given.

---

## Custom kits

Custom kits are kits that live **only in the bot's database**. They are not created in the
server's kit system; instead they are handed out item by item via `inventory.giveto`.
That means:

- They **don't show up in the in-game kit manager**. Players and in-game admins can't see,
  claim or edit them there.
- `kit list` on the server doesn't know them. `/kit list` in Discord doesn't show them either.
  The overview and contents are only available via `/customkit list` and `/customkit info`, and
  both are restricted to admins and only visible to you (ephemeral).
- There is no server-side limit of 128 kits, because these kits take up no space in the
  in-game kit manager.

**Creating and giving**

```
/customkit create name:Eventkit description:"Tournament loadout"
/customkit add-item kit:Eventkit item:rifle.ak amount:1
/customkit add-item kit:Eventkit item:ammo.rifle amount:128
/customkit give kit:Eventkit player:YourName      ← test on yourself first
/customkit all kit:Eventkit                       ← with confirmation button
```

**As an autokit:** `/kit autokit-add kit:Eventkit trigger:"🔄 Respawn" kit_type:"🔒 Custom kit (bot only)"`. The
autocomplete marks custom kits with 🔒. If you don't choose a `kit_type` and the kit only exists as a
custom kit, this is detected automatically. Cooldown, limit, quick-chat trigger and
`/kit wipe-reset` work exactly as with in-game kits.

**Import from a kit builder:** paste the `kit add` commands a kit builder generates – the bot strips
`kit add`, creates the kit if needed and adds every item:
```
/customkit import                                   ← opens a text box; paste the commands
/customkit import-file file:kit.txt                 ← or upload them as a .txt file (big kits)
/customkit import kit:VIP replace:False             ← optional: other kit name / append instead of replace
```
Example line: `kit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main` → kit "Custom kit 1",
item `ammo.grenadelauncher.buckshot`, amount 1. Condition and container are ignored (custom kit items go
into the inventory). Several kits in one paste are fine. By default a re-import **replaces** the kit's
items; its claim roles and cooldown stay. Max. 60 items per kit. Same in the web interface:
Kits → Custom kits → "📥 Import from kit builder".

**Members claim it themselves (by role):** give one or more roles access to a kit and set a cooldown.
Each member links their in-game player once; after that `/customkit claim` always sends the kit to
**their own** player – nobody can claim for someone else.
```
/customkit access kit:Eventkit role:@VIP                ← admins: allow a role (allow:False removes it)
/customkit cooldown kit:Eventkit duration:12h           ← admins: 30m, 12h, 1d, 1d 12h, 0 = no cooldown
/customkit link player:YourGamertag                     ← members, once: be online in-game
/customkit claim kit:Eventkit                           ← members: goes to the linked player
/customkit reset-cooldown kit:Eventkit player:Name      ← admins: claim again right away (empty = everyone)
/customkit unlink member:@Name                          ← admins: remove a link (e.g. gamertag changed)
```
- **Linking with a dropdown:** `/customkit link` (or the **🔗 Link my in-game player** button that
  `/customkit link-panel` posts in a channel) shows a dropdown of the online players; the member picks
  theirs and is linked right away. They can also type it: `/customkit link player:Name`. Each in-game player
  can be linked to only one Discord account, and changing the link needs an admin (`/customkit unlink`) –
  otherwise members could hop between friends' players to claim kits again (the cooldown counts per player).
  ⚠️ Nothing proves the player is really theirs. To make players prove it, turn on **Linking: also verify
  with quick chat in-game** (`LINK_VERIFY_QUICKCHAT`, Settings → Kits): after picking, they send two requested
  quick chat phrases in-game within 5 minutes. Linked players of the abuse-protection *allowed roles* may spawn
  rockets/C4 – keep the *allowed in-game names* list for that if you leave the check off.
- The **cooldown counts per player** (per kit). Linking the same player to another Discord account
  doesn't reset it. A changed cooldown applies immediately, also to earlier claims.
- New kits start with **no claim roles** (admins only) and a **24 h** cooldown.
- The player must be **online in-game** when claiming – nothing gets lost.
- The autocomplete of `/customkit claim` only shows kits the member's roles allow.
- Web interface: Kits → custom kit → "🙋 Claim by role" (role checkboxes + cooldown), and the
  "🔗 Linked players" list with an Unlink button.
- Every link and claim appears in the admin log; claims also in the kit history.

**Differences from in-game kits (limitations of `inventory.giveto`)**

| | In-game kit | Custom kit |
|---|---|---|
| Visible in the in-game kit manager | yes | **no** |
| Container (hotbar/clothing) and condition can be set | yes | no: everything goes into the inventory, condition 100 % |
| Full inventory | server behaviour | Surplus items may drop on the ground |
| Giving | 1 command | 1 command per item (max. 60 items per kit) |

The bot can't check whether an item shortname exists, because RCON doesn't provide an item list.
The autocomplete suggests known shortnames. Test new kits on yourself once. In the admin log you
can see the server's confirmation line for each item (`🎁 Name receives 1 × rifle.ak`).

---

## Support tickets with AI (DeepSeek)

Players open a ticket with a button. The bot creates a **private channel** for it that only the
player, the support roles and the bot can see. An AI assistant (DeepSeek) answers right away
from your knowledge base. If it can't help, it **pings the support roles**.

**When is staff pinged?** (once per ticket; after that only via the button, at most every 10 min)

- The AI decides a human is needed: ban appeal, cheater report, payment, lost items, question
  not covered by the knowledge base, user wants a human …
- The user clicks **🔔 Call staff**.
- The AI is unreachable (e.g. no balance left) or the reply limit per ticket has been reached.
- Without a `DEEPSEEK_API_KEY`, every new ticket goes straight to staff.

**The AI learns from staff answers:** when a ticket is closed, the bot sends the staff answers of that ticket
(including `/aiconfirm` messages; the player's messages only as context) to the AI and asks for **general, new**
information – rules, how something works, prices, times – that isn't in the knowledge base yet. It's added to
`support_knowledge.md` under **"Learned from tickets"** (at most 5 per ticket, each marked with the ticket number)
and posted in the admin log, so the AI knows it in the next tickets. Nothing personal or case-specific (names,
IDs, order numbers, refunds or other individual decisions), no admin commands, and never what only a player said.
Check the section now and then on the web Knowledge base page and edit or delete entries freely. When the
knowledge base reaches the AI's reading limit (12,000 characters) nothing more is added. Switch:
**Settings → Tickets & AI → AI learns from staff answers** (`TICKET_AI_LEARN`).

**Talk to the AI without pausing it – `/aiconfirm`:** staff can tell the ticket AI something with
`/aiconfirm message:"Both purchases are verified, file the second kit"`. The bot posts it as
*"🛡️ Staff confirmation – Name: …"*, the AI treats it as true, answers the player and **stays active** (it even
switches back on if a staff message had paused it). For 30 minutes the AI may then file what the "one purchase =
one kit" checks would otherwise block (a second kit, an order ID used before); those requests are marked
"allowed by staff via /aiconfirm". With `approve:True` the command also approves all open kit requests of the
ticket (only for the approver roles). Staff-only command.
You can also let the AI check a payment: `/aiconfirm message:"check the receipt ID and approve if you find it"` –
the AI looks the order up among the Tip4Serv purchases the bot received (tool `verify_payment`). If it's there,
unused and not paid by another Discord account, the AI approves the request (logged as "Name (via AI,
/aiconfirm)"). If the bot never received that order, the AI says so and you approve with the button.

When a staff member writes in the ticket, **the AI pauses automatically**. Use
`/ticket ai enabled:true` to turn it back on. On close, a **transcript** is posted as a text
file to `TICKET_LOG_CHANNEL_ID`, the user gets a DM, and the channel is deleted.

**Auto-close when resolved:** when the AI has answered and the player hasn't written for **5 minutes**
(setting *Ask "Is it resolved?" after*, `TICKET_RESOLVE_MINUTES`, `0` = off), the AI asks *"❓ Is your issue
resolved?"* with **✅ Yes, close the ticket** / **✖️ No, I still need help** / **📌 Keep it open** buttons. Yes – or a short typed answer
like "yes", "yep thanks", "ja", "resolved" – closes the ticket (transcript and DM as usual, reason "Resolved").
Anything else keeps it open and the AI carries on. It asks once per answer and never while staff has taken over,
staff was called, or a kit purchase in the ticket waits for approval.
The player doesn't have to wait for the question: if they write on their own that it's solved ("all fixed,
thanks", "it's resolved", "you can close it"), the AI says goodbye and closes the ticket right away. Questions
like "is it resolved?" or "not resolved yet" don't count. A ticket with a kit purchase that still waits for
approval stays open. `/ticket ai-test` shows "Would close the ticket" when the AI would close it.
**Keeping a ticket open:** if the player asks for it ("keep it open", "don't close it yet, I'll test later") or
presses **📌 Keep it open** under the question, the ticket is marked *kept open*: no more "Is it resolved?"
questions, and the AI only closes it when the player explicitly asks to close it later. This is saved, so it
survives restarts.

**Security:** The AI can only reply and call staff. It has **no access to RCON**, so it can't
give kits, unban anyone or run commands, no matter what's written in the ticket. It only gets the
knowledge base and the live status (online/offline, player count) – no player list and no custom
kits. Replies are sent without @mentions.


**Kits in tickets:** the AI can also help with kits – for the person who opened the ticket only
(setting *AI may check and claim kits for the ticket owner*, on by default):
- "Which kits can I get?" → it lists the custom kits their **Discord roles** allow, what's inside, when
  they **last claimed** each one and when they can claim again (shown as a live Discord countdown), plus
  role-locked autokits such as a VIP spawn kit and whether they have the role.
- "Give me my VIP kit" → it **claims the kit for them**: sent in-game to their linked player, with a ✅
  confirmation in the ticket and an entry in the admin log.
- It follows exactly the same rules as `/customkit claim` (role, cooldown, linked + online player) and
  can never claim for anyone else or give other kits – so nobody can talk it into more than they could
  claim themselves. Not linked yet? It explains `/customkit link`.

**Bought custom kits:** when a player writes in their ticket that they bought a custom kit (e.g. "I bought the Ak
Kit, Tebex order #1234"), the AI files a **purchase request**: the bot posts *"🛒 Kit purchase – staff approval
needed"* in the ticket with the kit contents and what the player said, pings the support roles and shows
**✅ Approve** / **✖️ Reject** (staff only). To let only certain roles decide purchases (e.g. a "Store team"), set
**Roles that approve kit purchases** (`KIT_APPROVER_ROLE_IDS`, Settings → Tickets): then those roles are pinged
instead, get access to the ticket automatically, and only they and bot admins can press the buttons. Nothing is given before that. On **Approve** the bot gives the kit a
Discord role – the kit's claim role if it has one, otherwise an existing role with the kit's name (e.g. your
"Ak Kit" role), otherwise it **creates a new role** named like the kit – links it as the kit's claim role and gives
it to the buyer (for N days if the purchase was time-limited, via timed roles). The player then claims the kit with
`/customkit claim` (linked player, cooldown as usual). Every request can only be decided once and survives restarts;
everything is in the admin log. The bot needs *Manage Roles* to create and give roles.

**Bought kits with their own item list:** if a player bought a kit by listing items ("M249 x2, 50 rockets, 50k hqm,
10 armoured doors …") instead of naming an existing kit, the AI turns the list into Rust shortnames and exact amounts
(`50k hqm` → `metal.refined ×50000`), looks up unclear items in the item list and asks the player about anything
still ambiguous (e.g. what "metal kit" means). It then files a **new custom kit request**: the staff message lists
every item with its shortname and amount – check them! On **Approve** the bot **creates the custom kit** with those
items (named as the player asked, or "<name> kit", numbered if taken), creates its role and gives it to the buyer as
above. You can edit the kit later in the web interface → Kits. If staff approves but the role can't be given (e.g.
missing permission), the request stays open and approving again reuses the kit that was already created.

**Tip4Serv: automatic purchase check.** The bot can see your [Tip4Serv](https://tip4serv.com) store's purchases
and approve paid custom kits by itself. Two ways – use one (or both, payments are counted once):

*A) Webhook (recommended, instant):* web interface → **Shop → 💳 Tip4Serv purchases** shows (to the owner) a
secret webhook URL. On tip4serv.com → Dashboard → **Create webhook URL for live payments** paste it and tick
*Payment successful* (plus *Payment refunded* / *Payment refused* – a refunded payment can't confirm a kit any more
and the admin log warns if it already did). No product command needed; the receipt's order number arrives with the
payment. Needs the web interface's public address; a quick-tunnel address changes when the bot window is closed –
then update the URL on Tip4Serv, or set up a fixed address (`WEB_TUNNEL_TOKEN` + `WEB_PUBLIC_URL`).
Connecting Tip4Serv's Discord bot (`/tiplink`) lets buyers log in with Discord, so payments carry their Discord ID –
the safest match.

*C) Tip4Serv's Discord bot:* if Tip4Serv's own bot posts purchases in a channel (e.g. *"Thank you
<@{discord_id}> for your {total_paid} {currency} donation !"*), pick that channel in **Settings → 💳 Tip4Serv
store → Channel with Tip4Serv purchase messages** (`TIP4SERV_CHANNEL_ID`). The bot reads the buyer's @mention, the
amount and the currency from every **bot** message there (members can't fake it), also messages posted while it
was offline (last 100). Matching is by Discord ID – the safest way. Needs the Message Content intent. If the
message has no order number, the same purchase arriving by webhook/server key within 15 minutes (same buyer and
amount) is counted once.

*B) Server key:*
1. On tip4serv.com add a **server for the bot** with the **plugin connection** (game Rust / Oxide – *not*
   "Connect with RCON", and *not* the store API key from Dashboard → API keys) and copy its **key** (three parts
   separated by dots, the first is the server's number) into
   **Settings → 💳 Tip4Serv store → Tip4Serv server key** (`TIP4SERV_KEY`).
2. Give your custom kit product (the one where players pay the total) this command **on that server**:
   `rcebot {discord_id} {total_paid} {currency} {PlayerName}`. Tip4Serv fills in the buyer's Discord ID, the amount,
   the currency and the in-game name; markers it can't fill are ignored.
3. The bot asks Tip4Serv for new purchases every minute, stores each one once and marks it as executed on Tip4Serv.
   Web → Shop → **💳 Tip4Serv purchases** lists them and the open kit requests.

**If a payment isn't found:** `/tip4serv status` shows whether the connection works, when it last checked, how
many purchases arrived and the latest ones; `/tip4serv find order:1110065` checks whether the bot received a certain
order (the order number is compared with everything Tip4Serv sent about the payment); `/tip4serv assign order:…
member:@…` tells the bot whose payment it is – their open request is checked again right away (staff / approver
roles). If purchases don't reach the bot at all (no key, connection error, or no `rcebot` command on the product),
requests say so ("Can't check Tip4Serv: …") and the AI tells the player the team checks by hand – instead of
waiting for a payment that never comes. Purchases made **before** the `rcebot` command was set on the product
never reach the bot, so `/tip4serv assign` can't help with those – approve them with the button or `/aiconfirm`.

When a player files a **new custom kit** in a ticket, the AI prices the item list with the **price list**
(`prices.txt`, editable on web → Shop → **🧾 Custom kit price list**; preset from your pricing sheet: rockets, C4,
AK kits with armour, resources, components, doors, teas, … plus the kit caps and the claim timer options 30h/24h/12h/
6h) and tells the player the total. If an unused payment of that player covers the price – also several payments
together – the request is **approved automatically** (`TIP4SERV_AUTO_APPROVE`): the kit is created with the bought
claim timer, gets its role and the player gets it. If the payment comes later, the approval happens as soon as it
arrives. Each payment confirms only one request; payments older than 30 days don't count.
Staff still decides (the request shows what was found, e.g. "Paid €30 – price €42.50 – €12.50 missing") when the
payment is too low, an item has no price, or the kit is an existing one. Over the caps, the AI asks the player to
lower the amounts. A payment matches the player by **Discord ID** (`{discord_id}`) or – if *Match payments by
in-game name* is on (`TIP4SERV_NAME_MATCH`, default) – by their linked player or the name they used at checkout.
⚠️ Names aren't verified (linking has no in-game check), so for real money turn name matching off and let Tip4Serv
collect the Discord ID. Only the owner can change the price list.

**One purchase = one kit:** each Tip4Serv payment confirms only one request (marked as used in the same database
step). An order/transaction ID the player gives (e.g. `tbx-1a2b3c4d`) can only be used for one purchase request –
a second ticket with the same ID gets "order already used". A member can only have one new custom kit waiting for
approval at a time. And if staff tries to approve a request whose payment was used by another request in the
meantime, the bot refuses ("one purchase gives one kit") and shows the current payment state instead.

**Cancelling duplicates:** the AI can cancel the ticket owner's own waiting requests (`cancel_kit_request`) – when
the player asks, when a staff confirmation says so ("remove the old request"), or when it filed a corrected
version. The request shows *🚫 Cancelled* and its buttons disappear; nothing is given. `/aiconfirm approve:True`
only approves when exactly **one** request is open in the ticket – with several, approve the right one with its
button. Order numbers written like "order ID 1110065" or "#1110065" also count for the one-purchase-one-kit check.

**Add-ons for a bought kit:** the member who originally bought a custom kit (through a ticket) can buy extra items
for it later ("add 3 more M249 and 10 C4 to my kit"). The AI files a **➕ Kit add-on** request with only the new
items; it's priced with the price list (add-ons keep the kit's claim timer, no timer is charged again), the caps
count the kit's total after adding, and it's approved automatically by a covering Tip4Serv payment or by staff.
On approval the items are added to the kit – amounts of items it already has go up. Anyone else gets "only the
original buyer can add to this kit"; kits made by hand have no buyer, so only staff changes those.

**Custom kits won in a giveaway:** the player says they won it – the AI files the same request marked
**🎉 Giveaway prize**: no payment check, staff always approves it with the buttons.

**Giveaway prizes in tickets:** the AI knows the ticket owner's giveaway wins from two sources:
- the bot's own **`/giveaway`** (straight from the database), and
- another bot that announces winners in a channel (setting *Channel with giveaway winners*, preset to
  this server's channel, `0` = off): Helios ("Giveaway Winner" embed with a *Prize* field) or Kryvon RCE
  ("🎉 Congratulations @a, @b! You won Vip Standard!"). Winners are matched by their @mention (or the
  name in the message); only messages from bots count.

**Automatic prize tickets:** when a `/giveaway` ends (or is rerolled), every winner automatically gets a
**🎉 Giveaway prize** ticket (setting *Prize ticket for /giveaway winners*, `GIVEAWAY_AUTO_TICKET`, on by
default; needs tickets). The AI congratulates them and redeems the prize right away – or calls staff.
If the winner already has an open ticket, the prize is posted there instead of opening a second one.
Without a DeepSeek key the ticket simply pings staff.

What the AI does with a win:
- **Lifetime kit prize** (prize text names one of your kits and says *lifetime / permanent / perm /
  forever*): the AI gives the player that kit's **Discord role** – the custom kit's claim role or the
  required role of a role-locked autokit – so they can claim it from then on. Each win can be redeemed
  **only once per winner**; it's posted in the ticket and the admin log.
- **Everything else** (store credit, a kit for a limited time, unknown prize): the AI tells the player and
  calls staff with the prize and a link to the winner message.
- The bot needs *Read Message History* in that channel and *Manage Roles* with its role above the kit roles.

### Setup

1. **DeepSeek platform:** Create an account at [platform.deepseek.com](https://platform.deepseek.com),
   top up your balance under [Usage/Billing](https://platform.deepseek.com/usage) (the API is
   paid, billed per token), then create a key under
   [API Keys](https://platform.deepseek.com/api_keys) → `DEEPSEEK_API_KEY`.
2. **Model:** The default is `DEEPSEEK_MODEL=deepseek-flash`. DeepSeek retired the old names
   `deepseek-chat`/`deepseek-reasoner` in July 2026. If you get
   "HTTP 400/422 – is DEEPSEEK_MODEL correct?", enter the current model name from the
   [API docs](https://api-docs.deepseek.com).
3. **Discord:** create a category for tickets → `TICKET_CATEGORY_ID`. A private log channel →
   `TICKET_LOG_CHANNEL_ID`. The support role(s) → `SUPPORT_ROLE_IDS`.
4. **Bot permissions:** In the ticket category the bot needs **Manage Channels** and
   **Manage Permissions**. For the ping to arrive, the support role must be "mentionable", or the
   bot gets **Mention @everyone, @here and All Roles**. The **Message Content Intent** must be
   enabled (see Installation).
5. **Knowledge base:** fill `support_knowledge.md` with your rules, wipe times and FAQ. Changes
   apply without a restart. Don't put anything secret in it, because the AI may share all of it.
6. Start the bot, then try `/ticket ai-status` (shows balance and model) and
   `/ticket ai-test question:"When is wipe?"`. Finally run `/ticket panel` in the channel of your
   choice. `ai-test` answers like a real ticket (same prompt and tools, you as the player), but in test mode:
   claims, purchase requests and redeems are only checked and listed under *Tools the AI used* – nothing is
   executed. Try e.g. `question:"I bought M249 x2, 50 rockets and 50k hqm, Tebex order 123"`.

**Privacy:** Ticket messages are sent to DeepSeek to generate replies. The panel points this
out. Check whether that's OK for your community (GDPR, privacy notice in your Discord). Without
an API key, the ticket system works entirely without AI.

---

## Community & moderation

These features come from the KRYVON bot and have been rebuilt and extended here:

| KRYVON bot | Here |
|---|---|
| Welcome message | `WELCOME_CHANNEL_ID` + a freely worded `WELCOME_MESSAGE` with the placeholders `{member}`, `{server}`, `{count}`, `{rules}` |
| Auto role | `AUTO_ROLE_ID` (with a check that the bot role is high enough) |
| Logs (join/leave, deleted/edited messages, moderation) | `DISCORD_LOG_CHANNEL_ID`, plus giveaways |
| `/timeout`, `/kick`, `/ban`, `/purge` | `/mod …`, plus `untimeout`, `unban`, purge by user, DM to the affected member |
| `/giveaway` (in memory only) | `/giveaway …` in the **database** – survives restarts; multiple winners, reroll, winner role, controllable from the web interface |
| `/ping`, `/rust`, `/rules` | `/ping` (incl. RCON latency), `/rust` (incl. live status), `/rules` (from `rules.md`, editable in the web interface) |
| `/ticket`, `/setup-ticket`, `/close` | `/ticket open`, `/ticket panel`, `/ticket close` – with AI, transcripts, staff ping |
| `/kit` (info only) | `/kit …` gives real kits, plus custom kits and autokits |
| `/helios-status` | dropped – this bot talks to the server directly via RCON and doesn't need Helios |

**Notes**

- The message log only shows deleted/edited messages if the bot has seen them since its last
  start (Discord doesn't send older content).
- Let your community know what gets logged (e.g. in the rules) – privacy.
- Giveaway prizes are **not** given out in-game automatically (Discord users and in-game names
  are not linked). Hand out prizes e.g. with `/customkit give` or `/kit give`.

### Wipe manager

Plan the next wipe with `/wipe set 2026-10-15 18:00` (or `15.10.2026 18:00`, time in `WIPE_TIMEZONE`,
default Europe/Berlin) and a repeat – once, every week, every 2 weeks or monthly (the first same
weekday of the next month, like Rust's forced wipe) – or in the web interface (🎉 Community → 🧹 Wipe).
Then the bot:

- shows **"Next wipe … (in 2 days)"** in the status message, `/rust` and `/wipe info`,
- posts **24 hours** and **1 hour** before in `WIPE_ANNOUNCE_CHANNEL_ID` (default: events channel; the
  1-hour post pings `WIPE_PING_ROLE_ID` if set), and warns **in-game 1 hour and 10 minutes** before,
- at the wipe time posts **"🧹 Server wiped!"** and starts fresh – after a backup – depending on the
  settings: kit cooldowns (`WIPE_RESET_KITS`, on), leaderboard (`WIPE_RESET_STATS`, off), points
  (`WIPE_RESET_POINTS`, off) – then moves on to the next date.

The wipe itself is still done by G-Portal. If it happens at another time, use `/wipe done` (or
**Wipe done now** in the web interface) to run the resets and the announcement right away.

### Scheduled messages, reports, watchlist, teams

- **Scheduled messages:** `/schedule add "Join our Discord: discord.gg/…" 30m` posts a message every 30
  minutes in-game (rich text like `<color=orange>` allowed), in a Discord channel or both – first after
  one interval, at least every 5 minutes. Pause, resume and delete with `/schedule` or in the web interface
  (🎉 Community).
- **Reports:** members use `/report <player> <reason>`. The report goes to `REPORT_CHANNEL_ID` (default:
  alert channel, then admin log) with a ping for the support roles and a **✅ Handled** button. All reports
  are in the web interface (🚩 Reports) – also with the number of earlier reports about the same player.
- **Watchlist:** `/watch add <player> <reason>` (or 🚩 Reports in the web interface) – when that exact
  name joins the server, staff is pinged (at most every 30 minutes per player) in `WATCHLIST_CHANNEL_ID`
  (⚙️ Settings → 🚨 Alerts & backups → *Channel for watchlist alerts*; empty = the report channel). Handy for ban evaders with
  a known name.
- **Team leaderboard:** the bot follows the team log lines (create, join, leave, new leader). `/teams top`
  ranks teams with at least two members by kills, K/D, playtime or size; `/teams of <player>` shows the
  members. Teams are named after their leader and only known from the moment the bot runs.

### Points & shop

Members earn points with the in-game player they linked (`/customkit link` – so points can only ever
go to their own verified account):

| Source | Setting (default) |
|---|---|
| Every hour online | `POINTS_PER_HOUR` (10) |
| PvP kill (the same victim counts at most once every 10 minutes – no farming) | `POINTS_PER_KILL` (5) |
| `/daily` (every 20 h; +`DAILY_STREAK_BONUS` per day in a row, max. 7 days, streak kept within 48 h) | `DAILY_POINTS` (50), `DAILY_STREAK_BONUS` (10) |
| **Playtime reward**: every N hours online a custom kit (sent in-game, announced with `say`) and/or points | `PLAYTIME_REWARD_HOURS` (0 = off), `PLAYTIME_REWARD_KIT`, `PLAYTIME_REWARD_POINTS` |

The name of the points is up to you (`POINTS_NAME`, e.g. *coins* or *scrap*). Members spend them with
`/shop buy`. Shop items are set up in the web interface (**🪙 Shop**): a **custom kit**, an **in-game
kit**, an **item** (shortname + amount) or a **Discord role** – permanent or for N days (uses the timed
roles below). Kits and items need the linked player to be online. If a delivery fails (e.g. RCON down),
the points are refunded automatically. Every purchase is in the admin log. The Shop page also shows all
balances, with **± Points** to correct one; in Discord admins use `/points give/take`.
`/profile` shows everything about a member at a glance (also useful for staff in tickets), and the
ticket AI sees the owner's balance too.

### Timed roles (e.g. VIP for 7 days)

`/temprole give @member @VIP 7d` gives a role that the bot **removes by itself** when the time is up
(checked every minute; also right after a restart if it ran out while the bot was offline). Giving it
again extends the time. `/temprole list` and the web interface (🎉 Community → ⏳ Timed roles) show
everything that's running, with a button to end it early. The member gets a DM when it ends.

Giveaway prizes use this too: the ticket AI gives a kit prize like **"VIP 7 days"** (or "VIP 2 weeks",
"VIP (30d)", "1 month") the kit's role for exactly that long. A kit prize without a duration and not
lifetime (e.g. "Vip Standard") gets the role for *Days for kit prizes without a duration*
(`GIVEAWAY_PRIZE_DAYS`) – or goes to staff if that is 0 (default). Someone who already has the role
for good keeps it – no timer is added.

### Admin abuse protection

When someone spawns **rockets or C4** and isn't allowed to, the bot immediately runs
`removeowner "<name>"`, `removemoderator "<name>"`, `global.god 0 "<name>"` (god mode off) and
`global.killplayer "<name>"`. When that player **respawns** (within an hour), it runs `global.sleep "<name>"`.

Deleting their stuff is **never automatic**: the alert in the alert channel (with a ping for `ALERT_ROLE_IDS`) is a
**case** with the buttons **🗑️ Approve deletion** and **✖️ Reject** (bot admins only). Only on approval does the
bot run `entity.deleteby "<name>"` (**deletes everything that player owns** – bases, deployables). Staff can also
decide in the web interface (🚩 Reports → 🚨 Admin abuse, or ⚙️ Settings → 🛡️ Admin abuse protection). Each case
can only be decided once; it survives restarts. Every step and the server's replies are in the admin log.
All commands can be changed in the advanced settings (*Commands before the kill / right after the kill / only
after staff approval / when they respawn*, `-` = nothing).

**Who is allowed** (⚙️ Settings → 🛡️ Admin abuse protection):
- `ABUSE_EXEMPT_ROLE_IDS` – Discord roles such as *Owner* and *Co-Owner*. The bot recognises their in-game
  name through `/customkit link`, so owners/co-owners should link their player.
- `ABUSE_EXEMPT_PLAYERS` – in-game names, comma-separated (works without linking – put the owner's gamertag
  here to be safe).

The protection **only runs once at least one of the two is set**, so the owner can't be hit by accident.
Every rockets/C4 spawn it sees is in the **admin log**, also when nothing happens – e.g. *"allowed (Discord role
Owner)"* or *"not punished: the protection is waiting for setup"* – so you can see why.

Good to know:
- Only spawns with the **in-game admin panel** count – the server logs them as
  `[ServerVar] giving <player> 3 x Rocket`. Gives via RCON/console (the bot's shop, kits and rewards,
  `/rcon`, other bots) are logged differently and never count. Rocket launchers don't count, only the ammo
  (`ABUSE_ITEMS`, default `rocket,c4,timed explosive,explosive.timed`).
- The log line doesn't say **who** gave the item. Admins spawn items for themselves, so that's the admin –
  but if an admin gives rockets to another player, the log shows (and the bot punishes) that player. The
  alert reminds staff to check.
- The commands to remove admin can differ per server. **Test them once** with `/rcon removeowner "TestName"`
  on a test account and adjust `ABUSE_REMOVE_ADMIN_COMMANDS` (several separated by `;`) if needed; the
  alert always shows what the server answered.

### Alerts & backups

- **Server-down alert:** if the bot has had no RCON connection for `SERVER_DOWN_ALERT_MINUTES`
  (default 10), it pings `ALERT_ROLE_IDS` (default: admin roles) once in `ALERT_CHANNEL_ID` (default:
  admin log channel) with the last error – and posts again when the server is back, with the
  downtime. `0` = off.
- **Backups:** once a day and right before every automatic update, the bot saves the database
  (stats, kits, tickets, links, giveaways, timed roles), web logins, rules and knowledge base as a ZIP
  in `data/backups/`. Backups older than `BACKUP_KEEP_DAYS` (default 7) are deleted, the newest 3
  always stay. Your `.env` (tokens, passwords) is **not** included. In the web interface (⚙️ Settings →
  🚨 Alerts & backups) you can create one right away and download them.
  **Restore:** stop the bot, put `bot.db` from the ZIP into `data/` (and the other files into the bot
  folder), start the bot.

---

## Troubleshooting

| Message / problem | Fix |
|---|---|
| `Handshake rejected` or `dropped the connection immediately` | Wrong RCON password or port. Did you restart the server after changing the password? |
| `RCON Connection failed: … timeout` | Wrong IP/port, server offline, or a firewall is blocking outgoing connections |
| Changing settings without an editor | Web interface → ⚙️ Settings → Save – applies right away (a few settings say "restart needed" – then "Restart bot now") |
| RCON won't connect | The bot tests this **automatically** and shows the cause in the bot window and in the web interface (red card on the Overview page). Saving new RCON details under ⚙️ Settings also runs the test right away. |
| RCON won't connect – cause unclear | Run **`rcon_test.bat`** (Windows) or `python tools/rcon_test.py`: checks settings, port, WebSocket, password and response step by step and names the cause |
| `Message Content Intent … disabled` | Developer Portal → Bot → enable Message Content Intent → Save Changes → restart the bot |
| Tunnel: "failed to request quick Tunnel" / no address | The PC can't reach Cloudflare (firewall/antivirus/DNS) – allow `bin/cloudflared.exe`, or use a tunnel token. The Overview card shows the exact error. |
| Slash commands missing | Set `GUILD_ID` and invite the bot with the `applications.commands` scope |
| Old (German) slash commands still show up | The bot removes old registrations at startup. Restart the bot (latest version), then press Ctrl+R in Discord. Without `GUILD_ID`, global commands can take up to 1 h to update – setting `GUILD_ID` makes changes instant. |
| Killfeed or chat stays empty | Set `CONSOLE_LOG_CHANNEL_ID` and compare the real log lines with `rce/log_parser.py` |
| Kick/ban has no effect | Test the command syntax via `/rcon` and adjust the `*_COMMAND_TEMPLATE` in the `.env` |
| Kit doesn't arrive | Test `/rcon kit givetoplayer "Kit" "Player"` and check the response in the console log. If the syntax differs, adjust `KIT_GIVE_TEMPLATE`. |
| Custom kit doesn't arrive | Test `/rcon inventory.giveto "YourName" "wood" 100`. If the syntax differs, adjust `ITEM_GIVE_TEMPLATE`. If the `giving …` line is missing from the admin log, the shortname is usually wrong. |
| Bot is online, but **every command shows "The application did not respond"**, `session has been invalidated` in the log | Privileged intents are off in the Developer Portal. The bot then **still starts, in limited mode** without those intents – all commands, RCON, kits and feeds work, only the affected features (e.g. tickets, welcome message) are paused. A yellow card in the web interface names the missing toggle (with a link). Fix: Developer Portal → **the same application as the token** (web interface → "Check bot token" shows name and intents) → Bot → *Privileged Gateway Intents* → enable → **Save Changes** → "Restart bot now". |
| "Limited mode" in the bot window | See the row above – one or both privileged intents are off. |
| "Discord denied …" on a command | The message now says **which action**, **in which channel** and **which permission** is missing. On startup the bot also checks all configured channels/roles: list in the bot window and as a "The bot is missing Discord permissions" card on the web interface Overview page. Fix: Server Settings → Roles → bot role → enable the permissions and drag the role up; for channels/categories with their own permissions, allow the bot role there – or use the "Re-invite bot with all permissions" button. |
| Auto role/winner role isn't assigned | Drag the bot role above that role in the server settings, allow *Manage Roles* |
| `/mod …`: "role is above the bot role" | Discord only allows moderation below your own highest role – drag the bot role up |
| `start.bat`: "Python was not found" | Install Python from python.org, tick "Add Python to PATH", sign out of Windows and back in |
| Web interface: "could not open port …" | Another program is using the port → set `WEB_PORT=8090` or similar |
| Forgot the web interface password | Set `WEB_PASSWORD` in the `.env` and restart the bot |
| Web interface not reachable from your phone | `WEB_HOST=0.0.0.0`, allow the Windows firewall for private networks, look up the PC's IP with `ipconfig` |
| Ticket: "Manage Channels" missing | Give the bot "Manage Channels" and "Manage Permissions" in the ticket category |
| Ticket: staff ping doesn't arrive | Make the support role "mentionable" or allow the bot to "Mention @everyone, @here and All Roles"; check `SUPPORT_ROLE_IDS` |
| `HTTP 402` / AI doesn't reply | DeepSeek balance is empty → [platform.deepseek.com/usage](https://platform.deepseek.com/usage), check status with `/ticket ai-status` |
| `HTTP 401` | Wrong `DEEPSEEK_API_KEY` → create a new key at [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| `/kit info` only shows raw data | The output format differs. Adjust the regex in `rce/kits.py`. |

---

## Security

- `.env` is listed in `.gitignore`. The token and RCON password never belong in Git or in
  screenshots.
- `/rcon` grants full server access. Only give the admin roles to people you trust, or set
  `ALLOW_RAW_RCON=false`.
- Text from Discord is sanitized before it's sent to the game (no rich-text tags, no quotes, no
  line breaks). Player names are shown in Discord without Markdown and without @mentions.
