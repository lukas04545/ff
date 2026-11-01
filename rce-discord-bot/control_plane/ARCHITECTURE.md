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
| Owner panel | Separate listener on `127.0.0.1` (reach it via SSH tunnel / Tailscale) + password + TOTP | It doesn't exist on the internet at all |

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
 └──────────────────┘     * owner panel bound to 127.0.0.1 only
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

## Isolation, layer by layer

1. **Process:** one OS process per server. Only the environment that `provisioning.environment()` builds is passed
   in. No control-plane secrets (master key, Stripe keys) ever reach a tenant.
2. **Filesystem:** each server has its own `0700` folder. The bot `chdir`s into it; every relative path (database,
   plugin data, uploads, logs) stays inside.
3. **Database:** its own SQLite file, encrypted with SQLCipher (AES-256) using a **per-server random key** from the
   control plane. A file copied from one tenant can't be read with anything another tenant has. *Purge* deletes the
   key: crypto-shredding, so archives of a purged server are unreadable too.
4. **Discord:** one bot token per server, so a separate gateway connection, rate limits and application identity
   (the customer's name and avatar). `secrets.fingerprint` has a unique index, so the same token can't run two
   servers.
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
          over_limit|deleted], suspend_reason, home_dir UNIQUE, web_port UNIQUE, node, created_at, deleted_at)
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
cd rce-discord-bot && pip install -r requirements.txt   # cryptography, sqlcipher3 already in there
python -m control_plane init                           # prints CP_MASTER_KEYS + CP_SESSION_SECRET
# put them + CP_BASE_DOMAIN, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID into /etc/rce-saas.env
python -m control_plane create-owner you                # password + authenticator app (TOTP)
python -m control_plane run                             # systemd service, Restart=always
```

Caddy (`/etc/caddy/Caddyfile`):
```
portal.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
import /var/lib/rce-saas/Caddyfile.tenants
```
Cloudflare: `*.yourdomain.com` and `portal` → the VPS (proxied). The Stripe webhook endpoint is
`https://portal.yourdomain.com/stripe/webhook` with the events `checkout.session.completed`,
`customer.subscription.*`, `invoice.paid` and `invoice.payment_failed`. Owner panel: `ssh -L 8090:127.0.0.1:8090
vps`, then open `http://localhost:8090`.

## Migration from the current single-bot setup (minimal downtime)

1. **Ship the lockdown first.** `saas_mode.py` is inert without `RCE_SAAS=1`, so the current bot behaves exactly as
   before.
2. **Stand up the control plane on the new VPS** (steps above). Create your owner account and test with a throwaway
   server.
3. **Prepare each existing server:**
   * if it uses `DB_ENCRYPTION`, turn it off and restart once (the import copies the plaintext database; the tenant
     re-encrypts it with its own new key on first start);
   * note its Discord token (it stays the same bot, so no re-invite is needed).
4. **Import, one server at a time:** `python -m control_plane import-server /path/to/old/bot --email you@… --name
   "Kryvon"`. For extra servers, use the `servers/<id>` folders.
   * The import copies the database with SQLite's backup API (consistent while the old bot runs), plus rules,
     knowledge base, price list, panel logins and 2FA, plugin settings and pictures, and every non-locked setting.
   * Paths, ports and the token are *not* copied. The token goes into the encrypted store.
   * The customer is **comped** (your own servers don't pay) and a slot is granted automatically.
5. **Cut over:**
   * stop the old process;
   * within ~5 s the supervisor starts the imported one (the same token, so the same bot shows up in Discord);
   * point the old domain (or a CNAME) at `<slug>.yourdomain.com`.

   Downtime is the bot restart, well under a minute. The database copied a moment earlier still holds everything up
   to the copy. For zero lost writes, stop the old bot *before* running the import (about a minute of downtime).
6. **Rollback:** the source folder isn't modified. Remove the imported server in the owner panel and start the old
   bot again.
7. **After migration:** sell to new communities through the portal. They get one slot; you unlock more in the owner
   panel when they buy the add-on.
