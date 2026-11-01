"""Settings on the website and the one-time login link from the console."""
import asyncio
import os
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import websettings
from tests.test_control_plane import cp, csrf_of  # noqa: F401


@pytest.fixture(autouse=True)
def own_env(monkeypatch):
    env = dict(os.environ)
    for k in websettings.BY_KEY:
        env.pop(k, None)
    monkeypatch.setattr(os, "environ", env)


def test_save_keeps_secrets_validates_and_audits(cp):  # noqa: F811
    changed = websettings.save(cp.db, cp.box, {"CP_BASE_DOMAIN": "https://RceBot.net/", "CP_DEEPSEEK_API_KEY": "sk-123456",
                                               "CP_HQ_ADMIN_IDS": "1, 2"}, actor="owner:boss")
    assert changed == ["CP_BASE_DOMAIN", "CP_DEEPSEEK_API_KEY", "CP_HQ_ADMIN_IDS"]
    assert os.environ["CP_BASE_DOMAIN"] == "rcebot.net" and os.environ["CP_HQ_ADMIN_IDS"] == "1,2"
    assert websettings.shown("CP_DEEPSEEK_API_KEY") == "set ••••3456"
    assert websettings.save(cp.db, cp.box, {"CP_DEEPSEEK_API_KEY": ""}, actor="t") == []    # blank keeps the secret
    raw = cp.db.one("SELECT value FROM settings_kv WHERE key = 'web_settings'")["value"]
    assert "sk-123456" not in raw                                                          # stored encrypted
    for bad in ({"CP_BASE_DOMAIN": "not a domain"}, {"CP_HQ_GUILD_ID": "abc"}, {"CP_OWNER_PATH": "/admin"},
                {"CP_GRACE_DAYS": "x"}, {"STRIPE_PRICE_ID": "a\nb"}):
        with pytest.raises(websettings.SettingsError):
            websettings.save(cp.db, cp.box, bad, actor="t")
    websettings.save(cp.db, cp.box, {"clear_CP_DEEPSEEK_API_KEY": "1"}, actor="t")
    assert websettings.shown("CP_DEEPSEEK_API_KEY") == "not set"
    # a restart: the saved values win over the file again
    os.environ["CP_BASE_DOMAIN"] = "from-file.net"
    websettings.apply_saved(cp.db, cp.box)
    assert os.environ["CP_BASE_DOMAIN"] == "rcebot.net"
    audit = cp.db.q("SELECT detail FROM audit_log WHERE action = 'settings.change'")
    assert audit and "sk-" not in " ".join(r["detail"] for r in audit)                     # names, never values


def test_login_link_works_once_and_expires(cp):  # noqa: F811
    token = websettings.new_login_link(cp.db)
    assert websettings.use_login_link(cp.db, token, consume=False)
    assert not websettings.use_login_link(cp.db, token + "x")
    assert websettings.use_login_link(cp.db, token) and not websettings.use_login_link(cp.db, token)
    old = websettings.new_login_link(cp.db, ttl=-1)
    assert not websettings.use_login_link(cp.db, old)
    a, b = websettings.new_login_link(cp.db), websettings.new_login_link(cp.db)          # a new link replaces the old
    assert not websettings.use_login_link(cp.db, a) and websettings.use_login_link(cp.db, b)


def test_first_login_from_the_console_then_settings_on_the_website(cp):  # noqa: F811
    from control_plane.web import build_owner
    from web.totp import code_at

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            assert "login-link" in await (await c.get("/login")).text()
            token = websettings.new_login_link(cp.db)
            page = await (await c.get(f"/once/{token}")).text()
            assert "create" in page.lower()                                   # no owner yet: make one
            r = await c.post(f"/once/{token}", data={"username": "boss", "password": "short"})
            assert "16+" in await r.text()
            r = await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await r.text()
            secret = re.search(r"font-size:1.2rem'>([A-Z2-7]+)<", page)[1]
            assert cp.db.one("SELECT totp_secret FROM owners WHERE username = 'boss'")["totp_secret"] == secret
            assert "doesn't work" in await (await c.get(f"/once/{token}")).text()   # used up
            page = await (await c.get("/settings")).text()
            assert "Your domain" in page and "DeepSeek" in page
            t = csrf_of(page)
            r = await c.post("/settings", data={"csrf": t, "CP_BASE_DOMAIN": "rcebot.net",
                                                 "CP_DEEPSEEK_API_KEY": "sk-abcdef", "CP_HQ_GUILD_ID": "123"})
            page = await r.text()
            assert "Saved" in page and "restart the control plane" in page
            assert cp.settings.base_domain == "rcebot.net" and cp.settings.deepseek_api_key == "sk-abcdef"
            assert cp.settings.hq_guild_id == 123 and "sk-abcdef" not in page
            assert (await c.post("/settings", data={"CP_BASE_DOMAIN": "evil.net"})).status == 403   # CSRF
            # a normal login still needs password + 2FA; a second link signs in as the owner
            c.session.cookie_jar.clear()
            assert (await c.get("/settings", allow_redirects=False)).status == 303
            token = websettings.new_login_link(cp.db)
            r = await c.post(f"/once/{token}", data={})
            assert "Signed in as boss" in await r.text()
            r = await c.post("/login", data={"username": "boss", "password": "owner-password-123",
                                             "code": code_at(secret, int(time.time() // 30))}, allow_redirects=False)
            assert r.status == 303
        finally:
            await c.close()
    asyncio.run(scenario())


def test_cli_login_link_prints_a_working_link(cp, capsys, monkeypatch):  # noqa: F811
    from control_plane import cli
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    monkeypatch.setattr(cli, "service_user", lambda: None)
    assert cli.main(["login-link"]) == 0
    out = capsys.readouterr().out
    token = re.search(r"/once/([A-Za-z0-9_-]+)", out)[1]
    assert f"{cp.settings.portal_url}/management/once/{token}" in out
    assert websettings.use_login_link(cp.db, token)


def test_setup_serves_a_one_time_link_then_stops(cp, capsys, monkeypatch, tmp_path):  # noqa: F811
    from control_plane import cli
    env = tmp_path / "rce-saas.env"
    env.write_text("")
    monkeypatch.setenv("CP_ENV_FILE", str(env))
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    monkeypatch.setattr(cli, "service_user", lambda: None)
    cp.settings.session_secret = "s" * 40
    assert cli.main(["setup", "--port", "0", "--minutes", "0"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://\S+:0/once/[A-Za-z0-9_-]+", out) and "Setup page closed" in out
    assert env.read_text() == ""                                  # an existing settings file is never replaced
