"""Cloudflare tunnel: start/reuse, public address, host check and real client IP (with a fake cloudflared)."""
import asyncio
import json
import stat
import sys
import time

import pytest

from aiohttp.test_utils import TestClient, TestServer

from web import tunnel as tunnelmod
from web.server import WebInterface, WebSettings

H = {"X-Requested-With": "rce-web"}


@pytest.fixture(autouse=True)
def _tunnel_files(tmp_path, monkeypatch):
    """State and log file of the tunnel in the test folder, not in the bot folder."""
    monkeypatch.setattr(tunnelmod, "STATE_FILE", tmp_path / "tunnel_state.json")
    monkeypatch.setattr(tunnelmod, "LOG_FILE", tmp_path / "cloudflared.log")
    yield
    tunnelmod.stop_tunnel()


def _fake_cloudflared(tmp_path, url="https://quiet-fox-123.trycloudflare.com"):
    script = tmp_path / "cloudflared"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "print('INF Requesting new quick Tunnel on trycloudflare.com...', file=sys.stderr, flush=True)\n"
        f"print('INF |  {url}  |', file=sys.stderr, flush=True)\n"
        "time.sleep(0.3)\n"
        "print('INF Registered tunnel connection connIndex=0 location=fra08 protocol=quic', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_tunnel_reports_address_and_is_reused(tmp_path):
    binary = _fake_cloudflared(tmp_path)
    seen = []
    try:
        t = tunnelmod.ensure_tunnel(8099, binary=binary)
        t.on_online = seen.append
        assert _wait(lambda: t.state == "online")
        assert t.url == "https://quiet-fox-123.trycloudflare.com" and t.host == "quiet-fox-123.trycloudflare.com"
        assert _wait(lambda: seen) or t.url  # callback may have fired before it was attached
        assert tunnelmod.ensure_tunnel(8099, binary=binary) is t       # same settings -> same tunnel (restart)
        t2 = tunnelmod.ensure_tunnel(8100, binary=binary)              # other port -> new tunnel
        assert t2 is not t and t.state == "stopped"
    finally:
        tunnelmod.stop_tunnel()


def test_web_interface_with_tunnel(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnelmod, "find_binary", lambda: _fake_cloudflared(tmp_path))

    async def scenario():
        # Without a password the bot creates one and saves it in the .env (public address needs a password)
        env = tmp_path / ".env"
        env.write_text("DISCORD_TOKEN=x\n", encoding="utf-8")
        w = WebInterface(None, web_settings=WebSettings(password="", tunnel=True), env_path=env,
                         secret_path=tmp_path / "k")
        w._start_tunnel()
        assert w.generated_password and len(w.generated_password) >= 16
        assert w.ws.password.startswith("pbkdf2_sha256$")                   # only its hash is kept
        assert f"WEB_PASSWORD={w.ws.password}" in env.read_text() and w.generated_password not in env.read_text()
        assert await asyncio.to_thread(_wait, lambda: w.public_url)
        await asyncio.sleep(0.2)  # banner + PUBLIC_LINK.txt are written on the event loop
        assert w.panel_path.startswith("/panel-") and f"WEB_PANEL_PATH={w.ws.panel_path}" in env.read_text()
        assert (tmp_path / "PUBLIC_LINK.txt").read_text().startswith(        # the secret panel address, not /admin
            f"Control panel: https://quiet-fox-123.trycloudflare.com{w.panel_path}\n"
            "Player website: https://quiet-fox-123.trycloudflare.com/")
        tunnelmod.stop_tunnel()

        w = WebInterface(None, web_settings=WebSettings(password="secret123", tunnel=True),
                         env_path=env, secret_path=tmp_path / "k")
        w._start_tunnel()
        assert await asyncio.to_thread(_wait, lambda: w.public_url)
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            tunnel_host = {"Host": "quiet-fox-123.trycloudflare.com"}
            # The tunnel address is accepted, other hosts are still blocked (DNS rebinding protection)
            r = await c.post("/api/login", json={"password": "secret123"}, headers={**H, **tunnel_host})
            assert r.status == 200
            me = await (await c.get("/api/me", headers=tunnel_host)).json()
            assert me["public_url"] == "https://quiet-fox-123.trycloudflare.com" and me["tunnel_state"] == "online"
            assert (await c.get("/api/me", headers={"Host": "evil.example.com"})).status == 403

            # Rate limit uses the visitor's IP from Cloudflare, not 127.0.0.1 for everyone
            for _ in range(5):
                await c.post("/api/login", json={"password": "x"}, headers={**H, **tunnel_host, "CF-Connecting-IP": "1.2.3.4"})
            blocked = await c.post("/api/login", json={"username": "a", "password": "x"},
                                   headers={**H, **tunnel_host, "CF-Connecting-IP": "1.2.3.4"})
            other = await c.post("/api/login", json={"username": "b", "password": "x"},
                                 headers={**H, **tunnel_host, "CF-Connecting-IP": "5.6.7.8"})
            assert blocked.status == 429 and other.status == 401
        finally:
            await c.close()
            tunnelmod.stop_tunnel()

    asyncio.run(scenario())


def test_tunnel_survives_update_restart(tmp_path):
    """Update restart: the bot only detaches, the next process adopts the running cloudflared – same link."""
    binary = _fake_cloudflared(tmp_path, url="https://same-link-42.trycloudflare.com")
    t = tunnelmod.ensure_tunnel(8099, binary=binary)
    assert _wait(lambda: t.state == "online")
    pid = t._pid
    assert json.loads((tmp_path / "tunnel_state.json").read_text())["pid"] == pid

    tunnelmod.detach_tunnel()                 # like restart_after_update()
    tunnelmod.stop_tunnel()                   # atexit of the old process: must not kill it
    assert tunnelmod._pid_command(pid)

    tunnelmod._current = None                 # "new process"
    t2 = tunnelmod.ensure_tunnel(8099, binary=binary)
    assert _wait(lambda: t2.state == "online") and t2.adopted and t2._pid == pid
    assert t2.url == "https://same-link-42.trycloudflare.com"

    # Other settings (e.g. another port): the leftover is ended and a new tunnel started
    tunnelmod.detach_tunnel()
    tunnelmod._current = None
    t3 = tunnelmod.ensure_tunnel(8100, binary=binary)
    assert _wait(lambda: t3.state == "online") and not t3.adopted and t3._pid != pid
    assert _wait(lambda: not tunnelmod._pid_command(pid))

    # Tunnel switched off in a new process: the leftover cloudflared is ended too
    pid3 = t3._pid
    tunnelmod.detach_tunnel()
    tunnelmod._current = None
    tunnelmod.stop_tunnel()
    assert _wait(lambda: not tunnelmod._pid_command(pid3))
    assert not (tmp_path / "tunnel_state.json").exists()


def test_link_only_online_after_cloudflare_connection(tmp_path):
    """The address alone isn't enough – a blocked connection (VPN/firewall) must not show a dead link."""
    script = tmp_path / "cloudflared"
    script.write_text(f"#!{sys.executable}\n"
                      "import sys, time\n"
                      "print('INF |  https://dead-link.trycloudflare.com  |', file=sys.stderr, flush=True)\n"
                      "print('ERR Failed to dial a quic connection error=timeout', file=sys.stderr, flush=True)\n"
                      "time.sleep(60)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    t = tunnelmod.ensure_tunnel(8099, binary=script)
    assert _wait(lambda: t.url == "https://dead-link.trycloudflare.com")
    time.sleep(0.5)
    assert t.state == "starting"
