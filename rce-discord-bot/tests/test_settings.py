"""Tests für Einstellungen im Webinterface: .env schreiben, Validierung, Einrichtungsmodus, Sessions, RCON-Test."""
import asyncio
import sys
from pathlib import Path

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

import settings_schema
from envfile import format_value, read_env, update_env
from rce.diagnostics import diagnose_rcon
from web import server
from web.server import WebInterface, WebSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mock_rcon_server  # noqa: E402

H = {"X-Requested-With": "rce-web"}


@pytest.fixture(autouse=True)
def offline_discord(monkeypatch):
    """Standard: Discord „nicht erreichbar“ -> Vorabprüfung blockiert nicht (Tests ohne Internet)."""
    async def fake(token):
        return {"reachable": False, "token_ok": None, "bot_name": None, "app_id": None, "intents": None,
                "error": "offline"}
    monkeypatch.setattr(server, "check_discord", fake)
    return monkeypatch


def _discord(monkeypatch, *, token_ok=True, message_content=True, members=True):
    async def fake(token):
        return {"reachable": True, "token_ok": token_ok, "bot_name": "Bot", "app_id": "42",
                "intents": {"message_content": message_content, "members": members} if token_ok else None,
                "error": None if token_ok else "Discord lehnt den Bot-Token ab."}
    monkeypatch.setattr(server, "check_discord", fake)
TRICKY = ['kick "{name}"', "<color=#5865F2>[Discord]</color>", "wert mit #kommentar", "a #b", " führend",
          "Zeile1\nZeile2", 'Pfad\\mit\\backslash', "'zitat'", '"doppelt"', "Willkommen {member}! – viel Spaß"]


def test_env_roundtrip_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Kommentar oben\nDISCORD_TOKEN=alt\n\n# RCON\nRCON_PORT=1\n", encoding="utf-8")
    updates = {f"K{i}": v for i, v in enumerate(TRICKY)}
    updates["RCON_PORT"] = "28016"
    update_env(env, updates)
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# Kommentar oben\nDISCORD_TOKEN=alt\n\n# RCON\nRCON_PORT=28016\n")
    values = dotenv_values(env)
    for i, v in enumerate(TRICKY):
        assert values[f"K{i}"] == v, (v, format_value(v))
    assert read_env(tmp_path / "fehlt.env") == {}


def test_update_env_creates_from_template(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("# Vorlage\nRCON_HOST=\nWEB_PORT=8080\n", encoding="utf-8")
    env = tmp_path / ".env"
    update_env(env, {"RCON_HOST": "1.2.3.4"}, template=template)
    assert read_env(env) == {"RCON_HOST": "1.2.3.4", "WEB_PORT": "8080"}


def test_validate():
    v = settings_schema.validate
    assert v("RCON_PORT", "abc") and v("RCON_PORT", "28016") is None
    assert v("RCON_HOST", "1.2.3.4:28016") and v("RCON_HOST", "1.2.3.4") is None
    assert v("DISCORD_TOKEN", "")  # Pflichtfeld
    assert v("GUILD_ID", "") is None and v("GUILD_ID", "12x")
    assert v("ADMIN_ROLE_IDS", "1, 2,3") is None and v("ADMIN_ROLE_IDS", "1,x")
    assert v("ALLOW_RAW_RCON", "vielleicht") and v("ALLOW_RAW_RCON", "true") is None
    assert v("WEB_HOST", "8.8.8.8") and v("WEB_HOST", "0.0.0.0") is None
    assert v("SAY_PREFIX", "a\nb") and v("WELCOME_MESSAGE", "a\nb") is None
    # Jede Einstellung aus .env.example ist im Formular vorhanden
    example = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert set(example) <= set(settings_schema.FIELDS_BY_KEY)


def _env(tmp_path, **values):
    env = tmp_path / ".env"
    update_env(env, values)
    return env


async def _client(web_iface):
    client = TestClient(TestServer(web_iface.app))
    await client.start_server()
    return client


def test_setup_mode_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("DISCORD_TOKEN", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "ADMIN_ROLE_IDS", "ADMIN_USER_IDS"):
        monkeypatch.delenv(key, raising=False)
    env = _env(tmp_path, DISCORD_TOKEN="tok", RCON_HOST="1.2.3.4")

    async def scenario():
        started = asyncio.Event()
        w = WebInterface(None, web_settings=WebSettings(password="pw"), setup_error="RCON_PORT fehlt",
                         on_config_saved=started.set, env_path=env, secret_path=tmp_path / "s.key")
        c = await _client(w)
        try:
            # Einmal-Link meldet an und führt zu den Einstellungen
            r = await c.get(f"/auth?token={w.login_token}", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/#settings"
            assert (await (await c.get("/api/me")).json())["setup_mode"] is True
            assert (await c.get("/api/kits")).status == 404  # im Einrichtungsmodus nur Einstellungen

            data = await (await c.get("/api/settings")).json()
            token = next(f for f in data["fields"] if f["key"] == "DISCORD_TOKEN")
            assert token["is_set"] is True and "value" not in token  # Geheimnisse werden nie ausgeliefert
            assert "RCON_PORT" in data["config_error"]

            # Ungültige Werte -> Fehler pro Feld, nichts gespeichert
            r = await c.put("/api/settings", json={"values": {"RCON_PORT": "abc"}}, headers=H)
            assert r.status == 400 and "RCON_PORT" in (await r.json())["errors"]

            # Noch unvollständig -> gespeichert, aber Hinweis
            r = await (await c.put("/api/settings", json={"values": {"RCON_PORT": "28016"}}, headers=H)).json()
            assert r["config_error"] and not started.is_set()

            # Vollständig -> Bot startet; leeres Geheimnis bleibt unverändert
            r = await (await c.put("/api/settings", json={"values": {
                "RCON_PASSWORD": "geheim", "ADMIN_USER_IDS": "123, 456", "DISCORD_TOKEN": ""}}, headers=H)).json()
            assert r["starting"] is True and started.is_set()
            saved = read_env(env)
            assert saved["DISCORD_TOKEN"] == "tok" and saved["ADMIN_USER_IDS"] == "123,456"
        finally:
            await c.close()

    asyncio.run(scenario())


def test_host_check_and_persistent_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _env(tmp_path, WEB_PASSWORD="pw")

    async def scenario():
        key = tmp_path / "s.key"
        w1 = WebInterface(None, web_settings=WebSettings(password="pw"), env_path=env, secret_path=key)
        c1 = await _client(w1)
        try:
            # Fremder Host-Header (DNS-Rebinding) wird abgewiesen
            assert (await c1.get("/api/me", headers={"Host": "evil.example:8080"})).status == 403
            await c1.post("/api/login", json={"password": "pw"}, headers=H)
            cookie = c1.session.cookie_jar.filter_cookies(c1.make_url("/"))["rce_session"].value
        finally:
            await c1.close()

        # "Neustart": neue Instanz mit gleichem Schlüssel -> Sitzung gilt weiter
        w2 = WebInterface(None, web_settings=WebSettings(password="pw"), env_path=env, secret_path=key)
        c2 = await _client(w2)
        try:
            assert (await c2.get("/api/me", cookies={"rce_session": cookie})).status == 200
        finally:
            await c2.close()

        # Anderes Passwort -> alte Sitzungen ungültig
        w3 = WebInterface(None, web_settings=WebSettings(password="neu"), env_path=env, secret_path=key)
        c3 = await _client(w3)
        try:
            assert (await c3.get("/api/me", cookies={"rce_session": cookie})).status == 401
        finally:
            await c3.close()

    asyncio.run(scenario())


def test_rcon_diagnostics_against_mock():
    async def scenario():
        runner = web.AppRunner(mock_rcon_server.create_app("pw"), shutdown_timeout=1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            ok = await diagnose_rcon("127.0.0.1", port, "pw")
            assert ok.ok and "3/50" in ok.steps[-1]["text"]
            wrong = await diagnose_rcon("127.0.0.1", port, "falsch")
            assert not wrong.ok and "Passwort" in wrong.steps[-1]["text"]
            special = await diagnose_rcon("127.0.0.1", port, "p#w")
            assert any(s["level"] == "warn" for s in special.steps)
            bad_host = await diagnose_rcon("127.0.0.1:1", port, "pw")
            assert not bad_host.ok and "IP" in bad_host.steps[0]["text"]
        finally:
            await runner.cleanup()
        closed = await diagnose_rcon("127.0.0.1", port, "pw")
        assert not closed.ok and closed.steps[-1]["level"] in ("info", "fail")

    asyncio.run(scenario())


def test_saving_rcon_settings_runs_test(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        runner = web.AppRunner(mock_rcon_server.create_app("pw"), shutdown_timeout=1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        env = _env(tmp_path, DISCORD_TOKEN="tok", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                   ADMIN_USER_IDS="1")
        w = WebInterface(None, web_settings=WebSettings(password="x"), env_path=env,
                         secret_path=tmp_path / "s.key", on_config_saved=lambda: None)
        c = await _client(w)
        try:
            await c.post("/api/login", json={"password": "x"}, headers=H)
            r = await (await c.put("/api/settings", json={"values": {"RCON_PORT": str(port)}}, headers=H)).json()
            assert r["rcon_test"]["ok"] is True
            r = await (await c.put("/api/settings", json={"values": {"RCON_PASSWORD": "falsch"}}, headers=H)).json()
            assert r["rcon_test"]["ok"] is False
            # Ohne RCON-Änderung kein Test
            r = await (await c.put("/api/settings", json={"values": {"SERVER_DISPLAY_NAME": "X"}}, headers=H)).json()
            assert r["rcon_test"] is None
        finally:
            await c.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_preflight_blocks_start_until_intents_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _env(tmp_path, DISCORD_TOKEN="tok", RCON_HOST="1.2.3.4", RCON_PORT="28016", RCON_PASSWORD="pw",
               ADMIN_USER_IDS="1", TICKET_CATEGORY_ID="5")  # Tickets -> Message Content Intent nötig

    async def scenario():
        started = asyncio.Event()
        w = WebInterface(None, web_settings=WebSettings(password="x"), env_path=env, secret_path=tmp_path / "k",
                         on_config_saved=started.set, setup_error="Intent fehlt")
        c = await _client(w)
        try:
            await c.post("/api/login", json={"password": "x"}, headers=H)
            # Intent im Portal noch aus -> kein Start, klare Meldung mit Link
            _discord(monkeypatch, message_content=False)
            r = await (await c.post("/api/setup/continue", headers=H)).json()
            assert r["ok"] is False and "Message Content Intent" in r["preflight_error"]
            assert "Support-Tickets" in r["preflight_error"] and r["fix_url"].endswith("/applications/42/bot")
            r = await (await c.put("/api/settings", json={"values": {"SERVER_DISPLAY_NAME": "X"}}, headers=H)).json()
            assert "preflight_error" in r and not r.get("starting") and not started.is_set()
            # Members-Intent wird nicht verlangt, weil keine Funktion ihn braucht
            assert "Server Members" not in r["preflight_error"]

            # Ungültiger Token
            _discord(monkeypatch, token_ok=False)
            r = await (await c.post("/api/setup/continue", headers=H)).json()
            assert r["ok"] is False and "Token" in r["preflight_error"]

            # Intent eingeschaltet -> Bot startet
            _discord(monkeypatch)
            r = await (await c.post("/api/setup/continue", headers=H)).json()
            assert r["starting"] is True and started.is_set()
        finally:
            await c.close()

    asyncio.run(scenario())


def test_required_intents():
    from config import required_intents
    cfg = type("Cfg", (), dict(chat_channel_id=None, chat_bridge_to_game=True, ticket_category_id=None,
                               discord_log_channel_id=None, welcome_channel_id=None, auto_role_id=None))()
    assert required_intents(cfg) == {"message_content": [], "members": []}
    cfg.ticket_category_id, cfg.auto_role_id = 5, 7
    need = required_intents(cfg)
    assert need["message_content"] == ["Support-Tickets"] and need["members"] == ["Auto-Rolle"]
