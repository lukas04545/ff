"""SQLite storage for statistics (leaderboards) and bot state.

Since Console Edition only provides player names (gamertag/PSN) in logs and the
player list, stats are counted per name. Name change = new entry.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import dbcrypt

LEADERBOARD_COLUMNS = {
    "kills": "kills DESC, deaths ASC",
    "deaths": "deaths DESC",
    "kd": "CAST(kills AS REAL) / MAX(deaths, 1) DESC, kills DESC",
    "playtime": "playtime_seconds DESC",
}


DEFAULT_CLAIM_COOLDOWN = 1440  # minutes (24 h) – default cooldown for self-service custom kit claims
CLAIM_SOURCE = "claim"         # kit_claims.source prefix of self-service claims
INGAME_CLAIM_SOURCE = "roleclaim"   # … of in-game kits claimed with their Discord role


def raw_values(raw: str | None) -> set[str]:
    """All scalar values in a stored JSON payload, lowercased (to find an order ID anywhere in it)."""
    import json
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return set()
    out: set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif v is not None:
            out.add(str(v).strip().lstrip("#").lower())
    walk(data)
    return out


class _Rollback(Exception):
    """Aborts a `with db:` transaction."""


class Storage:
    def __init__(self, path: Path, *, links_path: Path | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Small, fast writes – synchronous SQLite is plenty for a single server. DB_ENCRYPTION: SQLCipher (dbcrypt.py);
        # the file is converted first if the setting changed.
        dbcrypt.ensure_state(path)
        self._db = dbcrypt.connect(path)
        self._db.row_factory = dbcrypt.row_factory(self._db)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                kills INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0,
                suicides INTEGER NOT NULL DEFAULT 0,
                playtime_seconds INTEGER NOT NULL DEFAULT 0,
                platform TEXT,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

            -- Kit manager: autokit rules and claim history
            CREATE TABLE IF NOT EXISTS kit_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL,
                trigger TEXT NOT NULL,              -- 'respawn' or 'quickchat'
                phrase TEXT,                        -- quick chat identifier (only for 'quickchat')
                cooldown_minutes INTEGER NOT NULL DEFAULT 0,
                max_claims INTEGER NOT NULL DEFAULT 0,  -- 0 = unlimited (per player)
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kit_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,                    -- NULL = given manually via Discord
                kit TEXT NOT NULL,
                player TEXT NOT NULL COLLATE NOCASE,
                source TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_kit_claims_rule_player ON kit_claims (rule_id, player);

            -- Custom kits: stored only in the bot, invisible in the in-game kit manager
            CREATE TABLE IF NOT EXISTS custom_kits (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Support tickets
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER UNIQUE,
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                ingame_name TEXT,
                status TEXT NOT NULL DEFAULT 'open',      -- 'open' / 'closed'
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                escalated INTEGER NOT NULL DEFAULT 0,     -- staff was pinged
                staff_joined INTEGER NOT NULL DEFAULT 0,  -- staff has written -> AI paused
                ai_replies INTEGER NOT NULL DEFAULT 0,
                keep_open INTEGER NOT NULL DEFAULT 0,     -- player asked to keep it open: no auto-close
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                closed_by TEXT,
                close_reason TEXT
            );
            -- Giveaways (survive restarts)
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER UNIQUE,
                prize TEXT NOT NULL,
                winners INTEGER NOT NULL DEFAULT 1,
                host_id INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,              -- Unix time
                ended INTEGER NOT NULL DEFAULT 0,
                winner_ids TEXT NOT NULL DEFAULT '',   -- comma-separated
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (giveaway_id, user_id)
            );
            -- Giveaway wins redeemed through a ticket (each winner message only once)
            -- Redeemed giveaway wins: once per winner per giveaway message
            CREATE TABLE IF NOT EXISTS giveaway_redemptions (
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                action TEXT NOT NULL,
                redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, user_id)
            );
            -- Verified link Discord account <-> in-game player (one each, for self-service kit claims)
            CREATE TABLE IF NOT EXISTS linked_players (
                user_id INTEGER PRIMARY KEY,
                player TEXT NOT NULL UNIQUE COLLATE NOCASE,
                linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Discord roles that are removed again automatically (e.g. VIP for 7 days)
            CREATE TABLE IF NOT EXISTS timed_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,           -- Unix time
                reason TEXT,
                granted_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, user_id, role_id)
            );
            -- Points (economy): balance per Discord account, earned with the linked player
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0,
                earned_total INTEGER NOT NULL DEFAULT 0,
                daily_last INTEGER,                    -- Unix time of the last /daily
                daily_streak INTEGER NOT NULL DEFAULT 0,
                playtime_progress INTEGER NOT NULL DEFAULT 0   -- online seconds towards the next playtime reward
            );
            CREATE TABLE IF NOT EXISTS point_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_point_log_user ON point_log (user_id, id);
            CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                kind TEXT NOT NULL,                    -- custom_kit / kit / item / role
                target TEXT NOT NULL,                  -- kit name, item shortname or role ID
                amount INTEGER NOT NULL DEFAULT 1,     -- items: amount, role: days (0 = permanent)
                price INTEGER NOT NULL,
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Messages posted automatically every N minutes (in-game and/or Discord)
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                target TEXT NOT NULL DEFAULT 'game',   -- game / discord / both
                channel_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sent INTEGER,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Player reports (/report) and the staff watchlist
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                player TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',   -- open / handled
                handled_by TEXT,
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                handled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                player TEXT PRIMARY KEY COLLATE NOCASE,
                reason TEXT,
                added_by TEXT,
                last_alert INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- In-game teams (from the team log lines) for the team leaderboard
            CREATE TABLE IF NOT EXISTS teams (
                team_id INTEGER PRIMARY KEY,
                leader TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS team_members (
                player TEXT PRIMARY KEY COLLATE NOCASE,
                team_id INTEGER NOT NULL
            );
            -- Player count over time (population graph)
            CREATE TABLE IF NOT EXISTS population (
                ts INTEGER PRIMARY KEY,
                players INTEGER NOT NULL,
                max_players INTEGER,
                queued INTEGER NOT NULL DEFAULT 0
            );
            -- Admin abuse cases: steps that wait for staff approval (e.g. entity.deleteby)
            CREATE TABLE IF NOT EXISTS abuse_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player TEXT NOT NULL,
                what TEXT NOT NULL,
                done TEXT NOT NULL DEFAULT '',          -- commands already run automatically (with replies)
                status TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
                decided_by TEXT,
                result TEXT,
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT
            );
            -- Bought custom kits reported in a ticket: staff approves -> kit role for the buyer
            CREATE TABLE IF NOT EXISTS kit_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_id INTEGER NOT NULL,
                kit TEXT NOT NULL,
                days INTEGER NOT NULL DEFAULT 0,       -- 0 = permanent
                proof TEXT,
                items TEXT,                            -- JSON: new kit's items (NULL = existing kit)
                status TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
                decided_by TEXT,
                role_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT
            );
            -- AI chat moderation warnings
            CREATE TABLE IF NOT EXISTS ai_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                severity TEXT,
                reason TEXT,
                content TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_warnings_user ON ai_warnings (guild_id, user_id, created_at);
            -- Tip4Serv purchases ("rcebot ..." commands), one row per command; used once by a kit request
            CREATE TABLE IF NOT EXISTS store_payments (
                id TEXT PRIMARY KEY,                   -- "<payment id>:<command id>"
                payment_id TEXT NOT NULL,
                amount REAL,
                currency TEXT,
                discord_id INTEGER,
                player TEXT,                           -- in-game name from the command ({PlayerName})
                username TEXT,                         -- buyer fields Tip4Serv sends
                customer TEXT,
                command TEXT,
                raw TEXT,                              -- everything Tip4Serv sent about the payment (JSON)
                received_at INTEGER NOT NULL,
                used_by INTEGER,                       -- kit request that used this payment
                used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS custom_kit_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL COLLATE NOCASE REFERENCES custom_kits (name) ON DELETE CASCADE,
                shortname TEXT NOT NULL,
                amount INTEGER NOT NULL
            );
            -- Web store: kits for sale (custom kits), orders (PayPal / gift cards) and gift cards
            CREATE TABLE IF NOT EXISTS store_kits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,                    -- shown on the website
                kit TEXT NOT NULL COLLATE NOCASE,      -- custom kit (its claim role) or in-game kit name
                source TEXT NOT NULL DEFAULT 'custom', -- custom (bot kit) / ingame (server kit, given in-game)
                auto INTEGER NOT NULL DEFAULT 0,       -- added automatically (hidden until it has a price)
                items TEXT,                            -- in-game kit contents (JSON, refreshed by the store)
                price REAL NOT NULL,
                days INTEGER NOT NULL DEFAULT 0,       -- 0 = permanent, else the kit role for N days
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS store_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,            -- secret part of the order page address
                kind TEXT NOT NULL,                    -- kit / custom / giftcard
                title TEXT NOT NULL,
                store_kit_id INTEGER,
                items TEXT,                            -- custom kit: JSON [{"short", "amount"}]
                kit_name TEXT,                         -- custom kit: the name the buyer chose
                timer_hours INTEGER,
                player TEXT,
                user_id INTEGER,
                total REAL NOT NULL,                   -- full price
                currency TEXT NOT NULL,
                gift_code TEXT,
                gift_amount REAL NOT NULL DEFAULT 0,   -- paid with the gift card (held while pending)
                paypal_amount REAL NOT NULL DEFAULT 0, -- paid with PayPal
                paypal_order TEXT UNIQUE,
                capture_id TEXT,
                payer_email TEXT,
                admin_key INTEGER NOT NULL DEFAULT 0,  -- 1 = free with the admin key (no payment)
                status TEXT NOT NULL DEFAULT 'pending',   -- pending / paid / delivered / failed / cancelled / expired
                result TEXT,
                request_id INTEGER,                    -- kit request that delivered it
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            );
            -- The server's own (in-game) kits: the Discord role that lets members claim them (cogs/custom_kits.py)
            CREATE TABLE IF NOT EXISTS ingame_kit_roles (
                kit TEXT PRIMARY KEY COLLATE NOCASE,
                role_id INTEGER NOT NULL,
                cooldown_minutes INTEGER NOT NULL DEFAULT 1440
            );
            -- Discord roles the bot created for kits (so they can be removed again)
            CREATE TABLE IF NOT EXISTS kit_created_roles (
                role_id INTEGER PRIMARY KEY,
                kit TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            -- Kits the bot gives automatically to members with the kit's role (when online and the cooldown allows)
            CREATE TABLE IF NOT EXISTS kit_auto_give (
                kind TEXT NOT NULL,                     -- custom / ingame
                kit TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY (kind, kit)
            );
            -- Nukes bought in the shop: a points drop every Discord member can claim once (cogs/economy.py)
            CREATE TABLE IF NOT EXISTS nuke_drops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                points INTEGER NOT NULL,                -- per member who claims
                max_claims INTEGER NOT NULL,            -- 0 = everyone
                channel_id INTEGER,
                message_id INTEGER,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nuke_claims (
                drop_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                PRIMARY KEY (drop_id, user_id)
            );
            -- Premium currency (battle pass, coin packs in the web store) – separate from points
            CREATE TABLE IF NOT EXISTS premium_wallets (
                user_id INTEGER PRIMARY KEY,
                coins INTEGER NOT NULL DEFAULT 0,
                earned_total INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS premium_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gift_cards (
                code TEXT PRIMARY KEY,
                initial REAL NOT NULL,
                balance REAL NOT NULL,
                currency TEXT NOT NULL,
                note TEXT,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                disabled INTEGER NOT NULL DEFAULT 0,
                order_id INTEGER                       -- bought in the store (else created by an owner)
            );
            -- Plugins keep their own data here (JSON values per plugin and key)
            CREATE TABLE IF NOT EXISTS prison (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                roles TEXT NOT NULL DEFAULT '',        -- removed role IDs (restored on release), comma-separated
                reason TEXT,
                jailed_by TEXT,
                jailed_at INTEGER NOT NULL,
                expires_at INTEGER,                   -- auto-release time (epoch seconds); NULL = until freed by hand
                PRIMARY KEY (guild_id, user_id)
            );
            -- Player logins on the website ("Login with Discord"); only a hash of the cookie is stored
            CREATE TABLE IF NOT EXISTS player_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            -- Team slots on a player's own custom kit: each lets one more player claim it until expires_at
            CREATE TABLE IF NOT EXISTS kit_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL COLLATE NOCASE,
                owner_id INTEGER NOT NULL,
                member_id INTEGER,                    -- who is on the slot (Discord ID), NULL = free
                player TEXT,                          -- their in-game name (shown on the website)
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kit_slots_kit ON kit_slots (kit);
            -- Location teleports: a Location quick chat emote (North …) teleports the player to set coordinates
            CREATE TABLE IF NOT EXISTS teleports (
                phrase TEXT PRIMARY KEY,            -- the Location emote's quick chat id
                name TEXT NOT NULL DEFAULT '',      -- e.g. "Outpost"
                x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
                cooldown_minutes INTEGER NOT NULL DEFAULT 0,
                required_role_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS teleport_uses (
                phrase TEXT NOT NULL,
                player TEXT NOT NULL COLLATE NOCASE,
                used_at REAL NOT NULL,
                PRIMARY KEY (phrase, player)
            );
            CREATE TABLE IF NOT EXISTS plugin_data (
                plugin TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (plugin, key)
            );
            """
        )
        # Migrations: older databases don't have newer columns yet
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_rules)")}
        if "kit_type" not in columns:
            self._db.execute("ALTER TABLE kit_rules ADD COLUMN kit_type TEXT NOT NULL DEFAULT 'ingame'")
        if "required_role_id" not in columns:
            # Discord role the (linked) player needs for this autokit; NULL = everyone
            self._db.execute("ALTER TABLE kit_rules ADD COLUMN required_role_id INTEGER")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(tickets)")}
        if "transcript" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN transcript TEXT")
        if "keep_open" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN keep_open INTEGER NOT NULL DEFAULT 0")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(custom_kits)")}
        if "claim_role_ids" not in columns:
            # Roles whose members may claim the kit themselves (comma-separated IDs, empty = admins only)
            self._db.execute("ALTER TABLE custom_kits ADD COLUMN claim_role_ids TEXT NOT NULL DEFAULT ''")
        if "cooldown_minutes" not in columns:
            self._db.execute(f"ALTER TABLE custom_kits ADD COLUMN cooldown_minutes INTEGER NOT NULL "
                             f"DEFAULT {DEFAULT_CLAIM_COOLDOWN}")
        if "disabled" not in columns:
            # Switched off: the kit stays (items, roles, cooldown) but nobody can claim it
            self._db.execute("ALTER TABLE custom_kits ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        pk = [row["name"] for row in self._db.execute("PRAGMA table_info(giveaway_redemptions)") if row["pk"]]
        if pk == ["message_id"]:
            # Older layout allowed only one redemption per giveaway (wrong with several winners)
            self._db.executescript("""
                ALTER TABLE giveaway_redemptions RENAME TO giveaway_redemptions_old;
                CREATE TABLE giveaway_redemptions (
                    message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, prize TEXT NOT NULL,
                    action TEXT NOT NULL, redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (message_id, user_id));
                INSERT INTO giveaway_redemptions SELECT message_id, user_id, prize, action, redeemed_at
                    FROM giveaway_redemptions_old;
                DROP TABLE giveaway_redemptions_old;
            """)
        # Nukes were points-shop items for a short while; they're only sold in the web store now
        self._db.execute("DELETE FROM shop_items WHERE kind = 'nuke'")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_claims)")}
        if "user_id" not in columns:
            # Discord user who claimed the kit (self-service claims)
            self._db.execute("ALTER TABLE kit_claims ADD COLUMN user_id INTEGER")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_kit_claims_user ON kit_claims (user_id, kit)")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_requests)")}
        if "items" not in columns:
            # Requests for a NEW custom kit carry its items (JSON)
            self._db.execute("ALTER TABLE kit_requests ADD COLUMN items TEXT")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(store_payments)")}
        if "raw" not in columns:
            self._db.execute("ALTER TABLE store_payments ADD COLUMN raw TEXT")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_requests)")}
        for column, kind in (("addon", "INTEGER NOT NULL DEFAULT 0"),           # items for an existing kit
                             ("source", "TEXT NOT NULL DEFAULT 'purchase'"),   # purchase / giveaway
                             ("price", "REAL"), ("currency", "TEXT"),         # price list quote (new kits)
                             ("cooldown_minutes", "INTEGER"),                 # bought claim timer
                             ("checkout_name", "TEXT"),                       # name used on the store
                             ("payment_ids", "TEXT"),                         # matching store payments
                             ("pay_note", "TEXT")):                           # payment check result
            if column not in columns:
                self._db.execute(f"ALTER TABLE kit_requests ADD COLUMN {column} {kind}")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(store_kits)")}
        for column, kind in (("source", "TEXT NOT NULL DEFAULT 'custom'"), ("auto", "INTEGER NOT NULL DEFAULT 0"),
                             ("items", "TEXT"), ("category", "TEXT"),
                             ("servers", "TEXT NOT NULL DEFAULT ''")):          # several servers: where it's for
            if column not in columns:
                self._db.execute(f"ALTER TABLE store_kits ADD COLUMN {column} {kind}")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(prison)")}
        if "expires_at" not in columns:
            # When a timed jail should auto-release (epoch seconds); NULL = until freed by hand
            self._db.execute("ALTER TABLE prison ADD COLUMN expires_at INTEGER")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(store_orders)")}
        if "admin_key" not in columns:
            self._db.execute("ALTER TABLE store_orders ADD COLUMN admin_key INTEGER NOT NULL DEFAULT 0")
        if "kit_name" not in columns:
            self._db.execute("ALTER TABLE store_orders ADD COLUMN kit_name TEXT")
        for column in ("slot_count", "months", "slot_id", "coins"):  # team slots ("slots"), coin packs ("coins")
            if column not in columns:
                self._db.execute(f"ALTER TABLE store_orders ADD COLUMN {column} INTEGER")
        if "servers_done" not in columns:          # in-game kit for several servers: where it was given already
            self._db.execute("ALTER TABLE store_orders ADD COLUMN servers_done TEXT")
        if "shop_server" not in columns:           # several servers: the server whose shop it was bought in
            self._db.execute("ALTER TABLE store_orders ADD COLUMN shop_server TEXT")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.commit()
        self._open_links(links_path, path)

    # ------------------------------------------------------------ Shared links (all Rust servers of this bot)

    def _open_links(self, links_path: "Path | None", main_path: Path) -> None:
        """Linked Discord-account <-> in-game-player pairs can live in their own SQLite file in the shared data
        folder, so a link made on one Rust server (servers.py) counts on all of them. Without a shared path (a
        stand-alone copy, or tests) they stay in the main database as before."""
        self._links = self._db                                   # default: the main database
        if links_path is None or links_path == main_path:
            return
        links_path.parent.mkdir(parents=True, exist_ok=True)
        dbcrypt.ensure_state(links_path)
        conn = dbcrypt.connect(links_path, timeout=10)
        conn.row_factory = dbcrypt.row_factory(conn)
        conn.execute("PRAGMA journal_mode = WAL")                # several server processes read/write it
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS linked_players (
                   user_id INTEGER PRIMARY KEY,
                   player TEXT NOT NULL UNIQUE COLLATE NOCASE,
                   linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )""")
        conn.commit()
        self._links = conn
        # One-time merge: copy this server's own links (from the main database) into the shared store, keeping
        # whatever is already there. Every server does this once, so the shared store ends up with the union.
        try:
            rows = self._db.execute("SELECT user_id, player, linked_at FROM linked_players").fetchall()
        except dbcrypt.ERRORS:
            rows = []
        if rows:
            self._links.executemany(
                "INSERT OR IGNORE INTO linked_players (user_id, player, linked_at) VALUES (?, ?, ?)",
                [(r["user_id"], r["player"], r["linked_at"]) for r in rows])
            self._links.commit()

    def _ensure(self, name: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (name,))

    def _add(self, name: str, column: str, amount: int = 1) -> None:
        self._ensure(name)
        self._db.execute(
            f"UPDATE players SET {column} = {column} + ?, last_seen = CURRENT_TIMESTAMP WHERE name = ?",
            (amount, name),
        )

    def record_kill(self, killer: str | None, victim: str | None) -> None:
        if killer:
            self._add(killer, "kills")
        if victim:
            self._add(victim, "deaths")
        self._db.commit()

    def record_suicide(self, player: str) -> None:
        self._add(player, "suicides")
        self._db.commit()

    def add_playtime(self, names: list[str], seconds: int) -> None:
        for name in names:
            self._add(name, "playtime_seconds", seconds)
        self._db.commit()

    def set_platform(self, name: str, platform: str) -> None:
        self._ensure(name)
        self._db.execute("UPDATE players SET platform = ? WHERE name = ?", (platform, name))
        self._db.commit()

    def leaderboard(self, category: str, limit: int = 10) -> list[sqlite3.Row]:
        order = LEADERBOARD_COLUMNS[category]
        return self._db.execute(
            f"SELECT * FROM players ORDER BY {order} LIMIT ?", (limit,)
        ).fetchall()

    def player(self, name: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM players WHERE name = ?", (name,)).fetchone()

    def search_names(self, prefix: str, limit: int = 25) -> list[str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._db.execute(
            "SELECT name FROM players WHERE name LIKE ? ESCAPE '\\' ORDER BY last_seen DESC LIMIT ?",
            (f"{escaped}%", limit),
        ).fetchall()
        return [r["name"] for r in rows]

    def get_value(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        self._db.commit()

    # ------------------------------------------------------------ Kit manager

    def add_kit_rule(self, kit: str, trigger: str, phrase: str | None, cooldown_minutes: int,
                     max_claims: int, created_by: str, kit_type: str = "ingame",
                     required_role_id: int | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO kit_rules (kit, kit_type, trigger, phrase, cooldown_minutes, max_claims, created_by, "
            "required_role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kit, kit_type, trigger, phrase, cooldown_minutes, max_claims, created_by, required_role_id),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def update_kit_rule(self, rule_id: int, *, cooldown_minutes: int | None = None,
                        max_claims: int | None = None) -> bool:
        """Changes a rule's cooldown and/or limit (None = keep)."""
        row = self.kit_rule(rule_id)
        if row is None:
            return False
        cooldown = row["cooldown_minutes"] if cooldown_minutes is None else max(0, int(cooldown_minutes))
        limit = row["max_claims"] if max_claims is None else max(0, int(max_claims))
        self._db.execute("UPDATE kit_rules SET cooldown_minutes = ?, max_claims = ? WHERE id = ?",
                         (cooldown, limit, rule_id))
        self._db.commit()
        return True

    def set_kit_rule_kit(self, rule_id: int, kit: str) -> bool:
        cur = self._db.execute("UPDATE kit_rules SET kit = ? WHERE id = ?", (kit, rule_id))
        self._db.commit()
        return cur.rowcount > 0

    def set_kit_rule_role(self, rule_id: int, role_id: int | None) -> bool:
        cur = self._db.execute("UPDATE kit_rules SET required_role_id = ? WHERE id = ?", (role_id, rule_id))
        self._db.commit()
        return cur.rowcount > 0

    def kit_rules(self, *, enabled_only: bool = False, trigger: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM kit_rules WHERE 1=1", []
        if enabled_only:
            sql += " AND enabled = 1"
        if trigger:
            sql += " AND trigger = ?"
            args.append(trigger)
        return self._db.execute(sql + " ORDER BY id", args).fetchall()

    def kit_rule(self, rule_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM kit_rules WHERE id = ?", (rule_id,)).fetchone()

    def set_kit_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        cur = self._db.execute("UPDATE kit_rules SET enabled = ? WHERE id = ?", (int(enabled), rule_id))
        self._db.commit()
        return cur.rowcount > 0

    def delete_kit_rule(self, rule_id: int) -> bool:
        cur = self._db.execute("DELETE FROM kit_rules WHERE id = ?", (rule_id,))
        self._db.commit()
        return cur.rowcount > 0

    def kit_claim_state(self, rule_id: int, player: str) -> tuple[int, float | None]:
        """(number of claims, minutes since the last claim or None)."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n, (julianday('now') - julianday(MAX(claimed_at))) * 1440 AS minutes "
            "FROM kit_claims WHERE rule_id = ? AND player = ?",
            (rule_id, player),
        ).fetchone()
        return int(row["n"]), row["minutes"]

    def record_kit_claim(self, rule_id: int | None, kit: str, player: str, source: str) -> int:
        cur = self._db.execute(
            "INSERT INTO kit_claims (rule_id, kit, player, source) VALUES (?, ?, ?, ?)",
            (rule_id, kit, player, source),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def delete_kit_claim(self, claim_id: int) -> None:
        self._db.execute("DELETE FROM kit_claims WHERE id = ?", (claim_id,))
        self._db.commit()

    def reset_kit_claims(self, rule_id: int | None = None) -> int:
        """Clears the autokit history (e.g. after a wipe); manual gives are kept."""
        if rule_id is None:
            cur = self._db.execute("DELETE FROM kit_claims WHERE rule_id IS NOT NULL")
        else:
            cur = self._db.execute("DELETE FROM kit_claims WHERE rule_id = ?", (rule_id,))
        self._db.commit()
        return cur.rowcount

    def kit_claim_stats(self) -> dict[str, dict]:
        """Per kit (lower case): how often it was given/claimed, by how many players, and when last."""
        rows = self._db.execute("SELECT LOWER(kit) AS kit, COUNT(*) AS total, COUNT(DISTINCT player) AS players, "
                                "MAX(claimed_at) AS last FROM kit_claims GROUP BY LOWER(kit)").fetchall()
        return {r["kit"]: {"total": r["total"], "players": r["players"], "last": r["last"]} for r in rows}

    def kit_claim_history(self, player: str | None = None, limit: int = 15) -> list[sqlite3.Row]:
        if player:
            return self._db.execute(
                "SELECT * FROM kit_claims WHERE player = ? ORDER BY id DESC LIMIT ?", (player, limit)
            ).fetchall()
        return self._db.execute("SELECT * FROM kit_claims ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------ Location teleports

    def teleports(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM teleports ORDER BY phrase").fetchall()

    def teleport(self, phrase: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM teleports WHERE phrase = ?", (phrase,)).fetchone()

    def set_teleport(self, phrase: str, x: float, y: float, z: float, *, name: str | None = None,
                     cooldown_minutes: int | None = None, required_role_id: int | None | bool = False,
                     enabled: bool | None = None, by: str = "") -> None:
        """Creates or changes a teleport; None/False = keep that value (required_role_id=None clears the role)."""
        old = self.teleport(phrase)
        keep = (lambda key, default: old[key] if old is not None else default)
        self._db.execute(
            "INSERT OR REPLACE INTO teleports (phrase, name, x, y, z, cooldown_minutes, required_role_id, enabled, "
            "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (phrase, (name if name is not None else keep("name", ""))[:40], float(x), float(y), float(z),
             max(0, int(cooldown_minutes)) if cooldown_minutes is not None else keep("cooldown_minutes", 0),
             keep("required_role_id", None) if required_role_id is False else required_role_id,
             int(enabled) if enabled is not None else keep("enabled", 1), by[:80]))
        self._db.commit()

    def update_teleport(self, phrase: str, **fields) -> bool:
        old = self.teleport(phrase)
        if old is None:
            return False
        self.set_teleport(phrase, fields.get("x", old["x"]), fields.get("y", old["y"]), fields.get("z", old["z"]),
                          name=fields.get("name"), cooldown_minutes=fields.get("cooldown_minutes"),
                          required_role_id=fields.get("required_role_id", False), enabled=fields.get("enabled"),
                          by=fields.get("by", old["updated_by"] or ""))
        return True

    def delete_teleport(self, phrase: str) -> bool:
        cur = self._db.execute("DELETE FROM teleports WHERE phrase = ?", (phrase,))
        self._db.execute("DELETE FROM teleport_uses WHERE phrase = ?", (phrase,))
        self._db.commit()
        return cur.rowcount > 0

    def teleport_last_use(self, phrase: str, player: str) -> float | None:
        row = self._db.execute("SELECT used_at FROM teleport_uses WHERE phrase = ? AND player = ?",
                               (phrase, player)).fetchone()
        return row["used_at"] if row else None

    def record_teleport_use(self, phrase: str, player: str, when: float) -> None:
        self._db.execute("INSERT OR REPLACE INTO teleport_uses (phrase, player, used_at) VALUES (?, ?, ?)",
                         (phrase, player, when))
        self._db.commit()

    def reset_teleport_uses(self) -> int:
        cur = self._db.execute("DELETE FROM teleport_uses")
        self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ Custom Kits

    def create_custom_kit(self, name: str, description: str | None, created_by: str) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO custom_kits (name, description, created_by) VALUES (?, ?, ?)",
            (name, description, created_by),
        )
        self._db.commit()
        return cur.rowcount > 0

    def custom_kit(self, name: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM custom_kits WHERE name = ?", (name,)).fetchone()

    def custom_kits(self) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT k.*, COUNT(i.id) AS item_count FROM custom_kits k "
            "LEFT JOIN custom_kit_items i ON i.kit = k.name GROUP BY k.name ORDER BY k.name"
        ).fetchall()

    def delete_custom_kit(self, name: str) -> int:
        """Deletes the kit, its items and related autokit rules. Returns the number of deleted rules."""
        rules = self._db.execute(
            "DELETE FROM kit_rules WHERE kit_type = 'custom' AND kit = ? COLLATE NOCASE", (name,)
        ).rowcount
        self._db.execute("DELETE FROM custom_kit_items WHERE kit = ?", (name,))
        self._db.execute("DELETE FROM custom_kits WHERE name = ?", (name,))
        self._db.commit()
        return rules

    def add_custom_kit_item(self, kit: str, shortname: str, amount: int) -> int:
        cur = self._db.execute(
            "INSERT INTO custom_kit_items (kit, shortname, amount) VALUES (?, ?, ?)", (kit, shortname, amount)
        )
        self._db.commit()
        return int(cur.lastrowid)

    def clear_custom_kit_items(self, kit: str) -> int:
        cur = self._db.execute("DELETE FROM custom_kit_items WHERE kit = ?", (kit,))
        self._db.commit()
        return cur.rowcount

    def remove_custom_kit_item(self, kit: str, item_id: int) -> bool:
        cur = self._db.execute("DELETE FROM custom_kit_items WHERE id = ? AND kit = ?", (item_id, kit))
        self._db.commit()
        return cur.rowcount > 0

    def custom_kit_items(self, kit: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM custom_kit_items WHERE kit = ? ORDER BY id", (kit,)
        ).fetchall()

    def set_custom_kit_cooldown(self, kit: str, minutes: int) -> bool:
        cur = self._db.execute("UPDATE custom_kits SET cooldown_minutes = ? WHERE name = ?", (minutes, kit))
        self._db.commit()
        return cur.rowcount > 0

    def set_custom_kit_disabled(self, kit: str, disabled: bool) -> bool:
        cur = self._db.execute("UPDATE custom_kits SET disabled = ? WHERE name = ?", (int(bool(disabled)), kit))
        self._db.commit()
        return cur.rowcount > 0

    def set_all_custom_kits_disabled(self, disabled: bool) -> int:
        """Switches every custom kit off (or on). Returns how many kits changed."""
        cur = self._db.execute("UPDATE custom_kits SET disabled = ? WHERE disabled != ?",
                               (int(bool(disabled)), int(bool(disabled))))
        self._db.commit()
        return cur.rowcount

    def set_custom_kit_roles(self, kit: str, role_ids: list[int]) -> bool:
        value = ",".join(str(r) for r in dict.fromkeys(role_ids))
        cur = self._db.execute("UPDATE custom_kits SET claim_role_ids = ? WHERE name = ?", (value, kit))
        self._db.commit()
        return cur.rowcount > 0

    @staticmethod
    def claim_roles(row: sqlite3.Row) -> list[int]:
        return [int(r) for r in (row["claim_role_ids"] or "").split(",") if r.strip().isdigit()]

    def record_custom_claim(self, kit: str, player: str, user_id: int, user: str) -> int:
        cur = self._db.execute(
            "INSERT INTO kit_claims (rule_id, kit, player, source, user_id) VALUES (NULL, ?, ?, ?, ?)",
            (kit, player, f"{CLAIM_SOURCE}:discord:{user}", user_id),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def minutes_since_custom_claim(self, kit: str, player: str) -> float | None:
        """Minutes since this player last claimed the kit themselves (None = never). Cooldown is per player."""
        row = self._db.execute(
            "SELECT (julianday('now') - julianday(MAX(claimed_at))) * 1440 AS minutes FROM kit_claims "
            "WHERE player = ? AND kit = ? COLLATE NOCASE AND source LIKE ?",
            (player, kit, CLAIM_SOURCE + ":%"),
        ).fetchone()
        return row["minutes"]

    def reset_custom_claims(self, kit: str, player: str | None = None) -> int:
        """Resets claim cooldowns of a kit (for one player or everyone); the history entries stay visible."""
        sql = "UPDATE kit_claims SET source = 'reset-' || source WHERE kit = ? COLLATE NOCASE AND source LIKE ?"
        args: list = [kit, CLAIM_SOURCE + ":%"]
        if player is not None:
            sql += " AND player = ?"
            args.append(player)
        cur = self._db.execute(sql, args)
        self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ In-game kits claimed with a Discord role

    def ingame_kit_roles(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM ingame_kit_roles ORDER BY kit COLLATE NOCASE").fetchall()

    def ingame_kit_role(self, kit: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM ingame_kit_roles WHERE kit = ?", (kit,)).fetchone()

    def set_ingame_kit_role(self, kit: str, role_id: int) -> None:
        with self._db:
            self._db.execute("INSERT INTO ingame_kit_roles (kit, role_id) VALUES (?, ?) "
                             "ON CONFLICT(kit) DO UPDATE SET role_id = excluded.role_id", (kit, role_id))

    def set_ingame_kit_cooldown(self, kit: str, minutes: int) -> bool:
        with self._db:
            cur = self._db.execute("UPDATE ingame_kit_roles SET cooldown_minutes = ? WHERE kit = ?", (minutes, kit))
        return cur.rowcount > 0

    def record_created_role(self, role_id: int, kit: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO kit_created_roles (role_id, kit, created_at) VALUES (?, ?, ?)",
                             (role_id, kit, int(time.time())))

    def created_role_ids(self) -> set[int]:
        return {int(r["role_id"]) for r in self._db.execute("SELECT role_id FROM kit_created_roles")}

    def forget_created_role(self, role_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM kit_created_roles WHERE role_id = ?", (role_id,))

    def delete_ingame_kit_role(self, kit: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM ingame_kit_roles WHERE kit = ?", (kit,))

    def auto_give_kits(self) -> set[tuple[str, str]]:
        """(kind, kit in lower case) of the kits given automatically to their role's members."""
        return {(r["kind"], r["kit"].lower()) for r in self._db.execute("SELECT * FROM kit_auto_give")}

    def set_auto_give(self, kind: str, kit: str, on: bool) -> None:
        with self._db:
            if on:
                self._db.execute("INSERT OR IGNORE INTO kit_auto_give (kind, kit) VALUES (?, ?)", (kind, kit))
            else:
                self._db.execute("DELETE FROM kit_auto_give WHERE kind = ? AND kit = ?", (kind, kit))

    def record_ingame_claim(self, kit: str, player: str, user_id: int | None, user: str) -> None:
        with self._db:
            self._db.execute("INSERT INTO kit_claims (rule_id, kit, player, source, user_id) VALUES (NULL, ?, ?, ?, ?)",
                             (kit, player, f"{INGAME_CLAIM_SOURCE}:{user}", user_id))

    def minutes_since_ingame_claim(self, kit: str, player: str) -> float | None:
        row = self._db.execute(
            "SELECT (julianday('now') - julianday(MAX(claimed_at))) * 1440 AS minutes FROM kit_claims "
            "WHERE player = ? AND kit = ? COLLATE NOCASE AND source LIKE ?",
            (player, kit, INGAME_CLAIM_SOURCE + ":%")).fetchone()
        return row["minutes"]

    def reset_ingame_claims(self, kit: str, player: str | None = None) -> int:
        sql = "UPDATE kit_claims SET source = 'reset-' || source WHERE kit = ? COLLATE NOCASE AND source LIKE ?"
        args: list = [kit, INGAME_CLAIM_SOURCE + ":%"]
        if player is not None:
            sql += " AND player = ?"
            args.append(player)
        with self._db:
            cur = self._db.execute(sql, args)
        return cur.rowcount

    # ------------------------------------------------------------ Giveaway wins (other bots)

    def giveaway_redemption(self, message_id: int, user_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM giveaway_redemptions WHERE message_id = ? AND user_id = ?",
                                (message_id, user_id)).fetchone()

    def record_giveaway_redemption(self, message_id: int, user_id: int, prize: str, action: str) -> bool:
        cur = self._db.execute("INSERT OR IGNORE INTO giveaway_redemptions (message_id, user_id, prize, action) "
                               "VALUES (?, ?, ?, ?)", (message_id, user_id, prize, action))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ Linked players

    def linked_player(self, user_id: int) -> str | None:
        row = self._links.execute("SELECT player FROM linked_players WHERE user_id = ?", (user_id,)).fetchone()
        return row["player"] if row else None

    def player_owner(self, player: str) -> int | None:
        row = self._links.execute("SELECT user_id FROM linked_players WHERE player = ?", (player,)).fetchone()
        return int(row["user_id"]) if row else None

    def link_player(self, user_id: int, player: str) -> bool:
        """Links (or re-links) a Discord account to a player. False if the player belongs to someone else."""
        owner = self.player_owner(player)
        if owner is not None and owner != user_id:
            return False
        self._links.execute("INSERT OR REPLACE INTO linked_players (user_id, player) VALUES (?, ?)", (user_id, player))
        self._links.commit()
        return True

    def unlink_player(self, user_id: int) -> str | None:
        player = self.linked_player(user_id)
        self._links.execute("DELETE FROM linked_players WHERE user_id = ?", (user_id,))
        self._links.commit()
        return player

    def linked_players(self) -> list[sqlite3.Row]:
        return self._links.execute("SELECT * FROM linked_players ORDER BY player").fetchall()

    # ------------------------------------------------------------ Tickets

    _TICKET_FIELDS = {"channel_id", "ai_enabled", "escalated", "staff_joined", "ai_replies", "keep_open"}

    def create_ticket(self, user_id: int, topic: str, ingame_name: str | None) -> int:
        cur = self._db.execute(
            "INSERT INTO tickets (user_id, topic, ingame_name) VALUES (?, ?, ?)", (user_id, topic, ingame_name)
        )
        self._db.commit()
        return int(cur.lastrowid)

    def ticket(self, ticket_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()

    def ticket_by_channel(self, channel_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
        ).fetchone()

    def open_ticket_of_user(self, user_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM tickets WHERE user_id = ? AND status = 'open' AND channel_id IS NOT NULL", (user_id,)
        ).fetchone()

    def update_ticket(self, ticket_id: int, **fields) -> None:
        unknown = set(fields) - self._TICKET_FIELDS
        if unknown:
            raise ValueError(f"Unknown ticket fields: {unknown}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", (*fields.values(), ticket_id))
        self._db.commit()

    def increment_ticket_ai_replies(self, ticket_id: int) -> int:
        self._db.execute("UPDATE tickets SET ai_replies = ai_replies + 1 WHERE id = ?", (ticket_id,))
        self._db.commit()
        return int(self.ticket(ticket_id)["ai_replies"])

    def close_ticket(self, ticket_id: int, closed_by: str, reason: str | None,
                     transcript: str | None = None) -> None:
        self._db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP, closed_by = ?, close_reason = ?, "
            "transcript = ? WHERE id = ?",
            (closed_by, reason, transcript, ticket_id),
        )
        self._db.commit()

    def tickets(self, *, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM tickets WHERE channel_id IS NOT NULL", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        return self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    def delete_ticket(self, ticket_id: int) -> None:
        self._db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        self._db.commit()

    # ------------------------------------------------------------ Giveaways

    def create_giveaway(self, guild_id: int, channel_id: int, prize: str, winners: int, host_id: int,
                        ends_at: int) -> int:
        cur = self._db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, prize, winners, host_id, ends_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, prize, winners, host_id, ends_at),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def set_giveaway_message(self, giveaway_id: int, message_id: int) -> None:
        self._db.execute("UPDATE giveaways SET message_id = ? WHERE id = ?", (message_id, giveaway_id))
        self._db.commit()

    def delete_giveaway(self, giveaway_id: int) -> None:
        self._db.execute("DELETE FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
        self._db.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))
        self._db.commit()

    def giveaway(self, giveaway_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,)).fetchone()

    def giveaway_by_message(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()

    def giveaways(self, *, active_only: bool = False, limit: int = 50) -> list[sqlite3.Row]:
        sql = ("SELECT g.*, (SELECT COUNT(*) FROM giveaway_entries e WHERE e.giveaway_id = g.id) AS entries "
               "FROM giveaways g WHERE message_id IS NOT NULL")
        if active_only:
            sql += " AND ended = 0"
        return self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def ended_giveaways(self, guild_id: int, *, limit: int = 200) -> list[sqlite3.Row]:
        """Finished giveaways with at least one winner (newest first) – the ticket AI looks up wins here."""
        return self._db.execute(
            "SELECT * FROM giveaways WHERE guild_id = ? AND ended = 1 AND winner_ids != '' "
            "AND message_id IS NOT NULL ORDER BY ends_at DESC LIMIT ?", (guild_id, limit)).fetchall()

    def due_giveaways(self, now: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM giveaways WHERE ended = 0 AND message_id IS NOT NULL AND ends_at <= ?", (now,)
        ).fetchall()

    def add_giveaway_entry(self, giveaway_id: int, user_id: int) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)", (giveaway_id, user_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    def remove_giveaway_entry(self, giveaway_id: int, user_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (giveaway_id, user_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    def giveaway_entries(self, giveaway_id: int) -> list[int]:
        rows = self._db.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
        return [int(r["user_id"]) for r in rows]

    def finish_giveaway(self, giveaway_id: int, winner_ids: list[int]) -> None:
        self._db.execute(
            "UPDATE giveaways SET ended = 1, winner_ids = ? WHERE id = ?",
            (",".join(str(w) for w in winner_ids), giveaway_id),
        )
        self._db.commit()

    # ------------------------------------------------------------ Points & shop

    def wallet(self, user_id: int) -> sqlite3.Row:
        self._db.execute("INSERT OR IGNORE INTO wallets (user_id) VALUES (?)", (user_id,))
        return self._db.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()

    def add_points(self, user_id: int, delta: int, reason: str, *, log: bool = True) -> int:
        """Adds (or with a negative delta removes, never below 0) points. Returns the new balance.
        log=False: not in the history (e.g. the many small playtime payouts)."""
        self.wallet(user_id)
        earned = max(delta, 0)
        self._db.execute("UPDATE wallets SET points = MAX(points + ?, 0), earned_total = earned_total + ? "
                         "WHERE user_id = ?", (delta, earned, user_id))
        if log:
            self._db.execute("INSERT INTO point_log (user_id, delta, reason) VALUES (?, ?, ?)",
                             (user_id, delta, reason[:200]))
        self._db.commit()
        return int(self.wallet(user_id)["points"])

    def top_wallets(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._db.execute("SELECT user_id, points FROM wallets WHERE points > 0 ORDER BY points DESC LIMIT ?",
                                (limit,)).fetchall()

    def spend_points(self, user_id: int, cost: int, reason: str) -> int | None:
        """Takes `cost` points if the balance is high enough (atomic). New balance, or None if too few points."""
        self.wallet(user_id)
        cur = self._db.execute("UPDATE wallets SET points = points - ? WHERE user_id = ? AND points >= ?",
                               (cost, user_id, cost))
        if cur.rowcount == 0:
            self._db.commit()
            return None
        self._db.execute("INSERT INTO point_log (user_id, delta, reason) VALUES (?, ?, ?)", (user_id, -cost, reason[:200]))
        self._db.commit()
        return int(self.wallet(user_id)["points"])

    # ------------------------------------------------------------ Nukes (points drops)

    def create_nuke(self, buyer_id: int, item: str, points: int, max_claims: int, expires_at: int) -> int:
        with self._db:
            cur = self._db.execute("INSERT INTO nuke_drops (buyer_id, item, points, max_claims, created_at, expires_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?)",
                                   (buyer_id, item, points, max_claims, int(time.time()), expires_at))
        return int(cur.lastrowid)

    def set_nuke_message(self, drop_id: int, channel_id: int, message_id: int) -> None:
        with self._db:
            self._db.execute("UPDATE nuke_drops SET channel_id = ?, message_id = ? WHERE id = ?",
                             (channel_id, message_id, drop_id))

    def delete_nuke(self, drop_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM nuke_claims WHERE drop_id = ?", (drop_id,))
            self._db.execute("DELETE FROM nuke_drops WHERE id = ?", (drop_id,))

    def nuke(self, drop_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM nuke_drops WHERE id = ?", (drop_id,)).fetchone()

    def nuke_by_message(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM nuke_drops WHERE message_id = ?", (message_id,)).fetchone()

    def nuke_claims(self, drop_id: int) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM nuke_claims WHERE drop_id = ?", (drop_id,)).fetchone()[0])

    def claim_nuke(self, drop_id: int, user_id: int, now: int | None = None) -> str:
        """One member claims a drop (atomic: the claim and the points go in together). Returns "ok", "missing",
        "own" (the buyer), "expired", "empty" (everyone who could has claimed) or "claimed" (already did)."""
        now = int(time.time()) if now is None else now
        with self._db:
            drop = self._db.execute("SELECT * FROM nuke_drops WHERE id = ?", (drop_id,)).fetchone()
            if drop is None:
                return "missing"
            if drop["buyer_id"] == user_id:
                return "own"
            if self._db.execute("SELECT 1 FROM nuke_claims WHERE drop_id = ? AND user_id = ?",
                                (drop_id, user_id)).fetchone():
                return "claimed"
            if now >= drop["expires_at"]:
                return "expired"
            if drop["max_claims"] and self.nuke_claims(drop_id) >= drop["max_claims"]:
                return "empty"
            self._db.execute("INSERT INTO nuke_claims (drop_id, user_id, ts) VALUES (?, ?, ?)", (drop_id, user_id, now))
            self._db.execute("INSERT OR IGNORE INTO wallets (user_id) VALUES (?)", (user_id,))
            self._db.execute("UPDATE wallets SET points = points + ?, earned_total = earned_total + ? WHERE user_id = ?",
                             (drop["points"], drop["points"], user_id))
            self._db.execute("INSERT INTO point_log (user_id, delta, reason) VALUES (?, ?, ?)",
                             (user_id, drop["points"], f"nuke: {drop['item']}"[:200]))
        return "ok"

    # ------------------------------------------------------------ Premium currency

    def coins(self, user_id: int) -> int:
        row = self._db.execute("SELECT coins FROM premium_wallets WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["coins"]) if row else 0

    def add_coins(self, user_id: int, delta: int, reason: str) -> int:
        """Adds (or removes, never below 0) premium currency. Returns the new balance."""
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO premium_wallets (user_id) VALUES (?)", (user_id,))
            self._db.execute("UPDATE premium_wallets SET coins = MAX(coins + ?, 0), earned_total = earned_total + ? "
                             "WHERE user_id = ?", (delta, max(delta, 0), user_id))
            self._db.execute("INSERT INTO premium_log (user_id, delta, reason, ts) VALUES (?, ?, ?, ?)",
                             (user_id, delta, reason[:200], int(time.time())))
        return self.coins(user_id)

    def spend_coins(self, user_id: int, cost: int, reason: str) -> int | None:
        """Takes `cost` if the balance is high enough (atomic). New balance, or None if too few."""
        with self._db:
            cur = self._db.execute("UPDATE premium_wallets SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
                                   (cost, user_id, cost))
            if cur.rowcount == 0:
                return None
            self._db.execute("INSERT INTO premium_log (user_id, delta, reason, ts) VALUES (?, ?, ?, ?)",
                             (user_id, -cost, reason[:200], int(time.time())))
        return self.coins(user_id)

    def coin_history(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM premium_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                (user_id, limit)).fetchall()

    def top_wallets(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM wallets WHERE points > 0 OR earned_total > 0 "
                                "ORDER BY points DESC, earned_total DESC LIMIT ?", (limit,)).fetchall()

    def point_history(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM point_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                (user_id, limit)).fetchall()

    def set_daily(self, user_id: int, when: int, streak: int) -> None:
        self.wallet(user_id)
        self._db.execute("UPDATE wallets SET daily_last = ?, daily_streak = ? WHERE user_id = ?", (when, streak, user_id))
        self._db.commit()

    def add_playtime_progress(self, user_id: int, seconds: int) -> int:
        """Adds online seconds towards the playtime reward; returns the new total."""
        self.wallet(user_id)
        self._db.execute("UPDATE wallets SET playtime_progress = playtime_progress + ? WHERE user_id = ?",
                         (seconds, user_id))
        self._db.commit()
        return int(self.wallet(user_id)["playtime_progress"])

    def reset_points(self) -> int:
        """Sets every balance to 0 (e.g. on a wipe). The history stays."""
        cur = self._db.execute("UPDATE wallets SET points = 0 WHERE points != 0")
        self._db.commit()
        return cur.rowcount

    _SHOP_FIELDS = ("name", "kind", "target", "amount", "price", "description", "enabled")

    def shop_items(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM shop_items" + (" WHERE enabled = 1" if enabled_only else "")
        return self._db.execute(sql + " ORDER BY price, name").fetchall()

    def shop_item(self, item_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,)).fetchone()

    def shop_item_by_name(self, name: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM shop_items WHERE name = ?", (name.strip(),)).fetchone()

    def add_shop_item(self, name: str, kind: str, target: str, amount: int, price: int,
                      description: str | None) -> int:
        cur = self._db.execute("INSERT INTO shop_items (name, kind, target, amount, price, description) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (name, kind, target, amount, price, description))
        self._db.commit()
        return int(cur.lastrowid)

    def update_shop_item(self, item_id: int, **fields) -> bool:
        fields = {k: v for k, v in fields.items() if k in self._SHOP_FIELDS}
        if not fields:
            return False
        cur = self._db.execute(f"UPDATE shop_items SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                               (*fields.values(), item_id))
        self._db.commit()
        return cur.rowcount > 0

    def delete_shop_item(self, item_id: int) -> bool:
        cur = self._db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ Wipe resets

    def reset_stats(self) -> int:
        """Clears kills, deaths and playtime of all players (wipe reset). Platform and names stay."""
        cur = self._db.execute("UPDATE players SET kills = 0, deaths = 0, suicides = 0, playtime_seconds = 0")
        self._db.commit()
        return cur.rowcount

    def reset_all_custom_claims(self) -> int:
        """Resets the self-service cooldowns of all custom kits (wipe)."""
        cur = self._db.execute("UPDATE kit_claims SET source = 'reset-' || source WHERE source LIKE ?",
                               (CLAIM_SOURCE + ":%",))
        self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ Scheduled messages

    def scheduled_messages(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM scheduled_messages" + (" WHERE enabled = 1" if enabled_only else "")
        return self._db.execute(sql + " ORDER BY id").fetchall()

    def scheduled_message(self, msg_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM scheduled_messages WHERE id = ?", (msg_id,)).fetchone()

    def add_scheduled_message(self, text: str, interval_minutes: int, target: str, channel_id: int | None,
                              created_by: str, now: int) -> int:
        cur = self._db.execute("INSERT INTO scheduled_messages (text, interval_minutes, target, channel_id, "
                               "created_by, last_sent) VALUES (?, ?, ?, ?, ?, ?)",
                               (text, interval_minutes, target, channel_id, created_by, now))
        self._db.commit()
        return int(cur.lastrowid)

    def set_scheduled_enabled(self, msg_id: int, enabled: bool) -> bool:
        cur = self._db.execute("UPDATE scheduled_messages SET enabled = ? WHERE id = ?", (int(enabled), msg_id))
        self._db.commit()
        return cur.rowcount > 0

    def mark_scheduled_sent(self, msg_id: int, when: int) -> None:
        self._db.execute("UPDATE scheduled_messages SET last_sent = ? WHERE id = ?", (when, msg_id))
        self._db.commit()

    def delete_scheduled_message(self, msg_id: int) -> bool:
        cur = self._db.execute("DELETE FROM scheduled_messages WHERE id = ?", (msg_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ Reports & watchlist

    def add_report(self, reporter_id: int, player: str, reason: str) -> int:
        cur = self._db.execute("INSERT INTO reports (reporter_id, player, reason) VALUES (?, ?, ?)",
                               (reporter_id, player, reason))
        self._db.commit()
        return int(cur.lastrowid)

    def set_report_message(self, report_id: int, channel_id: int, message_id: int) -> None:
        self._db.execute("UPDATE reports SET channel_id = ?, message_id = ? WHERE id = ?",
                         (channel_id, message_id, report_id))
        self._db.commit()

    def report(self, report_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()

    def report_by_message(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM reports WHERE message_id = ?", (message_id,)).fetchone()

    def reports(self, *, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM reports", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        return self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    def handle_report(self, report_id: int, handled_by: str) -> bool:
        cur = self._db.execute("UPDATE reports SET status = 'handled', handled_by = ?, handled_at = CURRENT_TIMESTAMP "
                               "WHERE id = ? AND status = 'open'", (handled_by, report_id))
        self._db.commit()
        return cur.rowcount > 0

    def reports_by_player(self, player: str) -> int:
        row = self._db.execute("SELECT COUNT(*) AS n FROM reports WHERE player = ? COLLATE NOCASE", (player,)).fetchone()
        return int(row["n"])

    def watch(self, player: str, reason: str | None, added_by: str) -> bool:
        cur = self._db.execute("INSERT OR REPLACE INTO watchlist (player, reason, added_by) VALUES (?, ?, ?)",
                               (player, reason, added_by))
        self._db.commit()
        return cur.rowcount > 0

    def unwatch(self, player: str) -> bool:
        cur = self._db.execute("DELETE FROM watchlist WHERE player = ?", (player,))
        self._db.commit()
        return cur.rowcount > 0

    def watched(self, player: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM watchlist WHERE player = ?", (player,)).fetchone()

    def watchlist(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM watchlist ORDER BY created_at DESC").fetchall()

    def mark_watch_alert(self, player: str, when: int) -> None:
        self._db.execute("UPDATE watchlist SET last_alert = ? WHERE player = ?", (when, player))
        self._db.commit()

    # ------------------------------------------------------------ Teams

    def team_event(self, kind: str, player: str, team_id: int, other: str | None = None) -> None:
        """Keeps team membership up to date from the team log lines (create / join / leave / promote)."""
        if kind == "create":
            self._db.execute("INSERT OR REPLACE INTO teams (team_id, leader) VALUES (?, ?)", (team_id, player))
            self._db.execute("INSERT OR REPLACE INTO team_members (player, team_id) VALUES (?, ?)", (player, team_id))
        elif kind == "join":
            self._db.execute("INSERT OR IGNORE INTO teams (team_id, leader) VALUES (?, ?)", (team_id, other))
            self._db.execute("INSERT OR REPLACE INTO team_members (player, team_id) VALUES (?, ?)", (player, team_id))
            if other:
                self._db.execute("INSERT OR IGNORE INTO team_members (player, team_id) VALUES (?, ?)", (other, team_id))
        elif kind == "leave":
            self._db.execute("DELETE FROM team_members WHERE player = ? AND team_id = ?", (player, team_id))
            left = self._db.execute("SELECT player FROM team_members WHERE team_id = ? LIMIT 1", (team_id,)).fetchone()
            if left is None:
                self._db.execute("DELETE FROM teams WHERE team_id = ?", (team_id,))
            elif self._db.execute("SELECT 1 FROM teams WHERE team_id = ? AND leader = ? COLLATE NOCASE",
                                  (team_id, player)).fetchone():
                self._db.execute("UPDATE teams SET leader = ? WHERE team_id = ?", (left["player"], team_id))
        elif kind == "promote" and other:
            self._db.execute("UPDATE teams SET leader = ? WHERE team_id = ?", (other, team_id))
        self._db.commit()

    def team_members(self, team_id: int) -> list[str]:
        return [r["player"] for r in self._db.execute("SELECT player FROM team_members WHERE team_id = ?",
                                                      (team_id,)).fetchall()]

    def team_of(self, player: str) -> tuple[sqlite3.Row, list[str]] | None:
        row = self._db.execute("SELECT t.* FROM team_members m JOIN teams t ON t.team_id = m.team_id "
                               "WHERE m.player = ?", (player,)).fetchone()
        if row is None:
            return None
        members = [r["player"] for r in self._db.execute(
            "SELECT player FROM team_members WHERE team_id = ? ORDER BY player", (row["team_id"],))]
        return row, members

    def replace_teams(self, teams: dict[int, dict]) -> None:
        """All teams from a server team list (relationshipmanager.teaminfoall) – replaces what we had."""
        with self._db:
            self._db.execute("DELETE FROM team_members")
            self._db.execute("DELETE FROM teams")
            for team_id, t in teams.items():
                self._db.execute("INSERT INTO teams (team_id, leader) VALUES (?, ?)", (team_id, t["leader"]))
                self._db.executemany("INSERT OR REPLACE INTO team_members (player, team_id) VALUES (?, ?)",
                                     [(m, team_id) for m in t["members"]])

    def set_team(self, team_id: int, leader: str | None, members: list[str]) -> None:
        """One team from a server team list – its members leave any team we had them in."""
        with self._db:
            self._db.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
            self._db.execute("INSERT OR REPLACE INTO teams (team_id, leader) VALUES (?, ?)", (team_id, leader))
            self._db.executemany("INSERT OR REPLACE INTO team_members (player, team_id) VALUES (?, ?)",
                                 [(m, team_id) for m in members])

    def team_by_id(self, team_id: int) -> tuple[sqlite3.Row, list[str]] | None:
        """(team row with its leader, member names) or None if the team is gone."""
        row = self._db.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        if row is None:
            return None
        members = [r["player"] for r in self._db.execute(
            "SELECT player FROM team_members WHERE team_id = ? ORDER BY player", (team_id,))]
        return row, members

    TEAM_ORDER = {"kills": "kills DESC", "kd": "CAST(kills AS REAL) / MAX(deaths, 1) DESC, kills DESC",
                  "playtime": "playtime DESC", "members": "members DESC, kills DESC"}

    def team_leaderboard(self, category: str = "kills", limit: int = 10) -> list[sqlite3.Row]:
        order = self.TEAM_ORDER[category]
        return self._db.execute(
            "SELECT t.team_id, t.leader, COUNT(m.player) AS members, COALESCE(SUM(p.kills), 0) AS kills, "
            "COALESCE(SUM(p.deaths), 0) AS deaths, COALESCE(SUM(p.playtime_seconds), 0) AS playtime, "
            "GROUP_CONCAT(m.player, ', ') AS names "
            "FROM teams t JOIN team_members m ON m.team_id = t.team_id LEFT JOIN players p ON p.name = m.player "
            f"GROUP BY t.team_id HAVING members > 1 ORDER BY {order} LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------ Population

    def add_population_sample(self, ts: int, players: int, max_players: int | None, queued: int) -> None:
        self._db.execute("INSERT OR REPLACE INTO population (ts, players, max_players, queued) VALUES (?, ?, ?, ?)",
                         (ts, players, max_players, queued))
        self._db.execute("DELETE FROM population WHERE ts < ?", (ts - 40 * 86400,))
        self._db.commit()

    def population(self, since: int, bucket: int = 0) -> list[tuple[int, float, int]]:
        """(ts, players, max_players) since `since`; bucket > 0 = averages per bucket seconds."""
        if bucket:
            rows = self._db.execute(
                "SELECT (ts / ?) * ? AS t, AVG(players) AS p, MAX(max_players) AS m FROM population WHERE ts >= ? "
                "GROUP BY ts / ? ORDER BY t", (bucket, bucket, since, bucket)).fetchall()
        else:
            rows = self._db.execute("SELECT ts AS t, players AS p, max_players AS m FROM population WHERE ts >= ? "
                                    "ORDER BY ts", (since,)).fetchall()
        return [(int(r["t"]), round(float(r["p"]), 1), int(r["m"] or 0)) for r in rows]

    # ------------------------------------------------------------ Kit purchase requests

    def add_kit_request(self, ticket_id: int | None, user_id: int, kit: str, days: int, proof: str,
                        items: str | None = None, *, source: str = "purchase", price: float | None = None,
                        currency: str | None = None, cooldown_minutes: int | None = None,
                        checkout_name: str | None = None, addon: bool = False) -> int:
        cur = self._db.execute(
            "INSERT INTO kit_requests (ticket_id, user_id, kit, days, proof, items, source, price, currency, "
            "cooldown_minutes, checkout_name, addon) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, user_id, kit, days, proof, items, source, price, currency, cooldown_minutes, checkout_name,
             int(addon)))
        self._db.commit()
        return int(cur.lastrowid)

    def kit_buyer(self, kit: str) -> int | None:
        """Discord ID of the member who originally bought this custom kit (approved new-kit request), else None.
        Kits made by hand have no buyer."""
        row = self._db.execute("SELECT user_id FROM kit_requests WHERE kit = ? COLLATE NOCASE AND status = 'approved' "
                               "AND items IS NOT NULL AND addon = 0 ORDER BY id LIMIT 1", (kit,)).fetchone()
        if row:
            return int(row["user_id"])
        kit_row = self.custom_kit(kit)
        m = re.match(r"Bought by user (\d+)", (kit_row["description"] or "") if kit_row else "")
        return int(m.group(1)) if m else None

    def kits_bought_by(self, user_id: int) -> list[str]:
        rows = self._db.execute("SELECT DISTINCT kit FROM kit_requests WHERE user_id = ? AND status = 'approved' "
                                "AND items IS NOT NULL AND addon = 0", (user_id,)).fetchall()
        return [r["kit"] for r in rows if self.custom_kit(r["kit"]) is not None]

    def increase_custom_kit_item(self, kit: str, shortname: str, amount: int) -> None:
        """Adds the amount to the kit's existing row for this item, or adds a new row."""
        row = self._db.execute("SELECT id FROM custom_kit_items WHERE kit = ? AND shortname = ? ORDER BY id LIMIT 1",
                               (kit, shortname)).fetchone()
        if row:
            self._db.execute("UPDATE custom_kit_items SET amount = amount + ? WHERE id = ?", (amount, row["id"]))
        else:
            self._db.execute("INSERT INTO custom_kit_items (kit, shortname, amount) VALUES (?, ?, ?)",
                             (kit, shortname, amount))
        self._db.commit()

    def add_kit_request_proof(self, request_id: int, text: str) -> None:
        self._db.execute("UPDATE kit_requests SET proof = substr(trim(coalesce(proof, '') || ' ' || ?), 1, 600) "
                         "WHERE id = ?", (text, request_id))
        self._db.commit()

    def set_kit_request_payment(self, request_id: int, payment_ids: list[str], note: str) -> None:
        self._db.execute("UPDATE kit_requests SET payment_ids = ?, pay_note = ? WHERE id = ?",
                         (",".join(payment_ids) or None, note, request_id))
        self._db.commit()

    # ------------------------------------------------------------ Store payments (Tip4Serv)

    def add_store_payment(self, row_id: str, payment_id: str, *, amount: float | None, currency: str | None,
                          discord_id: int | None, player: str | None, username: str | None, customer: str | None,
                          command: str, raw: str | None = None) -> bool:
        """False if it was stored before (Tip4Serv re-sends until the command is marked executed)."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO store_payments (id, payment_id, amount, currency, discord_id, player, username, "
            "customer, command, raw, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, payment_id, amount, currency, discord_id, player, username, customer, command, raw,
             int(time.time())))
        self._db.commit()
        return cur.rowcount > 0

    def store_payments(self, *, unused: bool = False, since: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM store_payments WHERE received_at >= ?" + (" AND used_by IS NULL" if unused else "")
        return self._db.execute(sql + " ORDER BY received_at DESC LIMIT ?", (since, limit)).fetchall()

    # ------------------------------------------------------------ AI chat moderation

    def add_ai_warning(self, guild_id: int, user_id: int, channel_id: int, message_id: int, severity: str,
                       reason: str, content: str) -> None:
        self._db.execute("INSERT INTO ai_warnings (guild_id, user_id, channel_id, message_id, severity, reason, "
                         "content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (guild_id, user_id, channel_id, message_id, severity, reason, content, int(time.time())))
        self._db.commit()

    def count_ai_warnings(self, guild_id: int, user_id: int, *, since: int = 0) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM ai_warnings WHERE guild_id = ? AND user_id = ? "
                                    "AND created_at >= ?", (guild_id, user_id, since)).fetchone()[0])

    def ai_warnings(self, guild_id: int, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM ai_warnings WHERE guild_id = ? AND user_id = ? "
                                "ORDER BY created_at DESC, id DESC LIMIT ?", (guild_id, user_id, limit)).fetchall()

    def clear_ai_warnings(self, guild_id: int, user_id: int) -> int:
        cur = self._db.execute("DELETE FROM ai_warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self._db.commit()
        return cur.rowcount

    def store_payment_known(self, payment_id: str, *, source: str | None = None) -> bool:
        """source "webhook": only payments that came via the webhook."""
        sql = "SELECT 1 FROM store_payments WHERE payment_id = ?" + (" AND id LIKE '%:webhook'" if source else "")
        return self._db.execute(sql + " LIMIT 1", (payment_id,)).fetchone() is not None

    def void_store_payment(self, payment_id: str) -> list[sqlite3.Row]:
        """Refunded: unused rows can't confirm anything any more (used_by = 0). Returns rows that were already used."""
        used = self._db.execute("SELECT * FROM store_payments WHERE payment_id = ? AND used_by > 0",
                                (payment_id,)).fetchall()
        self._db.execute("UPDATE store_payments SET used_by = 0, used_at = ? WHERE payment_id = ? AND used_by IS NULL",
                         (int(time.time()), payment_id))
        self._db.commit()
        return used

    def last_webhook_payment(self) -> int | None:
        """Last payment that arrived live (Tip4Serv webhook or the Tip4Serv bot's Discord announcement)."""
        row = self._db.execute("SELECT MAX(received_at) FROM store_payments "
                               "WHERE id LIKE '%:webhook' OR id LIKE '%:discord'").fetchone()
        return row[0] if row and row[0] else None

    def similar_store_payment(self, discord_id: int | None, amount: float | None, at: int,
                              window: int = 900) -> sqlite3.Row | None:
        """The same purchase from another source (webhook / announcement / server key): same buyer and amount
        within 15 minutes."""
        if not discord_id or amount is None:
            return None
        return self._db.execute("SELECT * FROM store_payments WHERE discord_id = ? AND abs(amount - ?) < 0.01 "
                                "AND abs(received_at - ?) <= ? LIMIT 1", (discord_id, amount, at, window)).fetchone()

    def count_store_payments(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM store_payments").fetchone()[0])

    def find_store_payments(self, order: str) -> list[sqlite3.Row]:
        """Payments whose Tip4Serv ID or any other value Tip4Serv sent equals this order/transaction ID."""
        order = order.strip().lstrip("#").strip()
        if len(order) < 4:
            return []
        rows = self._db.execute("SELECT * FROM store_payments WHERE lower(payment_id) = lower(?) OR raw LIKE ? "
                                "ORDER BY received_at DESC LIMIT 50", (order, f"%{order}%")).fetchall()
        return [r for r in rows if r["payment_id"].lower() == order.lower() or order.lower() in raw_values(r["raw"])]

    def assign_store_payment(self, payment_id: str, discord_id: int) -> int:
        """Staff vouches that this payment belongs to a member (for payments without Discord ID)."""
        cur = self._db.execute("UPDATE store_payments SET discord_id = ? WHERE payment_id = ? AND used_by IS NULL",
                               (discord_id, payment_id))
        self._db.commit()
        return cur.rowcount

    def use_store_payments(self, row_ids: list[str], request_id: int) -> bool:
        """Marks payments as used by a request – all or nothing (each payment confirms only one purchase)."""
        try:
            with self._db:
                for rid in row_ids:
                    cur = self._db.execute("UPDATE store_payments SET used_by = ?, used_at = ? "
                                           "WHERE id = ? AND used_by IS NULL", (request_id, int(time.time()), rid))
                    if cur.rowcount != 1:
                        raise _Rollback
        except _Rollback:
            return False
        return bool(row_ids)

    def store_payments_used_elsewhere(self, row_ids: list[str], request_id: int) -> list[sqlite3.Row]:
        """Of these payments, the ones another request already used."""
        if not row_ids:
            return []
        marks = ",".join("?" * len(row_ids))
        return self._db.execute(f"SELECT * FROM store_payments WHERE id IN ({marks}) AND used_by IS NOT NULL "
                                "AND used_by != ?", (*row_ids, request_id)).fetchall()

    def release_store_payments(self, request_id: int) -> None:
        self._db.execute("UPDATE store_payments SET used_by = NULL, used_at = NULL WHERE used_by = ?", (request_id,))
        self._db.commit()

    def set_kit_request_message(self, request_id: int, channel_id: int, message_id: int) -> None:
        self._db.execute("UPDATE kit_requests SET channel_id = ?, message_id = ? WHERE id = ?",
                         (channel_id, message_id, request_id))
        self._db.commit()

    def kit_request(self, request_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM kit_requests WHERE id = ?", (request_id,)).fetchone()

    def kit_request_by_message(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM kit_requests WHERE message_id = ?", (message_id,)).fetchone()

    def kit_requests(self, *, status: str | None = None, user_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM kit_requests WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        return self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    def decide_kit_request(self, request_id: int, status: str, decided_by: str) -> bool:
        cur = self._db.execute("UPDATE kit_requests SET status = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP "
                               "WHERE id = ? AND status = 'pending'", (status, decided_by, request_id))
        self._db.commit()
        return cur.rowcount > 0

    def cancel_kit_request(self, request_id: int, by: str) -> bool:
        cur = self._db.execute("UPDATE kit_requests SET status = 'cancelled', decided_by = ?, "
                               "decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'", (by, request_id))
        self._db.commit()
        return cur.rowcount > 0

    def reopen_kit_request(self, request_id: int) -> None:
        """Approval failed (e.g. missing permission) – back to pending so staff can try again."""
        self._db.execute("UPDATE kit_requests SET status = 'pending', decided_by = NULL, decided_at = NULL "
                         "WHERE id = ?", (request_id,))
        self._db.commit()

    def set_kit_request_role(self, request_id: int, role_id: int) -> None:
        self._db.execute("UPDATE kit_requests SET role_id = ? WHERE id = ?", (role_id, request_id))
        self._db.commit()

    # ------------------------------------------------------------ Admin abuse cases

    def add_abuse_case(self, player: str, what: str, done: str) -> int:
        cur = self._db.execute("INSERT INTO abuse_cases (player, what, done) VALUES (?, ?, ?)", (player, what, done))
        self._db.commit()
        return int(cur.lastrowid)

    def set_abuse_message(self, case_id: int, channel_id: int, message_id: int) -> None:
        self._db.execute("UPDATE abuse_cases SET channel_id = ?, message_id = ? WHERE id = ?",
                         (channel_id, message_id, case_id))
        self._db.commit()

    def abuse_case(self, case_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM abuse_cases WHERE id = ?", (case_id,)).fetchone()

    def abuse_case_by_message(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM abuse_cases WHERE message_id = ?", (message_id,)).fetchone()

    def abuse_cases(self, *, status: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM abuse_cases", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        return self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    def decide_abuse_case(self, case_id: int, status: str, decided_by: str) -> bool:
        """pending -> approved/rejected (only once – two staff clicking at the same time can't both run it)."""
        cur = self._db.execute("UPDATE abuse_cases SET status = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP "
                               "WHERE id = ? AND status = 'pending'", (status, decided_by, case_id))
        self._db.commit()
        return cur.rowcount > 0

    def set_abuse_result(self, case_id: int, result: str) -> None:
        self._db.execute("UPDATE abuse_cases SET result = ? WHERE id = ?", (result, case_id))
        self._db.commit()

    # ------------------------------------------------------------ Timed roles

    def timed_role(self, guild_id: int, user_id: int, role_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM timed_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                                (guild_id, user_id, role_id)).fetchone()

    def set_timed_role(self, guild_id: int, user_id: int, role_id: int, expires_at: int, reason: str | None,
                       granted_by: str | None) -> None:
        self._db.execute(
            "INSERT INTO timed_roles (guild_id, user_id, role_id, expires_at, reason, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id, role_id) DO UPDATE SET "
            "expires_at = excluded.expires_at, reason = excluded.reason, granted_by = excluded.granted_by",
            (guild_id, user_id, role_id, expires_at, reason, granted_by))
        self._db.commit()

    def timed_roles(self, *, user_id: int | None = None) -> list[sqlite3.Row]:
        if user_id is None:
            return self._db.execute("SELECT * FROM timed_roles ORDER BY expires_at").fetchall()
        return self._db.execute("SELECT * FROM timed_roles WHERE user_id = ? ORDER BY expires_at",
                                (user_id,)).fetchall()

    def timed_role_by_id(self, row_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM timed_roles WHERE id = ?", (row_id,)).fetchone()

    def due_timed_roles(self, now: int) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM timed_roles WHERE expires_at <= ? ORDER BY expires_at",
                                (now,)).fetchall()

    def delete_timed_role(self, row_id: int) -> bool:
        cur = self._db.execute("DELETE FROM timed_roles WHERE id = ?", (row_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ Web store

    _STORE_KIT_FIELDS = ("name", "kit", "price", "days", "description", "enabled", "items", "category", "servers")

    def store_kits(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        """enabled_only: what is on sale (switched on and has a price)."""
        sql = "SELECT * FROM store_kits" + (" WHERE enabled = 1 AND price > 0" if enabled_only else "") + \
            " ORDER BY price, id"
        return self._db.execute(sql).fetchall()

    def store_kit(self, kit_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM store_kits WHERE id = ?", (kit_id,)).fetchone()

    def add_store_kit(self, name: str, kit: str, price: float, days: int, description: str | None, *,
                      source: str = "custom", auto: bool = False, enabled: bool = True,
                      category: str | None = None) -> int:
        cur = self._db.execute("INSERT INTO store_kits (name, kit, source, auto, price, days, description, enabled, "
                               "created_at, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (name, kit, source, int(auto), price, days, description, int(enabled),
                                int(time.time()), category))
        self._db.commit()
        return int(cur.lastrowid)

    def update_store_kit(self, kit_id: int, **fields) -> bool:
        fields = {k: v for k, v in fields.items() if k in self._STORE_KIT_FIELDS}
        if not fields:
            return False
        cur = self._db.execute(f"UPDATE store_kits SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                               (*fields.values(), kit_id))
        self._db.commit()
        return cur.rowcount > 0

    def delete_store_kit(self, kit_id: int) -> bool:
        cur = self._db.execute("DELETE FROM store_kits WHERE id = ?", (kit_id,))
        self._db.commit()
        return cur.rowcount > 0

    def add_store_order(self, **fields) -> int:
        fields.setdefault("created_at", int(time.time()))
        cur = self._db.execute(f"INSERT INTO store_orders ({', '.join(fields)}) VALUES "
                               f"({', '.join('?' for _ in fields)})", tuple(fields.values()))
        self._db.commit()
        return int(cur.lastrowid)

    def store_order(self, order_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM store_orders WHERE id = ?", (order_id,)).fetchone()

    def store_order_by(self, column: str, value) -> sqlite3.Row | None:
        if column not in ("token", "paypal_order"):
            raise ValueError(column)
        return self._db.execute(f"SELECT * FROM store_orders WHERE {column} = ?", (value,)).fetchone()

    def store_orders(self, *, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if status:
            return self._db.execute("SELECT * FROM store_orders WHERE status = ? ORDER BY id DESC LIMIT ?",
                                    (status, limit)).fetchall()
        return self._db.execute("SELECT * FROM store_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def update_store_order(self, order_id: int, **fields) -> None:
        self._db.execute(f"UPDATE store_orders SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), order_id))
        self._db.commit()

    def move_store_order(self, order_id: int, old: str | tuple, new: str, **fields) -> bool:
        """Changes the status only if it still is `old` (one of `old`) – so an order is paid/delivered only once."""
        olds = (old,) if isinstance(old, str) else tuple(old)
        sets = {"status": new, **fields}
        cur = self._db.execute(
            f"UPDATE store_orders SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ? AND status IN "
            f"({', '.join('?' for _ in olds)})", (*sets.values(), order_id, *olds))
        self._db.commit()
        return cur.rowcount > 0

    def add_gift_card(self, code: str, amount: float, currency: str, created_by: str, *, note: str | None = None,
                      expires_at: int | None = None, order_id: int | None = None) -> None:
        self._db.execute("INSERT INTO gift_cards (code, initial, balance, currency, note, created_by, created_at, "
                         "expires_at, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (code, amount, amount, currency, note, created_by, int(time.time()), expires_at, order_id))
        self._db.commit()

    def gift_card(self, code: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM gift_cards WHERE code = ?", (code,)).fetchone()

    def gift_cards(self, limit: int = 300) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM gift_cards ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def take_gift_balance(self, code: str, amount: float, now: int) -> bool:
        """Takes `amount` from a usable gift card (atomic). False if the balance is too low, disabled or expired."""
        cur = self._db.execute(
            "UPDATE gift_cards SET balance = ROUND(balance - ?, 2) WHERE code = ? AND disabled = 0 AND "
            "balance >= ? - 0.0001 AND (expires_at IS NULL OR expires_at > ?)", (amount, code, amount, now))
        self._db.commit()
        return cur.rowcount > 0

    def return_gift_balance(self, code: str, amount: float) -> None:
        self._db.execute("UPDATE gift_cards SET balance = ROUND(MIN(balance + ?, initial), 2) WHERE code = ?",
                         (amount, code))
        self._db.commit()

    def merge_gift_cards(self, target: str, sources: list[str], now: int) -> tuple[float, float, int | None]:
        """Moves the whole balance of the `sources` onto `target` – all or nothing. The combined card gets the
        earliest expiry date of the cards involved (no money lives longer than it would have). Emptied cards stay
        (balance 0, noted as merged) so an amount held for one of their unpaid orders can still come back to them.
        Returns (moved, new balance, expires_at); ValueError if a card changed meanwhile."""
        rows = {code: self.gift_card(code) for code in (target, *sources)}
        moved = round(sum(float(rows[c]["balance"]) for c in sources), 2)
        expiries = [r["expires_at"] for r in rows.values() if r["expires_at"]]
        expires_at = min(expiries) if expiries else None
        try:
            for code in sources:
                cur = self._db.execute(
                    "UPDATE gift_cards SET balance = 0, note = TRIM(COALESCE(note, '') || ' · merged into ' || ?) "
                    "WHERE code = ? AND balance = ? AND disabled = 0 AND (expires_at IS NULL OR expires_at > ?)",
                    (target[:4] + "…", code, rows[code]["balance"], now))
                if cur.rowcount != 1:
                    raise ValueError("A gift card changed while combining – please try again.")
            cur = self._db.execute(
                "UPDATE gift_cards SET balance = ROUND(balance + ?, 2), initial = ROUND(initial + ?, 2), "
                "expires_at = ? WHERE code = ? AND balance = ? AND disabled = 0 AND (expires_at IS NULL OR expires_at > ?)",
                (moved, moved, expires_at, target, rows[target]["balance"], now))
            if cur.rowcount != 1:
                raise ValueError("The gift card changed while combining – please try again.")
        except BaseException:
            self._db.rollback()
            raise
        self._db.commit()
        return moved, round(float(rows[target]["balance"]) + moved, 2), expires_at

    def set_gift_card_disabled(self, code: str, disabled: bool) -> bool:
        cur = self._db.execute("UPDATE gift_cards SET disabled = ? WHERE code = ?", (int(disabled), code))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ Plugins

    def plugin_get(self, plugin: str, key: str, default=None):
        row = self._db.execute("SELECT value FROM plugin_data WHERE plugin = ? AND key = ?", (plugin, key)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def plugin_set(self, plugin: str, key: str, value) -> None:
        self._db.execute("INSERT OR REPLACE INTO plugin_data (plugin, key, value) VALUES (?, ?, ?)",
                         (plugin, key, json.dumps(value)))
        self._db.commit()

    def plugin_delete(self, plugin: str, key: str) -> bool:
        cur = self._db.execute("DELETE FROM plugin_data WHERE plugin = ? AND key = ?", (plugin, key))
        self._db.commit()
        return cur.rowcount > 0

    def plugin_items(self, plugin: str, prefix: str = "") -> dict:
        """All keys of a plugin that start with `prefix` -> value."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out = {}
        for row in self._db.execute("SELECT key, value FROM plugin_data WHERE plugin = ? AND key LIKE ? ESCAPE '\\'",
                                    (plugin, f"{escaped}%")):
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
        return out

    def plugin_clear(self, plugin: str, prefix: str = "") -> int:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cur = self._db.execute("DELETE FROM plugin_data WHERE plugin = ? AND key LIKE ? ESCAPE '\\'",
                               (plugin, f"{escaped}%"))
        self._db.commit()
        return cur.rowcount

    def players_seen_since(self, since_utc: str) -> int:
        """Players first seen at or after a UTC time ("YYYY-MM-DD HH:MM:SS")."""
        return self._db.execute("SELECT COUNT(*) FROM players WHERE first_seen >= ?", (since_utc,)).fetchone()[0]

    # ------------------------------------------------------------ Prison (jail)

    def jail(self, guild_id: int, user_id: int, role_ids: list[int], reason: str | None, by: str,
             expires_at: int | None = None) -> None:
        self._db.execute("INSERT OR REPLACE INTO prison (guild_id, user_id, roles, reason, jailed_by, jailed_at, "
                         "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (guild_id, user_id, ",".join(str(r) for r in role_ids), reason, by, int(time.time()),
                          expires_at))
        self._db.commit()

    def prisoner(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM prison WHERE guild_id = ? AND user_id = ?",
                                (guild_id, user_id)).fetchone()

    def prisoners(self, guild_id: int) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM prison WHERE guild_id = ? ORDER BY jailed_at", (guild_id,)).fetchall()

    def expired_prisoners(self, now: int | None = None) -> list[sqlite3.Row]:
        """Timed jails whose release time has passed (across all guilds)."""
        return self._db.execute("SELECT * FROM prison WHERE expires_at IS NOT NULL AND expires_at <= ? "
                                "ORDER BY expires_at", (now if now is not None else int(time.time()),)).fetchall()

    def free(self, guild_id: int, user_id: int) -> list[int]:
        """Removes the prison record and returns the stored role IDs to restore."""
        row = self.prisoner(guild_id, user_id)
        if row is None:
            return []
        self._db.execute("DELETE FROM prison WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self._db.commit()
        return [int(r) for r in (row["roles"] or "").split(",") if r.strip().isdigit()]

    # ------------------------------------------------------------ Player logins (website)

    def add_player_session(self, token_hash: str, user_id: int, name: str, expires_at: int) -> None:
        now = int(time.time())
        self._db.execute("DELETE FROM player_sessions WHERE expires_at <= ?", (now,))
        self._db.execute("INSERT OR REPLACE INTO player_sessions (token_hash, user_id, name, created_at, expires_at) "
                         "VALUES (?, ?, ?, ?, ?)", (token_hash, user_id, name, now, expires_at))
        self._db.commit()

    def player_session(self, token_hash: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM player_sessions WHERE token_hash = ? AND expires_at > ?",
                                (token_hash, int(time.time()))).fetchone()

    def delete_player_session(self, token_hash: str) -> None:
        self._db.execute("DELETE FROM player_sessions WHERE token_hash = ?", (token_hash,))
        self._db.commit()

    # ------------------------------------------------------------ Team slots on custom kits

    def add_kit_slots(self, kit: str, owner_id: int, count: int, expires_at: int) -> list[int]:
        now = int(time.time())
        ids = [int(self._db.execute("INSERT INTO kit_slots (kit, owner_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                                    (kit, owner_id, expires_at, now)).lastrowid) for _ in range(count)]
        self._db.commit()
        return ids

    def kit_slot(self, slot_id: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM kit_slots WHERE id = ?", (slot_id,)).fetchone()

    def kit_slots(self, *, kit: str | None = None, owner_id: int | None = None) -> list[sqlite3.Row]:
        where, args = [], []
        if kit is not None:
            where.append("kit = ? COLLATE NOCASE")
            args.append(kit)
        if owner_id is not None:
            where.append("owner_id = ?")
            args.append(owner_id)
        sql = "SELECT * FROM kit_slots" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return self._db.execute(sql, args).fetchall()

    def set_kit_slot_member(self, slot_id: int, member_id: int | None, player: str | None) -> None:
        self._db.execute("UPDATE kit_slots SET member_id = ?, player = ? WHERE id = ?", (member_id, player, slot_id))
        self._db.commit()

    def extend_kit_slot(self, slot_id: int, expires_at: int) -> None:
        self._db.execute("UPDATE kit_slots SET expires_at = ? WHERE id = ?", (expires_at, slot_id))
        self._db.commit()

    def expired_kit_slots(self, now: int | None = None) -> list[sqlite3.Row]:
        """Slots whose time ran out while someone is still on them."""
        return self._db.execute("SELECT * FROM kit_slots WHERE member_id IS NOT NULL AND expires_at <= ? ORDER BY id",
                                (now if now is not None else int(time.time()),)).fetchall()

    def has_kit_purchase(self, user_id: int, kit: str) -> bool:
        """Did this member get the kit by an approved purchase/prize of their own (not a team slot)?"""
        return self._db.execute("SELECT 1 FROM kit_requests WHERE user_id = ? AND kit = ? COLLATE NOCASE AND "
                                "status = 'approved' AND addon = 0 LIMIT 1", (user_id, kit)).fetchone() is not None

    def close(self) -> None:
        if self._links is not self._db:
            self._links.close()
        self._db.close()
