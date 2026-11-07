"""First-run setup: missing channels/categories are created and saved; a default knowledge base is written."""
import asyncio
import itertools
from dataclasses import replace
from types import SimpleNamespace as NS

from tests.test_plugins import pb  # noqa: F401

ids = itertools.count(9000)


class Role:
    def __init__(self, id):
        self.id, self.name = id, ""


class Chan:
    def __init__(self, guild, name, category=None, overwrites=None):
        self.id, self.name, self.category, self.overwrites = next(ids), name, category, overwrites
        self.text_channels, self.sent = [], []
        guild.all[self.id] = self

    async def send(self, text):
        self.sent.append(text)


class FakeGuild:
    def __init__(self):
        self.id, self.name, self.all, self.categories = 1, "Kryvon Discord", {}, []
        self.default_role, self.me = Role(1), Role(2)
        self.roles = [Role(5)]

    def get_channel(self, cid):
        return self.all.get(cid)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def create_role(self, name, permissions=None, colour=None, mentionable=False, reason=None):
        role = Role(next(ids))
        role.name, role.permissions = name, permissions
        self.roles.append(role)
        return role

    async def create_category(self, name, overwrites=None, reason=None):
        c = Chan(self, name, overwrites=overwrites)
        self.categories.append(c)
        return c

    async def create_text_channel(self, name, category=None, overwrites=None, reason=None, topic=None):
        ch = Chan(self, name, category, overwrites)
        category.text_channels.append(ch)
        return ch


def test_missing_channels_are_created_and_saved(pb, monkeypatch, tmp_path):  # noqa: F811
    from cogs.auto_setup import AutoSetup
    from envfile import read_env
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("")
    guild = FakeGuild()
    keep = Chan(guild, "my-own-chat")
    pb.bot.config = replace(pb.bot.config, server_display_name="Rusty Outpost", chat_channel_id=keep.id,
                            admin_role_ids=frozenset({5}), status_channel_id=None, events_channel_id=None,
                            killfeed_channel_id=None, admin_log_channel_id=None, console_log_channel_id=None,
                            ticket_log_channel_id=None, ticket_category_id=None,
                            manager_ai_channel_ids=frozenset(), linked_role_id=None)
    applied = []

    async def apply_config(new):
        applied.append(new)
        return []
    monkeypatch.setattr(pb.bot, "apply_config", apply_config)
    monkeypatch.setattr("config.load_config", lambda path=None: NS(path=path))
    a = AutoSetup(pb.bot)
    made = asyncio.run(a.ensure_channels(guild))
    names = [c.name for c in guild.categories]
    assert names == ["🎮 Rusty Outpost", "🔒 Rusty Outpost staff", "🎫 Tickets"]
    assert "#📊・status" in made and "#💬・game-chat" not in made                   # their own chat stays
    env = read_env(tmp_path / ".env")
    assert "CHAT_CHANNEL_ID" not in env and env["STATUS_CHANNEL_ID"].isdigit() and env["TICKET_CATEGORY_ID"]
    staff = guild.categories[1]
    log = staff.text_channels[0]
    assert log.overwrites[guild.default_role].view_channel is False and log.overwrites[guild.get_role(5)].view_channel
    status = guild.categories[0].text_channels[0]
    assert status.overwrites[guild.default_role].send_messages is False
    assert applied                                                                # applied without a restart
    linked = next(r for r in guild.roles if getattr(r, "name", "") == "🔗 Linked")      # the linked players' role
    assert env["LINKED_ROLE_ID"] == str(linked.id) and "@🔗 Linked" in made
    ai = next(c for c in staff.text_channels if c.name == "🧠・bot-setup")         # the management AI's channel
    assert env["MANAGER_AI_CHANNEL_IDS"] == str(ai.id) and "Management AI" in ai.sent[0]
    assert ai.overwrites[guild.default_role].view_channel is False


def test_default_knowledge_only_when_empty(pb, tmp_path):  # noqa: F811
    from cogs.auto_setup import AutoSetup
    path = tmp_path / "k.md"
    pb.bot.config = replace(pb.bot.config, server_display_name="Rusty Outpost", support_knowledge_file=path,
                            rules_file=tmp_path / "rules.md")
    (tmp_path / "rules.md").write_text("1. No cheating")
    pb.bot.db.create_custom_kit("VIP", "", "t")
    a = AutoSetup(pb.bot)
    assert a.write_default_knowledge(FakeGuild())
    text = path.read_text()
    assert "# Rusty Outpost" in text and "No cheating" in text and "VIP" in text and "/link" in text
    path.write_text("my own knowledge")
    assert not a.write_default_knowledge(FakeGuild()) and path.read_text() == "my own knowledge"


def test_auto_channels_default(monkeypatch):
    from cogs.auto_setup import enabled
    monkeypatch.delenv("AUTO_CHANNELS", raising=False)
    monkeypatch.setenv("RCE_SAAS", "0")
    assert not enabled()                                                          # own install: as it is
    monkeypatch.setenv("RCE_SAAS", "1")
    assert enabled()                                                              # hosted: yes
    monkeypatch.setenv("AUTO_CHANNELS", "false")
    assert not enabled()
