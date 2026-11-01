"""The owner moves servers from their own tokens onto the ONE shared bot (each fenced to its Discord server)."""
import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import cli, provisioning
from tests.test_control_plane import TOKEN, TOKEN2, cp, csrf_of, customer, paid  # noqa: F401


def _server(cp, name, token, guild):  # noqa: F811
    cid = cp.db.one("SELECT id FROM customers")["id"] if cp.db.one("SELECT id FROM customers") else customer(cp)
    paid(cp, cid)
    cp.db.run("UPDATE customers SET base_slots = 5 WHERE id = ?", (cid,))
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, name, actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], token, actor="t")
    home = cp.db.one("SELECT home_dir FROM servers WHERE id = ?", (s["id"],))["home_dir"]
    provisioning.write_env(__import__("pathlib").Path(home), {"GUILD_ID": guild})
    return s["id"]


def test_move_every_server_onto_the_shared_bot(cp, capsys, monkeypatch):  # noqa: F811
    a, b = _server(cp, "Kryvon", TOKEN, "111"), _server(cp, "Other", TOKEN2, "")
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    monkeypatch.setattr(cli, "service_user", lambda: None)
    cp.settings.shared_bot_token, cp.settings.shared_client_id = "", ""
    assert cli.main(["shared-bot"]) == 1 and "Set the shared bot first" in capsys.readouterr().out
    cp.settings.shared_bot_token, cp.settings.shared_client_id = "S" * 30 + ".x.y", "999"
    assert cli.main(["shared-bot"]) == 1                                       # b has no Discord server id yet
    out = capsys.readouterr().out
    assert "#%d Kryvon → shared bot, Discord server 111" % a in out and "Which Discord server?" in out
    assert "client_id=999" in out
    env = provisioning.environment(cp.db, cp.box, cp.settings, a)
    assert env["DISCORD_TOKEN"] == cp.settings.shared_bot_token and env["SAAS_SHARED_BOT"] == "1"
    assert env["SAAS_GUILD_ID"] == "111" and provisioning.get_secret(cp.db, cp.box, a, "discord_token") is None
    assert cp.db.one("SELECT status FROM servers WHERE id = ?", (a,))["status"] == "active"
    assert cli.main(["shared-bot", "--server", str(b), "--guild", "111"]) == 1                # one Discord, one server
    assert "already connected" in capsys.readouterr().out
    assert cli.main(["shared-bot", "--server", str(b), "--guild", "222"]) == 0
    assert cli.main(["shared-bot"]) == 0                                       # running it again changes nothing


def test_owner_panel_button(cp):  # noqa: F811
    from control_plane.web import build_owner
    from web.totp import code_at, new_secret
    from web.users import hash_password
    sid = _server(cp, "Kryvon", TOKEN, "111")
    cp.settings.shared_bot_token, cp.settings.shared_client_id = "S" * 30 + ".x.y", "999"
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
            assert "Use the shared bot" in page and "placeholder='111'" in page and "client_id=999" in page
            await c.post(f"/servers/{sid}/shared-bot", data={"csrf": csrf_of(page), "guild_id": ""})
            row = cp.db.one("SELECT bot_mode, guild_id FROM servers WHERE id = ?", (sid,))
            assert (row["bot_mode"], row["guild_id"]) == ("shared", "111")
            assert "<button>Use the shared bot</button>" not in await (await c.get("/")).text()
        finally:
            await c.close()
    asyncio.run(scenario())
