"""Test-Server, der einen RCE-WebRCON-Server simuliert (ohne echten Rust-Server testen).

Start:   python tools/mock_rcon_server.py --port 28016 --password test123
In .env: RCON_HOST=127.0.0.1  RCON_PORT=28016  RCON_PASSWORD=test123

Die Beispielzeilen entsprechen dem angenommenen RCE-Log-Format (siehe rce/log_parser.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from aiohttp import web

PLAYERS = ["xXSniperXx", "Bob The Builder", "PSN_Lena", "GamerTag42"]

SAMPLE_LINES = [
    "xXSniperXx was killed by Bob The Builder",
    "PSN_Lena was killed by wolf",
    "GamerTag42 was killed by autoturret_deployed (entity)",
    "Bob The Builder was killed by 1923847",
    "PSN_Lena was suicide by Suicide",
    "[CHAT LOCAL] GamerTag42 : d11_quick_chat_orders_slot_6",
    "[CHAT LOCAL] PSN_Lena : d11_quick_chat_responses_slot_5",
    "[CHAT TEAM] GamerTag42 : d11_quick_chat_i_need_phrase_format d11_Scrap",
    "Bob The Builder [SCARLETT] has entered the game",
    "[EVENT] event_cargoship",
    "[EVENT] event_airdrop",
    "[GamerTag42] created a new team, ID: 1337",
    "[PSN_Lena] has joined [GamerTag42]s team, ID: [1337]",
    "[ServerVar] SERVER giving PSN_Lena kit starter",
    "[ SAVE ] Saved 48211 ents",
]


async def handler(request: web.Request) -> web.WebSocketResponse:
    if request.match_info["password"] != request.app["password"]:
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app["sockets"].add(ws)
    online = set(PLAYERS[:3])

    async def send(message: str, identifier: int = 0) -> None:
        await ws.send_str(json.dumps({"Message": message, "Identifier": identifier, "Type": "Generic"}))

    async def spam() -> None:
        while not ws.closed:
            await asyncio.sleep(random.uniform(2, 5))
            # Spieler kommen und gehen, damit Join/Leave sichtbar wird
            if random.random() < 0.3:
                online.symmetric_difference_update({random.choice(PLAYERS)})
            await send(random.choice(SAMPLE_LINES))

    task = asyncio.create_task(spam())
    try:
        async for msg in ws:
            data = json.loads(msg.data)
            cmd, ident = data.get("Message", ""), data.get("Identifier", 0)
            print(f"<- {cmd}")
            if cmd == "playerlist":
                await send(json.dumps([
                    {"DisplayName": n, "Ping": random.randint(20, 90), "ConnectedSeconds": random.randint(60, 9000),
                     "Health": 100.0} for n in sorted(online)
                ], indent=2), ident)
            elif cmd == "serverinfo":
                await send(json.dumps({
                    "Hostname": "<color=orange>Mock</color> RCE Server", "MaxPlayers": 50,
                    "Players": len(online), "Queued": 0, "Joining": 0, "EntityCount": 48211,
                    "GameTime": "10/08/2026 13:37:00", "Uptime": 7265, "Map": "Procedural Map",
                    "Framerate": 30, "Memory": 4096, "Restarting": False,
                }, indent=2), ident)
            elif cmd == "kit list":
                await send("[KITMANAGER] Active kits:\nstarter\nvip\nraid", ident)
            elif cmd.startswith("kit info"):
                await send("[KITMANAGER] Kit info:\n"
                           "Shortname: stone.pickaxe Amount: [1] Condition: [100] Container: [Belt]\n"
                           "Shortname: bandage Amount: [5] Condition: [100] Container: [Main]\n"
                           "Shortname: hoodie Amount: [1] Condition: [80] Container: [Wear]", ident)
            elif cmd == "getauthlevels":
                await send("Admin\nGamerTag42\nVIP\nPSN_Lena", ident)
            elif cmd.startswith("kit givetoplayer"):
                await send("", ident)
                kit, player = cmd.split('"')[1], cmd.split('"')[3]
                await send(f"[ServerVar] SERVER giving {player} kit {kit}")
            elif cmd.startswith("inventory.giveto"):
                await send("", ident)
                player, item = cmd.split('"')[1], cmd.split('"')[3]
                amount = cmd.split('"')[4].strip()
                await send(f"giving {player} {amount} x {item}")
            elif cmd.startswith("say "):
                await send("", ident)
                await send(f"[CHAT SERVER] SERVER : {cmd[4:]}")
            else:
                await send(f"Executing console system command '{cmd}'")
                await send(f"(Mock) Befehl ausgeführt: {cmd}", ident)
    finally:
        task.cancel()
        request.app["sockets"].discard(ws)
    return ws


async def _close_sockets(app: web.Application) -> None:
    for ws in list(app["sockets"]):
        await ws.close()


def create_app(password: str) -> web.Application:
    app = web.Application()
    app["password"] = password
    app["sockets"] = set()
    app.router.add_get("/{password}", handler)
    app.on_shutdown.append(_close_sockets)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28016)
    parser.add_argument("--password", default="test123")
    args = parser.parse_args()
    web.run_app(create_app(args.password), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
