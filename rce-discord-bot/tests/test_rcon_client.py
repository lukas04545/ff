"""Testet den WebRCON-Client gegen den Mock-Server (inkl. Reconnect)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mock_rcon_server  # noqa: E402

from rce.rcon_client import RconClient, RconNotConnected  # noqa: E402


async def _start_mock(port: int = 0, password: str = "pw"):
    runner = web.AppRunner(mock_rcon_server.create_app(password), shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_commands_logs_and_reconnect():
    async def scenario():
        runner, port = await _start_mock()
        client = RconClient("127.0.0.1", port, "pw", reconnect_min=0.2, reconnect_max=0.5)
        received, states = [], []

        async def on_msg(m):
            received.append(m.text)

        async def on_state(c):
            states.append(c)

        client.add_message_listener(on_msg)
        client.add_state_listener(on_state)
        await client.start()
        assert await client.wait_until_connected(5)

        players = json.loads(await client.command("playerlist"))
        assert {p["DisplayName"] for p in players}
        info = json.loads(await client.command("serverinfo"))
        assert info["MaxPlayers"] == 50
        assert await client.command("say hallo", expect_response=False) == ""

        await asyncio.sleep(6)  # Mock sendet alle 2-5 s eine Logzeile
        assert received, "keine Live-Konsolenzeilen empfangen"

        # Server neu starten -> Client muss sich selbst neu verbinden
        await runner.cleanup()
        await asyncio.sleep(0.3)
        assert not client.connected
        with pytest.raises(RconNotConnected):
            await client.command("serverinfo")
        runner, _ = await _start_mock(port)
        assert await client.wait_until_connected(5)
        assert json.loads(await client.command("serverinfo"))["Players"] >= 0
        assert states[:3] == [True, False, True]

        await client.close()
        await runner.cleanup()

    asyncio.run(scenario())


def test_wrong_password_does_not_connect():
    async def scenario():
        runner, port = await _start_mock()
        client = RconClient("127.0.0.1", port, "falsch", reconnect_min=0.2)
        await client.start()
        assert not await client.wait_until_connected(1.5)
        await client.close()
        await runner.cleanup()

    asyncio.run(scenario())
