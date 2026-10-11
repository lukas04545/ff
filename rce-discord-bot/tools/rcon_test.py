"""RCON connection test: checks step by step why the connection to the server works or doesn't.

Run:  rcon_test.bat (Windows, double-click)  or  python tools/rcon_test.py
Reads RCON_HOST, RCON_PORT and RCON_PASSWORD from the .env in the bot folder. Never prints the password.
The same test is available in the web interface under Settings → "Test RCON".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from rce.diagnostics import diagnose_rcon  # noqa: E402

TAGS = {"ok": "[ OK ]", "fail": "[ERROR]", "warn": "[WARN]", "info": "[INFO]"}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 64)
    print(" RCON connection test for Rust Console Edition (G-Portal)")
    print("=" * 64)
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        print(f"[ERROR] No .env found in {BASE_DIR}")
        return 1
    env = dotenv_values(env_file)
    result = asyncio.run(diagnose_rcon(env.get("RCON_HOST") or "", env.get("RCON_PORT") or "",
                                   env.get("RCON_PASSWORD") or ""))
    for step in result.steps:
        print(f"{TAGS[step['level']]} {step['text']}")
    print("\n✅ RCON works!" if result.ok else "\n❌ RCON is not working yet – see above.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
