"""The control panel stays reachable: it starts first, slash commands sync in the background (and only when they
changed – Discord's daily limit), a crash while starting opens the panel with the reason."""
import asyncio

from tests.test_web import bot  # noqa: F401


def test_commands_sync_only_when_changed_and_never_blocks(bot, monkeypatch):  # noqa: F811
    calls = []

    async def sync():
        calls.append(1)
        return [1, 2]
    monkeypatch.setattr(bot, "sync_commands", sync)
    monkeypatch.setattr(bot, "_commands_digest", lambda: "same")
    asyncio.run(bot._sync_commands_at_start())
    asyncio.run(bot._sync_commands_at_start())                  # a restart: nothing changed → not sent again
    assert calls == [1]
    monkeypatch.setattr(bot, "_commands_digest", lambda: "changed")
    asyncio.run(bot._sync_commands_at_start())
    assert calls == [1, 1]

    async def stuck():                                          # Discord rate limit: waits for hours
        await asyncio.sleep(3600)
    monkeypatch.setattr(bot, "sync_commands", stuck)
    monkeypatch.setattr(bot, "_commands_digest", lambda: "again")
    real_wait = asyncio.wait_for

    async def quick(aw, timeout):
        return await real_wait(aw, 0.05)
    monkeypatch.setattr(asyncio, "wait_for", quick)
    asyncio.run(bot._sync_commands_at_start())                  # gives up quietly, logged
    assert bot.db.get_value("commands_synced") == "changed"


def test_a_crash_while_starting_opens_the_panel_with_the_reason(monkeypatch, tmp_path):
    import bot as botmod
    monkeypatch.chdir(tmp_path)
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="h", RCON_PORT="1", RCON_PASSWORD="p", ADMIN_ROLE_IDS="1",
                     DATABASE_PATH=str(tmp_path / "b.db"), WEB_PASSWORD="secret123").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(botmod, "disable_console_quick_edit", lambda: None)
    monkeypatch.setattr(botmod, "setup_logging", lambda: None)
    monkeypatch.setattr(botmod.time, "sleep", lambda s: None)
    import web.server as ws

    async def preflight(cfg):
        return {}
    monkeypatch.setattr(ws, "discord_preflight", preflight)
    runs, setups = [], []

    def boom(self, token, log_handler=None):
        runs.append(1)
        raise RuntimeError("a plugin exploded")
    monkeypatch.setattr(botmod.RceBot, "run", boom)

    async def setup(error, fix_url=None):
        setups.append(error)
        raise SystemExit                                        # stop the test here
    monkeypatch.setattr(botmod, "run_setup", setup)
    import servers
    monkeypatch.setattr(servers, "is_child", lambda: True)
    monkeypatch.setattr(servers, "watch_parent", lambda: None)
    try:
        botmod.main()
    except SystemExit:
        pass
    assert len(runs) == botmod.START_CRASHES and "a plugin exploded" in setups[0]
