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
| **Admin abuse protection** | Live log stream + RCON | Someone not allowed spawns rockets/C4 → admin removed, teleported away, then killed with `global.killplayer`, staff pinged. See [Admin abuse protection](#admin-abuse-protection). |
| **Timed roles** | Discord + database | `/temprole`: roles that end by themselves (e.g. VIP for 7 days) – also for giveaway prizes. See [Timed roles](#timed-roles-eg-vip-for-7-days). |
| **Alerts & backups** | Bot | Staff ping when the server is offline, daily backups. See [Alerts & backups](#alerts--backups). |
| **Plugins** | `plugins/` + `data/plugins/` | Extra features as single files, switched on/off with their own settings in the web interface – live, no restart. 11 growth plugins built in, your own can be uploaded. See [Plugins](#plugins). |
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
│   ├── abusewatch.py      # AI abuse watch: flags suspicious admin activity in the server log
│   ├── kit_requests.py    # Bought custom kits: AI request → staff approve button → kit role
│   ├── store.py           # Tip4Serv purchases (polled every minute) → automatic approval
│   ├── automod.py         # AI chat moderation: warns offensive members in chosen channels
│   ├── webshop.py         # Web store: PayPal checkout, gift cards, delivery of bought kits
│   ├── aichat.py          # AI chats with members in chosen channels + acts as prison guard
│   ├── prison.py          # /prison jail/free/list: lock a member to the prison channel (timed too)
│   ├── player_accounts.py # Player website login (one-time code) + team slots on custom kits
│   ├── countdown.py       # /countdown: one self-editing countdown message
│   └── updates.py         # Automatic updates: checks GitHub regularly, restarts after an update
├── plugins/               # Built-in plugins (one file each, on/off in the web interface → 🧩 Plugins)
│   ├── invites.py         # Invite rewards
│   ├── website.py         # Player website (site root /)
│   ├── casino.py          # Casino on the player website: bets, payouts, stats, daily loss limit
│   ├── starter_kit.py     # Starter kit for brand-new players
│   ├── lowpop_bonus.py    # Extra points while the server is nearly empty
│   ├── filling_ping.py    # "Server is filling up" ping (/pingme)
│   ├── comeback.py        # Wipe comeback DM (/wipedm)
│   ├── early_bird.py      # Early-bird wipe reward
│   ├── bounties.py        # /bounty
│   ├── weekly_recap.py    # Weekly recap post (/recap preview)
│   ├── achievements.py    # Badges (/achievements show)
│   └── wipe_poll.py       # Post-wipe feedback polls (/wipepoll)
├── plugin_api.py          # Plugin base class + helpers (see PLUGINS.md)
├── plugin_manager.py      # Finds, loads and switches plugins; settings in data/plugins.json
├── PLUGINS.md             # How to write your own plugin
├── support/
│   ├── deepseek.py        # Client for the DeepSeek platform API
│   ├── casino_games.py    # Casino games: slots, coin flip, dice, roulette, blackjack, Plinko (exact RTP)
│   ├── plinko_image.py    # Plinko board pictures for Discord (animated GIF of the drop)
│   ├── casino_images.py   # Slots, coin flip, dice, roulette GIFs and the blackjack table for Discord
│   ├── poker.py           # Texas Hold'em engine for the casino poker tables (side pots, hand ranking)
│   ├── plugin_ai.py       # AI writes a plugin from a description (Plugins page → Create with AI)
│   ├── paypal.py          # PayPal checkout (Orders v2: create, capture, verify)
│   ├── pricing.py         # Price list for custom kits (prices.txt)
│   ├── prompt.py          # System prompt, history, parsing the AI reply
│   └── tip4serv.py        # Tip4Serv store connection (signed requests, rcebot commands)
├── support_knowledge.md   # Knowledge base for the AI  ← fill in yourself
├── rules.md               # Rules for /rules  ← adjust
├── prices.txt             # Custom kit price list (Tip4Serv auto-approve)  ← adjust
├── web/
│   ├── server.py          # Web interface: login + JSON API (runs inside the bot process)
│   ├── item_images.py     # Item pictures: downloaded once (ITEM_IMAGE_URL), cached in data/item_images/
│   ├── store_pages.py     # Web store pages of the player website (/store)
│   ├── account_pages.py   # Player account pages (/account): manage own kits, team slots
│   ├── casino_pages.py    # Casino pages of the player website (/casino)
│   └── static/            # Frontend (HTML/CSS/JS, no external files): style.css, neon.css (dark command-centre theme)
├── logbuffer.py           # Recent log lines for the web interface
├── settings_schema.py     # All settings for the form (label, type, help, validation)
├── envfile.py             # Read/write .env (comments are preserved)
├── updater.py             # Downloads + installs new versions from GitHub (ZIP install or git checkout)
├── backups.py             # Backup ZIPs in data/backups/ (+ copy / live backup to e.g. Google Drive)
├── tools/install_linux.sh # Install on a Linux server as a service (optionally restoring a backup)
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
http://localhost:8080/admin`; the browser opens the web interface automatically. With `GUILD_ID` set,
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

## Hosting on a Linux server (VPS)

Any small Linux server works (1 CPU, 1–2 GB RAM is plenty; e.g. InterServer, Hetzner, Oracle Free). With
**Ubuntu** or Debian:

1. **On the old PC – make a full backup:** web interface → ⚙️ Settings → 🚨 Alerts & backups → turn on
   *Include the .env*, save, click **💾 Backup now**, download the ZIP. Then **stop the bot on the PC** (and keep it
   off – two bots with the same token answer everything twice).
2. **Upload the ZIP** to the server, e.g. in PowerShell on the PC:
   `scp "$HOME\Downloads\backup-….zip" root@SERVER-IP:/root/`
3. **Log in to the server** (PC: `ssh root@SERVER-IP`; phone: an SSH app like Termius) and run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/lukas04545/ff/main/rce-discord-bot/tools/install_linux.sh -o install.sh
   bash install.sh /root/backup-….zip
   ```
   Without a backup (fresh bot) just `bash install.sh` – the settings page link then appears in the log.

The script installs Python and Git, creates a user `rcebot` (the bot doesn't run as root), downloads the bot to
`/home/rcebot/ff/rce-discord-bot`, restores the backup (database, logins, rules, knowledge base, price list,
.env) and sets up the service **`rcebot`**: it starts with the server, restarts after crashes and after automatic
updates (git checkout → updates via fast-forward).

| | |
|---|---|
| Live log | `journalctl -u rcebot -f` |
| Web interface link | `cat /home/rcebot/ff/rce-discord-bot/PUBLIC_LINK.txt` (also posted in the admin log) |
| Stop / start / restart | `systemctl stop rcebot` · `systemctl start rcebot` · `systemctl restart rcebot` |

The web interface stays on 127.0.0.1 and is reached through the Cloudflare tunnel – no port has to be opened.
Google Drive for desktop doesn't exist for Linux, so `BACKUP_COPY_DIR` is cleared when restoring.

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

Every section (card) can be **collapsed** by tapping its title – the page remembers it – and each page has
**Collapse all / Expand all** at the top. The layout adapts to phones (navigation as a scrollable bar, one column,
wide tables scroll sideways inside their card).

The web interface runs **inside the bot** – no extra program, no extra packages. After startup
your browser opens automatically; otherwise go to **<http://localhost:8080/admin>**. The control panel is at **`/admin`**; the address
itself (`/`) is the 🌍 Player website (when that plugin is on – otherwise it forwards to `/admin`).

| Page | Features |
|---|---|
| 📊 Overview | Server status (players, map, uptime, FPS), **population graph** (24 h / 7 / 30 days, busiest hours), online players with **kick/ban**, unban, message to the game |
| ⌨️ Console | **Live server console** and running any RCON command (if `ALLOW_RAW_RCON=true`) |
| 🎒 Kits | Give kits (player/everyone), view in-game kits, create/edit **custom kits**, create/pause/delete **autokits**, wipe reset, give history |
| 🧰 Kit builder | **Visual kit builder** (like the Helios kit builder): all 566 RCE items by category with search and **item pictures**, 7 wear / 24 inventory / 6 belt slots, click or drag items in, drag to move/swap, amount & condition per slot. Generates the `kit add` commands (copy), **creates the in-game kit on the server** or **saves it as a custom kit**. Load existing kits from the server, save kits in the browser, export/import share codes, import pasted `kit add` commands |
| 🪙 Shop | Shop items (custom kit, in-game kit, item, role – optionally for N days), prices, on sale/hidden; all point balances with ± correction; how members earn points |
| 🚩 Reports | Admin abuse cases (approve/reject deletion), player reports (open/all, mark handled) and the watchlist (add/remove) |
| 🎫 Tickets | All tickets with status, **transcripts of closed tickets**, DeepSeek balance |
| 🏆 Stats | Leaderboard (kills, deaths, K/D, playtime), platform, last seen; **team leaderboard** |
| 🎉 Community | **Wipe** (next date, repeat, wipe done), **scheduled messages**, timed roles, giveaways (end early, reroll), **edit rules**, overview of the community settings |
| 📚 Knowledge base | Edit `support_knowledge.md` right in the browser (applies immediately) |
| 🖥️ System | CPU and memory of the PC the bot runs on (whole PC and the bot itself, charts of the last 10 minutes), disk space, uptime and the memory of every server's bot process – Linux and Windows, nothing to install |
| 🧾 Bot log | The bot's last 500 log lines |
| ⚙️ Settings | **All settings** from the `.env` as a form: channels and roles as dropdowns (once the bot is connected), **Test RCON**, **Check bot token** (incl. intent status and invite link). **Saved settings apply right away** – only the bot token, Discord server, database file, web interface address/port and a newly needed privileged intent need **Restart bot now** (the page says which) |

Everything you do in the web interface is posted to the admin log channel in Discord
("🌐 Web interface: …"). The interface also works on your phone.

**Settings in the `.env`:**

| Variable | Default | Meaning |
|---|---|---|
| `WEB_PASSWORD` | empty | Login password. **Empty = sign in via the link in the bot window** (or the random password shown there). Best to set your own – you can also do that under ⚙️ Settings. |
| `WEB_HOST` | `127.0.0.1` | `127.0.0.1` = this PC only. `0.0.0.0` = also phone/laptop on the **same home network** via `http://<your-PC-IP>:8080/admin` (Windows then asks once for firewall access → allow "Private networks"). |
| `WEB_PORT` | `8080` | Port, in case 8080 is already in use |
| `WEB_OPEN_BROWSER` | `true` | Open the browser automatically on startup |
| `WEB_ENABLED` | `true` | Turn the web interface off completely |

**Security:** Password login (max. 5 attempts per minute, locked for 15 minutes after 15 wrong passwords),
sessions expire after 12 hours,
protection against other websites (CSRF), all player and console texts are displayed as plain
text only. Forms of the player website (store, account, casino, battle pass …) are only accepted from the site
itself: a POST whose `Origin` is another site, whose `Sec-Fetch-Site` isn't same-origin (this decides when the
`Origin` is `null` – privacy mode on the site itself is fine, a sandboxed frame of another site is not), or whose
`Referer` is another site is refused. Website login codes are limited per IP (5 per 15 minutes)
**and** per player (3 per 15 minutes), so nobody can flood a player's Discord DMs from many addresses. An automated
test (`tests/test_web_forms_fuzz.py`) crawls every page and form of the website – logged out and logged in – sends
hostile input into every field, URL parameter and database name, and fails on any unescaped HTML (XSS), crash,
open redirect or form accepted from another site. However, the web interface has **no HTTPS** – **never expose it to the internet via
port forwarding on your router**. For access on the go, use a VPN such as Tailscale or WireGuard
into your home network instead.

**Look:** the web interface comes in a dark "command centre" theme by default – midnight navy/violet, rounded panels,
lavender page titles with a small label above them, stat cards with a lime-to-teal edge and a pastel gradient on the
main buttons. The menu is grouped into coloured panels (Server, Kits, Community, Admin) that show how many pages they
hold and can be collapsed; above the menu a server card shows the server name and a status chip with the players
online. The top bar has **search (Ctrl K)** – type a page or a setting and press Enter: settings open on the Settings
page and are highlighted – plus chips with the map and players. The colours are variables at the top of
`web/static/neon.css`. The 🌙/☀️ button switches between *dark* and *light*; the ⏸️/✨ button lets the soft green
specks in the background drift slowly (off by default – the page is drawn once, no canvas, no blur, so it stays fast).

**Item pictures:** the kit builder, the kit lists and the player website show pictures of the items. The bot
downloads each picture once from `ITEM_IMAGE_URL` (default: RustLabs, `{shortname}` is replaced) and keeps it in
`data/item_images/` – browsers only ever load them from the bot. Items without a picture show their category emoji.
`ITEM_IMAGE_URL=off` switches pictures off. If the configured source has no picture, the bot tries other sources and
alternative spellings of the shortname (spaces, dots, underscores). **Store → 🖼️ Item pictures → Search item
pictures** fetches every item of the kit builder now and lists the ones that still have none; for those, put your
own PNG/JPG/WebP into `data/item_images/<shortname>.img` and it is used right away.
The bot also fetches every missing picture by itself in the background ~30 s after it starts (the bot log says
how many it found and which source refused). Pages never wait more than a few seconds for a picture: the grey
placeholder is shown meanwhile and is never cached, so the real picture appears on the next page load. A source that
refuses the bot (HTTP 403/429) or fails is tried again after 5 minutes – only "no picture anywhere" (404) waits a day.
**Search item pictures** shows a table per source (pictures, not there, refused/failed, last problem) – if a source
refuses everything, your server's connection is blocked there: pick another one under *Settings → Web → Item
pictures from*.

**Speed:** pages show small WebP copies of the item pictures and the logo (made once with Pillow, kept in
`data/item_images/thumbs/`), many times smaller than the originals. Pages, CSS, JS and JSON are sent gzip-compressed;
pictures and panel files carry an ETag, so browsers re-check them with a tiny "unchanged" reply instead of
downloading them again. Panel files are linked by their content hash, so browsers keep them until they change. The
website's background is painted once instead of on every scroll. Without Pillow (`pip install -r requirements.txt`
installs it) the full-size pictures are sent.

### Accounts for other admins

The server owner logs in with username **`owner`** and the `WEB_PASSWORD`. Under **👥 Users** (owners
only) you create accounts for your other admins – username + password (🎲 generates a strong one) +
role:

| Role | Can do |
|---|---|
| **Admin** | Everything (overview, console, kits, kit builder, tickets, stats, community, …) **except** Settings, bot restart and user management |
| **Owner** | Everything, including settings and managing accounts |

- Passwords are stored only as salted **PBKDF2-HMAC-SHA256** hashes (600 000 rounds) in `data/web_users.json`
  (never in plain text). Accounts from older versions (scrypt) are upgraded automatically at their next login.
  Plain single SHA-256 is deliberately not used: it can be brute-forced billions of times per second.
- Admins change their own password via **🔑 My password** in the sidebar. The owner can reset
  passwords, change roles, disable or delete accounts – disabling/deleting/resetting logs that person
  out immediately.
- Every action in the web interface is written to the admin log channel **with the username**.
- Login protection, per IP **and** per account: max. 5 attempts per minute, and after 15 wrong passwords
  within 15 minutes that IP/account is locked for 15 minutes (even with the right password). The same applies
  to *My password*, the one-time login link and the Tip4Serv webhook address.
- Player website and item pictures (public): max. 60 pages and 300 pictures per minute per visitor, 1200 pages
  per minute in total; new picture downloads are capped at 60 per minute. Logged-in admins aren't limited.

**How other admins reach the web interface** (it runs on your PC):

- **From anywhere – built-in Cloudflare tunnel (on by default):** just start the bot. It sets up a free,
  encrypted Cloudflare tunnel by itself (downloads Cloudflare's `cloudflared` once into `bin/`, ~40 MB,
  from Cloudflare's official GitHub releases) and prints your link in a box in the bot window:
  ```
  ========================================================================
    🌍 YOUR WEB INTERFACE LINK – works from anywhere (phone, other PCs):
       Control panel:  https://quiet-fox-123.trycloudflare.com/admin
       Player website: https://quiet-fox-123.trycloudflare.com/
    Login: owner / <your password>
  ========================================================================
  ```
  If you haven't set a `WEB_PASSWORD`, the bot creates a strong one on the first start, saves it in the
  `.env` (as a hash only) and shows it once in that box. Nothing to open on your router; the web interface itself still only
  listens on your PC. To turn it off: Settings → Web interface → *Reachable from the internet* → Off.
  - The link is also saved in **`PUBLIC_LINK.txt`** (next to `start.bat`), shown as a card on the
    **Overview** (with a copy button) and posted in your **admin log channel** – so your admins always find
    the current one.
  - It stays the same while the bot runs (also across restarts from the web interface) and **changes when
    you close the bot window**.
  - **Your own domain (fixed address):** if your domain is on Cloudflare (bought there, or its nameservers
    moved there), open ⚙️ Settings → Web interface → **🌐 Use your own domain (Cloudflare)**, enter the address
    you want (e.g. `panel.yourdomain.com`, or the domain itself) and a Cloudflare **API token**, and click
    *Connect my domain*. The bot creates the tunnel, points it at the web interface, adds the DNS record
    (`CNAME` → the tunnel, proxied) and saves the tunnel token + address – then it checks that the address
    reaches the bot. Create the token at Cloudflare → My Profile → API Tokens → *Create Token* → *Custom
    token* with **Account › Cloudflare Tunnel › Edit**, **Zone › DNS › Edit** and **Zone › Zone › Read** for your
    domain. The API token is only used for this setup and is **not saved**. Existing DNS records the bot didn't
    make (e.g. an `A` record for your website) are never changed – pick another name then. Running it again
    reuses the same tunnel. Use the bare domain (e.g. `yourdomain.com`) to get the player website at `https://yourdomain.com/` and
    the control panel at `https://yourdomain.com/admin`.
    By hand instead: create a tunnel in the Cloudflare dashboard (Zero Trust → Networks → Tunnels), point its
    public hostname to `http://127.0.0.1:8080` (your `WEB_PORT`; use 127.0.0.1, not `localhost` – that can be
    IPv6 and give a 502), then paste the tunnel **token** into *Cloudflare tunnel token* and the address into
    *Fixed public address* (advanced settings).
  - The address is public, so give every admin their own account with a strong password (👥 Users) –
    logins are rate-limited per visitor IP and per account.
  - macOS: install it once with `brew install cloudflared`.
- **Same home network only:** Settings → Web interface → *Reachable from* → "Also home network
  (0.0.0.0)", restart the bot. Others open `http://<your PC's local IP>:8080/admin` (find it with `ipconfig`).
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


### 🖥️ Several Rust servers

One bot install can run more than one Rust server. Open **🖥️ Servers** (Admin, owner only) → **Add a server**, then
enter a name, the server's IP, RCON port and password, and choose its Discord bot:

- **The main Discord bot (recommended):** one bot for every server. Every slash command that more than one server
  has gets an optional **`server`** choice, e.g. `/status server:2x Monthly`. Leave it out and the command goes to
  the server whose channel you're typing in, otherwise to the main server. A command only one server has (e.g. a
  plugin that's only on there) always goes there.
  - **Buttons, menus and pop-up forms** go to the server whose message they're on.
  - **Chat messages and reactions** go to the server whose channel it is: the channels set in that server's
    settings and the ticket channels it created. Every other channel belongs to the main server.
  - **The bot's status line shows every server.** By default it takes turns ("Main: 12/100 players", then
    "2x Monthly: 30/50 players", 15 seconds each). Settings → Behaviour on the main server: *Bot status with several
    servers* = `rotate` / `combined` (all in one line: "Main 12/100 · 2x Monthly 30/50") / `main` (only the main
    server), and *show each server for … seconds*. Each server's short name in it is *This server's name in the bot
    status* (in that server's own panel).
  - An existing server with its own bot can be switched over with **Use the main Discord bot**.
  - **Tickets are split by server.** "Open ticket" first asks *Which server is your ticket about?* (🏠 Main server /
    🛢️ Kryvon2x). The chosen server's bot opens the ticket as its own, so everything in it is that server's: its
    AI, its kits, its rules, and its staff tools. The AI is told the ticket is only about that server. It never
    gives or promises anything for another server, and asks the player to open a new ticket for the other one. A kit
    that's only for another server can't be claimed through it either. An extra server's ticket channels start with
    its name (`kryvon2x-ticket-0007-…`). Until it has its own, it uses the main server's ticket category, ticket log,
    support roles and DeepSeek key.
- **A Discord bot of its own:** paste its token. Create another application in the Discord Developer Portal: Bot →
  Reset Token, switch on the Message Content and Server Members intents, and invite it to your Discord. The Discord
  server ID is optional; empty means the same Discord as the main server.

- **Everything is separate:** each server gets its own folder `servers/<name>/` with its own settings (`.env`),
  database (kits, players, stats, points, tickets …), console, Discord channels, **its own control panel** (next port:
  8081, 8082 …, with its own secret address) and its own public link. The main bot starts the extra servers by
  itself in the background (no extra window), restarts a crashed one, and stops them when it stops. On the Servers
  page you start, stop, restart or remove a server, open its panel and read its log.
- **Shared by all servers:**
  - **Admin abuse protection:** by default it's set up on the main server, and the extra servers' panels show it
    locked. To give a server its own setup (other forbidden items, other allowed players, other commands, its own
    loop), switch to it in the panel and turn on *Settings → 🛡️ Admin abuse protection → This server has its own
    abuse protection settings*, save, and reload. Either way, when one server punishes an abuser, the others run the
    "remove admin" commands for that player too.
  - **Logins:** the same accounts, owner password, two-factor login and panel themes. Logged in on one panel at
    home, you're logged in on all.
- **Switch servers in the panel:** with more than one server, pick the server in the drop-down at the top of the
  sidebar. The panel switches right there, at the same address and on the same page, at home, on your link or on
  your own domain. Everything you see (overview, console, kits, players, settings …) is then that server's. The
  choice is kept until the browser closes or you log out. The Servers page always lists every server and marks the one
  shown now. If a server isn't running, the panel stays where it was and tells you.
- **Plugins on every server:** each server has its own plugins, switched on and off on its own. *🧩 Plugins → 🖥️
  Plugins on all servers* shows every plugin with a column per server: on/off with a button to switch it there,
  and **📥 Install** where one of your own plugins (uploaded or made with the AI) isn't there yet. Install copies its
  file, settings and pictures from the server that has it; it arrives switched off. **📤 Copy to …** updates it on
  another server after you changed it. Built-in plugins are on every server already. The rest of the Plugins page is
  the server you're viewing (drop-down).
- **The player website shows every server:** the home page has a card per server (name, online, players). The main
  server's name there is *Settings → Server display name* (e.g. "Kryvon 10x"); the others use their name from the
  Servers page. **Store:** players first pick a server's shop ("Enter the Kryvon 2x shop"). Each shop shows only
  the kits for that server; kits for both appear in both. A *Change server* link sits at the top. The other
  servers' in-game kits are added to their shop automatically (hidden until you set a price under 💳 Store). An own
  kit built in the 2x shop is the 2x server's: it's copied there, and the 10x server says where it's for. Gift
  cards, nukes and the battle pass are the same in every shop.
- **Store kits for one server or several:** in *💳 Store → Kits for sale*, tick which server(s) each kit is for. The
  website shows it ("For: Main + Kryvon2x").
  - **Bot kits:** copied to every server they're for, with their claim role, so buyers claim them there. On a server
    a kit isn't for, `/customkit claim` says where it's for.
  - **In-game kits:** given once on each server they're for, when the buyer is online there. The kit must exist on
    that server with the same name.
  - The servers ask each other "who's online" and "give this" over a signed connection on your PC only.
- **Copy kits across:** *Kits → 📋 Copy kits to another server* copies the bot's custom kits (items, description,
  claim cooldown) to another server. Claim roles come along when both use the same Discord. "Replace" overwrites
  kits with the same name there.
- **New servers start with:** the main server's admin user IDs, plus its admin roles when it's the same Discord. Set
  the channels and everything else in the new server's own panel (Settings).
- **Updates:** installed by the main server only; the extra servers restart with it. Removing a server keeps its
  folder (renamed to `servers/_removed-…`), so nothing is lost.

### 🎨 Panel themes (owner picks one per account)

Under **👥 Users → 🎨 Panel themes** the owner chooses how the control panel looks for every account, including
their own. Admins can't change it themselves. The new theme shows the next time they open or reload the panel.

- **Neon (default):** the normal look, with the ☀️/🌙 and ✨ buttons.
- **Jump scares 👻:** a dark, blood-red horror look with flickering headings, dripping cards and red eyes in the
  corner. Now and then, every few minutes and sometimes on a menu click, a scary face jumps at the screen with a
  scream. The sound is made in the browser, and only starts after their first click (a browser rule). There is no
  fast flashing.
- **Early 2000s 💾:** an old-school home page with a scrolling marquee, an "under construction" sign, a visitor
  counter, a sparkle trail and **popup ads**. The ads are windows inside the page. They can be dragged and closed, and
  clicking one opens a real 2000s TV ad on YouTube in a new tab, or a rickroll (about 4 in 10). Only accounts on
  this theme ever get them; switching an account back to Neon stops them right away. Under *Panel themes → Early 2000s popup ads* you set how many appear at login (0–100,
  default 20), how often they come back (every 0–120 minutes, 0 = never, default 3), how many come back each time
  (default 2) and the most open at once (default 6). Changes apply when they reload the panel.

### 🔒 Security of the control panel and the website

- **Hidden panel address:** from outside, the panel isn't at `/admin`. It lives at a random secret address
  (`WEB_PANEL_PATH`, e.g. `/panel-Ab3x…`), created at the first start. You'll find it in `PUBLIC_LINK.txt`, the bot
  window and *Settings → Web interface → Secret address of the control panel*, where you can also change it. The
  change applies right away, and the old link stops working. `/admin` still works on the bot's own PC. Anyone without the
  address gets "not found" for `/admin`, the login, the API and the panel's files, so scanners can't even tell a
  panel exists. Share the address only with your admins.
- **Honeypot at `/admin`:** anyone outside the bot's PC who opens `/admin` gets a fake login page that never lets
  anyone in. Every visit and login try is logged with the time, IP, location (country and city from Cloudflare when
  available), browser, operating system, the full user agent, language, the referring site, which server's `/admin`
  it was, whether it came through the tunnel and the username tried. The **🍯 Honeypot** page (Admin menu, owner
  only) shows totals (visits, login tries, different IPs, last 24 hours, top countries), a filter, an "only login
  tries" switch, every entry with a **Details** button listing everything saved, a **Grouped by IP** view (visits,
  first and last seen, locations, browsers, usernames tried) and a **CSV download**. The last 500 entries are kept,
  shared by all servers. A short line also goes to the admin log channel (once per IP every 6 hours, never a
  ping). The username **and password** tried are saved; the password only shows on the Honeypot page (behind a
  "Show passwords" switch, and in the CSV), never in Discord. If someone types one of your *real* panel passwords
  (an admin who went to `/admin` by mistake), it is **not** saved – the entry is flagged red and the admin log warns
  you to change that password. `/admin` is **rate limited**: 10 requests (5 login tries) per IP and 120 in total per
  minute; anything above that gets "Too many login attempts" and isn't logged, so nobody can flood the list. A website can't see a visitor's MAC address, so it
  isn't logged. Turn it off under *Settings → Web interface → Honeypot at /admin*; `/admin` then just answers
  "not found".
- **Secret way in from the player website:** in *Settings → Web interface → Secret phrase to the panel from the
  website* (`WEB_PANEL_KNOCK`), set a phrase of at least 8 characters. An admin who types it into the **Players**
  search of the player website lands on the control panel and logs in as usual. Nothing on the website shows that this
  exists; every other search is a normal player search. Pick something nobody would search for, and type `off` to
  switch it off. It is off until you set it.
- **Two-factor login (2FA):** *🔐 My account* → **Set up two-factor login** – then logging in needs your password
  **and** the 6-digit code from an authenticator app (Google/Microsoft Authenticator, Authy, 1Password, Bitwarden …).
  Strongly recommended for everyone, especially with the public link. Each code works once. Lost phone: the owner
  clicks **Reset 2FA** on the Users page (the owner's own: delete `data/web_2fa.json` on the bot's PC).
- **Confirm before risky actions:** uploading or deleting plugins, changing settings, creating/changing/deleting
  accounts, restart, update, backups, gift cards and switching 2FA off ask for your password (and 2FA code) again
  unless you logged in or confirmed within the last 15 minutes – a forgotten open session isn't enough to run code
  on your PC.
- **Browser protections on every page:** a strict Content-Security-Policy for the panel (only the bot's own scripts –
  no inline or foreign code), no framing by other sites (clickjacking), `nosniff`, a restrictive Permissions-Policy,
  no version numbers in the `Server` header; over HTTPS (the Cloudflare tunnel) also HSTS and a `Secure` login cookie.
  API answers are never cached.
- **No plain passwords stored:** the owner password (`WEB_PASSWORD`) and the secret phrase (`WEB_PANEL_KNOCK`)
  are kept in the `.env` only as salted PBKDF2-SHA256 hashes. If you type a plain password into the `.env` by hand,
  for example after forgetting it, the bot replaces it with its hash on the next start. A login hashes what you
  typed and compares the two hashes. Your browser sends the password only through the encrypted connection (https
  through the Cloudflare tunnel).
- **Login:** passwords hashed with PBKDF2-SHA256 (600 000 rounds), lockout after repeated failures (per IP and per
  account), every login with its IP in the admin log, sessions end after 12 hours or when the password changes.
- **The one-time login link** from the bot window only works on the bot's own PC (never through the public link).
- **Website forms** (store, account, casino, battle pass …) can't be sent from other websites.

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
| `/customkit access`, `/customkit cooldown`, `/customkit reset-cooldown`, `/customkit link-player`, `/customkit unlink` | Admins | Who may claim, how often, reset cooldowns, link a member to a player (also offline), remove links |
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
| `/giftcard create`, `/giftcard list`, `/giftcard disable` | Owners (server owner, Administrator, `ADMIN_USER_IDS`) | Create web store gift cards (codes are only shown to you), see their balances, disable one |
| `/giftcard check` | everyone | How much is left on a gift card |
| `/profile [member]` | everyone | Linked player, PvP stats, playtime, points, /daily, custom kits with cooldowns, timed roles |
| `/wipe info` | everyone | Next wipe with countdown |
| `/wipe set`, `/wipe clear`, `/wipe done` | Admins | Plan the next wipe (with repeat), clear it, or mark it as done now (runs the resets) |
| `/schedule add/list/toggle/remove` | Admins | Messages posted automatically every N minutes in-game and/or in Discord |
| `/countdown` | Admins | One message in the current channel that counts down in place (pings a chosen role once at the start) |
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
/customkit link-player member:@Name player:<name>       ← admins: link a member directly – the player can be offline
                                                          (move:True takes the player from another account)
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


### 🗃️ Kit database

**Kits → 🗃️ Kit database** in the control panel lists every kit in one place – the bot's custom kits and the
server's in-game kits – with its contents, role, claim cooldown, store listing and how often it was claimed.
**Search** by kit name, item (shortname or name, e.g. *ak*, *facemask*) or role, **filter** (bot kits, in-game kits,
for sale, auto-give on, without a role, never claimed) and **sort** (name, most/last claimed, most items). Click a kit
to manage it: add/remove items (bot kits), load an in-game kit's contents from the server, change its role, claim
cooldown and auto-give, give it to a player or everyone online, see its store listing, autokits and claim stats, or
delete a bot kit.

### 🎭 Every kit has its own Discord role

With **Every kit has its own Discord role** on (`KIT_ROLES`, Settings → Kits – on by default) kits and Discord
roles go hand in hand:

- **Every kit is connected to its role** – the bot's custom kits and the server's own in-game kits (`kit list`). The
  bot uses your **existing** roles: names are compared loosely (capitals, spaces, the word "kit", emojis and small
  typos don't matter – "m4 spawn kit" finds "M4 Spawn", "mp5 roadsgin" finds "MP5 Roadsign"). It checks every 10
  minutes, so new kits get connected too; custom kits that already have a claim role keep it. Kits without a
  matching role stay without one – unless **Create a new role for kits without a matching role**
  (`KIT_ROLES_CREATE`, off by default) is on.
- **🤖 Match with existing roles (AI)** (Kits → Kit roles) proposes a role for every kit – the loose name match first,
  the AI for the rest (roles the bot made itself never count). Check the list, change what's wrong, then **Apply**:
  kits are connected to the chosen roles, members of a duplicate role the bot made get your role instead, and the
  bot's duplicates no kit uses any more are deleted. **↩️ Remove roles the bot made** deletes every role the bot
  created for kits (it lists them first) and disconnects them. Which roles the bot made: the ones it remembers, the
  Discord audit log ("View Audit Log"), else roles named exactly like their kit made since kit roles exist.
- **Role → kit:** whoever has a kit's role can claim that kit with `/customkit claim` (or ask the ticket AI) –
  in-game kits included: they are given in-game with `KIT_GIVE_TEMPLATE` to the member's linked player while they're
  online, once per cooldown (default 1 day, change it with `/customkit cooldown`, reset with
  `/customkit reset-cooldown`). Give someone the role by hand (or via Tip4Serv) and they have the kit; take it
  away and they can't claim it any more.
- **Kit → role:** buying a kit gives its role – custom kits (ticket purchases and the web store) did that already;
  now an in-game kit bought in the web store also gives its role (for the store kit's days, else for good) when the
  kit is given in-game, and its cooldown starts then.

**In the control panel (Kits → 🎭 Kit roles)** you see every kit next to its role(s) and compare: how many members
have the role, how many of them linked their player and are online and ready, and per member (▸ Members) their
linked player, whether they're online and when they can claim again. Change a kit's cooldown right there, **🔄 Check
kit roles now** links new kits at once, **🎁 Give now** gives the kit to everyone with the role who is online and off
cooldown, and **Auto-give** (per kit) lets the bot do that by itself – when a member with the role joins and every 5
minutes – so role holders get their kit without claiming it. Auto-give needs a cooldown (> 0), otherwise the kit
would be given every 5 minutes.

The bot needs the **Manage Roles** permission and its own role above the kit roles. To list who has a role it needs
the **Server Members Intent** (Discord Developer Portal → your app → Bot → Privileged Gateway Intents) – the panel
tells you if it's missing; auto-give works without it. Points-shop items and kits staff hand out (`/kit give`,
`/customkit give`) stay one-off gifts without a role.

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

**AI chat (talks with members):** pick channels in **Settings → 🎉 Community → AI chat: channels**
(`AI_CHAT_CHANNEL_IDS`). There the AI chats with the community – answers questions, small talk, helps new players.
*When to answer* (`AI_CHAT_MODE`): `mentions` (default – when someone @mentions the bot or replies to its message)
or `all` (every message; use *AI chat: seconds per member between answers*, `AI_CHAT_COOLDOWN`, default 10).
**Your own instructions** (`AI_CHAT_INSTRUCTIONS`): personality, tone, topics and rules – e.g. "You are Rusty, a
sarcastic but helpful survivor", "Always answer in English", "Promote our Saturday events" – they take priority
over its defaults. `AI_CHAT_NAME` sets the name it uses. With `AI_CHAT_KNOWLEDGE` (on) it answers server questions
from the knowledge base and the live status; it never invents rules or prices and points to a ticket when it
doesn't know. It only talks: it can't give anything, never pings anyone (@everyone, roles, users) and keeps
answers short, in the member's language. It sees the last messages of the channel as context. Needs the DeepSeek
API key and the Message Content intent; messages in those channels are sent to DeepSeek.

**AI chat moderation:** pick the channels to watch in **Settings → 🎉 Community → AI moderation: channels**
(`AI_MOD_CHANNEL_IDS`, e.g. #general). The AI checks new messages there in batches – every
*AI moderation: check every … seconds* (`AI_MOD_INTERVAL`, default 15; 0 = every new message is read right away, higher = fewer
and cheaper DeepSeek calls but later warnings; one request for all waiting messages, at once when 30 are waiting) and only flags clearly offensive ones – insults or harassment aimed at someone, slurs, hate
speech, threats, sexual harassment, private data. Swearing, banter and game trash talk ("ez", "get raided lol") are
fine; when unsure it does nothing. A flagged member gets a reply the AI writes for that message, in the language they wrote in (e.g. *"⚠️ @name
Calling other players worthless isn't okay here – please keep it respectful. `1/3`"*; by default at most one per member per minute – *AI moderation: wait before warning the same member again* (`AI_MOD_WARN_COOLDOWN`, 0 = warn again right away for every offensive message)), and every case goes to the admin log with the message and a link.
Optional: delete severe messages (`AI_MOD_DELETE`, needs *Manage Messages*) and a Discord timeout after
`AI_MOD_TIMEOUT_AFTER` warnings in 24 h (default 3, 0 = never; needs *Timeout Members*) – the AI picks the length
itself, 5 minutes to 2 hours, by how bad the message is and the member's warnings that day
(`AI_MOD_TIMEOUT_MINUTES` is only the fallback). **Your own rules:** *AI moderation: your own rules* (`AI_MOD_INSTRUCTIONS`) – plain-language instructions that
take priority over the defaults, e.g. "Be strict: also warn for 'stfu'", "Swearing in German is fine", "Never warn
for 'noob'", "Warnings should sound friendly and mention #rules". Staff and bots are never checked. `/automod warnings @member` and `/automod clear @member`
(staff). Needs the DeepSeek API key and the Message Content intent; the watched messages are sent to DeepSeek.

**Prison (jail):** lock a member down so they can only see one **prison channel** and their own support ticket –
nothing else. Set a **Prison role** and **Prison channel** in **Settings → 🎉 Community** (`PRISON_ROLE_ID`,
`PRISON_CHANNEL_ID`; drag the bot's role above the prison role). `/prison jail @member [reason] [minutes]` gives
them the prison role, takes away their other roles (saved and restored when freed), denies the prison role *View
Channel* everywhere except the prison channel (new channels are locked too), and DMs them why. Their own ticket
stays visible because a ticket grants its owner a per-member overwrite, which beats the role deny. With `minutes`
it's a **timed jail** – the bot releases them automatically when the time is up (checked once a minute) and gives
their roles back. Leave `minutes` empty to jail **permanently** – they stay in prison until a staff member runs
`/prison free @member`. `/prison list` shows who's in (with the release time, or that it's until freed by hand).
Staff only (*Moderate Members*).

**AI moderation can jail, too:** turn on *AI moderation: send serious offenders to prison* (`AI_MOD_JAIL`, needs
the prison role + channel). For serious or repeated offences the AI then puts the member in the prison channel for
a while instead of only warning/timing out – it picks the length itself up to *AI moderation: longest jail*
(`AI_MOD_JAIL_MINUTES`, default 60, max 28 days), then they're auto-released. Turn on *AI moderation: allow
permanent jail* (`AI_MOD_JAIL_PERMANENT`) and the AI may, for the very worst cases, jail with **no timer** until
staff free them by hand. **AI prison guard:** turn on *AI prison guard* (`AI_MOD_GUARD`, needs the DeepSeek key +
prison channel) and the AI replies to jailed members in the prison channel as a calm, firm guard – it explains why
they're there and how long is left (or that staff decide), stays respectful even if they aren't, and points appeals
to staff (it can't actually free anyone). Give the guard its own personality with *AI prison guard: your own rules*
(`AI_MOD_GUARD_INSTRUCTIONS`); left empty it uses your *AI moderation: your own rules*.

**The AI learns from staff answers:** when a ticket is closed, the bot sends the staff answers of that ticket
(including `/aiconfirm` messages; the player's messages only as context) to the AI and asks for **general, new**
information – rules, how something works, prices, times – that isn't in the knowledge base yet. It's added to
`support_knowledge.md` under **"Learned from tickets"** (at most 5 per ticket, each marked with the ticket number)
and posted in the admin log, so the AI knows it in the next tickets. Nothing personal or case-specific (names,
IDs, order numbers, refunds or other individual decisions), no admin commands, and never what only a player said.
Check the section now and then on the web Knowledge base page and edit or delete entries freely. When the
knowledge base reaches its size limit (500,000 characters with RAG, 12,000 without) nothing more is added. To
spot duplicates the AI gets the sections related to the ticket. Switch:
**Settings → Tickets & AI → AI learns from staff answers** (`TICKET_AI_LEARN`).

**Knowledge base as a RAG pipeline (retrieval):** the knowledge base isn't pasted whole into every AI answer any
more. `support/rag.py` splits `support_knowledge.md` into **sections** – every `#` heading starts one, long
sections are cut at paragraphs – and for each ticket answer (and AI chat message) picks the sections that match the
conversation (the ticket description and the latest messages) with **BM25** search: headings count double,
light stemming ("wipes" finds "wipe"), accents ignored and typo tolerance ("wipee" finds "wipe"). Only those
sections – up to *Knowledge base text per AI answer* (`KNOWLEDGE_RAG_CHARS`, default 6,000 characters), plus the
first section with the server's basics when there's room – go into the prompt, in the file's order. The ticket AI
also gets a **`search_knowledge_base`** tool, so it can look up anything the first pick missed (in the knowledge
base's own words); `/ticket ai-test` shows the searches and "x of y sections (RAG)". A knowledge base smaller than
the budget is still sent whole, so small ones work exactly as before; a big one no longer gets cut off at 12,000
characters (up to 500,000 now). Everything runs on your PC in plain Python – no extra packages, no embedding API.
The web **Knowledge base** page shows how many sections it has and has a **test box**: type a question a player
might ask and see which sections the AI would get. Tip: clear `#`/`##` headings per topic help the search most.
Switch: **Settings → Tickets & AI → Knowledge base: send only the matching sections (RAG)** (`KNOWLEDGE_RAG`, on
by default; off = the whole file, first 12,000 characters, like before).

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

## Web store (PayPal & gift cards)

Players buy kits for real money on the player website: **your web link + `/store`** (the 🌍 Player website
plugin must be on). Three tabs: Kits can be put into **categories** (web interface → Store → Kits for sale → *Category*); the Kits page then groups them under headings with a category bar at the top (kits without one come last under *Other kits*).

- **Kits** – web interface → **💳 Store** → *Kits for sale*. Every kit staff make (Kit builder / Kits page – not
  the kits players bought) and every **in-game kit** of the server is added there **automatically** (checked every
  5 minutes and whenever you open the page) – **hidden until you set a price** (*Set price* → it's on sale). Bot
  kits give the kit's claim role (optionally for N days, `/customkit claim`); in-game kits are given in-game once –
  right away if the buyer is online, otherwise automatically the next time they join. Kits you *Remove* are never
  added again. You can also add a bot kit by hand with its own name, price and duration.
- **Build your own** – players pick items and amounts; the price comes from the price list (`prices.txt`, the same
  one the ticket AI and Tip4Serv use: bundles, free "with" items, caps and claim timers). Buyers can also give their kit a
  name (optional; otherwise it's named after their player). `STORE_CUSTOM_KITS`. The picker shows **every item of
  the staff kit builder**, grouped by category. Items with a rule in `prices.txt` use it; for all the others set
  *Price for every other item* (`STORE_DEFAULT_ITEM_PRICE`, per single item, e.g. `0.05`). While it is `0` those
  items are shown greyed out under "No price yet" and can't be bought – nothing is priced behind your back.
- **Gift cards** – players can buy gift cards (`STORE_GIFTCARD_VALUES`, e.g. `10,25,50`) and check a card's balance.
  They can also **combine** several gift cards onto one (Gift cards page → *Combine gift cards*, or
  `/giftcard combine` on Discord): the whole balance moves onto one card, the others are emptied; the combined card
  keeps the earliest expiry date of the cards involved.

**Checkout:** the buyer enters their in-game name – it must be linked to their Discord account (`/customkit link`),
that's who gets the kit – and optionally a gift card code, then pays with **PayPal**. Right after the payment the
kit is delivered like an approved kit purchase: the buyer gets the kit's role (for N days if it's time-limited, or a
new custom kit with their items) and claims it with `/customkit claim` in Discord. They also get a DM, and every
purchase is posted in `STORE_CHANNEL_ID` (default: admin log).

**Gift cards:** owners create them in **💳 Store → Gift cards** in the web interface or with **`/giftcard create`** in
Discord (server owner / Administrator; the codes are only shown to them) – value, how many, optional validity and note –
codes like `NUSF-UHNP-CXSX-EXJ3`. A card pays all or part of an order (the rest goes to PayPal) and keeps any
remaining balance. Cards can be disabled any time. Wrong codes are rate-limited (10 per 15 minutes per visitor).

**Admin key (free checkout for staff):** set *Admin key* (`STORE_ADMIN_KEY`, at least 12 characters) in Settings →
💳 Store. Staff open the store with `?admin=1` at the end of the address (e.g. `…/store?admin=1`); the checkout
then has an *Admin key* field – with the right key the item is delivered right away without any payment (works
without PayPal too). Every use is posted in the purchase log with a ping and marked "🔑 free (admin key)" under
Orders. Wrong keys: 5 tries per 15 minutes per visitor. Empty = off (default).

**Setup:**
1. [developer.paypal.com](https://developer.paypal.com) → *Apps & Credentials* → *Create App* → copy the
   **Client ID** and **Secret** into Settings → 💳 Store (`PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`).
2. Test with `PAYPAL_MODE=sandbox` (default, PayPal's test system with test accounts – no real money; the store
   shows a "Test mode" note). For real payments switch to `live` with your **live** Client ID/Secret.
3. Optional: `STORE_CURRENCY` (default: the price list's currency), `STORE_GIFTCARD_VALUES`, `STORE_CHANNEL_ID`.

### 🪂 Battle royale

Plugin **Battle royale** (Plugins page, off until you switch it on). Rounds of battle royale on your server: players
drop from the sky at random spots inside the zone with a parachute and a pistol, a **ring of radiation** closes in,
the last one alive wins.

- **Lobby:** an admin runs `/br start [seconds]`, players join on Discord with `/br join` (linked with
  `/customkit link` and online in-game); admins can `/br add` / `/br remove` players. `/br status` shows the lobby, who's
  alive (with kills) and the zone. `/br stop` ends a round and deletes its zones.
- **Start:** with *Kill players at the start* on (default) everyone is killed once (`global.killplayer`), so they
  respawn with an empty inventory – their gear stays in a bag where they died (the lobby announcement warns them). The
  moment one respawns the bot gives the start kit and tells them in-game to **put the parachute on** – after *Time to
  put the parachute on before the drop* (default **15 seconds**, a 5-second warning before) it teleports them to *Drop
  height* (default y 250) above a random spot inside the zone (`global.teleportpos`). Respawning again while waiting
  starts the timer over with a fresh kit; 0 seconds = teleport straight away.
- **Parachute already on:** pick an **in-game kit** as *Start kit* with the parachute in the **Wear** container (and
  the pistol in the Belt) – a kit puts items straight into their slots. Without a kit the *Start items* (default
  `parachute 1`, `pistol.semiauto 1`, `ammo.pistol 40`) are given with `inventory.giveto` and land in the inventory.
  Test once in-game that your server accepts the parachute in a Wear kit.
- **Zone:** the safe circle starts at *Start zone radius* around *Start zone centre* – 0 (default) = the **whole
  island**: the circle reaches the map's farthest corner, so the radiation ring starts outside the map. Drops land on
  the map, away from its outer *Keep drops away from the map edge* % (default 12 – the sea around the island). Just
  outside it the bot fills **everything** with tall **radiation boxes**: 40 boxes around the circle, each starting
  exactly at the zone edge and reaching outward past the map's farthest corner, wide enough to overlap all the way out
  – so there's no spot outside the zone without radiation, however far out a player runs. *Box height* (default
  600 m, centred on *Height of the zone centres*, default y 100) covers hills and the air
  (`zones.createcustomzone "br_rad0_0" (x,y,z) <rotation> Box (<width>,600,<depth>) 1 1 <radiation> 1 1`, orange ring
  and "RADIATION" message) – plus a blue border without radiation. When the zone changes, the **new boxes are made
  first and the old ones deleted after** (the names alternate between `rad0`/`rad1`), so there's never a moment
  without radiation. *Radiation strength* (default 50 – the guide's example uses 52) sets how fast it hurts. **Check
  once in a test round** that the orange boxes start exactly at the zone edge and point straight outward – if they
  cut into the safe zone, set *Box turn* to reversed. *Radiation zone shape* can switch to spheres (a thinner ring). Every *Zone shrinks every*
  minutes (warned *Warn before shrinking* seconds before) the circle shrinks to *New zone size* % around a new centre
  inside the old circle, down to *Smallest zone*; the guide has no command to resize a zone, so the boxes are made
  again (new first, then the old ones deleted). *Radiation pieces at most* (default 40) is the number of boxes.
- **Vending machines mark the border:** with *Vending machines on the zone border* on (default) the bot puts
  *Vending machines around the border* (default 16) high in the sky (*Vending machine height*, default y 200) on the
  edge of the safe circle – they show on the map, so everyone sees where the zone is. Spawned with
  `entity.spawn vendingmachine.deployed x,y,z` (guide: Entity spawning); the bot reads each machine's ID from the
  server's reply and deletes the old ones (`entity.deleteentity <id>`) every time the zone changes and when the round
  ends (also after a bot restart). If a reply has no ID the bot stops placing machines for that round, so nothing it
  can't delete piles up. All three (spawn command, ID pattern, delete command) are settings – check them once with
  **Test a marker** (below).
- **Control panel – Server → 🪂 Battle royale** (owner): the round's state (zone size and centre, zones, vending
  machines, next shrink, players and kills) and buttons: **🧪 Start test round (no players)** – zone, radiation and
  vending machines only, no kills or drops, no winner, no in-game announcements, shrinks only when you press **🔻 Shrink
  zone now** (that button also works in real rounds); **⏹ Stop round** deletes everything; **🗑 Delete all zones** stops a running round and deletes every battle
  royale zone by name – also ones left over from earlier rounds or older versions (`br_rad_*`, `br_rad0_*`, `br_rad1_*`,
  `br_border*`); **🏪 Test a marker** spawns
  one vending machine above the map centre, deletes it again and shows the server's replies (the ID it found, the
  delete command's answer) – the plugin's settings are on the same page. **🧹 Clear vending machines** sends the delete command for every
  vending machine the bot has spawned (it remembers their IDs, up to 2,000) and shows the server's answers – so once
  the delete command is right, one press clears them all; **Forget the list** empties it when they're really gone.
- **Far outside:** players farther out than the radiation (with boxes: beyond the map edge, e.g. in the sea) are
  read with `server.printpos` every *Check positions every* seconds, warned in-game and killed after *Kill players
  beyond the radiation after* seconds (0 = never).
- **Out and winner:** any death of a dropped player counts (PvP, radiation, fall, suicide, the bot's kill) and leaving
  the server; "X left" is announced in-game. The last one alive wins – announced in-game and in the channel, with
  optional *Points for the winner*, *Points per kill* and *Kit for the winner*. Its zones are deleted; a round cut off
  by a bot restart is ended at the next start and its zones deleted.
- Every command is a setting. Try a round first and adjust *Radiation strength*; if the boxes look turned sideways in-game, tell us. Best
  run on a quiet server or your second server – the radiation ring hits everything in it, also bases.

### 🛡️ Offline protection zones

Switched on and set up in the control panel under **Server → 🛡️ Offline protection** (off until you switch it on).
One protection zone per in-game team. The **team leader** places it at his position with the quick chats *"Can I build here?"* then *"Yes"* (within *Seconds to answer "Yes"*, default 20); members and players without a team get a short in-game note. Placing again moves the zone. The zone turns **red** when every member has been offline longer than **Offline time before red** (minutes, default 30) and **green** the moment any member joins. Teams and leaders are read from the server with `relationshipmanager.teaminfoall` at start and every *Re-read all teams* minutes (default 10), kept up to date from the team log lines in between (the bot makes sure `relationshipmanager.logteamactions 1` is on), and looked up with `relationshipmanager.findplayerteam` when an unknown player places; online/offline comes from the player list. Only commands from the Rust Console Edition guide *Custom zones* are used: the leader's position via `server.printpos "<name>"`; the zone via `zones.createcustomzone "team_<id>" (x,y,z) 0 Sphere <radius> 1 1 0 1 1` (*Zone radius*, default 30 m) plus `zones.editcustomzone` `showarea 1` (a ring visible within 100 m) and `showchatmessage 1`; **red** = `allowbuildingdamage 0`, `color (255,0,0)` and an enter message; **green** = `allowbuildingdamage 1`, `color (0,255,0)`; moving or a team that's gone = `zones.deletecustomzone`. Every command is an editable setting (placeholders `{zone}` `{team}` `{leader}` `{x}` `{y}` `{z}` `{radius}`); an empty one is never sent and the page says what's missing. **Zone size (team size and VIP):** *Zone size by team size* takes one line per team size, `members=radius` – e.g. `1=20`, `2=25`, `3-4=30`, `5+=40`; team sizes no line covers use *Zone radius*. VIP teams get *VIP zone size (%)* of that (default 150 = 1.5×). VIP = the linked Discord account (`/customkit link`) has the *VIP Discord role*, or the name is on the *VIP players* list; *VIP size when …* decides whether the leader must be VIP (default) or any member. When a team grows or shrinks, or its VIP status changes, the zone is deleted and created again at the same spot with the new size (the guide has no command to resize a zone), then gets its red/green status again; the panel shows every zone's size with a ⭐ VIP badge. Zones from before this feature keep their size until something changes. **Leaving the team (optional):** with *Delete the zone when a member leaves the team* on, the zone is deleted the moment anyone leaves the team or is kicked (from the team log line – or, if that line was missed, as soon as the team list no longer has them); the leader is told in-game and has to place it again. Joining never deletes a zone. Off by default. **Expiry:** a zone whose team nobody has been online for *Delete the zone after … hours* (default 72, 0 = never) is deleted with the remove command; the leader can place a new one when the team is back. It's a **main feature** of the bot with its own page in the control panel (**Server → 🛡️ Offline protection**, owners only – not on the Plugins page): on/off, settings, and every zone with its status, which members are online right now (and who keeps a green zone green, or when it turns red), offline since, when it will be deleted, and a Delete button. **Status sent again:** every *Send every zone's status again every … minutes* (default 10, 0 = only on changes) and right after RCON reconnects, the red/green commands are sent again for every zone – so a zone reset by a server restart, or one whose command got lost, matches the panel again. A status change only counts once its commands went through; with RCON down it's retried at the next check (every 20 s). Optional Discord channel for status changes and expired zones. **Help from the support AI:** players who struggle with the quick chats can open a ticket – the ticket AI looks up their team and zone (`get_my_protection_zone`) and places or moves the zone for them (`place_protection_zone`) with the same rules as in-game: only for the ticket owner's own team (from their linked account), only if they're the team leader, and only while they're online, at the spot they stand on (the AI asks them to stand in the middle of their base first). A placed zone is confirmed in the ticket and in the admin log ("via ticket #… (AI)").

### 💣 Nukes in the web store

Players can buy a **nuke** for real money on the website (store → **💣 Nukes**, PayPal and/or gift card). Once
it's paid, the bot drops it on Discord: a *NUKE* message with a **Claim** button in the nuke channel (no
@everyone ping). Every Discord member except the buyer can press it once and gets the set points, for 24 hours
or until the set number of members have claimed; the button keeps working after a bot restart. Set it on the
control panel's **💳 Store** tab (card **💣 Nukes**): the price (0 = not for sale), the points every member gets,
how many members can claim (0 = everyone) and the channel (empty = the Discord server's system channel) –
saved in `.env` as `STORE_NUKE_PRICE`, `STORE_NUKE_POINTS`, `STORE_NUKE_CLAIMS`, `STORE_NUKE_CHANNEL_ID`. The buyer enters their linked in-game
name like for kits. If the drop can't be posted (e.g. no channel), the order shows as failed and staff click
Retry (Store → Orders). Needs points & shop on.

### Player accounts & team slots (My account)

Players can log in on the website to manage their **own** custom kits (the ones they bought): **your web link +
`/account`** (the *My account* link appears when `PLAYER_ACCOUNTS` is on, the default).

- **Login** is a one-time code – no password, nothing to set up. The player enters their in-game name; the bot DMs
  a 6-digit code to the linked Discord account (linked with `/customkit link`); entering it logs them in for 30
  days (a cookie; only a hash of it is stored). They must have DMs from server members on.
- **Add items** to one of their kits: the same item picker as the store, priced by the price list. After payment
  the items are added to that kit.
- **Team slots** let a teammate use the kit. Set a price with `STORE_SLOT_PRICE` (per slot per 30 days, `0` = off)
  and a cap with `STORE_SLOT_MAX`. On My account the owner buys slots (per month, PayPal/gift card) and then puts a
  teammate on each slot by their in-game name – that teammate gets the kit's role and can `/customkit claim` it.
  When a slot's month runs out the bot takes the role back (unless the teammate still has the kit another way) and
  DMs the owner; the owner can free a slot or re-assign it any time. Players only ever see and change their own
  kits and slots.

**Safety:** prices are always calculated by the bot – the browser only sends what was picked. The bot creates the
PayPal order for the exact amount and delivers only after it captured the payment and PayPal reports COMPLETED for
exactly that amount and currency; each order is delivered once. Buyers who pay but close the page still get their
order (checked every 5 minutes); orders not paid within 3 hours expire and a held gift card amount goes back to
the card. If a paid order can't be delivered (e.g. the buyer left the Discord), staff gets an alert and can click
**Retry delivery** under 💳 Store → Orders. Refunds are done in your PayPal account.

## Plugins

Features that aren't part of the core come as **plugins**: one Python file each, switched on/off and configured in
the web interface (**🧩 Plugins**, owner only). Switching, settings and reloading take effect immediately – no
restart. A broken plugin never stops the bot: it shows the error on its card and stays off.

**📌 Your plugins in the menu:** any plugin can get its own tab in the control panel's sidebar – click **📌 Add
to menu** on its card – in the category you choose: **Server, Kits, Community, Plugins or Admin** (the *Menu
category* box on its tab, or the box next to it on the Plugins page). The Plugins page lists the pinned ones at the
top (*In the menu*: open, category, move ↑ ↓, ✕ remove) and the tab itself has **📌 Remove from menu**. Choice,
categories and order are saved in `data/plugins.json`.

The panel's logo (top left and on the login screen) is the **Discord bot's own profile picture** – change it in the
Discord Developer Portal and the panel follows (it's served by the bot at `/img/bot-avatar.png`).

- **Built-in plugins** are in `plugins/` and come with updates.
- **Your own plugins** go to `data/plugins/` (upload a `.py` file on the Plugins page, or copy it there and click
  ↻ Reload). Updates never touch them, and they are in every backup. How to write one: [PLUGINS.md](PLUGINS.md).
  Only use plugins from people you trust – a plugin runs inside the bot with full access.
- **Plugins survive updates.** Everything in `data/plugins/` (uploaded, AI-made, copied there) plus all on/off
  states and settings (`data/plugins.json`) are never touched by an update. A file there with the **same name as a
  built-in** replaces that built-in – so an update that adds a built-in called like your plugin can't hide yours, and
  you can keep a changed built-in (card shows *Your copy – replaces the built-in*; *Delete my copy* brings the
  built-in back with the same on/off state and settings). If you edited a built-in file in `plugins/` directly (or
  put your own file there), the updater copies your version into `data/plugins/` before it replaces or deletes
  that file, says so in the admin log, and a changed plugin no longer blocks git updates. A switched-on plugin that
  can't start after a restart (e.g. written for an older bot version) is reported in the admin log.
- **Create a plugin with AI:** its own **✨ Create plugin** page in the sidebar: describe what you want in plain
  words and the integrated AI (DeepSeek, the same key as the support AI) writes the plugin for you. You see the
  code, a short summary and any safety warnings in the window, can edit it, and only when you click *Create* is it
  saved (switched off, like any plugin) – then it shows up on the Plugins page to switch on. The same page also has
  the manual *upload your own .py file* option. AI-written code runs inside the bot with full access, so read it
  before you create it – you stay in control.
- On/off state and settings are saved in `data/plugins.json`; the data plugins keep is in the database.

### Growth plugins (built in)

| Plugin | What it does | Default |
|---|---|---|
| 🤝 **Invite rewards** | `/invites stats`, `/invites top`. When someone a member invited links their player and plays *N* hours, both get points (optional recruiter role). Rejoins, self-invites and brand-new Discord accounts don't count. Needs the **Server Members Intent** and the bot permission **Manage Server**. | off |
| 🌍 **Player website** | A public website at the root of *your web link* (e.g. `https://yourdomain.com/`; old `/play` links forward there) in the Kryvon look (dark, rusty red/orange, your logo – upload another one on the plugin card): live status, next wipe, leaderboards (kills, K/D, playtime, teams), player profiles with search (stats, team, badges, bounty), the points shop with item pictures, rules, about, how to join, Discord and store buttons. Read-only – no login, no secrets, no Discord IDs. **Seasonal theme** (setting, default *auto*): during October the site turns spooky – a purple night sky with the moon, bats flying past, cobwebs on the cards, a spider on its thread under the menu, pumpkins and a *Happy Halloween* banner on the home page (text adjustable); *halloween* = always, *off* = never. On phones the menu is a ☰ button. | off |
| 📺 **Streamers** | Who streams on your server – live on the player website and in Discord. List them in the settings, one per line: `platform | channel | in-game name` (e.g. `twitch | kryvon_live | Gamer7`, `kick | kryvon`, `youtube | @KryvonRust`). Every few minutes (default 2) the bot checks who is live: **Twitch** with the official API (free *Client ID* + *Secret* from dev.twitch.tv → Console → Register Your Application), **Kick** with its public channel data, **YouTube** from the channel's /live page (no API key – YouTube's quota is far too small for this; works as long as YouTube keeps that page the same). **Website:** a **Streams** page (`/streams`) with the biggest live stream embedded – watch right on your site with the platform's own player – the others as cards (click to watch there) and links to the offline streamers. **Discord:** when someone goes live, an embed with title, game, viewers and preview in the chosen channel (optionally pinging a role, once per stream); it updates while they're live and says "Stream ended after …" afterwards. A stream that's back within 30 minutes continues the same message (no second ping); two checks in a row without it are needed before it counts as ended. `/streams` shows who's live. *Only while they play on our server*: a stream only counts while the streamer's in-game player (3rd column) is online. | off |
| 📊 **Player count channel** | A channel whose **name** shows how many players are online, e.g. *🟢 Online: 12/50* – visible in everyone's channel list. By default the bot creates a voice channel for it at the top of the server that nobody can join (needs the *Manage Channels* permission); or pick your own channel. The text is a setting (`{players}`, `{max}`, `{queued}`, `{joining}`, `{server}`), plus a separate name while the server is offline (*🔴 Server offline*). Discord lets a bot rename a channel only twice every 10 minutes, so the name updates at most every *Update every … minutes* (default and minimum 5) and only when it changed. | off |
| 🎰 **Casino** | A *Casino* page on the player website (`/casino`) where logged-in players (player login, *My account*) gamble their points: 🎰 slots, 🪙 coin flip and 🎲 dice with a return to player of exactly 95 %, 🎡 European roulette (one zero – the real 97.3 %) and 🃏 blackjack with real casino rules (6 decks, dealer stands on soft 17 and peeks, 3:2 blackjack, double incl. after a split, one split – ≈ 99.5 % with basic strategy). Every game is animated for about 5 seconds before the result shows: the slot reels spin and stop one after another, the roulette wheel spins and stops on the winning number, the coin flips, the dice number rolls, and blackjack cards are dealt and the dealer's hole card turns over. When the spin ends the balance counts up or down to the new amount and a win gets a short confetti burst. Games play without reloading the page. The website allows scripts only from itself (`script-src 'self'` – no inline or third-party scripts, so injected code can't run); without scripts the casino still works with plain page loads. The animations only move things (smooth on phones too) and are skipped when the device asks for reduced motion. Results come from a secure random generator on the server; fractions of a point are rounded fairly, so the RTP is exact. 🔻 **Plinko** like on the well-known crypto casinos: a triangle of pegs (8–16 rows), low/medium/high risk, coloured slots from red edges (up to 1611×) to the yellow middle. Drop up to 20 balls at once (*Plinko: most balls per drop* in the settings) and drop again while they are still falling; each ball is one bet. The server picks every ball's path, the page animates it peg by peg on a canvas. Every board returns exactly 97.5 % (fixed paytables, checked in the tests). **On Discord** every game except poker can be played too, in an embed with an animated picture that plays once and stops on the result: `/slots` (the reels spin and stop one after another), `/coinflip` (the coin flips and lands), `/dice` (the number rolls and the marker slides along the win/lose bar), `/roulette` (the wheel spins, the ball drops into the pocket) – the result, win/loss and new balance appear when the animation ends, with a **Play again** button – and `/blackjack` with **Hit / Stand / Double / Split** buttons and a picture of the table after every move (an unfinished hand, also from the website, is shown again). Same games, odds, bets and limits as the website; switch them off with *Casino games on Discord* in the plugin settings. Plinko too: `/plinko bet balls risk rows` takes the bets and posts an embed with an animated picture of the board – the balls fall along the paths the server picked, then the embed shows each ball's multiplier, the win/loss and the new balance, with a **Drop again** button (only for that player). Same paytables, bets and limits as the website; switch it off with *Plinko on Discord* in the plugin settings. ♠️ **Poker tables** (Texas Hold'em, no limit, up to 6 players): players play *against each other* – sit down with a buy-in from your points, your chips go back to your points when you leave. Real rules: moving button, blinds (heads-up: the button posts the small blind), real minimum raises, side pots for all-ins, split pots, best-5-of-7 hands. The table updates live in the browser; each player only ever receives his own cards (the others are shown at the showdown), the deck stays on the server. The dealer deals by a timer: once 2 players sit, the next hand comes after a few seconds ("Next hand in 5 …" on the table, *seconds between hands* in the settings, default 8). Who runs out of time checks or folds and sits out until he's back (stood up after 5 minutes, chips back). If the bot stops or restarts, every player gets back his chips – also those already in the pot. Tables are set in the plugin settings (one per line: name | small blind | big blind | smallest buy-in | biggest buy-in), plus seconds per turn and an optional house cut (rake, off by default). Settings: smallest/biggest bet, a daily loss limit per player, each game on/off. The plugin card shows rounds played, points bet/paid out and the house profit. Play money only – points are earned in-game and can't be bought or cashed out. | off |
| 🎖️ **Battle pass** | A *Battle pass* page on the player website (log in with *My account*): a season of 100 tiers (editable) with a **free** and a **premium** reward row – points, items given in-game, and the premium currency (**💎 Coins**, name and icon adjustable). XP from time online (10/min), PvP kills (50) and a daily bonus (300); 500 XP per tier. The default premium row has a weapon or armour piece every few tiers (an AK at 99). The **premium pass costs 1100 Coins** and the premium row pays out **1200 Coins** over the season – enough for the next pass. Coins come from **coin packs in the web store** (PayPal or gift card, packs set in the plugin settings), from the pass, and from admins (`/coins give`, `/coins balance`). **Or buy the premium pass for real money:** set *Premium pass price in the web store (money)* (e.g. `9.99`). The Battle pass page then also shows *… or buy it for 9.99 €* (PayPal / gift card), and the Store gets a 🎖️ Battle pass tab. Rewards are claimed one by one or with *Claim all*; items need the player online and are never lost if they're not. With several servers, item rewards are given on whichever of your servers the player is on. Raise *Season number* to start a new season. | off |
| 🎁 **Starter kit** | A name the bot has never seen gets a welcome message in-game and a one-time kit. | on (choose the kit) |
| 🌱 **Low-pop bonus** | While fewer than *N* players are online, playtime points are multiplied – pays people to seed the server. | on |
| 🔥 **Filling-up ping** | Members opt in with `/pingme on`; the role is pinged when the server reaches e.g. 10 or 20 players (cooldown per threshold). | off (choose a role) |
| 📬 **Wipe comeback DM** | *N* hours before a wipe, linked players who haven't played for a few days get a DM. Max. one per wipe, sent slowly, opt-out with `/wipedm off`. | off |
| 🐦 **Early-bird reward** | The first *N* linked players online after a wipe get points and/or a kit (each Discord account once). | on |
| 🎯 **Bounties** | `/bounty place <player> <points>`, `/bounty list`. The killer collects via the killfeed; the placer, teammates and the target's own account can't. Unclaimed bounties are refunded. | on |
| 📰 **Weekly recap** | Weekly post: top killers, playtime, K/D, most embarrassing killer, new players, peak population, biggest team. Optional points for the top players. `/recap` = preview. | on |
| 🏅 **Achievements** | Badges for kills, hours, `/daily` streaks, wipes played, bounties and invites – points + announcement, shown in `/profile`. | on |
| 🗳️ **Wipe feedback poll** | *N* hours after each wipe, Discord polls (your own questions) – `/wipepoll` posts them now. Needs the **Send Polls** permission. | on |

All rewards go to **linked** accounts (`/customkit link`) – Console Edition only gives player names, so this stops
alt accounts from collecting twice. Plugins that pay points need **Points & shop** to be on.

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
roles below). (Nukes are sold for real money in the web store, see *💣 Nukes in the web store*.)
Kits and items need the linked player to be online. If a delivery fails (e.g. RCON down),
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

The bot watches the server log **constantly** (the live RCON console stream, which reconnects on its own), so a
raid-item spawn is caught the moment it's logged. When someone spawns **rockets or C4** and isn't allowed to, the
bot immediately runs `removeowner "<name>"`, `removemoderator "<name>"`, `global.god 0 "<name>"` (god mode off)
and `global.teleportpos (999,999,999) "<name>"` (teleports the abuser far away), then – after a short wait
(*Wait before the kill*, `ABUSE_KILL_DELAY`, default **10 seconds**) – `global.killplayer "<name>"`. When that
player **respawns** (within an hour), it runs `global.sleep "<name>"`.

**Reliability:** if RCON blinks while a punishment runs, the commands that failed are sent again (in order) as soon
as the connection is back – up to 3 times – and the alert says loudly if any command still didn't go through (run it
by hand with `/rcon`). The bot's own gives only hide a spawn when they contain a raid item (a shop kit with wood or
metal no longer silences the protection). The log reaches the bot over RCON, so while RCON is down nothing can be
seen: when it reconnects after more than 3 seconds, the admin log says *"Abuse protection was blind for N s"*, so you
know to check the server log. ⚙️ Settings → 🛡️ Admin abuse protection shows the last spawns and what happened.

Deleting their stuff is **never automatic**: the alert in the alert channel (with a ping for `ALERT_ROLE_IDS`) is a
**case** with the buttons **🗑️ Approve deletion** and **✖️ Reject** (bot admins only). Only on approval does the
bot run `entity.deleteby "<name>"` (**deletes everything that player owns** – bases, deployables). Staff can also
decide in the web interface (🚩 Reports → 🚨 Admin abuse, or ⚙️ Settings → 🛡️ Admin abuse protection). Each case
can only be decided once; it survives restarts. Every step and the server's replies are in the admin log.
All commands can be changed in ⚙️ Settings → 🛡️ Admin abuse protection (*Commands to remove admin / before the
kill / teleport the abuser away / wait before the kill / kill command / right after the kill / only after staff
approval / when they respawn*, `-` = nothing, and an empty teleport or a 0-second wait skips that step).

**Loop (like the RCON repeater):** *Loop: commands to repeat* (default `global.killplayer "{name}"`), *Loop: every …
seconds* (0 = off, at least 3) and *Loop: how many times* (0 = forever). After the punishment the bot repeats those
commands for the abuser until the count is reached, or until staff press **⏹ Stop loop**. That button is on the
Discord message under the alert and in ⚙️ Settings → 🛡️ Admin abuse protection, which also lists every running loop.
A running loop goes on after a bot restart.

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


**AI abuse watch:** on top of the fixed rule above, the AI reads the admin-related server log lines – admin-panel
spawns, kit gives by admins, bans, role changes and the kills of those admins – every `AI_ABUSE_WATCH_MINUTES`
(default 5) and flags suspicious **patterns**: loot/raid items/weapons/resources for themselves or friends, spawning
right before kills or raids, banning someone they just fought, handing out admin roles, many gives to the same
players. A flag is only an **alert** (*🕵️ Suspected admin abuse – name*, with the reason and the evidence lines) in
`AI_ABUSE_CHANNEL_ID` (default: alert channel / admin log); severe ones ping the alert roles. Nothing is done
automatically, and the same suspicion isn't repeated within 30 minutes. Trusted owners (the allowed names/roles
above) and the bot's own gives (shop, kits, rewards) are never flagged. Your own rules: *AI abuse watch: your own
rules* (`AI_ABUSE_INSTRUCTIONS`, e.g. "Admins may spawn building materials during build events"). Switch:
`AI_ABUSE_WATCH` (default on, needs the DeepSeek key). Only these admin lines are sent to DeepSeek, not the whole log.
### Alerts & backups

- **Server-down alert:** if the bot has had no RCON connection for `SERVER_DOWN_ALERT_MINUTES`
  (default 10), it pings `ALERT_ROLE_IDS` (default: admin roles) once in `ALERT_CHANNEL_ID` (default:
  admin log channel) with the last error – and posts again when the server is back, with the
  downtime. `0` = off.
- **Backups:** once a day and right before every automatic update, the bot saves the database
  (stats, kits, tickets, links, giveaways, timed roles, purchases), web logins, rules, knowledge base and price
  list as a ZIP in `data/backups/`. Backups older than `BACKUP_KEEP_DAYS` (default 7) are deleted, the newest 3
  always stay. Your `.env` (tokens, passwords) is **not** included unless you turn on *Include the .env*
  (`BACKUP_INCLUDE_ENV`). In the web interface (⚙️ Settings → 🚨 Alerts & backups) you can create one right
  away and download them.
- **Google Drive (or any cloud folder):** install [Google Drive for desktop](https://www.google.com/drive/download/)
  on the bot PC and enter a folder inside it in *Also copy backups to folder* (`BACKUP_COPY_DIR`, e.g.
  `G:\My Drive\RCE bot backups`). Every backup ZIP is copied there (old copies are removed after the same number of
  days), and **`live-backup.zip`** there is refreshed every `BACKUP_LIVE_MINUTES` (default 10, `0` = off) whenever
  something changed – Google uploads it by itself and keeps older versions (right-click → *Manage versions*).
  Files are written under a temporary name first, so a half-written file is never uploaded. Don't put the live
  `data/bot.db` itself into the Drive folder: a database that's synced while it's written can break. If copying
  fails (e.g. Drive not running), the admin log and the backup box say so.
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
