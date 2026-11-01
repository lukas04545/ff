"""Tenant provisioning: customers, servers (= one Discord bot + one Rust server + one website + one database) and
their secrets. Every server gets its own folder (0700), .env, encrypted SQLite database, web port and subdomain.

Bot per server: "shared" (default – the platform's bot, added to the customer's Discord with "Add to Discord" and
fenced to that one Discord server) or "custom" (the customer's own bot token = own name and avatar; only with the
'custom_bot' add-on the owner switches on).

Lifecycle of a server ("awaiting_token" = waiting for its bot: a token, or the shared bot added to a Discord):
  provisioning -> awaiting_token -> active <-> stopped
                                     |-> suspended (billing / owner)    -> active again (reactivation)
                                     |-> over_limit (slots revoked)      -> active again (slot granted)
  remove: deleted (stopped, folder + keys kept for the retention time, restorable by the owner)
  purge:  keys shredded (the encrypted database becomes unreadable) and the folder deleted – final
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from control_plane import features, slots
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

SLUG = re.compile(r"[^a-z0-9]+")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{20,}$")
RESERVED = {"www", "admin", "owner", "portal", "app", "api", "mail", "status", "sites", "fallback"}
ADDRESS = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])$")
HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")   # nothing else of the control plane leaks


class ProvisionError(Exception):
    pass


def _now() -> int:
    return int(time.time())


# ------------------------------------------------------------ Customers

def create_customer(db: DB, email: str, password: str, name: str = "", *, actor: str = "signup") -> int:
    from web.users import hash_password
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 200:
        raise ProvisionError("Enter a valid e-mail address.")
    if len(password) < 10:
        raise ProvisionError("The password needs at least 10 characters.")
    try:
        with db.transaction():
            cur = db.run("INSERT INTO customers (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                         (email, name.strip()[:80], hash_password(password), _now()))
            db.run("INSERT INTO subscriptions (customer_id, status, updated_at) VALUES (?, 'none', ?)",
                   (cur.lastrowid, _now()))
            db.audit(actor, "customer.create", customer_id=cur.lastrowid, detail=email)
    except sqlite3.IntegrityError:
        raise ProvisionError("An account with this e-mail exists already.") from None
    return int(cur.lastrowid)


# ------------------------------------------------------------ Servers

def _slug(db: DB, name: str) -> str:
    base = SLUG.sub("-", name.lower()).strip("-")[:24] or "server"
    if base in RESERVED:
        base += "-srv"
    slug = base
    while db.one("SELECT 1 FROM servers WHERE slug = ?", (slug,)):
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


def _free_port(db: DB, settings: Settings) -> int:
    used = {r["web_port"] for r in db.q("SELECT web_port FROM servers")}
    for port in range(settings.port_min, settings.port_max + 1):
        if port not in used:
            return port
    raise ProvisionError("No free port left – raise CP_PORT_MAX or add a node.")


def provision_server(db: DB, box: SecretBox, settings: Settings, customer_id: int, name: str, *,
                     actor: str) -> dict:
    """Creates a server for the customer if a slot is free (checked server-side, inside the write lock).
    Returns {"id", "slug", "url", "panel_url", "panel_password"} – the panel password is shown ONCE."""
    name = " ".join(str(name).split())[:60]
    if len(name) < 2:
        raise ProvisionError("Give the server a name.")
    cust = db.one("SELECT status FROM customers WHERE id = ?", (customer_id,))
    if cust is None or cust["status"] != "active":
        raise ProvisionError("This account is not active.")
    with db.transaction():
        slots.reserve(db, customer_id)                    # raises SlotError when all slots are used
        slug, port = _slug(db, name), _free_port(db, settings)
        home = (settings.tenants_dir / f"c{customer_id}" / slug).resolve()
        cur = db.run("INSERT INTO servers (customer_id, slug, name, status, home_dir, web_port, created_at) "
                     "VALUES (?, ?, ?, 'provisioning', ?, ?, ?)", (customer_id, slug, name, str(home), port, _now()))
        sid = int(cur.lastrowid)
        db.audit(actor, "server.create", customer_id=customer_id, server_id=sid, detail=f"{slug} port {port}")
    password = finish_provisioning(db, box, settings, sid)
    return {"id": sid, "slug": slug, "url": settings.url_for(slug), "panel_password": password,
            "panel_url": f"{settings.url_for(slug)}/{panel_path(home)}"}


def finish_provisioning(db: DB, box: SecretBox, settings: Settings, server_id: int) -> str:
    """Folder, .env and secrets. Safe to run again (e.g. after a crash half-way). Returns a new panel password."""
    from web.users import hash_password
    s = db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
    home = Path(s["home_dir"])
    (home / "data").mkdir(parents=True, exist_ok=True)
    for path in (home.parent, home, home / "data"):
        try:
            os.chmod(path, 0o700)                         # only the service user can enter
        except OSError:
            pass
    for kind in ("db_key", "health_token"):
        if db.one("SELECT 1 FROM secrets WHERE server_id = ? AND kind = ?", (server_id, kind)) is None:
            put_secret(db, box, server_id, kind, secrets.token_hex(32))
    password = secrets.token_urlsafe(12)
    write_env(home, {"WEB_PANEL_PATH": panel_path(home) or new_panel_path()})   # keep it on a re-run
    write_env(home, {
        "WEB_ENABLED": "true", "WEB_HOST": "127.0.0.1", "WEB_PORT": str(s["web_port"]), "WEB_TUNNEL": "false",
        "WEB_PUBLIC_URL": settings.url_for(s["slug"]), "WEB_OPEN_BROWSER": "false",
        "WEB_PASSWORD": hash_password(password), "SERVER_DISPLAY_NAME": s["name"],
        "DATABASE_PATH": "data/bot.db", "AUTO_UPDATE": "false",
        "FULL_BACKUP_ENABLED": "false", "DB_ENCRYPTION": "true", "BACKUP_COPY_DIR": "",
    })
    if s["status"] == "provisioning":
        db.run("UPDATE servers SET status = ? WHERE id = ?",
               ("active" if slots.bot_ready(db, server_id) else "awaiting_token", server_id))
    return password


def new_panel_path() -> str:
    """The tenant panel's secret address. Never /admin: on a website /admin is the honeypot (fake login)."""
    return "panel-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y")


def panel_path(home: Path) -> str:
    """The tenant panel's current secret address (WEB_PANEL_PATH in its .env; the tenant may change it)."""
    from envfile import read_env
    try:
        value = (read_env(Path(home) / ".env").get("WEB_PANEL_PATH") or "").strip().strip("/")
    except OSError:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value) and value.lower() != "admin" else ""


def panel_url(settings: Settings, server: dict) -> str:
    """Link to the tenant's real panel – never /admin (that's the honeypot)."""
    path = panel_path(Path(server["home_dir"]))
    return f"{settings.url_for(server['slug'])}/{path}" if path else settings.url_for(server["slug"])


def write_env(home: Path, values: dict[str, str]) -> None:
    """The tenant's .env: only non-secret, control-plane-owned values (the panel can't change them – SaaS lock).
    The bot token, database key and health token are NEVER written here – they're passed in the environment."""
    from envfile import update_env
    path = home / ".env"
    if not path.exists():
        path.write_text("", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    update_env(path, values)


def put_secret(db: DB, box: SecretBox, server_id: int, kind: str, value: str, *, fingerprint: bool = False) -> None:
    db.run("INSERT INTO secrets (server_id, kind, ciphertext, fingerprint, updated_at) VALUES (?, ?, ?, ?, ?) "
           "ON CONFLICT(server_id, kind) DO UPDATE SET ciphertext = excluded.ciphertext, "
           "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
           (server_id, kind, box.encrypt(value, f"server:{server_id}:{kind}"),
            box.fingerprint(value) if fingerprint else None, _now()))


def get_secret(db: DB, box: SecretBox, server_id: int, kind: str) -> str | None:
    row = db.one("SELECT ciphertext FROM secrets WHERE server_id = ? AND kind = ?", (server_id, kind))
    return box.decrypt(row["ciphertext"], f"server:{server_id}:{kind}") if row else None


def set_bot_token(db: DB, box: SecretBox, server_id: int, token: str, *, actor: str) -> None:
    """The customer's OWN Discord bot token for this server (needs the 'custom_bot' add-on) – encrypted; one token
    runs exactly one server. The server switches from the shared bot to this bot."""
    token = token.strip()
    if not TOKEN_SHAPE.match(token):
        raise ProvisionError("That doesn't look like a Discord bot token (Developer Portal → Bot → Reset Token).")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    if not features.has(db, s["customer_id"], "custom_bot"):
        raise ProvisionError("Your own bot (own name and avatar) is an add-on – contact us to unlock it. Until then "
                             "the server runs on our bot.")
    try:
        with db.transaction():
            put_secret(db, box, server_id, "discord_token", token, fingerprint=True)
            db.run("UPDATE servers SET bot_mode = 'custom', guild_id = NULL WHERE id = ?", (server_id,))
            if s["status"] == "awaiting_token":
                db.run("UPDATE servers SET status = 'active' WHERE id = ?", (server_id,))
            db.audit(actor, "server.token_set", customer_id=s["customer_id"], server_id=server_id)
    except sqlite3.IntegrityError:
        raise ProvisionError("This bot token is already used by another server – every server needs its own "
                             "bot (Discord Developer Portal → New Application).") from None


def use_shared_bot(db: DB, server_id: int, *, actor: str) -> None:
    """Back from an own bot to the platform's shared bot (the customer adds it to their Discord next)."""
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    with db.transaction():
        db.run("DELETE FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (server_id,))
        db.run("UPDATE servers SET bot_mode = 'shared', guild_id = NULL, status = CASE WHEN status = 'active' "
               "THEN 'awaiting_token' ELSE status END WHERE id = ?", (server_id,))
        db.audit(actor, "server.shared_bot", customer_id=s["customer_id"], server_id=server_id)


def bind_guild(db: DB, server_id: int, guild_id: str, *, actor: str) -> None:
    """The shared bot was added to this Discord server (verified by Discord's OAuth answer, not by the customer's
    word): the server is fenced to it. One Discord server can belong to only one tenant server."""
    guild_id = str(guild_id).strip()
    if not guild_id.isdigit():
        raise ProvisionError("Discord didn't say which server the bot was added to.")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None or s["bot_mode"] != "shared":
        raise ProvisionError("This server doesn't use the shared bot.")
    try:
        with db.transaction():
            db.run("UPDATE servers SET guild_id = ?, status = CASE WHEN status = 'awaiting_token' THEN 'active' "
                   "ELSE status END WHERE id = ?", (guild_id, server_id))
            db.audit(actor, "server.guild_bound", customer_id=s["customer_id"], server_id=server_id, detail=guild_id)
    except sqlite3.IntegrityError:
        raise ProvisionError("That Discord server is already connected to another server here.") from None


def change_address(db: DB, settings: Settings, server_id: int, new: str, *, actor: str) -> str:
    """The customer picks their website's address <new>.<domain>. The old address keeps working (an alias)."""
    new = str(new or "").strip().lower()
    if not ADDRESS.match(new):
        raise ProvisionError("3–30 letters, digits or dashes (not at the start or end), e.g. kryvon-eu.")
    if new in RESERVED:
        raise ProvisionError("That address is reserved – pick another one.")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    if s["slug"] == new:
        return new
    old_host, new_host = f"{s['slug']}.{settings.base_domain}", f"{new}.{settings.base_domain}"
    taken = db.one("SELECT 1 FROM servers WHERE slug = ?", (new,)) or \
        db.one("SELECT value FROM settings_kv WHERE key = ? AND value != ?", (f"host_alias:{new_host}", str(server_id)))
    if taken:
        raise ProvisionError("That address is taken – pick another one.")
    with db.transaction():
        db.run("UPDATE servers SET slug = ? WHERE id = ?", (new, server_id))
        db.run("DELETE FROM settings_kv WHERE key = ?", (f"host_alias:{new_host}",))
        db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)", (f"host_alias:{old_host}", str(server_id)))
        db.audit(actor, "server.address", customer_id=s["customer_id"], server_id=server_id, detail=f"{s['slug']} → {new}")
    write_env(Path(s["home_dir"]), {"WEB_PUBLIC_URL": settings.url_for(new)})
    return new


# ---------------------------------------------------------------- own domains (paid add-on "custom_domain")

MAX_DOMAINS = 3


def domains(db: DB, server_id: int) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM domains WHERE server_id = ? AND status != 'removed' ORDER BY "
                                  "created_at", (server_id,))]


def add_domain(db: DB, settings: Settings, server_id: int, hostname: str, *, actor: str) -> str:
    """The customer's own domain for this server's websites (needs the add-on). Cloudflare still has to accept it
    (cftunnel.request_custom_hostname) and the customer points a CNAME at sites.<domain>."""
    host = str(hostname or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    if not HOSTNAME.match(host):
        raise ProvisionError("Enter a domain like www.yourserver.com (no https://, no path).")
    base = settings.base_domain.lower()
    if host == base or host.endswith("." + base):
        raise ProvisionError(f"Addresses on {base} are free – change your address above instead.")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    if not features.has(db, s["customer_id"], "custom_domain"):
        raise ProvisionError("Your own domain is part of a bigger package (or an add-on) – upgrade to use it.")
    if len(domains(db, server_id)) >= MAX_DOMAINS:
        raise ProvisionError(f"Up to {MAX_DOMAINS} domains per server.")
    row = db.one("SELECT server_id, status FROM domains WHERE hostname = ?", (host,))
    if row is not None and row["status"] != "removed":
        raise ProvisionError("That domain is already connected.")
    db.run("INSERT OR REPLACE INTO domains (hostname, server_id, status, created_at, cf_id, note) "
           "VALUES (?, ?, 'pending', ?, NULL, '')", (host, server_id, _now()))
    db.audit(actor, "domain.add", customer_id=s["customer_id"], server_id=server_id, detail=host)
    return host


def remove_domain(db: DB, server_id: int, hostname: str, *, actor: str) -> None:
    db.run("UPDATE domains SET status = 'removed' WHERE hostname = ? AND server_id = ?", (hostname, server_id))
    db.audit(actor, "domain.remove", server_id=server_id, detail=hostname)


def tenant_guild(server: dict) -> str:
    """The Discord server id in a tenant's own .env (GUILD_ID) – where its bot worked before."""
    from envfile import read_env
    try:
        value = (read_env(Path(server["home_dir"]) / ".env").get("GUILD_ID") or "").strip()
    except OSError:
        return ""
    return value if value.isdigit() else ""


def shared_invite(settings: Settings) -> str:
    """The plain invite link of the shared bot (for the owner: no portal OAuth round trip)."""
    from urllib.parse import urlencode
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": settings.shared_client_id, "permissions": settings.shared_permissions,
        "scope": "bot applications.commands", "integration_type": "0"})


def move_to_shared_bot(db: DB, settings: Settings, server_id: int, guild_id: str = "", *, actor: str) -> str:
    """OWNER: the server runs on the platform's shared bot (fenced to its Discord server) instead of its own token.
    guild_id: its Discord server; empty = the one in its .env. Returns the guild id."""
    if not settings.shared_bot_token:
        raise ProvisionError("Set the shared bot first (Settings → Shared Discord bot: token and client id).")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    already = str(s["guild_id"] or "") if s["bot_mode"] == "shared" else ""
    guild_id = str(guild_id or "").strip() or already or tenant_guild(dict(s))
    if not guild_id.isdigit():
        raise ProvisionError("Which Discord server? Enter its id (Discord: right-click the server → Copy Server ID).")
    if settings.hq_guild_id and guild_id == str(settings.hq_guild_id):
        raise ProvisionError("That's your management Discord – a server's bot can't run there.")
    if s["bot_mode"] == "shared" and str(s["guild_id"] or "") == guild_id:
        return guild_id
    use_shared_bot(db, server_id, actor=actor)
    bind_guild(db, server_id, guild_id, actor=actor)
    return guild_id


def environment(db: DB, box: SecretBox, settings: Settings, server_id: int) -> dict[str, str]:
    """The ONLY environment a tenant process gets: no control plane secrets (master keys, Stripe) leak into it."""
    s = db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update({
        "RCE_SAAS": "1", "RCE_HOME": s["home_dir"], "PYTHONUNBUFFERED": "1", "HOME": s["home_dir"],
        "DB_KEY": get_secret(db, box, server_id, "db_key") or "", "DB_ENCRYPTION": "true",
        "SAAS_HEALTH_TOKEN": get_secret(db, box, server_id, "health_token") or "",
        "WEB_PORT": str(s["web_port"]), "WEB_HOST": "127.0.0.1",
    })
    if s["bot_mode"] == "custom":
        env.update({"DISCORD_TOKEN": get_secret(db, box, server_id, "discord_token") or "", "SAAS_SHARED_BOT": "0"})
    else:                                   # the platform's bot, fenced to this server's one Discord (saas_fence.py)
        env.update({"DISCORD_TOKEN": settings.shared_bot_token, "SAAS_SHARED_BOT": "1",
                    "SAAS_GUILD_ID": s["guild_id"] or "", "GUILD_ID": s["guild_id"] or ""})
    ai = features.has(db, s["customer_id"], "ai")
    env["SAAS_AI"] = "1" if ai else "0"
    env["DEEPSEEK_API_KEY"] = settings.deepseek_api_key if ai else ""
    # Management AI: the owner panel can always use it on this server; the server's own Discord only if the owner
    # chose this server. Its key is separate from the AI add-on's (tickets/chat/moderation).
    env["SAAS_MANAGER_AI"] = "1" if s["manager_ai"] else "0"
    env["SAAS_OWNER_AI_KEY"] = settings.deepseek_api_key
    env["SAAS_MANAGE_TOKEN"] = manage_token(env["SAAS_HEALTH_TOKEN"])
    return env


def manage_token(health_token: str) -> str:
    """The bearer token of POST /saas/manage – derived, so it needs no extra secret row."""
    import hashlib
    return hashlib.sha256(f"{health_token}:manage".encode()).hexdigest()


def set_manager_ai(db: DB, server_id: int, on: bool, *, actor: str) -> None:
    """Owner only: the management AI in this server's Discord (a running server restarts with it)."""
    s = db.one("SELECT customer_id FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    db.run("UPDATE servers SET manager_ai = ? WHERE id = ?", (int(bool(on)), server_id))
    db.audit(actor, f"server.manager_ai.{'on' if on else 'off'}", customer_id=s["customer_id"], server_id=server_id)


# ------------------------------------------------------------ Status changes

def set_server_status(db: DB, server_id: int, status: str, *, actor: str, reason: str | None = None) -> None:
    s = db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
    if s is None or s["status"] == "deleted":
        raise ProvisionError("No such server.")
    if status == "active":
        if s["status"] == "over_limit":
            raise ProvisionError("This server is over the slot limit – grant a slot first.")
        if not slots.bot_ready(db, server_id):
            status = "awaiting_token"
    db.run("UPDATE servers SET status = ?, suspend_reason = ? WHERE id = ?", (status, reason, server_id))
    db.audit(actor, f"server.{status}", customer_id=s["customer_id"], server_id=server_id, detail=reason or "")


def set_customer_status(db: DB, customer_id: int, status: str, *, actor: str, reason: str | None = None) -> None:
    """Suspending a customer stops all of their servers (data kept); reactivating starts them again."""
    if db.one("SELECT 1 FROM customers WHERE id = ? AND status != 'deleted'", (customer_id,)) is None:
        raise ProvisionError("No such customer.")
    db.run("UPDATE customers SET status = ?, suspend_reason = ? WHERE id = ?", (status, reason, customer_id))
    db.audit(actor, f"customer.{status}", customer_id=customer_id, detail=reason or "")


def remove_server(db: DB, server_id: int, *, actor: str) -> None:
    """Stops and hides the server, frees its slot; folder and keys stay for the retention time (restorable)."""
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    db.run("UPDATE servers SET status = 'deleted', deleted_at = ?, web_port = ? WHERE id = ?",
           (_now(), -server_id, server_id))                # the port is free again (UNIQUE)
    db.run("DELETE FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (server_id,))   # token reusable
    db.audit(actor, "server.remove", customer_id=s["customer_id"], server_id=server_id, detail=s["slug"])


def purge_server(db: DB, settings: Settings, server_id: int, *, actor: str) -> None:
    """Final: shreds the server's keys (its encrypted database can't be read any more) and deletes its folder."""
    s = db.one("SELECT * FROM servers WHERE id = ? AND status = 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("Remove the server first (purge only works on removed servers).")
    db.run("DELETE FROM secrets WHERE server_id = ?", (server_id,))
    home = Path(s["home_dir"]).resolve()
    if home.is_dir() and settings.tenants_dir.resolve() in home.parents:   # never anything outside the tenants folder
        shutil.rmtree(home, ignore_errors=True)
    db.audit(actor, "server.purge", customer_id=s["customer_id"], server_id=server_id, detail=s["slug"])


def runnable(db: DB, server_id: int, settings: Settings | None = None) -> bool:
    """Should this server's process run right now? (server active + customer active + paid or in grace + its bot
    set up; a shared-bot server also needs the platform's shared bot token configured)"""
    from control_plane.billing import subscription_ok
    s = db.one("SELECT s.status, s.customer_id, s.bot_mode, c.status AS cstatus FROM servers s JOIN customers c "
               "ON c.id = s.customer_id WHERE s.id = ?", (server_id,))
    if s is None or s["status"] != "active" or s["cstatus"] != "active" or not slots.bot_ready(db, server_id):
        return False
    if s["bot_mode"] == "shared" and settings is not None and not settings.shared_bot_token:
        return False
    return subscription_ok(db, s["customer_id"])
