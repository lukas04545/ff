# Selling the bot as a subscription – multi-tenant architecture

## Assumptions (and why)

The stack is the bot in this repo: **Python 3.11+, discord.py, aiohttp (web panel + player website), SQLite per
server**. It already runs every extra Rust server as a **separate process in its own folder** (own `.env`,
database, web port, Discord token). The SaaS design keeps that model and puts a **control plane** on top. A rewrite
to a shared multi-tenant process would be riskier and weaker on isolation.

| Decision | Default chosen | Why (solo developer) |
|---|---|---|
| Hosting | One Linux VPS (8 vCPU / 16 GB runs ~60–100 servers) behind Cloudflare | Cheap and simple; scale out by adding nodes later |
| Unit of isolation | **One OS process per server** (`python bot.py`, `RCE_SAAS=1`, own folder) | Crash, memory leak or Discord rate limit stays inside one server |
| Tenant data | **One SQLite file per server, encrypted with SQLCipher and a per-server key** | Strongest separation for the least ops: no shared schema, no `WHERE tenant_id` to forget |
| Control plane DB | SQLite (WAL); same schema works on Postgres | One writer process; easy backups |
| Web routing | **Caddy** (`<slug>.yourdomain.com` → the server's port) + Cloudflare wildcard DNS in front | Automatic TLS, websockets/streaming, DDoS absorbed by Cloudflare |
| Billing | **Stripe** Checkout + Customer Portal + webhooks (no SDK) | Stripe handles cards, dunning, invoices, EU VAT |
| Owner panel | Website at `portal.<domain>/management` (own cookie path, password + TOTP, rate limits, optional IP allowlist) plus a private listener on `127.0.0.1` | Reachable from anywhere without SSH; `CP_OWNER_ALLOW_IPS` or `CP_OWNER_PATH=off` shrink the exposure again |

## Overview

```
                    Cloudflare (DNS *.yourdomain.com, DDoS, TLS)
                                   │
                              Caddy :443
        ┌──────────────────────────┼───────────────────────────────┐
  portal.yourdomain.com     kryvon.yourdomain.com          other.yourdomain.com
        │                          │                               │
 ┌──────▼───────────┐     ┌────────▼─────────┐            ┌────────▼─────────┐
 │ CONTROL PLANE    │     │ tenant server #1 │            │ tenant server #2 │   ← one process each
 │ portal  :8000    │     │ bot.py RCE_SAAS  │   …        │ bot.py RCE_SAAS  │     own token, folder,
 │ owner   :8090*   │     │ web :20000       │            │ web :20001       │     encrypted DB, port
 │ supervisor ──────┼────►│ /saas/health     │            │ /saas/health     │
 │ health poller    │     └──────────────────┘            └──────────────────┘
 │ control.db       │     /var/lib/rce-saas/tenants/c1/kryvon/{.env,data/bot.db(SQLCipher),logs}
 └──────────────────┘     * owner panel on 127.0.0.1 + portal/management
```

* **Supervisor** (`supervisor.py`) runs a reconcile loop. It compares what *should* run (server active, customer
  active, subscription paid or in grace, token set) with what *is* running, then starts or stops processes. Crashes
  restart with backoff (5 s … 5 min). Five crashes within 10 minutes count as a **crashloop**: the server stays
  stopped and the owner panel flags it. Each process gets a memory limit, its own process group and its own log.
* **Why one process per server and not one process for many bots:** discord.py keeps one gateway connection and one
  rate-limit state per client. In separate processes, one customer's 429 storm, memory leak or bad plugin
  configuration can't stall the event loop of another. Cost: about 60–120 MB RAM per server, which is fine on one
  VPS for the first ~100 servers.
* **Scaling past one host:** the same supervisor runs as an agent on each node and reconciles only the servers whose
  `servers.node` is its name. The control-plane database moves to Postgres, the per-server folders stay local to
  their node, and Caddy points `<slug>` at `node-ip:port` over WireGuard. Swap `ProcessLauncher` for a container
  launcher (one container per server, read-only code mount, per-container user) when you want kernel-level isolation.

## Bot identity: shared bot (default) or own bot (paid add-on)

Every server runs on one of two bots:

* **Shared bot (default, included):** the platform's bot (`CP_SHARED_BOT_TOKEN`). In the portal the customer clicks
  **Add the bot to your Discord**, which goes through Discord's OAuth2 install page.
  * The callback exchanges the code with the client secret, and **Discord's answer** names the server the bot was
    added to. The `guild_id` in the URL is ignored, and only someone with *Manage Server* there can add a bot.
  * The `state` is HMAC-signed, tied to the customer and server, and expires after 15 minutes.
  * One Discord server can belong to only one tenant server (unique index).
  * Each tenant still runs **its own process** (own data, website, settings); they all share the token.
  * **Guild fence** (`saas_fence.py`): without sharding, Discord sends every connection every guild's events and
    lets it act anywhere. The fence wraps every gateway parser, so other guilds' events are dropped before
    discord.py builds objects. That includes slash commands, buttons and modals, which bypass normal dispatch, so
    another tenant's button click can never run here. DMs are dropped too.
  * It also wraps every REST call: only its own guild and its own channels, no global command changes (they're
    every tenant's), and no listing of the bot's guilds. Commands are synced to the tenant's guild only. Logins of
    the shared token are spaced 6 s apart (Discord: one identify per 5 s).
  * *Scaling note:* every shared-bot process receives every shared guild's events (it drops them, but still
    downloads them). That's fine for dozens of shared-bot servers. For hundreds, move to a single gateway
    connector that forwards each guild's events to its tenant process; the fence and the per-tenant processes stay
    the same.
* **Own bot (add-on `custom_bot`, switched on by the owner):** the customer pastes their own bot token, so the
  server has its own name, avatar and rate limits, and no fence is needed. Without the add-on the portal doesn't
  offer it and the server refuses the token (covered by a test).
  * Switching the add-on off moves the customer's servers back to the shared bot: the token is deleted, nothing
    else is, and the customer adds the shared bot to their Discord again.

* **Moving servers onto the shared bot:** `python3 -m control_plane shared-bot` (all servers; `--server N`,
  `--guild <id>`), or owner panel → server → *Use the shared bot*. Each server is fenced to its Discord server (the
  `GUILD_ID` from its settings, or the id you enter). Its own token is deleted. The owner panel shows the shared
  bot's invite link: add it once to every Discord server that doesn't have it yet. Using your existing bot
  application as the shared bot keeps it in the Discords it's already in.

## The shop: packages (monthly subscriptions)

* **The portal is a shop:** the start page shows what the bot does and the packages (`plans` table,
  `control_plane/plans.py`). *Choose* leads to sign-up, then straight to Stripe Checkout for that package.
* **The dashboard guides new customers** in four steps, each ticked off by itself:
  1. package paid;
  2. server created;
  3. bot added to their Discord;
  4. RCON connected.
  Subscribers can switch packages there; Stripe prorates.
* **Owner panel → 🛒 Packages:**
  * name, monthly price (EUR/USD/GBP), number of servers, AI and own-bot add-ons, perks, "most popular", order,
    for sale yes/no;
  * saving creates the monthly Stripe price by itself (`STRIPE_PRICE_ID` isn't needed any more);
  * a new price makes a new Stripe price, and existing subscribers keep theirs;
  * a new shop starts with three example packages; packages are never deleted, only taken off sale.
* **A package decides the entitlements:**
  * `base_slots` comes from the package's server count;
  * the AI / own-bot add-ons are granted with `granted_by = "plan"` and only those are taken away on a downgrade;
  * add-ons and slot grants the owner gave by hand stay;
  * too many servers after a downgrade: the newest become `over_limit` (stopped, data kept).
* **Stripe drives it:** `checkout.session.completed` (metadata `plan_id`) and `customer.subscription.updated`
  (its price) apply the package.
* **Without Stripe:** owner panel → customer → *Set package* (optionally *free / comped*).
* **Shop name:** Settings → *Shop name* (`CP_SHOP_NAME`).

## Tenant websites: address, own domain, themes

* **Own address:** each server's website is `<address>.<domain>`. The customer can change the address in the
  portal (Website card), and the old address keeps working as an alias.
* **Own domain (add-on `custom_domain`):** comes with packages that include it, or the owner switches it on.
  * The customer enters e.g. `www.theirserver.com` and adds a CNAME to `sites.<domain>`.
  * Cloudflare for SaaS (SSL/TLS → Custom Hostnames, enabled once on your zone) serves it with a certificate. The
    Cloudflare setup creates `sites.<domain>` and sets it as the fallback origin.
  * The tunnel's last rule sends unknown hostnames to the portal listener, which routes them by the `domains` table.
  * Connecting it at Cloudflare: owner panel → 🌐 Domains with a token used once (*Zone › SSL and Certificates ›
    Edit*, not saved). Or, only if the owner chooses, a saved token with just that permission
    (`CP_CF_SAAS_TOKEN`) does it automatically.
* **The website is free:** on hosted servers the player website is on from the start, in every package.
  * Customers add a logo, a header picture and up to 6 server pictures (a gallery on the home page; big photos are
    made smaller in the browser).
  * They run their own web shop (kits, items, gift cards) with their own PayPal account: admin panel → Store, and
    Settings → Store. PayPal's return link follows the address the player used, including their own domain.
* **Premium = own domain:** besides packages that include it, customers can add the own-domain add-on to any
  package (`CP_DOMAIN_PRICE` per month). It's an extra item in their Stripe subscription (prorated). The webhook
  grants and takes back `custom_domain` (`granted_by = "addon"`), and a package change never removes it.
* **The management AI never edits code:** its code tools, `MANAGER_AI_CODE`, the `.bak`/Keep/Revert machinery and
  `revert_code.py` are gone.
* **Themes:** the player website has a *Look (theme)* setting (rust, neon, arctic, jungle, bloodmoon, gold, clean)
  next to the logo, colours and texts. The Halloween look still wins in October.

## Owner panel vs. a server's panel

* **The owner panel** has the same "command centre" look as a server's admin panel (`owner_ui.py`): a sidebar
  with coloured groups, stat cards (customers, servers, bots online, players, monthly revenue, need attention) and
  its own pages: Overview, System (the VPS and every server process), Audit log, Customers, Servers, Domains,
  Packages and Settings.
* **A hosted server's panel shows only its own things.** Not shown, and their API calls refused:
  * the Servers page, Create plugin and System;
  * the host's settings (token, ports, paths, updates, database …);
  * without the AI add-on: every AI setting and the knowledge base page (`saas_mode.hidden_views()`, AI keys locked).
* **Knowledge base and ticket learning are per server:** every tenant runs in its own folder (`RCE_HOME`), and the
  knowledge file is relative and locked there. What the AI learns from tickets is written into that server's
  file only.

## Add-ons and updates

* **Add-ons** (`features` table, owner panel → customer → Add-ons, or `python -m control_plane addon …`):
  `custom_bot` and `ai`. Like slots, only the owner can change them.
  * **AI** (tickets AI, AI chat, AI moderation) runs only with the add-on. The tenant then gets the
    platform's DeepSeek key (`CP_DEEPSEEK_API_KEY`) in its environment.
  * Without the add-on the bot forces the key empty. `DEEPSEEK_API_KEY` is locked in the tenant panel, so a tenant
    can't bring its own key to get around it.
  * Hosted bots load their `.env` with *override*, so the control plane's values (token, guild, AI key, port, DB key)
    are pinned after loading: a tenant's `.env` can never override them.
  * **Management AI is not part of the add-on.** It uses the platform key separately (`SAAS_OWNER_AI_KEY`) and
    is reachable in two places only:
    * **Owner panel → server → 🧠 Management AI**, always, for every server. The panel POSTs to the server's own
      bot at `127.0.0.1:<port>/saas/manage` with a per-server bearer token (`SAAS_MANAGE_TOKEN`, derived from the
      health token; any other caller gets 404). Risky tools run only if *allow risky actions* is ticked; code
      editing tools never exist in hosted mode. Every request is audited (`manager_ai.request`).
    * **The server's Discord management channel**, only on servers the owner switched on (owner panel → *Discord
      AI on/off*, column `servers.manager_ai`, env `SAAS_MANAGER_AI`). Elsewhere the channel stays silent and the
      `MANAGER_AI_*` settings are locked in the tenant panel.
  * When an add-on changes, the supervisor sees the server's environment changed and restarts it with the new setup.
* **Updates only from the owner panel:** tenants can't update themselves. Their auto-updater is off, the update API
  is blocked and the panel says "installed by your host".
  * Owner panel → **Updates**: *Check for updates* shows the installed and newest version. *Install update for ALL
    servers* installs it into the shared code folder, then restarts every running server one by one (rolling, never
    all at once). The control plane then asks you to restart its own service.
  * CLI: `python -m control_plane update [--install]`.

## The management Discord (HQ)

Connect your own Discord server to the control plane (`control_plane/hq.py`, runs inside `python -m control_plane run`).
The bot builds everything by itself and repairs it at every start: roles, categories, channels, permissions and panels.

* **One token for everything:** leave `CP_HQ_BOT_TOKEN` empty and HQ runs on the shared bot. It then never leaves
  other Discords (those are your customers'), only loads the management Discord's members and never answers
  outside it. A server's bot can't be bound to the management Discord. `CP_HQ_BOT_TOKEN` still gives HQ a bot of
  its own.
* **Setup:**
  1. Use the shared bot (above) or create a separate bot in the Discord Developer Portal. Switch on its *Message Content* and
     *Server Members* intents.
  2. Invite it to your management server with *Manage Roles*, *Manage Channels*, *View Channels*, *Send Messages*,
     *Embed Links* and *Read Message History*. Easiest: give it *Administrator* in that private server only.
  3. Set `CP_HQ_BOT_TOKEN`, `CP_HQ_GUILD_ID` and `CP_HQ_ADMIN_IDS` (your Discord user id, comma list), then restart
     the control plane.
* **Staff roles** (created by the bot, no Discord permissions – they only open the HQ channels; one per person):
  * **HQ Admin** – everything: all buttons, add-ons, slots, updates, risky AI actions, staff roles, the audit feed.
  * **HQ Support** – the management AI without risky actions, *Restart* / *Reactivate* servers, sees every customer.
  * **HQ Viewer** – reads every panel and alert, can't type or press anything.
  * The server owner, `CP_HQ_ADMIN_IDS` and `CP_HQ_ADMIN_ROLE_ID` always count as HQ Admin.
  * Give or take the roles in `#hq-staff` (user pickers, HQ Admin only) or in Discord's normal role menu.
* **Channels** (hidden from @everyone):
  * `🛠 RCE HQ` contains:
    * `#hq-overview`: customers, subscriptions, servers, live bots/players, the version, and *Check for updates* /
      *Install update for ALL servers* (asks again).
    * `#hq-ai`: the management AI for any server, e.g. `#12 list the emote binds` (Support and Admin can type).
    * `#hq-alerts`: new customers and servers, crashes, bots offline/back, RCON lost/back, late payments.
    * `#hq-audit`: every action from the portal, the owner panel and HQ (Admin only).
    * `#hq-staff`: the staff role panel (Admin only).
  * `👥 Customers` (more categories after 50): one channel per customer who bought the bot. It holds:
    * a customer panel (account, subscription, slots, add-ons; buttons *Suspend/Reactivate account*, *+1 slot*,
      *Comp*, add-ons on/off);
    * one panel per server (state, Discord, RCON, players, errors, a link to its secret admin panel; buttons
      *Restart*, *Suspend/Reactivate*, *Discord AI on/off*).
    * Typing in the channel talks to that customer's server's management AI (`#<id> …` if they have more servers).
* **Updates:** panels, alerts and the audit feed refresh every minute and right after any change. Unchanged panels
  aren't edited.
* **The bot itself:** it leaves any other Discord server it's added to, and refuses to run with the shared bot's token.
* **Actions:** every one goes through the same code as the owner panel (`actions.py`) and is audited as
  `discord:<user id>`. Remove and purge stay in the owner panel only.
* **Risky AI actions:** they first come back as "not done" with an *Allow risky actions and run again* button
  (HQ Admin only).

## Isolation, layer by layer

1. **Process:** one OS process per server. Only the environment that `provisioning.environment()` builds is passed
   in. No control-plane secrets (master key, Stripe keys) ever reach a tenant.
2. **Filesystem:** each server has its own `0700` folder. The bot `chdir`s into it; every relative path (database,
   plugin data, uploads, logs) stays inside.
3. **Database:** its own SQLite file, encrypted with SQLCipher (AES-256) using a **per-server random key** from the
   control plane. A file copied from one tenant can't be read with anything another tenant has. *Purge* deletes the
   key: crypto-shredding, so archives of a purged server are unreadable too.
4. **Discord:** own bot (add-on): its own token, gateway connection, rate limits and name/avatar; one token runs one
   server (unique fingerprint). Shared bot: one token, but each tenant process is **fenced** to its own Discord server
   in and out (`saas_fence.py`, see above).
5. **Web:** each server's own website and panel on its own port, reachable at `<slug>.<domain>`. Panel logins, 2FA,
   sessions and secrets live in its own folder. Cookies are scoped to its own host.
6. **Hosted-mode lockdown in the bot** (`saas_mode.py`, `RCE_SAAS=1`). Every tenant runs the same code folder, so
   anything that could reach beyond the tenant's own folder is off:
   * no code changes: management-AI code tools, uploaded or AI-generated Python plugins, auto-updates, full backups
     of the code folder and their restore;
   * no extra servers from the bot's own Servers page (that would bypass slots);
   * these are set by the control plane and locked in the tenant panel: file paths (`DATABASE_PATH`,
     `SUPPORT_KNOWLEDGE_FILE`, `RULES_FILE`, `PRICE_LIST_FILE`, `BACKUP_COPY_DIR`), web host, port, tunnel and
     public URL, the Discord token, and outbound URLs (`ITEM_IMAGE_URL`, `DEEPSEEK_BASE_URL`);
   * the database key comes only from `DB_KEY` in the environment and is never written to a shared key file.
7. **Proxy trust:** Caddy always sets `X-Forwarded-For`. The bot trusts requests from `127.0.0.1` *without* that
   header as "this PC" (no rate limit, panel gate open), so proxied internet traffic is never treated as local.

## How the admin panel reads across tenants without breaking isolation

The control plane **never opens a tenant database**. The owner panel shows:

* **control.db metadata:** customers, subscriptions, slot grants, servers, audit log;
* **supervisor state:** running, crashed or crashloop, pid, restarts, last log lines;
* **each bot's own report** via `GET /saas/health` on `127.0.0.1:<port>`. It needs a **per-server bearer token**
  and returns only Discord ready, bot user, guild id and name, RCON connected, players online, latency and setup
  mode. Nothing of the community's data (players, kits, tickets) leaves the tenant.

## Data model

```
customers(id, email UNIQUE, name, password_hash, status[active|suspended|deleted], suspend_reason,
          base_slots DEFAULT 1, stripe_customer_id UNIQUE, created_at)
subscriptions(customer_id PK→customers, stripe_subscription_id UNIQUE, status[none|active|trialing|past_due|
          canceled|unpaid|comped], current_period_end, grace_until, updated_at)
slot_grants(id, customer_id→customers, slots>0, reason, granted_by, granted_at, revoked_by, revoked_at)
          -- written ONLY by the owner panel / CLI; slot_limit = base_slots + SUM(active grants)
servers(id, customer_id→customers, slug UNIQUE, name, status[provisioning|awaiting_token|active|stopped|suspended|
          over_limit|deleted], suspend_reason, home_dir UNIQUE, web_port UNIQUE, node, created_at, deleted_at,
          bot_mode[shared|custom], guild_id, manager_ai 0|1)      -- UNIQUE(guild_id) for live shared-bot servers
features(id, customer_id→customers, feature[custom_bot|ai], granted_by, granted_at, revoked_by, revoked_at)
          -- add-ons, written ONLY by the owner panel / CLI
settings_kv(key PK, value)                 -- e.g. the update status
secrets(server_id→servers, kind[discord_token|db_key|health_token], ciphertext (AES-GCM, AAD "server:<id>:<kind>"),
          fingerprint, updated_at, PK(server_id, kind));  UNIQUE(fingerprint) WHERE kind='discord_token'
health(server_id PK, checked_at, process, pid, restarts, discord_ready, bot_user, guild_id, guild_name,
          rcon_connected, players_online, latency_ms, setup_mode, error)
owners(username PK, password_hash, totp_secret)
stripe_events(id PK, received_at)          -- webhook idempotency
audit_log(id, at, actor, action, customer_id, server_id, detail)
```

**Per-server isolated data** (inside `tenants/c<customer>/<slug>/`, never in control.db):
* `.env` holds non-secret settings; control-plane-owned keys are locked.
* `data/bot.db` (SQLCipher) holds players, kits, custom kits, autokits, emote binds, teleports, shop, tickets,
  links, points, plugin data and everything else the bot stores.
* Also in `data/`: the panel logins (`web_users.json`), 2FA, plugin settings and uploaded pictures.
* `logs/bot.log`.

## Lifecycles

**Signup:** create the customer (subscription `none`), then Stripe Checkout. The `checkout.session.completed`
webhook links the Stripe customer and sets `active`.

**Add a server** (portal → `POST /servers`), inside **one write transaction** (`BEGIN IMMEDIATE`):
1. `slots.reserve` checks the limit;
2. a unique slug and a free port are chosen;
3. the server row is inserted (`provisioning`).

After the transaction:
1. create the folder (0700) and `.env` (port, public URL, a hashed one-time panel password shown once, locked
   values);
2. generate the per-server `db_key` and `health_token`;
3. status becomes `awaiting_token`.

The customer pastes the bot token (encrypted, unique), so the status becomes `active`. The supervisor starts the
process within seconds and Caddy gets the route. The bot starts in setup mode until the customer enters RCON in the
server's own panel. Every step is idempotent (`finish_provisioning` can be re-run).

**Unlock a slot (owner):** owner panel → *Unlock slots* writes a `slot_grants` row and runs `release_over_limit`,
which brings back any servers parked over the limit, oldest first.

**Billing:** `active`/`trialing` keep running. `past_due` keeps running for `CP_GRACE_DAYS`, then stops.
`canceled`/`unpaid` stop. The processes stop and the website shows "paused" (Caddy rewrite); **all data is kept**.
Payment (`invoice.paid`, or a new subscription) makes them run again automatically. `comped` is a free account set
by the owner (your own servers, partners).

**Remove / purge:** *Remove* (customer or owner) stops the server, frees the slot, port and token, and keeps the
folder and keys, so the owner can restore it. *Purge* (owner, only after remove) deletes the keys and the folder.

## Slot enforcement (anti-tampering)

* The limit is computed **only on the server** from `customers.base_slots` + active `slot_grants`. No portal route
  reads a slot value from the request; extra form fields are ignored (covered by a test).
* `slot_grants` and `base_slots` are written by the owner panel (a separate listener, owner session + TOTP + CSRF)
  and the CLI only.
* The check-then-insert runs inside `BEGIN IMMEDIATE`, so parallel "add server" requests can't both take the last
  slot (a test runs 6 at once: exactly 1 succeeds).
* Every customer action is scoped by **ownership** (`servers.customer_id = session customer`). Another customer's
  server id returns 404, not 403, so you can't even probe whether it exists.
* Revoking slots never deletes anything. The newest extra servers become `over_limit` (stopped) until a slot is
  granted again or the customer removes a server.

## Security considerations

* **Bot tokens:** AES-256-GCM, master key outside the database (`CP_MASTER_KEYS` in the service's environment
  file, `chmod 600`, not in backups of `control.db`).
  * Associated data binds each ciphertext to its row and kind, so a ciphertext moved into another row fails.
  * Key versions allow rotation (`rotate-keys`).
  * Tokens are never written to a tenant's `.env`; they're passed in the process environment only (readable by the
    service user and root, like any secret the process must use).
  * The portal never shows a stored token back.
* **Owner-only panel:** bound to `127.0.0.1`/VPN, so it's not routable from the internet.
  * Separate cookie and HMAC key from the portal; a customer session is useless there (covered by a test).
  * Password (PBKDF2) plus TOTP, login rate limit, CSRF on every action, `audit_log` of every change and every
    login.
* **Customer portal:** PBKDF2 passwords, rate-limited login and signup, signed `HttpOnly; SameSite=Strict; Secure`
  cookies, CSRF tokens plus an Origin check, a strict CSP, `no-store`, and ownership checks on every server route.
* **Stripe webhooks:** HMAC signature with a 5-minute tolerance, processed once per event id. The webhook can only
  change subscription status, never slots or servers.
* **Tenant breakout:** hosted mode switches off everything that runs custom code or touches paths or ports outside
  the tenant (see Isolation §6). SSRF: outbound URL settings are locked, the control plane's owner listener requires
  auth even from localhost, and `/saas/health` needs its per-server token.
* **Resource abuse:** per-process memory limit, crash-loop stop, the bot's own flood shield, Caddy request-body
  limit, and Cloudflare in front.
* **Backups:** back up `control.db` and the tenant folders separately from `CP_MASTER_KEYS` and per-server keys.
  Tenant databases are encrypted, so a stolen backup without the keys is useless.

## Running it

```bash
cd /home/rcebot/ff/rce-discord-bot && pip install -r requirements.txt   # cryptography, sqlcipher3 already in there
python3 -m control_plane setup --domain yourdomain.com  # first time: writes /etc/rce-saas.env, then prints a
                                                        # one-time link http://<server ip>:8099/once/… (30 minutes)
python3 -m control_plane run                            # systemd service, Restart=always
python3 -m control_plane login-link                     # any time later: a one-time link to the owner panel
python3 -m control_plane links                          # where everything is
```
* **Everything in the browser:** the one-time link signs you in once (15 minutes; `setup`: 30). Without an owner
  account it first lets you create yours and shows your 2FA key.
* **Settings page:** owner panel → **⚙️ Settings** holds the domain, DeepSeek key, shared bot, Stripe, management
  Discord, owner panel address/IP allowlist, updates and the memory limit.
  * Secrets show only as `set ••••1234`; leave a field empty to keep the value.
  * Values are stored encrypted in `control.db` and win over the file. Only the master key and the session secret
    stay in `/etc/rce-saas.env`.
  * Most changes apply at once; ones marked *restart* need a restart of the control plane.
* **`setup` is plain HTTP on the server's IP:** open the port only for the first setup (`ufw allow 8099/tcp`, then
  `ufw delete allow 8099/tcp`) and use `https://portal.<domain>/management` afterwards.
* **No `set -a` or `sudo -E` needed:** every command loads `/etc/rce-saas.env` by itself (`CP_ENV_FILE` for another
  path).
* **Run as root:** a command reads that file, hands the data folders to the user that owns the bot folder (e.g.
  `rcebot`) and continues as that user (`CP_RUN_AS_ROOT=1` turns that off).
* The shared bot's Discord app needs the redirect `https://portal.yourdomain.com/discord/callback`.

**Cloudflare instead of Caddy (recommended – nothing to install or open):**
`python3 -m control_plane cloudflare --domain yourdomain.com` (or owner panel → Settings → *Set up Cloudflare*) asks
once for a Cloudflare API token (*Account › Cloudflare Tunnel › Edit*, *Zone › DNS › Edit*, *Zone › Zone › Read*). It:
* creates the tunnel `rce-saas-<domain>`;
* routes `*.<domain>` and `<domain>` to the portal listener;
* creates the proxied record `*.<domain>`;
* moves the old addresses of adopted servers (their previous panel hostnames) to it.

Only the tunnel token is kept, encrypted with the website settings; the API token is not stored. `run` starts
cloudflared. The portal listener then passes each `<slug>.<domain>` on to that server's port (`hostproxy.py`), sets
`X-Forwarded-For` to the visitor, keeps the `Host`, and sends suspended servers to their *paused* page.

Caddy (`/etc/caddy/Caddyfile`):
```
portal.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
import /var/lib/rce-saas/Caddyfile.tenants
```
Cloudflare: `*.yourdomain.com` and `portal` → the VPS (proxied). The Stripe webhook endpoint is
`https://portal.yourdomain.com/stripe/webhook` with the events `checkout.session.completed`,
`customer.subscription.*`, `invoice.paid` and `invoice.payment_failed`.

**Where is what?** `python -m control_plane links` prints the owner panel's address and every server's website
and secret admin panel (`--email you@x.io` for one customer).

**Owner panel:** open `https://portal.yourdomain.com/management` (no extra Caddy or DNS entry, it's part of the
portal). Sign in with your owner username, password and 2FA code.
* `CP_OWNER_PATH` changes the address (e.g. `/my-secret-panel`); `CP_OWNER_PATH=off` removes it from the website `/admin`
  is reserved (it's the honeypot on every bot website), and so are the portal's own pages.
* Tenant panels never live at `/admin` either: every server gets a secret panel address (`WEB_PANEL_PATH`,
  `panel-…`) at provisioning, and the portal shows the customer that link. `/admin` on a tenant site is the honeypot.
* `CP_OWNER_ALLOW_IPS=1.2.3.4,5.6.7.8` shows it only to these IPs (everyone else gets 404).
* The login is limited per IP and per username. The session cookie only works under that path, a customer's portal
  login gives no access, and search engines are told not to index it.
* The private listener still works too: `ssh -L 8090:127.0.0.1:8090 vps`, then `http://localhost:8090` – handy if
  you lock yourself out of the website version (it has its own rate limits).

## Migration from the current single-bot setup (minimal downtime)

**All servers at once (recommended):**
```bash
python3 -m control_plane adopt --dry-run                            # lists main + every servers/<id>
systemctl stop <the old bot's service>                              # or kill the PIDs `adopt` names
python3 -m control_plane adopt                                      # prints a one-time login link at the end
python3 -m control_plane run                                        # (or restart its service)
```
* **What moves:** every server of the old install becomes a tenant of your account. The account is comped and gets
  the own-bot and AI add-ons, plus one slot per server.
* **What each tenant keeps:**
  * its Discord bot token, so the same bot stays in the same Discord with no re-invite;
  * its database, including an encrypted one (opened with the install's key – `DB_KEY` or the bot user's key file
    – copied plain, then re-encrypted by the tenant with its own key);
  * the Discord ↔ game links, rules, knowledge base, prices, panel logins and 2FA, plugin settings and pictures;
  * every non-locked setting, including its panel's secret address and password.
* **Safety:** `adopt` refuses to run while the old bot still runs. Copying a changing database and starting the same
  token twice would both go wrong. Downtime is the time between stopping the old bot and `run` starting the
  tenants (seconds).
* **Extra servers on the main server's bot (`discord: shared`):** they can't share that token as a separate process.
  They're adopted with everything else but wait for their own bot token (owner panel → server → *Set bot token*).
* **The shared bot:** when it's set up, `adopt` puts every server on it (fenced to the `GUILD_ID` in its settings),
  so the whole platform runs on ONE bot token. `--own-tokens` keeps each server's own token instead.
* **Disabled extra servers** are adopted as *stopped*.
* **Running it twice** skips what's already adopted.
* **New addresses:** each server's website becomes `<slug>.yourdomain.com` (`python3 -m control_plane links` lists
  them). Point old domains there if you had any.

**One server at a time:** `python3 -m control_plane import-server /path/to/folder --email you@… --name "Kryvon"` does
the same for a single folder.

6. **Rollback:** the source folder isn't modified. Remove the imported server in the owner panel and start the old
   bot again.
7. **After migration:** sell to new communities through the portal. They get one slot; you unlock more in the owner
   panel when they buy the add-on.
