"""The management Discord (HQ): channels and panels per customer, buttons, the management AI, access control."""
import asyncio
import itertools
from types import SimpleNamespace as NS

import pytest

from control_plane import hq as hqmod
from control_plane import manage_ai, provisioning
from tests.test_control_plane import cp, customer, paid  # noqa: F401

GID, ADMIN, STRANGER = 900, 11, 22
ids = itertools.count(5000)


class Msg:
    def __init__(self, channel, content="", embed=None, view=None):
        self.id, self.channel, self.content, self.embed, self.view = next(ids), channel, content, embed, view
        self.edits = 0

    async def edit(self, embed=None, view=None):
        self.embed, self.view, self.edits = embed, view, self.edits + 1

    async def reply(self, content, mention_author=False, view=None):
        return await self.channel.send(content, view=view)


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Channel:
    def __init__(self, guild, name, category=None, topic=None, overwrites=None):
        self.id, self.guild, self.name, self.category, self.topic = next(ids), guild, name, category, topic
        self.overwrites = overwrites if overwrites is not None else dict(getattr(category, "overwrites", {}))
        self.sent, self.repaired = [], 0

    async def edit(self, overwrites=None, reason=None):
        self.overwrites, self.repaired = overwrites, self.repaired + 1

    async def send(self, content=None, embed=None, view=None):
        m = Msg(self, content, embed, view)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == mid:
                return m
        raise AssertionError("missing message")

    def typing(self):
        return Typing()


class Category:
    def __init__(self, guild, name, overwrites):
        self.id, self.guild, self.name, self.overwrites = next(ids), guild, name, overwrites
        self.text_channels = []

    @property
    def channels(self):
        return self.text_channels

    async def edit(self, overwrites=None, reason=None):
        self.overwrites = overwrites

    async def create_text_channel(self, name, topic=None, reason=None, overwrites=None):
        ch = Channel(self.guild, name, self, topic, overwrites)
        self.text_channels.append(ch)
        self.guild.all[ch.id] = ch
        return ch


class Obj:
    def __init__(self, id, name=""):
        self.id, self.name, self.members = id, name, []


class Member:
    def __init__(self, guild, id, name):
        self.id, self.guild, self.display_name, self.bot, self.roles = id, guild, name, False, []

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            self.roles.append(r)
            r.members.append(self)

    async def remove_roles(self, *roles, reason=None):
        for r in roles:
            self.roles.remove(r)
            r.members.remove(self)


class Guild:
    def __init__(self):
        self.id, self.owner_id, self.categories, self.all = GID, 1, [], {}
        self.default_role, self.me = Obj(GID), Obj(2)
        self.roles, self.members = [], {}

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        raise hqmod.discord.NotFound(NS(status=404, reason="x"), "unknown member")

    async def create_role(self, name, colour=None, mentionable=False, reason=None):
        role = Obj(next(ids), name)
        self.roles.append(role)
        return role

    def get_channel(self, cid):
        return self.all.get(cid)

    async def create_category(self, name, overwrites=None, reason=None):
        cat = Category(self, name, overwrites)
        self.categories.append(cat)
        return cat


class Client:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild if gid == GID else None

    def is_ready(self):
        return True


class Response:
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def defer(self, ephemeral=False, thinking=False):
        self.done = True

    async def send_message(self, content, ephemeral=False, view=None):
        self.done = True
        self.log.append(content)


def interaction(guild, custom_id, user_id=ADMIN, channel=None):
    log = []
    user = NS(id=user_id, guild=guild, bot=False, roles=[])
    i = NS(data={"custom_id": custom_id}, user=user, channel=channel, response=Response(log), log=log,
           guild_id=guild.id)

    async def followup(content, ephemeral=False, view=None):
        log.append(content)
    i.followup = NS(send=followup)
    return i


@pytest.fixture
def world(cp, monkeypatch):  # noqa: F811
    cp.settings.hq_bot_token, cp.settings.hq_guild_id = "hq-token", GID
    cp.settings.hq_admin_ids = frozenset({ADMIN})
    guild = Guild()
    changed = []

    async def on_change():
        changed.append(1)

    class Sup:
        restarted = []

        async def restart(self, sid):
            self.restarted.append(sid)
    sup = Sup()
    h = hqmod.HQ(cp.db, cp.box, cp.settings, on_change, sup, client=Client(guild))
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    cp.db.run("UPDATE servers SET status = 'active' WHERE id = ?", (s["id"],))
    return NS(cp=cp, hq=h, guild=guild, cid=cid, sid=s["id"], changed=changed, sup=sup)


def _channel(w, name):
    return next(ch for cat in w.guild.categories for ch in cat.text_channels if ch.name == name)


def test_runs_on_the_shared_bot_and_needs_a_guild(cp):  # noqa: F811
    cp.settings.hq_guild_id, cp.settings.shared_bot_token = GID, "shared"
    cp.settings.hq_bot_token = ""
    h = hqmod.HQ(cp.db, cp.box, cp.settings, None, client=Client(Guild()))
    assert h.token == "shared" and h.on_shared and hqmod.enabled(cp.settings)       # one token for everything
    cp.settings.hq_bot_token = "own"
    assert not hqmod.HQ(cp.db, cp.box, cp.settings, None, client=Client(Guild())).on_shared
    cp.settings.hq_bot_token = cp.settings.shared_bot_token = ""
    with pytest.raises(hqmod.HQError):
        hqmod.HQ(cp.db, cp.box, cp.settings, None, client=Client(Guild()))
    cp.settings.hq_bot_token, cp.settings.hq_guild_id = "other", 0
    with pytest.raises(hqmod.HQError):
        hqmod.HQ(cp.db, cp.box, cp.settings, None, client=Client(Guild()))


def test_shared_bot_never_answers_or_leaves_in_customer_discords(cp):  # noqa: F811
    cp.settings.hq_guild_id, cp.settings.shared_bot_token, cp.settings.hq_bot_token = GID, "shared", ""
    hq_guild = Guild()
    h = hqmod.HQ(cp.db, cp.box, cp.settings, None, client=Client(hq_guild))
    other = NS(id=12345, owner_id=1, left=[])

    async def leave():
        other.left.append(1)
    other.leave = leave
    i = interaction(other, "hq:u:install")
    asyncio.run(h.on_interaction(i))
    asyncio.run(h.on_guild_join(other))
    assert i.log == [] and other.left == []                                 # silent, and it stays


def test_sync_builds_private_channels_and_panels_once(world):
    w = world

    async def go():
        await w.hq.sync()
        await w.hq.sync()                                         # nothing changed: no new messages, no edits
    asyncio.run(go())
    names = [c.name for c in w.guild.categories]
    assert names == [hqmod.HQ_CATEGORY, hqmod.CUSTOMER_CATEGORY]
    for cat in w.guild.categories:                               # hidden from @everyone
        assert cat.overwrites[w.guild.default_role].view_channel is False
    overview = _channel(w, "hq-overview")
    assert len(overview.sent) == 1 and "overview" in overview.sent[0].embed.title
    ch = _channel(w, hqmod.channel_name(w.cid, "a@x.io"))   # customer() uses this e-mail
    assert len(ch.sent) == 2 and ch.sent[0].embed.title.startswith(f"👤 #{w.cid}")
    srv = ch.sent[1]
    assert srv.embed.title == f"🖥️ #{w.sid} Main" and "/panel-" in srv.embed.url and "/admin" not in srv.embed.url
    ids_ = [c.custom_id for c in srv.view.children]
    assert f"hq:s:restart:{w.sid}" in ids_ and f"hq:m:on:{w.sid}" in ids_
    assert all(m.edits == 0 for m in ch.sent)


def test_buttons_change_things_only_for_admins(world):
    w = world

    async def go():
        await w.hq.sync()
        i = interaction(w.guild, f"hq:s:restart:{w.sid}", user_id=STRANGER)
        await w.hq.on_interaction(i)
        assert i.log == ["Not for you."] and w.sup.restarted == []
        i = interaction(w.guild, f"hq:s:restart:{w.sid}")
        await w.hq.on_interaction(i)
        assert w.sup.restarted == [w.sid] and "restarting" in i.log[-1]
        await w.hq.on_interaction(interaction(w.guild, f"hq:m:on:{w.sid}"))
        assert w.cp.db.one("SELECT manager_ai FROM servers WHERE id = ?", (w.sid,))["manager_ai"] == 1
        await w.hq.on_interaction(interaction(w.guild, f"hq:c:grant:{w.cid}"))
        await w.hq.on_interaction(interaction(w.guild, f"hq:f:ai:on:{w.cid}"))
        await w.hq.on_interaction(interaction(w.guild, f"hq:s:suspend:{w.sid}"))
        i = interaction(w.guild, "hq:s:purge:999")
        await w.hq.on_interaction(i)
        assert i.log[-1].startswith("❌")
    asyncio.run(go())
    from control_plane import features, slots
    assert slots.slot_limit(w.cp.db, w.cid) == 2 and "ai" in features.active(w.cp.db, w.cid)
    assert w.cp.db.one("SELECT status FROM servers WHERE id = ?", (w.sid,))["status"] == "suspended"
    srv = _channel(w, hqmod.channel_name(w.cid, "a@x.io")).sent[1]
    assert srv.edits >= 1 and f"hq:s:reactivate:{w.sid}" in [c.custom_id for c in srv.view.children]
    audit = [r["actor"] for r in w.cp.db.q("SELECT actor FROM audit_log WHERE action LIKE 'server.%'")]
    assert f"discord:{ADMIN}" in audit and len(w.changed) >= 4


def test_typing_in_a_customer_channel_talks_to_their_management_ai(world, monkeypatch):
    w = world
    calls = []

    async def fake_ask(db, box, sid, text, history, *, allow_risky):
        calls.append((sid, text, allow_risky, len(history)))
        if allow_risky:
            return {"ok": True, "answer": "Time set.", "done": ["⏰ time 12"], "asked": ["✔️ env.time 12"]}
        return {"ok": True, "answer": "Not allowed.", "done": [], "asked": ["✖️ env.time 12"]}
    monkeypatch.setattr(manage_ai, "ask", fake_ask)

    async def go():
        await w.hq.sync()
        ch = _channel(w, hqmod.channel_name(w.cid, "a@x.io"))
        author = NS(id=ADMIN, guild=w.guild, bot=False, roles=[])
        msg = Msg(ch, "set the time to noon")
        msg.author, msg.guild = author, w.guild
        stranger = Msg(ch, "set the time")
        stranger.author, stranger.guild = NS(id=STRANGER, guild=w.guild, bot=False, roles=[]), w.guild
        await w.hq.on_message(stranger)
        assert calls == []                                                 # not an admin: ignored
        await w.hq.on_message(msg)
        reply = ch.sent[-1]
        assert calls == [(w.sid, "set the time to noon", False, 0)] and "Not done (risky)" in reply.content
        button = reply.view.children[0].custom_id
        await w.hq.on_interaction(interaction(w.guild, button, user_id=STRANGER, channel=ch))
        assert len(calls) == 1
        await w.hq.on_interaction(interaction(w.guild, button, channel=ch))
        assert calls[-1] == (w.sid, "set the time to noon", True, 0) and "Time set." in ch.sent[-1].content
        # the HQ AI channel needs "#<id>"
        ai = _channel(w, "hq-ai")
        m = Msg(ai, "list binds")
        m.author, m.guild = author, w.guild
        await w.hq.on_message(m)
        assert "Start with the server" in ai.sent[-1].content
        m = Msg(ai, f"#{w.sid} list binds")
        m.author, m.guild = author, w.guild
        await w.hq.on_message(m)
        assert calls[-1][:2] == (w.sid, "list binds")
    asyncio.run(go())
    assert any(r["action"] == "manager_ai.request" and r["actor"] == f"discord:{ADMIN}"
               for r in w.cp.db.q("SELECT * FROM audit_log"))


def test_customer_channel_with_two_servers_asks_which(world):
    w = world
    w.cp.db.run("UPDATE customers SET base_slots = 2 WHERE id = ?", (w.cid,))
    s2 = provisioning.provision_server(w.cp.db, w.cp.box, w.cp.settings, w.cid, "Second", actor="t")
    asyncio.run(w.hq.sync())
    ch = _channel(w, hqmod.channel_name(w.cid, "a@x.io"))
    assert w.hq._pick_server(ch.id, "hi")[2].startswith("Which server?")
    assert w.hq._pick_server(ch.id, f"#{s2['id']} hi")[:2] == (s2["id"], "hi")
    assert "isn't one of this customer's" in w.hq._pick_server(ch.id, "#9999 hi")[2]


def member(w, uid, name, key=None):
    m = Member(w.guild, uid, name)
    w.guild.members[uid] = m
    if key:
        role = w.guild.get_role(w.hq.role_ids[key])
        m.roles.append(role)
        role.members.append(m)
    return m


def test_roles_and_every_channel_are_created_and_repaired(world):
    w = world
    asyncio.run(w.hq.sync())
    assert sorted(r.name for r in w.guild.roles) == ["HQ Admin", "HQ Support", "HQ Viewer"]
    hq_cat = w.guild.categories[0]
    assert [c.name for c in hq_cat.text_channels] == list(hqmod.CHANNELS)
    roles = {k: w.guild.get_role(v) for k, v in w.hq.role_ids.items()}
    audit, ai = _channel(w, "hq-audit"), _channel(w, "hq-ai")
    assert roles["admin"] in audit.overwrites and roles["viewer"] not in audit.overwrites      # admins only
    assert ai.overwrites[roles["support"]].send_messages is True
    assert ai.overwrites[roles["viewer"]].send_messages is False and ai.overwrites[roles["viewer"]].view_channel
    cust = _channel(w, hqmod.channel_name(w.cid, "a@x.io"))
    assert cust.overwrites[roles["viewer"]].send_messages is False
    # somebody opens the audit channel to everyone → repaired; the roles aren't made twice
    audit.overwrites = {}
    asyncio.run(w.hq.sync())
    assert audit.repaired == 1 and len(w.guild.roles) == 3
    assert "HQ Support" in _channel(w, "hq-staff").sent[0].embed.fields[1].name


def test_staff_levels(world, monkeypatch):
    w = world
    asked = []

    async def fake_ask(db, box, sid, text, history, *, allow_risky):
        asked.append(text)
        return {"ok": True, "answer": "ok", "done": [], "asked": []}
    monkeypatch.setattr(manage_ai, "ask", fake_ask)
    asyncio.run(w.hq.sync())
    viewer, support = member(w, 31, "Vic", "viewer"), member(w, 32, "Sue", "support")
    ch = _channel(w, hqmod.channel_name(w.cid, "a@x.io"))

    async def go():
        i = interaction(w.guild, f"hq:s:restart:{w.sid}", user_id=31)
        i.user = viewer
        await w.hq.on_interaction(i)
        assert "can't do that" in i.log[-1] and w.sup.restarted == []
        i = interaction(w.guild, f"hq:s:restart:{w.sid}")
        i.user = support
        await w.hq.on_interaction(i)
        assert w.sup.restarted == [w.sid]
        for cid_ in (f"hq:s:suspend:{w.sid}", f"hq:c:grant:{w.cid}", "hq:u:install", "hq:r:admin"):
            i = interaction(w.guild, cid_)
            i.user = support
            await w.hq.on_interaction(i)
            assert "can't do that" in i.log[-1], cid_
        for who, text in ((viewer, "list kits"), (support, "list binds")):
            m = Msg(ch, text)
            m.author, m.guild = who, w.guild
            await w.hq.on_message(m)
    asyncio.run(go())
    assert asked == ["list binds"]                                      # the viewer only reads


def test_staff_panel_gives_and_takes_roles(world):
    w = world
    asyncio.run(w.hq.sync())
    newbie = member(w, 41, "Nia")

    async def go():
        i = interaction(w.guild, "hq:r:support")
        i.data["values"] = ["41"]
        await w.hq.on_interaction(i)
        assert "Nia → HQ Support" in i.log[-1]
        assert [r.name for r in newbie.roles] == ["HQ Support"]
        i = interaction(w.guild, "hq:r:admin")
        i.data["values"] = ["41", "999"]                                  # unknown people are skipped
        await w.hq.on_interaction(i)
        assert [r.name for r in newbie.roles] == ["HQ Admin"]               # one HQ role per person
        i = interaction(w.guild, "hq:r:none")
        i.data["values"] = ["41"]
        await w.hq.on_interaction(i)
        assert newbie.roles == [] and w.hq.level(newbie) == hqmod.NONE
    asyncio.run(go())
    staff = _channel(w, "hq-staff").sent[0]
    assert staff.edits >= 1
    assert any(r["action"] == "hq.staff.support" for r in w.cp.db.q("SELECT action FROM audit_log"))


def test_alerts_and_audit_feed(world):
    w = world
    asyncio.run(w.hq.sync())                                            # first look: nothing posted
    alerts, audit = _channel(w, "hq-alerts"), _channel(w, "hq-audit")
    assert alerts.sent == [] and audit.sent == []
    w.cp.db.run("INSERT OR REPLACE INTO health (server_id, checked_at, process, discord_ready, rcon_connected) "
                "VALUES (?, 1, 'crashloop', 0, 0)", (w.sid,))
    customer(w.cp, "new@x.io")
    w.cp.db.audit("owner:boss", "server.restart", server_id=w.sid)
    asyncio.run(w.hq.sync())
    text = "\n".join(m.content for m in alerts.sent)
    assert "New customer" in text and "new@x.io" in text and "crashloop" in text
    feed = "\n".join(m.content for m in audit.sent)
    assert "server.restart" in feed and "owner:boss" in feed
    asyncio.run(w.hq.sync())                                            # nothing new: nothing posted again
    assert len(audit.sent) == 1
