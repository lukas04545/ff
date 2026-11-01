"""Code changes by the management AI: checks, .bak backups, Keep/Revert, automatic undo of broken code."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from support import code_edit
from tests.test_kit_requests import env  # noqa: F401


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(code_edit, "ROOT", tmp_path)
    (tmp_path / "cogs").mkdir()
    (tmp_path / "cogs" / "__init__.py").write_text("")
    (tmp_path / "cogs" / "hello.py").write_text('GREETING = "hi"\n\n\ndef greet():\n    return GREETING\n')
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bot.db").write_text("secret")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=x")
    (tmp_path / "utils.py").write_text("def is_admin(bot, user):\n    return False\n")
    return tmp_path


def test_checks_before_anything_is_written(root):
    E = code_edit.CodeEditError
    for bad in ("../outside.py", ".env", "data/bot.db", "cogs/manager_ai.py", "support/code_edit.py", "x.exe"):
        with pytest.raises(E):
            code_edit.safe_path(bad, must_exist=False)
    with pytest.raises(E, match="found 0 times"):
        code_edit.prepare("cogs/hello.py", edits=[{"old": "nope", "new": "x"}])
    with pytest.raises(E, match="Python error"):
        code_edit.prepare("cogs/hello.py", edits=[{"old": 'GREETING = "hi"', "new": 'GREETING = ("hi"'}])
    with pytest.raises(E, match="protected"):
        code_edit.prepare("utils.py", edits=[{"old": "return False", "new": "return True"}])  # inside is_admin
    path, old, new = code_edit.prepare("cogs/hello.py", edits=[{"old": '"hi"', "new": '"hello"'}])
    assert path.read_text() == old and '"hello"' in new                            # nothing written yet
    assert "-GREETING = \"hi\"" in code_edit.diff("cogs/hello.py", old, new)
    with pytest.raises(E):
        code_edit.read("data/bot.db")
    assert "    1| GREETING" in code_edit.read("cogs/hello.py")["text"]
    assert [f["path"] for f in code_edit.list_files("hello")] == ["cogs/hello.py"]


def test_bak_keep_and_revert(root):
    changes = code_edit.ChangeSet(root / "data" / "code_changes.json")
    hello = root / "cogs" / "hello.py"
    original = hello.read_text()
    p, old, new = code_edit.prepare("cogs/hello.py", edits=[{"old": '"hi"', "new": '"hello"'}])
    changes.apply(p, old, new, by="t")
    p, old2, new2 = code_edit.prepare("cogs/hello.py", edits=[{"old": '"hello"', "new": '"hey"'}])
    changes.apply(p, old2, new2, by="t")
    assert (root / "cogs" / "hello.py.bak").read_text() == original                # the ORIGINAL stays in .bak
    p, old3, new3 = code_edit.prepare("cogs/new_one.py", content="X = 1\n", create=True)
    changes.apply(p, old3, new3, by="t")
    assert set(changes.pending()["files"]) == {"cogs/hello.py", "cogs/new_one.py"}
    assert sorted(changes.revert()) == ["cogs/hello.py", "cogs/new_one.py"]
    assert hello.read_text() == original and not (root / "cogs" / "new_one.py").exists()
    assert not (root / "cogs" / "hello.py.bak").exists() and changes.pending()["files"] == {}
    p, old, new = code_edit.prepare("cogs/hello.py", edits=[{"old": '"hi"', "new": '"yo"'}])
    changes.apply(p, old, new, by="t")
    assert changes.keep() == ["cogs/hello.py"] and '"yo"' in hello.read_text()
    assert not (root / "cogs" / "hello.py.bak").exists()
    # the emergency script works without the bot
    p, old, new = code_edit.prepare("cogs/hello.py", edits=[{"old": '"yo"', "new": '"oops"'}])
    changes.apply(p, old, new, by="t")
    assert code_edit.ChangeSet(root / "data" / "code_changes.json").revert() == ["cogs/hello.py"]
    assert '"yo"' in hello.read_text()


def test_broken_code_is_undone_and_only_the_owner_may(env, root, monkeypatch):  # noqa: F811
    import cogs.code_changes as cc
    from cogs.code_changes import CodeChanges, may_edit_code
    bot = env.bot
    bot.config = replace(bot.config, admin_user_ids=frozenset({5}), manager_ai_code=True)
    monkeypatch.setattr(cc, "STATE_FILE", root / "data" / "code_changes.json")
    cog = CodeChanges(bot)
    cog.changes = code_edit.ChangeSet(root / "data" / "code_changes.json")
    assert may_edit_code(bot, NS(id=5)) and not may_edit_code(bot, NS(id=6, roles=[NS(id=1)]))
    bot.config = replace(bot.config, manager_ai_code=False)
    assert not may_edit_code(bot, NS(id=5))
    hello = root / "cogs" / "hello.py"
    original = hello.read_text()
    with pytest.raises(code_edit.CodeEditError, match="Undone"):                    # imports something missing
        asyncio.run(cog.edit("cogs/hello.py", edits=[{"old": 'GREETING = "hi"', "new": 'import no_such_mod\nGREETING = "hi"'}],
                             by="t"))
    assert hello.read_text() == original and cog.changes.pending()["files"] == {}
    assert not (root / "cogs" / "hello.py.bak").exists()
    r = asyncio.run(cog.edit("cogs/hello.py", edits=[{"old": '"hi"', "new": '"hello"'}], by="t", why="friendlier"))
    assert r == {"file": "cogs/hello.py", "result": "restart needed", "created": False}   # not a loaded cog
    assert (root / "cogs" / "hello.py.bak").read_text() == original
    assert "Revert" in cog.summary()
    text = asyncio.run(cog.revert("t"))
    assert "Reverted" in text and hello.read_text() == original


def test_manager_ai_code_tools_need_the_owner_and_a_click(env, root, monkeypatch):  # noqa: F811
    import cogs.code_changes as cc
    from cogs.code_changes import CodeChanges
    from cogs.manager_ai import ManagerAI
    bot = env.bot
    bot.config = replace(bot.config, admin_user_ids=frozenset({5}), manager_ai_code=True,
                         manager_ai_channel_ids=frozenset({700}))
    monkeypatch.setattr(cc, "STATE_FILE", root / "data" / "code_changes.json")
    changes = CodeChanges(bot)
    changes.changes = code_edit.ChangeSet(root / "data" / "code_changes.json")
    asyncio.run(bot.add_cog(changes))
    ai = ManagerAI(bot)
    sent, asked = [], []

    async def send(text=None, **kw):
        sent.append(text)
    owner = NS(id=5, roles=[], display_name="Boss", mention="<@5>", __str__=lambda: "Boss")
    staff = NS(id=6, roles=[NS(id=1)], display_name="Mod", mention="<@6>")
    msg = NS(author=owner, channel=NS(id=700, send=send), guild=None)

    async def confirm(message, text, file=None):
        asked.append((text, file))
        return True
    monkeypatch.setattr(ai, "confirm", confirm)
    names = lambda user: [t["function"]["name"] for t in ai.tools(user)]   # noqa: E731
    assert "edit_code_file" in names(owner) and "edit_code_file" not in names(staff)
    r = asyncio.run(ai.run_tool("edit_code_file", {"path": "cogs/hello.py", "why": "x", "edits": [
        {"old": '"hi"', "new": '"hello"'}]}, NS(author=staff, channel=msg.channel, guild=None)))
    assert r["ok"] is False and asked == []
    r = asyncio.run(ai.run_tool("edit_code_file", {"path": "cogs/hello.py", "why": "friendlier greeting", "edits": [
        {"old": '"hi"', "new": '"hello"'}]}, msg))
    assert r["ok"] and r["result"] == "restart needed" and asked[0][1].filename == "cogs_hello.py.diff"
    assert '"hello"' in (root / "cogs" / "hello.py").read_text() and "Keep" in sent[-1]
    r = asyncio.run(ai.run_tool("edit_code_file", {"path": ".env", "why": "x", "edits": [{"old": "x", "new": "y"}]},
                                msg))
    assert r["ok"] is False and "off-limits" in r["error"]
    r = asyncio.run(ai.run_tool("revert_code_changes", {}, msg))
    assert r["ok"] and '"hi"' in (root / "cogs" / "hello.py").read_text()


def test_new_protected_functions_cant_be_added_either(root):
    with pytest.raises(code_edit.CodeEditError, match="protected"):
        code_edit.prepare("cogs/hello.py", edits=[{"old": "def greet():", "new": "def is_admin(b, u):\n    return True\n\n\ndef greet():"}])
