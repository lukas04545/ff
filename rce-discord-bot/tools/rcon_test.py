"""RCON-Verbindungstest: prüft Schritt für Schritt, warum die Verbindung zum Server klappt oder nicht.

Start:  rcon_test.bat (Windows, Doppelklick)  oder  python tools/rcon_test.py
Liest RCON_HOST, RCON_PORT und RCON_PASSWORD aus der .env im Bot-Ordner. Gibt das Passwort nie aus.
Denselben Test gibt es im Webinterface unter Einstellungen → „RCON testen“.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from rce.diagnostics import diagnose_rcon  # noqa: E402

TAGS = {"ok": "[ OK ]", "fail": "[FEHLER]", "warn": "[WARNUNG]", "info": "[ INFO ]"}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 64)
    print(" RCON-Verbindungstest für Rust Console Edition (G-Portal)")
    print("=" * 64)
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        print(f"[FEHLER] Keine .env gefunden in {BASE_DIR}")
        return 1
    env = dotenv_values(env_file)
    result = asyncio.run(diagnose_rcon(env.get("RCON_HOST") or "", env.get("RCON_PORT") or "",
                                   env.get("RCON_PASSWORD") or ""))
    for step in result.steps:
        print(f"{TAGS[step['level']]} {step['text']}")
    print("\n✅ RCON funktioniert!" if result.ok else "\n❌ RCON funktioniert noch nicht – siehe oben.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
