"""The control plane's own database: customers, subscriptions, slot grants, servers, encrypted secrets, health and
the audit log. It holds METADATA only – no tenant data. Each tenant server's data lives in its own folder and its
own (encrypted) SQLite file, which the control plane never opens.

SQLite in WAL mode: one control-plane process writes; plenty for thousands of servers. (Postgres later: the same
schema, swap the connection – see ARCHITECTURE.md.)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    suspend_reason TEXT,
    base_slots INTEGER NOT NULL DEFAULT 1 CHECK (base_slots >= 0),   -- included in the plan
    stripe_customer_id TEXT UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    stripe_subscription_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'none',          -- Stripe status: active, trialing, past_due, canceled, unpaid …
    current_period_end INTEGER,
    grace_until INTEGER,                          -- past_due: keeps running until then
    updated_at INTEGER NOT NULL
);
-- Extra server slots: ONLY written by the owner panel (and the CLI). Customers have no route that touches it.
CREATE TABLE IF NOT EXISTS slot_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    slots INTEGER NOT NULL CHECK (slots > 0),
    reason TEXT NOT NULL DEFAULT '',
    granted_by TEXT NOT NULL,
    granted_at INTEGER NOT NULL,
    revoked_by TEXT,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    slug TEXT NOT NULL UNIQUE,                    -- subdomain: <slug>.<BASE_DOMAIN>
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisioning' CHECK (status IN (
        'provisioning', 'awaiting_token', 'active', 'stopped', 'suspended', 'over_limit', 'deleted')),
    suspend_reason TEXT,
    home_dir TEXT NOT NULL UNIQUE,
    web_port INTEGER NOT NULL UNIQUE,
    node TEXT NOT NULL DEFAULT 'local',           -- which host runs it (one host to start with)
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_servers_customer ON servers (customer_id);
-- Encrypted secrets per server (AES-GCM, bound to "server:<id>:<kind>")
CREATE TABLE IF NOT EXISTS secrets (
    server_id INTEGER NOT NULL REFERENCES servers(id),
    kind TEXT NOT NULL CHECK (kind IN ('discord_token', 'db_key', 'health_token')),
    ciphertext TEXT NOT NULL,
    fingerprint TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (server_id, kind)
);
-- One Discord bot token runs exactly one server (two processes on one token would answer twice)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_token_per_server ON secrets (fingerprint) WHERE kind = 'discord_token';
CREATE TABLE IF NOT EXISTS health (
    server_id INTEGER PRIMARY KEY REFERENCES servers(id),
    checked_at INTEGER NOT NULL,
    process TEXT NOT NULL,                        -- running, stopped, crashed, crashloop, starting
    pid INTEGER,
    restarts INTEGER NOT NULL DEFAULT 0,
    discord_ready INTEGER, bot_user TEXT, guild_id TEXT, guild_name TEXT,
    rcon_connected INTEGER, players_online INTEGER, latency_ms INTEGER, setup_mode INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS owners (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    totp_secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    actor TEXT NOT NULL,                          -- owner:<name>, customer:<id>, stripe, system
    action TEXT NOT NULL,
    customer_id INTEGER,
    server_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
"""


class DB:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    def transaction(self):
        """`with db.transaction():` – BEGIN IMMEDIATE: takes the write lock first, so two requests can't both see
        "one slot free" and both create a server."""
        return _Tx(self.conn)

    def audit(self, actor: str, action: str, *, customer_id: int | None = None, server_id: int | None = None,
              detail: str = "") -> None:
        self.run("INSERT INTO audit_log (at, actor, action, customer_id, server_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (int(time.time()), actor, action, customer_id, server_id, detail[:1000]))

    def close(self) -> None:
        self.conn.close()


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
