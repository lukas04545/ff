"""SQLite-Speicher für Statistiken (Leaderboards) und Bot-Zustand.

Da die Console Edition in Logs und Spielerliste nur Spielernamen (Gamertag/PSN)
liefert, werden Statistiken pro Name gezählt. Namensänderungen = neuer Eintrag.
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


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Kleine, schnelle Schreibzugriffe – für einen einzelnen Server reicht synchrones SQLite.
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

            -- Kitmanager: Autokit-Regeln und Vergabe-Verlauf
            CREATE TABLE IF NOT EXISTS kit_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL,
                trigger TEXT NOT NULL,              -- 'respawn' oder 'quickchat'
                phrase TEXT,                        -- Quick-Chat-Bezeichner (nur bei 'quickchat')
                cooldown_minutes INTEGER NOT NULL DEFAULT 0,
                max_claims INTEGER NOT NULL DEFAULT 0,  -- 0 = unbegrenzt (pro Spieler)
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kit_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,                    -- NULL = manuell per Discord vergeben
                kit TEXT NOT NULL,
                player TEXT NOT NULL COLLATE NOCASE,
                source TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_kit_claims_rule_player ON kit_claims (rule_id, player);

            -- Custom Kits: nur im Bot gespeichert, im Ingame-Kitmanager unsichtbar
            CREATE TABLE IF NOT EXISTS custom_kits (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Support-Tickets
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER UNIQUE,
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                ingame_name TEXT,
                status TEXT NOT NULL DEFAULT 'open',      -- 'open' / 'closed'
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                escalated INTEGER NOT NULL DEFAULT 0,     -- Staff wurde gepingt
                staff_joined INTEGER NOT NULL DEFAULT 0,  -- Staff hat geschrieben -> KI pausiert
                ai_replies INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                closed_by TEXT,
                close_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS custom_kit_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit TEXT NOT NULL COLLATE NOCASE REFERENCES custom_kits (name) ON DELETE CASCADE,
                shortname TEXT NOT NULL,
                amount INTEGER NOT NULL
            );
            """
        )
        # Migration: ältere Datenbanken kennen die Spalte kit_type noch nicht
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(kit_rules)")}
        if "kit_type" not in columns:
            self._db.execute("ALTER TABLE kit_rules ADD COLUMN kit_type TEXT NOT NULL DEFAULT 'ingame'")
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

    # ------------------------------------------------------------ Kitmanager

    def add_kit_rule(self, kit: str, trigger: str, phrase: str | None, cooldown_minutes: int,
                     max_claims: int, created_by: str, kit_type: str = "ingame") -> int:
        cur = self._db.execute(
            "INSERT INTO kit_rules (kit, kit_type, trigger, phrase, cooldown_minutes, max_claims, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kit, kit_type, trigger, phrase, cooldown_minutes, max_claims, created_by),
        )
        self._db.commit()
        return int(cur.lastrowid)

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
        """(Anzahl Vergaben, Minuten seit der letzten Vergabe oder None)."""
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
        """Löscht den Autokit-Verlauf (z. B. nach einem Wipe); manuelle Vergaben bleiben."""
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
        """Löscht Kit, Items und zugehörige Autokit-Regeln. Gibt die Zahl gelöschter Regeln zurück."""
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

    def remove_custom_kit_item(self, kit: str, item_id: int) -> bool:
        cur = self._db.execute("DELETE FROM custom_kit_items WHERE id = ? AND kit = ?", (item_id, kit))
        self._db.commit()
        return cur.rowcount > 0

    def custom_kit_items(self, kit: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM custom_kit_items WHERE kit = ? ORDER BY id", (kit,)
        ).fetchall()

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
            raise ValueError(f"Unbekannte Ticket-Felder: {unknown}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", (*fields.values(), ticket_id))
        self._db.commit()

    def increment_ticket_ai_replies(self, ticket_id: int) -> int:
        self._db.execute("UPDATE tickets SET ai_replies = ai_replies + 1 WHERE id = ?", (ticket_id,))
        self._db.commit()
        return int(self.ticket(ticket_id)["ai_replies"])

    def close_ticket(self, ticket_id: int, closed_by: str, reason: str | None) -> None:
        self._db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP, closed_by = ?, close_reason = ? "
            "WHERE id = ?",
            (closed_by, reason, ticket_id),
        )
        self._db.commit()

    def delete_ticket(self, ticket_id: int) -> None:
        self._db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
