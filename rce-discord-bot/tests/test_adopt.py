"""Moving a running install (main server + its extra servers) into tenants in one go."""
import asyncio
import json
import re
import sqlite3
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import cli, provisioning
from tests.test_control_plane import TOKEN, TOKEN2, cp, csrf_of  # noqa: F401

TOKEN3 = "MTAz" + "C" * 22 + ".Kklmno." + "x" * 30


def _db(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE kits (name TEXT)")
    c.execute("INSERT INTO kits VALUES (?)", (value,))
    c.commit()
    c.close()


@pytest.fixture
def old(tmp_path):
    root = tmp_path / "old-bot"
    (root / "data").mkdir(parents=True)
    (root / ".env").write_text(f"DISCORD_TOKEN={TOKEN}\nSERVER_DISPLAY_NAME=Main EU\nWEB_PANEL_PATH=panel-mainpath12\n"
                               "RCON_HOST=1.2.3.4\nWEB_PORT=8080\n")
    _db(root / "data" / "bot.db", "starter")
    _db(root / "data" / "linked_players.db", "link")
    (root / "data" / "servers.json").write_text(json.dumps([
        {"id": "us", "name": "US", "discord": "own"},
        {"id": "eu2", "name": "EU 2", "discord": "shared"},
        {"id": "old", "name": "Old", "discord": "own", "enabled": False}]))
    for sid, token in (("us", TOKEN2), ("eu2", TOKEN), ("old", TOKEN3)):
        (root / "servers" / sid).mkdir(parents=True)
        (root / "servers" / sid / ".env").write_text(f"DISCORD_TOKEN={token}\nRCON_HOST=5.6.7.{len(sid)}\n")
        _db(root / "servers" / sid / "data" / "bot.db", f"kit-{sid}")
    return root


def _kits(home):
    c = sqlite3.connect(home / "data" / "bot.db")
    try:
        return [r[0] for r in c.execute("SELECT name FROM kits")]
    finally:
        c.close()


def test_adopt_every_server_once(cp, old):  # noqa: F811
    out = cli.adopt(cp.db, cp.box, cp.settings, old, "me@x.io")
    assert [r["name"] for r in out] == ["Main EU", "US", "EU 2", "Old"] and not any(r["skipped"] for r in out)
    by = {r["sid"]: r for r in out}
    for sid, kit in (("main", "starter"), ("us", "kit-us"), ("eu2", "kit-eu2")):
        home = cp.db.one("SELECT home_dir FROM servers WHERE id = ?", (by[sid]["id"],))["home_dir"]
        from pathlib import Path
        assert _kits(Path(home)) == [kit]
        assert (Path(home) / "data" / "linked_players.db").exists()
    main = cp.db.one("SELECT * FROM servers WHERE id = ?", (by["main"]["id"],))
    assert provisioning.panel_url(cp.settings, main).endswith("/panel-mainpath12")       # same panel path
    assert provisioning.get_secret(cp.db, cp.box, main["id"], "discord_token") == TOKEN
    status = {sid: cp.db.one("SELECT status FROM servers WHERE id = ?", (r["id"],))["status"] for sid, r in by.items()}
    assert status == {"main": "active", "us": "active", "eu2": "awaiting_token", "old": "stopped"}
    assert provisioning.get_secret(cp.db, cp.box, by["eu2"]["id"], "discord_token") is None   # main's token isn't reused
    env = provisioning.environment(cp.db, cp.box, cp.settings, main["id"])
    assert env["DISCORD_TOKEN"] == TOKEN
    again = cli.adopt(cp.db, cp.box, cp.settings, old, "me@x.io")
    assert all(r["skipped"] for r in again) and cp.db.one("SELECT COUNT(*) AS n FROM servers")["n"] == 4


def test_adopt_copies_an_encrypted_database(cp, old, monkeypatch):  # noqa: F811
    import dbcrypt
    key = "ab" * 32
    monkeypatch.setenv("DB_KEY", key)
    assert dbcrypt.ensure_installed()
    dbcrypt._convert(old / "data" / "bot.db", key, encrypt=True)
    assert dbcrypt.is_plain(old / "data" / "bot.db") is False
    out = cli.adopt(cp.db, cp.box, cp.settings, old, "me@x.io")
    from pathlib import Path
    home = Path(cp.db.one("SELECT home_dir FROM servers WHERE id = ?", (out[0]["id"],))["home_dir"])
    assert dbcrypt.is_plain(home / "data" / "bot.db") and _kits(home) == ["starter"]
    monkeypatch.setenv("DB_KEY", "cd" * 32)                                        # wrong key: stops, nothing half
    with pytest.raises(Exception):
        cli.copy_database(old / "data" / "bot.db", home / "data" / "x.db")


def test_adopt_command_waits_for_the_old_bot(cp, old, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    monkeypatch.setattr(cli, "service_user", lambda: None)
    assert cli.main(["adopt", "--email", "me@x.io", "--source", str(old), "--dry-run"]) == 0
    assert "4 server(s)" in capsys.readouterr().out
    monkeypatch.setattr(cli, "old_bot_processes", lambda source: [4242])
    assert cli.main(["adopt", "--email", "me@x.io", "--source", str(old)]) == 1
    assert "PID 4242" in capsys.readouterr().out and cp.db.one("SELECT COUNT(*) AS n FROM servers")["n"] == 0
    monkeypatch.setattr(cli, "old_bot_processes", lambda source: [])
    assert cli.main(["adopt", "--source", str(old)]) == 0                         # no e-mail needed
    text = capsys.readouterr().out
    assert "EU 2 → server" in text and "give it its own bot token" in text
    c = cp.db.one("SELECT * FROM customers WHERE email = ?", (cli.OWNER_ACCOUNT,))
    assert c["name"] == "Your own servers"
    from control_plane import websettings
    token = re.search(r"/once/([A-Za-z0-9_-]+)", text)[1]                       # the login link is printed
    assert websettings.use_login_link(cp.db, token)
    assert cli.old_bot_processes(old) == []                              # nothing runs from that folder


def test_owner_sets_the_missing_bot_token(cp, old):  # noqa: F811
    from control_plane.web import build_owner
    from web.totp import code_at, new_secret
    from web.users import hash_password
    out = {r["sid"]: r for r in cli.adopt(cp.db, cp.box, cp.settings, old, "me@x.io")}
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            await c.post("/login", data={"username": "boss", "password": "owner-password-123",
                                         "code": code_at(secret, int(time.time() // 30))})
            page = await (await c.get("/")).text()
            assert "Bot token needed!" in page
            t = csrf_of(page)
            sid = out["eu2"]["id"]
            r = await c.post(f"/servers/{sid}/token", data={"csrf": t, "token": TOKEN2})
            assert "already used by another server" in await r.text()
            await c.post(f"/servers/{sid}/token", data={"csrf": t, "token": "MTA0" + "D" * 22 + ".Lpqrst." + "w" * 30})
            assert cp.db.one("SELECT status FROM servers WHERE id = ?", (sid,))["status"] == "active"
        finally:
            await c.close()
    asyncio.run(scenario())
