"""SQLite storage for statistics (leaderboards) and bot state.

Since Console Edition only provides player names (gamertag/PSN) in logs and the
player list, stats are counted per name. Name change = new entry.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

LEADERBOARD_COLUMNS = {
    "kills": "kills DESC, deaths ASC",
    "deaths": "deaths DESC",
    "kd": "CAST(kills AS REAL) / MAX(deaths, 1) DESC, kills DESC",
    "playtime": "playtime_seconds DESC",
}


DEFAULT_CLAIM_COOLDOWN = 1440  # minutes (24 h) – default cooldown for self-service custom kit claims
CLAIM_SOURCE = "claim"         # kit_claims.source prefix of self-service claims


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Small, fast writes – synchronous SQLite is plenty for a single server.
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
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
            CREATE TABLE IF NOT EXISTS custom_kit_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL COLLATE NOCASE REFERENCES custom_kits (name) ON DELETE CASCADE,
                shortname TEXT NOT NULL,
                amount INTEGER NOT NULL
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
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(custom_kits)")}
        if "claim_role_ids" not in columns:
            # Roles whose members may claim the kit themselves (comma-separated IDs, empty = admins only)
            self._db.execute("ALTER TABLE custom_kits ADD COLUMN claim_role_ids TEXT NOT NULL DEFAULT ''")
        if "cooldown_minutes" not in columns:
            self._db.execute(f"ALTER TABLE custom_kits ADD COLUMN cooldown_minutes INTEGER NOT NULL "
                             f"DEFAULT {DEFAULT_CLAIM_COOLDOWN}")
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
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_claims)")}
        if "user_id" not in columns:
            # Discord user who claimed the kit (self-service claims)
            self._db.execute("ALTER TABLE kit_claims ADD COLUMN user_id INTEGER")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_kit_claims_user ON kit_claims (user_id, kit)")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.commit()

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

    def kit_claim_history(self, player: str | None = None, limit: int = 15) -> list[sqlite3.Row]:
        if player:
            return self._db.execute(
                "SELECT * FROM kit_claims WHERE player = ? ORDER BY id DESC LIMIT ?", (player, limit)
            ).fetchall()
        return self._db.execute("SELECT * FROM kit_claims ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

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
        row = self._db.execute("SELECT player FROM linked_players WHERE user_id = ?", (user_id,)).fetchone()
        return row["player"] if row else None

    def player_owner(self, player: str) -> int | None:
        row = self._db.execute("SELECT user_id FROM linked_players WHERE player = ?", (player,)).fetchone()
        return int(row["user_id"]) if row else None

    def link_player(self, user_id: int, player: str) -> bool:
        """Links (or re-links) a Discord account to a player. False if the player belongs to someone else."""
        owner = self.player_owner(player)
        if owner is not None and owner != user_id:
            return False
        self._db.execute("INSERT OR REPLACE INTO linked_players (user_id, player) VALUES (?, ?)", (user_id, player))
        self._db.commit()
        return True

    def unlink_player(self, user_id: int) -> str | None:
        player = self.linked_player(user_id)
        self._db.execute("DELETE FROM linked_players WHERE user_id = ?", (user_id,))
        self._db.commit()
        return player

    def linked_players(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM linked_players ORDER BY player").fetchall()

    # ------------------------------------------------------------ Tickets

    _TICKET_FIELDS = {"channel_id", "ai_enabled", "escalated", "staff_joined", "ai_replies"}

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

    def team_of(self, player: str) -> tuple[sqlite3.Row, list[str]] | None:
        row = self._db.execute("SELECT t.* FROM team_members m JOIN teams t ON t.team_id = m.team_id "
                               "WHERE m.player = ?", (player,)).fetchone()
        if row is None:
            return None
        members = [r["player"] for r in self._db.execute(
            "SELECT player FROM team_members WHERE team_id = ? ORDER BY player", (row["team_id"],))]
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

    def close(self) -> None:
        self._db.close()
