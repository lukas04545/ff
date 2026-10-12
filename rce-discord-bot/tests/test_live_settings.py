"""Settings saved in the web interface apply right away – only a few need a restart."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

H = {"X-Requested-With": "rce-web"}


def test_settings_apply_without_restart(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=x\nRCON_HOST=127.0.0.1\nRCON_PORT=1\nRCON_PASSWORD=pw\nADMIN_ROLE_IDS=1\n"
                   f"WEB_PASSWORD=secret123\nDATABASE_PATH={tmp_path / 'bot.db'}\n"
                   f"SUPPORT_KNOWLEDGE_FILE={tmp_path / 'k.md'}\nWEB_TUNNEL=false\nECONOMY_ENABLED=false\n",
                   encoding="utf-8")
    # Loading the .env writes into os.environ – register every key so it is restored for the other tests
    for key in ("DISCORD_TOKEN", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "ADMIN_ROLE_IDS", "WEB_PASSWORD",
                "DATABASE_PATH", "SUPPORT_KNOWLEDGE_FILE", "WEB_TUNNEL", "STATUS_INTERVAL", "DEEPSEEK_API_KEY",
                "ECONOMY_ENABLED", "ADMIN_LOG_CHANNEL_ID", "WIPE_TIMEZONE", "GUILD_ID", "TICKET_CATEGORY_ID"):
        monkeypatch.setenv(key, "")
    from bot import RceBot
    from cogs.economy import Economy
    from cogs.feeds import Feeds
    from cogs.status import Status
    from cogs.tickets import Tickets
    from cogs.wipe import Wipe
    from config import load_config
    from web.server import WebInterface

    async def no_preflight(self):
        return None
    monkeypatch.setattr(WebInterface, "_preflight", no_preflight)

    async def scenario():
        bot = RceBot(load_config(env))
        for cls in (Status, Tickets, Economy, Wipe, Feeds):
            await bot.add_cog(cls(bot))
        reconfigured = []

        async def reconfigure(host, port, password):
            reconfigured.append((host, port, password))
            return True
        bot.rcon.reconfigure = reconfigure
        web = WebInterface(bot, env_path=env, secret_path=tmp_path / "k")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await (await c.put("/api/settings", json={"values": {
                "STATUS_INTERVAL": "120", "ADMIN_LOG_CHANNEL_ID": "555", "DEEPSEEK_API_KEY": "sk-test",
                "RCON_PORT": "28016", "ECONOMY_ENABLED": "true", "WIPE_TIMEZONE": "Europe/London"}}, headers=H)).json()
            assert r["applied"] and r["restart_required"] is False and r["restart_for"] == []
            assert bot.config.status_interval == 120 and bot.config.admin_log_channel_id == 555
            assert bot.get_cog("Status").update_status.seconds == 120
            assert bot.get_cog("Tickets").ai.api_key == "sk-test"                      # new AI client
            assert bot.get_cog("Economy").earn_loop._task is not None                   # switched on live
            assert str(bot.get_cog("Wipe").tz) == "Europe/London"
            assert bot.get_cog("Feeds").admin_log.channel_id == 555                    # new channel used
            assert reconfigured[-1] == ("127.0.0.1", 28016, "pw")                      # RCON reconnects

            # Token / Discord server / tickets (new intent) still need a restart – and say so
            r = await (await c.put("/api/settings", json={"values": {
                "DISCORD_TOKEN": "new-token", "TICKET_CATEGORY_ID": "77"}}, headers=H)).json()
            assert r["restart_required"] is True
            assert "Bot token" in r["restart_for"] and any("Message Content" in x for x in r["restart_for"])
            assert bot.config.ticket_category_id == 77                                 # the rest is live anyway
            settings = await (await c.get("/api/settings")).json()
            assert settings["restart_required"] and "Bot token" in settings["restart_for"]
        finally:
            await c.close()
            bot.get_cog("Economy").earn_loop.cancel()
            await bot.rcon.close()
            bot.db.close()
    asyncio.run(scenario())
