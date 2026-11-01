"""Tenant provisioning: customers, servers (= one Discord bot + one Rust server + one website + one database) and
their secrets. Every server gets its own folder (0700), .env, encrypted SQLite database, web port and subdomain.

Lifecycle of a server:
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

from control_plane import slots
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings

SLUG = re.compile(r"[^a-z0-9]+")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{20,}$")
RESERVED = {"www", "admin", "owner", "portal", "app", "api", "mail", "status"}
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
    Returns {"id", "slug", "url", "panel_password"} – the panel password is shown ONCE."""
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
    return {"id": sid, "slug": slug, "url": settings.url_for(slug), "panel_password": password}


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
    write_env(home, {
        "WEB_ENABLED": "true", "WEB_HOST": "127.0.0.1", "WEB_PORT": str(s["web_port"]), "WEB_TUNNEL": "false",
        "WEB_PUBLIC_URL": settings.url_for(s["slug"]), "WEB_OPEN_BROWSER": "false",
        "WEB_PASSWORD": hash_password(password), "SERVER_DISPLAY_NAME": s["name"],
        "DATABASE_PATH": "data/bot.db", "AUTO_UPDATE": "false", "MANAGER_AI_CODE": "false",
        "FULL_BACKUP_ENABLED": "false", "DB_ENCRYPTION": "true", "BACKUP_COPY_DIR": "",
    })
    has_token = db.one("SELECT 1 FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (server_id,))
    if s["status"] == "provisioning":
        db.run("UPDATE servers SET status = ? WHERE id = ?", ("active" if has_token else "awaiting_token", server_id))
    return password


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
    """The customer's own Discord bot token for this server – encrypted; one token runs exactly one server."""
    token = token.strip()
    if not TOKEN_SHAPE.match(token):
        raise ProvisionError("That doesn't look like a Discord bot token (Developer Portal → Bot → Reset Token).")
    s = db.one("SELECT * FROM servers WHERE id = ? AND status != 'deleted'", (server_id,))
    if s is None:
        raise ProvisionError("No such server.")
    try:
        with db.transaction():
            put_secret(db, box, server_id, "discord_token", token, fingerprint=True)
            if s["status"] == "awaiting_token":
                db.run("UPDATE servers SET status = 'active' WHERE id = ?", (server_id,))
            db.audit(actor, "server.token_set", customer_id=s["customer_id"], server_id=server_id)
    except sqlite3.IntegrityError:
        raise ProvisionError("This bot token is already used by another server – every server needs its own "
                             "bot (Discord Developer Portal → New Application).") from None


def environment(db: DB, box: SecretBox, settings: Settings, server_id: int) -> dict[str, str]:
    """The ONLY environment a tenant process gets: no control plane secrets (master keys, Stripe) leak into it."""
    s = db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update({
        "RCE_SAAS": "1", "RCE_HOME": s["home_dir"], "PYTHONUNBUFFERED": "1", "HOME": s["home_dir"],
        "DISCORD_TOKEN": get_secret(db, box, server_id, "discord_token") or "",
        "DB_KEY": get_secret(db, box, server_id, "db_key") or "", "DB_ENCRYPTION": "true",
        "SAAS_HEALTH_TOKEN": get_secret(db, box, server_id, "health_token") or "",
        "WEB_PORT": str(s["web_port"]), "WEB_HOST": "127.0.0.1",
    })
    return env


# ------------------------------------------------------------ Status changes

def set_server_status(db: DB, server_id: int, status: str, *, actor: str, reason: str | None = None) -> None:
    s = db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
    if s is None or s["status"] == "deleted":
        raise ProvisionError("No such server.")
    if status == "active":
        if s["status"] == "over_limit":
            raise ProvisionError("This server is over the slot limit – grant a slot first.")
        if db.one("SELECT 1 FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (server_id,)) is None:
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


def runnable(db: DB, server_id: int) -> bool:
    """Should this server's process run right now? (server active + customer active + paid or in grace)"""
    from control_plane.billing import subscription_ok
    s = db.one("SELECT s.status, s.customer_id, c.status AS cstatus FROM servers s JOIN customers c "
               "ON c.id = s.customer_id WHERE s.id = ?", (server_id,))
    if s is None or s["status"] != "active" or s["cstatus"] != "active":
        return False
    if db.one("SELECT 1 FROM secrets WHERE server_id = ? AND kind = 'discord_token'", (server_id,)) is None:
        return False
    return subscription_ok(db, s["customer_id"])
