"""Tests the WebRCON client against the mock server (incl. reconnect)."""
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
        assert await client.command("say hello", expect_response=False) == ""

        await asyncio.sleep(6)  # mock sends a log line every 2-5 s
        assert received, "no live console lines received"

        # Restart server -> client must reconnect on its own
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
        client = RconClient("127.0.0.1", port, "wrong", reconnect_min=0.2)
        await client.start()
        assert not await client.wait_until_connected(1.5)
        await client.close()
        await runner.cleanup()

    asyncio.run(scenario())


def test_reconfigure_reconnects_without_restart():
    """New IP/port/password from the settings: the client reconnects right away (no bot restart, no long backoff)."""
    async def scenario():
        runner, port = await _start_mock()
        runner2, port2 = await _start_mock(password="pw2")
        client = RconClient("127.0.0.1", port, "wrong", reconnect_min=30, reconnect_max=60)
        await client.start()
        assert not await client.wait_until_connected(1.0)          # wrong password, next try only in 30 s
        assert await client.reconfigure("127.0.0.1", port, "pw")
        assert await client.wait_until_connected(3)
        assert not await client.reconfigure("127.0.0.1", port, "pw")  # unchanged -> nothing happens
        # Switch to another server while connected
        assert await client.reconfigure("127.0.0.1", port2, "pw2")
        await asyncio.sleep(0.2)
        assert await client.wait_until_connected(3) and client.port == port2
        assert json.loads(await client.command("serverinfo"))["MaxPlayers"] == 50
        await client.close()
        await runner.cleanup()
        await runner2.cleanup()

    asyncio.run(scenario())


async def _quiet_server(answer_after: int):
    """A WebRCON server that ignores the first `answer_after` commands (slow), then answers."""
    seen = {"n": 0}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            seen["n"] += 1
            if seen["n"] > answer_after:
                ident = json.loads(msg.data)["Identifier"]
                await ws.send_str(json.dumps({"Identifier": ident, "Message": "ok", "Type": "Generic"}))
        return ws
    app = web.Application()
    app.router.add_get("/pw", handler)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_slow_server_is_probed_before_the_connection_is_dropped(monkeypatch):
    from rce import rcon_client
    from rce.rcon_client import RconTimeout
    monkeypatch.setattr(rcon_client, "PROBE_TIMEOUT", 1.0)

    async def scenario(answer_after, expect_drop):
        runner, port = await _quiet_server(answer_after)
        client = RconClient("127.0.0.1", port, "pw", command_timeout=0.2, reconnect_min=5)
        states = []

        async def on_state(c):
            states.append(c)
        client.add_state_listener(on_state)
        await client.start()
        assert await client.wait_until_connected(5)
        for _ in range(3):
            with pytest.raises(RconTimeout):
                await client.command("playerlist")
        await asyncio.sleep(1.5)                                   # the probe runs
        try:
            if expect_drop:
                assert states == [True, False] and "stopped answering" in client.last_drop
            else:
                assert states == [True] and client.connected        # slow, not dead: kept
                assert await client.command("serverinfo") == "ok"
        finally:
            await client.close()
            await runner.cleanup()
    asyncio.run(scenario(answer_after=3, expect_drop=False))      # the probe (4th) is answered
    asyncio.run(scenario(answer_after=99, expect_drop=True))      # never answers: reconnect, with the reason


def test_admin_log_shows_hiccups_once_with_the_reason(monkeypatch):
    from types import SimpleNamespace as NS

    from cogs.feeds import Feeds
    lines = []
    rcon = NS(connected=False, last_drop="")
    feeds = Feeds.__new__(Feeds)
    log_feed = NS(add_line=lines.append)
    feeds.bot = NS(rcon=rcon, config=NS(admin_log_channel_id=1), feed=lambda cid: log_feed)
    monkeypatch.setattr(Feeds, "HICCUP_SECONDS", 0.2)

    async def scenario():
        rcon.connected = True
        await feeds.on_rce_connection(True)                         # first connection after a start
        rcon.connected, rcon.last_drop = False, "the server stopped answering (3 commands without a reply)"
        await feeds.on_rce_connection(False)
        await asyncio.sleep(0.05)
        rcon.connected = True
        await feeds.on_rce_connection(True)                         # back quickly: one 🟡 line
        await asyncio.sleep(0.3)
        rcon.connected, rcon.last_drop = False, "the game server closed the connection (code 1006)"
        await feeds.on_rce_connection(False)
        await asyncio.sleep(0.3)                                    # still down: the 🔴 line
        rcon.connected = True
        await feeds.on_rce_connection(True)
    asyncio.run(scenario())
    assert lines[0] == "🟢 RCON connection established"
    assert lines[1].startswith("🟡 RCON hiccup – back after") and "stopped answering" in lines[1]
    assert lines[2].startswith("🔴 RCON connection lost") and "code 1006" in lines[2]
    assert lines[3] == "🟢 RCON connection established" and len(lines) == 4
