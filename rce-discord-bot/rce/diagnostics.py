"""Step-by-step test of the RCON connection (for rcon_test.bat and the web interface)."""
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

    # 1. Settings
    if not host:
        r.add("fail", "Server IP is missing.")
        return r
    if ":" in host or "/" in host:
        r.add("fail", f"Server IP \"{host}\" contains \":\" or \"/\" – enter just the IP and the port separately.")
        return r
    try:
        port = int(str(port_raw).strip())
    except ValueError:
        r.add("fail", f"RCON port \"{port_raw}\" is not a number.")
        return r
    if not password:
        r.add("fail", "RCON password is missing (G-Portal → Settings → Basic settings).")
        return r
    bad = sorted(set(password) & URL_BREAKING_CHARS)
    if bad:
        r.add("warn", f"The password contains special characters ({' '.join(bad)}) that often prevent connecting. "
                      "Set a password with only letters and digits in G-Portal and restart the server.")
    r.add("ok", f"Settings: {host}:{port}, password with {len(password)} characters.")

    # 2. TCP
    state = await _tcp_open(host, port)
    if state != "open":
        reason = ("connection refused – no RCON running there" if state == "refused"
                  else "no response (server offline, wrong port or firewall)")
        r.add("fail", f"Port {port}: {reason}. The RCON port is NOT the game port – check \"RCON\" under "
                      "Status/Network in the G-Portal panel.")
        candidates = sorted({p for p in (port + 1, port + 2, port - 1, port + 10, 28016, 28017, 28082, 28083)
                             if 0 < p < 65536 and p != port})
        results = await asyncio.gather(*(_tcp_open(host, p, 3.0) for p in candidates))
        open_ports = [p for p, s in zip(candidates, results) if s == "open"]
        if open_ports:
            r.add("info", f"Open ports nearby: {', '.join(map(str, open_ports))} – one of them is "
                          "probably RCON. Compare with the panel.")
        else:
            r.add("info", "None of the usual ports are open – is the server running?")
        return r
    r.add("ok", f"Port {port} is reachable.")

    # 3. WebSocket + password, 4. test command
    try:
        async with aiohttp.ClientSession() as session:
            try:
                ws = await asyncio.wait_for(session.ws_connect(f"ws://{host}:{port}/{password}"), 10)
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (401, 403, 404):
                    r.add("fail", f"Server rejected the connection (HTTP {exc.status}) – wrong RCON password.")
                else:
                    r.add("fail", f"WebSocket rejected (HTTP {exc.status}) – is this really the RCON port?")
                return r
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                r.add("fail", f"No WebRCON on this port ({type(exc).__name__}). Probably the wrong port.")
                return r
            r.add("ok", "WebRCON connection established.")
            await ws.send_str(json.dumps({"Identifier": 4242, "Message": "serverinfo", "Name": "RconTest"}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            data = None
            while data is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    r.add("warn", "Connected, but no reply to \"serverinfo\". Is the server still starting? "
                                  "Test again in a few minutes.")
                    return r
                try:
                    msg = await asyncio.wait_for(ws.receive(), remaining)
                except asyncio.TimeoutError:
                    continue
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    r.add("fail", "Server closed the connection immediately – almost always a wrong "
                                  "RCON password (restart the server after changing it in G-Portal!).")
                    return r
                if msg.type == aiohttp.WSMsgType.TEXT:
                    parsed = json.loads(msg.data)
                    if parsed.get("Identifier") == 4242:
                        data = parsed
            await ws.close()
    except Exception as exc:  # unexpected: report in full
        r.add("fail", f"Unexpected error: {type(exc).__name__}: {exc}")
        return r

    text = str(data.get("Message", "")).strip()
    try:
        info = json.loads(text)
        r.add("ok", f"Server responds: {info.get('Hostname', '?')} – "
                    f"{info.get('Players', '?')}/{info.get('MaxPlayers', '?')} players")
    except json.JSONDecodeError:
        r.add("ok", f"Server responds: {text[:200]}")
    r.ok = True
    return r
