"""Automatic updates: ZIP install (protected files, manifest, checks) and git checkout (fast-forward)."""
import asyncio
import io
import json
import shutil
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from updater import Updater

SHA = "a" * 40


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ff-x/README.md", "repo readme – not part of the bot folder")
        for rel, text in files.items():
            zf.writestr(f"ff-x/rce-discord-bot/{rel}", text)
    return buf.getvalue()


def _fake_github(files, sha=SHA, calls=None):
    def http_get(url, **kwargs):
        (calls if calls is not None else []).append(url)
        if url == "https://github.com/lukas04545/ff.git/info/refs?service=git-upload-pack":
            return (b"001e# service=git-upload-pack\n0000015" + b"b" * 40 + b" HEAD\x00multi_ack symref=HEAD:refs/heads/main\n"
                    b"003f" + sha.encode() + b" refs/heads/main\n003f" + b"c" * 40 + b" refs/heads/other\n0000")
        if url == "https://github.com/lukas04545/ff/commits/main.atom":
            return (b"<feed><entry><id>tag:github.com,2008:Grit::Commit/" + sha.encode() + b"</id>"
                    b"<title>\n   Better tickets &amp; more\n  </title></entry></feed>")
        if url.startswith("https://api.github.com/"):
            raise AssertionError("the rate-limited GitHub API must not be used")
        if url == f"https://codeload.github.com/lukas04545/ff/zip/{sha}":
            return _zip(files)
        raise AssertionError(url)
    return http_get


def _write(base, files):
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def test_zip_update_keeps_own_files(tmp_path):
    _write(tmp_path, {
        "bot.py": "print('old')\n", "requirements.txt": "discord.py\n", ".env": "DISCORD_TOKEN=secret\n",
        "data/bot.db": "db", "rules.md": "my own rules\n", "support_knowledge.md": "default kb\n",
        "start.bat": "@echo off\nREM old start script without the marker\n", "cogs/stale.py": "x = 1\n",
        "notes.txt": "my file, not from the update\n"})
    # Manifest of the previously installed version: knowledge base unchanged since then, stale.py was ours
    import hashlib
    (tmp_path / "data/update_state.json").write_text(json.dumps({"sha": "b" * 40, "files": {
        "support_knowledge.md": hashlib.sha256(b"default kb\n").hexdigest(), "cogs/stale.py": "x"}}))
    pip = []
    before = []
    up = Updater("lukas04545/ff", base_dir=tmp_path, pip_install=lambda req: pip.append(req.read_text()),
                 before_apply=lambda: before.append((tmp_path / "bot.py").read_text()),
                 http_get=_fake_github({
                     "bot.py": "print('new')\n", "cogs/new.py": "y = 2\n", "requirements.txt": "discord.py\naiohttp\n",
                     ".env": "DISCORD_TOKEN=from-the-zip\n", "data/bot.db": "zip-db", "rules.md": "new default rules\n",
                     "support_knowledge.md": "new default kb\n", "start.bat": "@echo off\nREM update-safe\n"}))
    r = up.check()
    assert r.status == "updated" and r.restart_needed and r.sha == SHA and r.title == "Better tickets & more"
    assert sorted(r.changed) == ["bot.py", "cogs/new.py", "requirements.txt", "support_knowledge.md"]
    assert before == ["print('old')\n"]                                   # backup hook ran before writing
    assert r.removed == ["cogs/stale.py"] and r.requirements_changed and pip == ["discord.py\naiohttp\n"]
    read = lambda rel: (tmp_path / rel).read_text(encoding="utf-8")
    assert read("bot.py") == "print('new')\n" and read("cogs/new.py") == "y = 2\n"
    assert read(".env") == "DISCORD_TOKEN=secret\n" and read("data/bot.db") == "db"       # protected
    assert read("rules.md") == "my own rules\n"                                            # edited -> kept
    assert read("support_knowledge.md") == "new default kb\n"                              # unedited -> updated
    assert "old start script" in read("start.bat") and "update-safe" in read("start.bat.new")
    assert not (tmp_path / "cogs/stale.py").exists() and (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "README.md").exists()                                           # outside the bot folder
    assert up.current_version()["sha"] == SHA

    calls = []
    up.http_get = _fake_github({}, calls=calls)
    assert up.check().status == "up_to_date" and not any("codeload" in u for u in calls)  # no second download


def test_zip_update_checks_before_writing(tmp_path):
    _write(tmp_path, {"bot.py": "print('old')\n", "requirements.txt": "discord.py\n"})
    up = Updater("lukas04545/ff", base_dir=tmp_path, pip_install=lambda req: None,
                 http_get=_fake_github({"bot.py": "print('broken'\n", "requirements.txt": "discord.py\n"}))
    r = up.check()
    assert r.status == "error" and "syntax error in bot.py" in r.message and not r.restart_needed
    assert (tmp_path / "bot.py").read_text() == "print('old')\n"

    from updater import UpdateError

    def failing_pip(req):
        raise UpdateError("Update skipped: installing the new Python packages failed")
    up = Updater("lukas04545/ff", base_dir=tmp_path, pip_install=failing_pip,
                 http_get=_fake_github({"bot.py": "print('new')\n", "requirements.txt": "nonexistent-pkg\n"}))
    assert up.check().status == "error" and (tmp_path / "bot.py").read_text() == "print('old')\n"

    # Same content as installed (e.g. first check after a manual ZIP install): no restart, version recorded
    up = Updater("lukas04545/ff", base_dir=tmp_path, http_get=_fake_github({"bot.py": "print('old')\n"}))
    assert up.check().status == "up_to_date" and up.current_version()["sha"] == SHA


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_checkout_fast_forward(tmp_path):
    origin = tmp_path / "origin"
    _write(origin, {"rce-discord-bot/bot.py": "v = 1\n", "rce-discord-bot/requirements.txt": "a\n"})
    _git(origin, "init", "-q")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "first")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    bot_dir = tmp_path / "clone" / "rce-discord-bot"
    pip = []
    up = Updater("lukas04545/ff", base_dir=bot_dir, pip_install=lambda req: pip.append(req.read_text()))
    assert up.mode == "git" and up.check().status == "up_to_date"

    _write(origin, {"rce-discord-bot/bot.py": "v = 2\n", "rce-discord-bot/requirements.txt": "a\nb\n"})
    _git(origin, "commit", "-qam", "second version")
    (bot_dir / ".env").write_text("SECRET=1\n")                 # untracked files don't block the update
    r = up.check()
    assert r.status == "updated" and r.title == "second version" and r.requirements_changed
    assert (bot_dir / "bot.py").read_text() == "v = 2\n" and pip == ["a\nb\n"]
    assert up.current_version()["title"] == "second version"

    _write(origin, {"rce-discord-bot/bot.py": "v = 3\n"})
    _git(origin, "commit", "-qam", "third")
    (bot_dir / "bot.py").write_text("v = 'my local edit'\n")    # hand-edited code -> blocks, never overwritten
    r = up.check()
    assert r.status == "skipped" and "changed bot code" in r.message and "bot.py" in r.message
    assert (bot_dir / "bot.py").read_text() == "v = 'my local edit'\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_update_keeps_web_edited_files(tmp_path):
    """Editing rules/knowledge/prices in the web interface must not block updates – the owner's version stays."""
    origin = tmp_path / "origin"
    _write(origin, {"rce-discord-bot/bot.py": "v = 1\n", "rce-discord-bot/rules.md": "upstream rules\n",
                    "rce-discord-bot/prices.txt": "upstream prices\n"})
    _git(origin, "init", "-q"); _git(origin, "add", "."); _git(origin, "commit", "-qm", "first")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    bot_dir = tmp_path / "clone" / "rce-discord-bot"
    up = Updater("lukas04545/ff", base_dir=bot_dir, pip_install=lambda req: None)

    # The owner edited their knowledge base/prices in the panel; upstream changes code and also the rules file.
    (bot_dir / "prices.txt").write_text("MY custom prices\n")
    (bot_dir / "rules.md").write_text("MY rules\n")
    _write(origin, {"rce-discord-bot/bot.py": "v = 2\n", "rce-discord-bot/rules.md": "new upstream rules\n"})
    _git(origin, "commit", "-qam", "second")
    r = up.check()
    assert r.status == "updated"                                    # not blocked any more
    assert (bot_dir / "bot.py").read_text() == "v = 2\n"            # code updated
    assert (bot_dir / "prices.txt").read_text() == "MY custom prices\n"   # owner's edits kept
    assert (bot_dir / "rules.md").read_text() == "MY rules\n"             # kept even though upstream changed it
    assert up.check().status == "up_to_date"


def test_cog_restarts_after_update(monkeypatch):
    from cogs.updates import Updates
    from updater import Result
    events = []
    cfg = SimpleNamespace(auto_update=True, auto_update_interval=5, auto_update_repo="lukas04545/ff",
                          auto_update_branch="main")
    bot = SimpleNamespace(config=cfg, launched_by_script=True, audit=events.append,
                          request_update_restart=lambda: events.append("restart"))
    cog = Updates(bot)
    cog.updater.check = lambda: Result("up_to_date", "Already the newest version.", SHA, "x")
    assert asyncio.run(cog.check_now()).status == "up_to_date" and events == []
    cog.updater.check = lambda: Result("updated", "ok", SHA, "Better tickets", ["bot.py"])
    asyncio.run(cog.check_now())
    assert events[-1] == "restart" and "Better tickets" in events[0] and "aaaaaaa" in events[0]


def test_restart_after_update_uses_start_script_exit_code():
    import bot
    with pytest.raises(SystemExit) as exc:
        bot.restart_after_update(True)
    assert exc.value.code == bot.EXIT_UPDATED == 4


def test_update_settings_validation():
    from settings_schema import validate
    assert validate("AUTO_UPDATE_REPO", "lukas04545/ff") is None
    assert validate("AUTO_UPDATE_REPO", "https://github.com/x") is not None
    assert validate("AUTO_UPDATE_BRANCH", "main") is None and validate("AUTO_UPDATE_BRANCH", "a b") is not None
    assert validate("AUTO_UPDATE_INTERVAL", "10") is None


def test_zip_update_keeps_changed_and_own_plugins(tmp_path):
    """Built-in plugin files you changed (or your own file in plugins/) are kept in data/plugins/ before the update
    replaces or deletes them; untouched built-ins are simply updated; your plugins in data/plugins/ are never touched."""
    import hashlib
    h = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
    _write(tmp_path, {
        "bot.py": "print('old')\n",
        "plugins/bounties.py": "BOUNTIES = 'my edit'\n",           # built-in, edited by the owner
        "plugins/invites.py": "INVITES = 1\n",                     # built-in, untouched
        "plugins/oldie.py": "OLDIE = 'my edit'\n",                 # built-in removed upstream, edited by the owner
        "plugins/mine.py": "MINE = 1\n",                           # owner's own file; the update adds a built-in "mine"
        "data/plugins/hello.py": "HELLO = 1\n",                    # owner's plugin in the safe folder
    })
    (tmp_path / "data/update_state.json").write_text(json.dumps({"sha": "b" * 40, "files": {
        "bot.py": h("print('old')\n"), "plugins/bounties.py": h("BOUNTIES = 1\n"),
        "plugins/invites.py": h("INVITES = 1\n"), "plugins/oldie.py": h("OLDIE = 1\n")}}))
    up = Updater("lukas04545/ff", base_dir=tmp_path, pip_install=lambda req: None, http_get=_fake_github({
        "bot.py": "print('new')\n", "plugins/bounties.py": "BOUNTIES = 2\n", "plugins/invites.py": "INVITES = 2\n",
        "plugins/mine.py": "BUILTIN_MINE = 1\n", "data/plugins/hello.py": "FROM_ZIP = 1\n"}))
    r = up.check()
    assert r.status == "updated"
    read = lambda rel: (tmp_path / rel).read_text()  # noqa: E731
    # built-ins updated
    assert read("plugins/bounties.py") == "BOUNTIES = 2\n" and read("plugins/invites.py") == "INVITES = 2\n"
    assert read("plugins/mine.py") == "BUILTIN_MINE = 1\n" and not (tmp_path / "plugins/oldie.py").exists()
    # ... and the owner's versions survive in data/plugins/ (they replace the built-ins there)
    assert read("data/plugins/bounties.py") == "BOUNTIES = 'my edit'\n"
    assert read("data/plugins/oldie.py") == "OLDIE = 'my edit'\n"
    assert read("data/plugins/mine.py") == "MINE = 1\n"
    assert not (tmp_path / "data/plugins/invites.py").exists()           # untouched built-in: nothing to keep
    assert read("data/plugins/hello.py") == "HELLO = 1\n"                 # data/ is never written by the update
    assert sorted(x.split(" -> ")[0] for x in r.rescued) == ["plugins/bounties.py", "plugins/mine.py", "plugins/oldie.py"]
    assert "Kept your plugin file(s)" in r.message

    # A later update never overwrites a different file that is already in data/plugins/
    (tmp_path / "plugins/bounties.py").write_text("BOUNTIES = 'second edit'\n")
    up.http_get = _fake_github({"bot.py": "print('newer')\n", "plugins/bounties.py": "BOUNTIES = 3\n",
                                "plugins/invites.py": "INVITES = 2\n", "plugins/mine.py": "BUILTIN_MINE = 1\n"},
                               sha="d" * 40)
    r = up.check()
    assert read("data/plugins/bounties.py") == "BOUNTIES = 'my edit'\n"
    kept = [p for p in (tmp_path / "data/plugins/rescued").iterdir()]
    assert len(kept) == 1 and kept[0].read_text() == "BOUNTIES = 'second edit'\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_update_keeps_changed_and_own_plugins(tmp_path):
    origin = tmp_path / "origin"
    _write(origin, {"rce-discord-bot/bot.py": "v = 1\n", "rce-discord-bot/plugins/bounties.py": "B = 1\n"})
    _git(origin, "init", "-q"); _git(origin, "add", "."); _git(origin, "commit", "-qm", "first")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    bot_dir = tmp_path / "clone" / "rce-discord-bot"
    up = Updater("lukas04545/ff", base_dir=bot_dir, pip_install=lambda req: None)

    (bot_dir / "plugins/bounties.py").write_text("B = 'my edit'\n")      # changed a built-in plugin
    (bot_dir / "plugins/mine.py").write_text("MINE = 1\n")              # own file; upstream will add "mine" too
    _write(origin, {"rce-discord-bot/bot.py": "v = 2\n", "rce-discord-bot/plugins/bounties.py": "B = 2\n",
                    "rce-discord-bot/plugins/mine.py": "BUILTIN_MINE = 1\n"})
    _git(origin, "add", "."); _git(origin, "commit", "-qm", "second")
    r = up.check()
    assert r.status == "updated", r.message                              # a changed plugin no longer blocks it
    assert (bot_dir / "bot.py").read_text() == "v = 2\n"
    assert (bot_dir / "plugins/bounties.py").read_text() == "B = 2\n"
    assert (bot_dir / "plugins/mine.py").read_text() == "BUILTIN_MINE = 1\n"
    assert (bot_dir / "data/plugins/bounties.py").read_text() == "B = 'my edit'\n"
    assert (bot_dir / "data/plugins/mine.py").read_text() == "MINE = 1\n"
    assert len(r.rescued) == 2 and up.check().status == "up_to_date"
