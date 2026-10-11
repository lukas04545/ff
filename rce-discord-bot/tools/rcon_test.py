"""RCON-Verbindungstest: prüft Schritt für Schritt, warum die Verbindung zum Server klappt oder nicht.

Start:  rcon_test.bat (Windows, Doppelklick)  oder  python tools/rcon_test.py
Liest RCON_HOST, RCON_PORT und RCON_PASSWORD aus der .env im Bot-Ordner.
Gibt das Passwort nie aus.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parents[1]
OK, FAIL, WARN, INFO = "[ OK ]", "[FEHLER]", "[WARNUNG]", "[ INFO ]"
SPECIAL = set("/?#%&@!+ :\\\"'<>")


def say(tag: str, text: str) -> None:
    print(f"{tag} {text}")


async def tcp_open(host: str, port: int, timeout: float = 4.0) -> str:
    """'open', 'refused' oder 'timeout'."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except (asyncio.TimeoutError, OSError):
        return "timeout"


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 64)
    print(" RCON-Verbindungstest für Rust Console Edition (G-Portal)")
    print("=" * 64)

    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        say(FAIL, f"Keine .env gefunden in {BASE_DIR}")
        return 1
    env = {**dotenv_values(env_file), **{k: v for k, v in os.environ.items() if k.startswith("RCON_")}}
    host = (env.get("RCON_HOST") or "").strip()
    port_raw = (env.get("RCON_PORT") or "").strip()
    password = (env.get("RCON_PASSWORD") or "").strip()

    # ---------------------------------------------------------------- 1. Einstellungen
    print("\n1) Einstellungen in der .env")
    problems = False
    if not host:
        say(FAIL, "RCON_HOST ist leer (nur die IP, z. B. 147.93.162.77 – ohne Port).")
        problems = True
    elif ":" in host or "/" in host:
        say(FAIL, f"RCON_HOST '{host}' enthält ':' oder '/'. Nur die IP eintragen, den Port separat in RCON_PORT.")
        problems = True
    else:
        say(OK, f"RCON_HOST = {host}")
    try:
        port = int(port_raw)
        say(OK, f"RCON_PORT = {port}")
    except ValueError:
        say(FAIL, f"RCON_PORT '{port_raw}' ist keine Zahl.")
        port = 0
        problems = True
    if not password:
        say(FAIL, "RCON_PASSWORD ist leer (G-Portal → Einstellungen → Grundeinstellungen → RCON-Passwort).")
        problems = True
    else:
        say(OK, f"RCON_PASSWORD gesetzt ({len(password)} Zeichen)")
        bad = sorted(set(password) & SPECIAL)
        if bad:
            say(WARN, f"Das Passwort enthält Sonderzeichen ({' '.join(bad)}). Diese gehen in der WebRCON-URL oft "
                      "kaputt → in G-Portal ein Passwort NUR aus Buchstaben und Zahlen setzen, Server neu starten.")
    if problems:
        print("\nBitte zuerst die .env korrigieren.")
        return 1

    # ---------------------------------------------------------------- 2. Erreichbarkeit
    print(f"\n2) Ist {host}:{port} per TCP erreichbar?")
    state = await tcp_open(host, port)
    if state != "open":
        reason = ("Verbindung abgelehnt – auf diesem Port läuft kein RCON" if state == "refused"
                  else "keine Antwort (Server aus, falscher Port oder Firewall)")
        say(FAIL, f"Port {port}: {reason}.")
        print("   Der RCON-Port ist NICHT der Spielport (oft 28015). Er steht im G-Portal-Panel")
        print("   unter Netzwerk/Status bei „RCON“. Prüfe ein paar typische Ports …")
        candidates = sorted({p for p in (port + 1, port + 2, port - 1, port + 10, 28016, 28017, 28082, 28083)
                             if 0 < p < 65536 and p != port})
        results = await asyncio.gather(*(tcp_open(host, p, 3.0) for p in candidates))
        open_ports = [p for p, r in zip(candidates, results) if r == "open"]
        if open_ports:
            say(INFO, f"Offene TCP-Ports gefunden: {', '.join(map(str, open_ports))} – "
                      "einer davon ist wahrscheinlich RCON. Mit dem Wert aus dem G-Portal-Panel vergleichen.")
        else:
            say(INFO, "Keiner der typischen Ports ist offen. Läuft der Server? Port im G-Portal-Panel nachsehen.")
        return 1
    say(OK, f"Port {port} ist offen.")

    # ---------------------------------------------------------------- 3. WebSocket + Passwort
    print("\n3) WebRCON-Verbindung (ws://IP:PORT/PASSWORT)")
    url = f"ws://{host}:{port}/{password}"
    try:
        async with aiohttp.ClientSession() as session:
            try:
                ws = await asyncio.wait_for(session.ws_connect(url, autoping=True), 10)
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (401, 403, 404):
                    say(FAIL, f"Server lehnt ab (HTTP {exc.status}) – RCON-Passwort falsch.")
                else:
                    say(FAIL, f"WebSocket-Handshake abgelehnt (HTTP {exc.status}). Ist das wirklich der RCON-Port?")
                return 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                say(FAIL, f"Kein WebSocket auf diesem Port ({type(exc).__name__}: {exc}). "
                          "Vermutlich nicht der RCON-Port (oder ein Spiel-/Query-Port).")
                return 1
            say(OK, "WebSocket verbunden.")

            # ------------------------------------------------------------ 4. Befehl testen
            print("\n4) Testbefehl 'serverinfo'")
            await ws.send_str(json.dumps({"Identifier": 4242, "Message": "serverinfo", "Name": "RconTest"}))
            try:
                deadline = asyncio.get_running_loop().time() + 10
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    msg = await asyncio.wait_for(ws.receive(), remaining)
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        say(FAIL, f"Server hat die Verbindung sofort geschlossen (Code {ws.close_code}) – "
                                  "fast immer ein FALSCHES RCON-Passwort. Passwort in G-Portal prüfen/neu setzen "
                                  "und den Server danach neu starten.")
                        return 1
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("Identifier") == 4242:
                        break
            except asyncio.TimeoutError:
                say(WARN, "Verbunden, aber keine Antwort auf 'serverinfo' innerhalb von 10 s. "
                          "Server evtl. gerade beim Starten – in ein paar Minuten erneut testen.")
                return 1
            text = str(data.get("Message", "")).strip()
            try:
                info = json.loads(text)
                say(OK, f"Antwort erhalten: {info.get('Hostname', '?')} – "
                        f"{info.get('Players', '?')}/{info.get('MaxPlayers', '?')} Spieler")
            except json.JSONDecodeError:
                say(OK, f"Antwort erhalten: {text[:200]}")
            await ws.close()
    except Exception as exc:  # unerwartet: vollständig anzeigen
        say(FAIL, f"Unerwarteter Fehler: {type(exc).__name__}: {exc}")
        return 1

    print("\n✅ RCON funktioniert! Der Bot kann sich mit diesen Einstellungen verbinden.")
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)
