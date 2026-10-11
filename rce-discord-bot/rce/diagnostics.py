"""Schritt-für-Schritt-Test der RCON-Verbindung (für rcon_test.bat und das Webinterface)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import aiohttp

URL_BREAKING_CHARS = set("/?#%&@!+ :\\\"'<>")


@dataclass
class DiagnosisResult:
    ok: bool = False
    steps: list[dict] = field(default_factory=list)  # {"level": ok|fail|warn|info, "text": ...}

    def add(self, level: str, text: str) -> None:
        self.steps.append({"level": level, "text": text})


async def _tcp_open(host: str, port: int, timeout: float = 4.0) -> str:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except (asyncio.TimeoutError, OSError):
        return "timeout"


async def diagnose_rcon(host: str, port_raw: str | int, password: str) -> DiagnosisResult:
    r = DiagnosisResult()
    host, password = (host or "").strip(), (password or "").strip()

    # 1. Einstellungen
    if not host:
        r.add("fail", "Server-IP fehlt.")
        return r
    if ":" in host or "/" in host:
        r.add("fail", f"Server-IP „{host}“ enthält „:“ oder „/“ – nur die IP eintragen, den Port separat.")
        return r
    try:
        port = int(str(port_raw).strip())
    except ValueError:
        r.add("fail", f"RCON-Port „{port_raw}“ ist keine Zahl.")
        return r
    if not password:
        r.add("fail", "RCON-Passwort fehlt (G-Portal → Einstellungen → Grundeinstellungen).")
        return r
    bad = sorted(set(password) & URL_BREAKING_CHARS)
    if bad:
        r.add("warn", f"Das Passwort enthält Sonderzeichen ({' '.join(bad)}), die die Verbindung oft verhindern. "
                      "In G-Portal ein Passwort nur aus Buchstaben und Zahlen setzen und den Server neu starten.")
    r.add("ok", f"Einstellungen: {host}:{port}, Passwort mit {len(password)} Zeichen.")

    # 2. TCP
    state = await _tcp_open(host, port)
    if state != "open":
        reason = ("Verbindung abgelehnt – dort läuft kein RCON" if state == "refused"
                  else "keine Antwort (Server aus, falscher Port oder Firewall)")
        r.add("fail", f"Port {port}: {reason}. Der RCON-Port ist NICHT der Spielport – im G-Portal-Panel "
                      "unter Status/Netzwerk bei „RCON“ nachsehen.")
        candidates = sorted({p for p in (port + 1, port + 2, port - 1, port + 10, 28016, 28017, 28082, 28083)
                             if 0 < p < 65536 and p != port})
        results = await asyncio.gather(*(_tcp_open(host, p, 3.0) for p in candidates))
        open_ports = [p for p, s in zip(candidates, results) if s == "open"]
        if open_ports:
            r.add("info", f"Offene Ports in der Nähe: {', '.join(map(str, open_ports))} – einer davon ist "
                          "wahrscheinlich RCON. Mit dem Panel vergleichen.")
        else:
            r.add("info", "Keiner der typischen Ports ist offen – läuft der Server?")
        return r
    r.add("ok", f"Port {port} ist erreichbar.")

    # 3. WebSocket + Passwort, 4. Testbefehl
    try:
        async with aiohttp.ClientSession() as session:
            try:
                ws = await asyncio.wait_for(session.ws_connect(f"ws://{host}:{port}/{password}"), 10)
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (401, 403, 404):
                    r.add("fail", f"Server lehnt ab (HTTP {exc.status}) – RCON-Passwort falsch.")
                else:
                    r.add("fail", f"WebSocket abgelehnt (HTTP {exc.status}) – ist das wirklich der RCON-Port?")
                return r
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                r.add("fail", f"Kein WebRCON auf diesem Port ({type(exc).__name__}). Vermutlich der falsche Port.")
                return r
            r.add("ok", "WebRCON-Verbindung aufgebaut.")
            await ws.send_str(json.dumps({"Identifier": 4242, "Message": "serverinfo", "Name": "RconTest"}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            data = None
            while data is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    r.add("warn", "Verbunden, aber keine Antwort auf „serverinfo“. Startet der Server gerade? "
                                  "In ein paar Minuten erneut testen.")
                    return r
                try:
                    msg = await asyncio.wait_for(ws.receive(), remaining)
                except asyncio.TimeoutError:
                    continue
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    r.add("fail", "Server hat die Verbindung sofort geschlossen – fast immer ein falsches "
                                  "RCON-Passwort (nach Änderung in G-Portal den Server neu starten!).")
                    return r
                if msg.type == aiohttp.WSMsgType.TEXT:
                    parsed = json.loads(msg.data)
                    if parsed.get("Identifier") == 4242:
                        data = parsed
            await ws.close()
    except Exception as exc:  # unerwartet: vollständig melden
        r.add("fail", f"Unerwarteter Fehler: {type(exc).__name__}: {exc}")
        return r

    text = str(data.get("Message", "")).strip()
    try:
        info = json.loads(text)
        r.add("ok", f"Server antwortet: {info.get('Hostname', '?')} – "
                    f"{info.get('Players', '?')}/{info.get('MaxPlayers', '?')} Spieler")
    except json.JSONDecodeError:
        r.add("ok", f"Server antwortet: {text[:200]}")
    r.ok = True
    return r
