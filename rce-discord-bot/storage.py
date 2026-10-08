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
            """
        )
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

    def close(self) -> None:
        self._db.close()
