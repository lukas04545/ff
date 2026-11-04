"""HQ customer tickets: open a private ticket, link the account with a code, an AI assistant sets the bot up for
that customer only, a package role, and a one-time login link once it's set up."""
import asyncio
import json
from types import SimpleNamespace as NS

from control_plane import billing, hq_tickets, provisioning
from tests.test_control_plane import cp, customer, paid  # noqa: F401
from tests.test_hq import ADMIN, GID, Member, _channel, interaction, world  # noqa: F401

USER = 777


def run(coro):
    return asyncio.run(coro)


def test_link_codes_are_secret_and_stable(cp):  # noqa: F811
    a, b = customer(cp, "a@x.io"), customer(cp, "b@x.io")
    code = hq_tickets.link_code(cp.settings, a)
    assert code == hq_tickets.link_code(cp.settings, a) and code != hq_tickets.link_code(cp.settings, b)
    assert hq_tickets.customer_for_code(cp.db, cp.settings, code.lower().replace("-", " ")) == a
    assert hq_tickets.customer_for_code(cp.db, cp.settings, "0000-0000-0000") is None
    cp.settings.session_secret = "x" * 40                      # another platform: another code
    assert hq_tickets.link_code(cp.settings, a) != code


def test_tools_only_ever_touch_the_linked_customer(cp):  # noqa: F811
    mine, other = customer(cp, "me@x.io"), customer(cp, "other@x.io")
    paid(cp, mine)
    paid(cp, other)
    theirs = provisioning.provision_server(cp.db, cp.box, cp.settings, other, "Theirs", actor="t")
    cp.settings.shared_bot_token, cp.settings.shared_client_id = "shared", "123"
    checks = []

    async def verify(gid, uid):
        checks.append((gid, uid))
        return None if gid == "4242" else "The bot isn't in that Discord server yet."

    t = hq_tickets.TicketTools(cp.db, cp.box, cp.settings, USER, verify_guild=verify,
                               password_link=lambda sid, by: f"https://x/saas/password?t=T{sid}")
    assert not run(t.run("account_status", {}))["ok"]                                    # not linked yet
    assert not run(t.run("link_account", {"code": "nope"}))["ok"]
    out = run(t.run("link_account", {"code": hq_tickets.link_code(cp.settings, mine)}))
    assert out["ok"] and out["running"] and t.customer == mine
    sid = run(t.run("create_server", {"name": "My EU"}))["server_id"]
    assert "discord.com/oauth2" in run(t.run("bot_invite_link", {}))["invite"]
    assert not run(t.run("login_link", {"server_id": sid}))["ok"]                         # not set up yet
    assert "isn't one of yours" in run(t.run("connect_discord", {"server_id": theirs["id"],
                                                                  "discord_server_id": "4242"}))["error"]
    assert "isn't one of yours" in run(t.run("login_link", {"server_id": theirs["id"]}))["error"]
    assert not run(t.run("connect_discord", {"server_id": sid, "discord_server_id": "999"}))["ok"]
    assert run(t.run("connect_discord", {"server_id": sid, "discord_server_id": "4242"}))["ok"]
    assert checks[-1] == ("4242", USER)                                                  # checked with the bot
    assert cp.db.one("SELECT guild_id FROM servers WHERE id = ?", (sid,))["guild_id"] == "4242"
    cp.db.run("UPDATE servers SET status = 'active' WHERE id = ?", (sid,))
    link = run(t.run("login_link", {"server_id": sid}))
    assert link["ok"] and link["link"].endswith(f"T{sid}")
    assert cp.db.one("SELECT 1 FROM audit_log WHERE action = 'server.password_link' AND server_id = ?", (sid,))


def test_package_role_label(cp):  # noqa: F811
    cid = customer(cp, "r@x.io")
    assert hq_tickets.subscription_label(cp.db, cid) is None                              # nothing runs: no role
    billing.start_trial(cp.db, cid, 7, actor="t")
    assert hq_tickets.subscription_label(cp.db, cid) == "🧪 Free trial"
    paid(cp, cid)
    assert hq_tickets.subscription_label(cp.db, cid) == "✅ Customer"


class FakeAI:
    """Answers like DeepSeek: first a tool call, then text."""

    def __init__(self, calls):
        self.calls, self.seen = list(calls), []

    async def chat(self, msgs, tools=None, json_mode=True, max_tokens=0):
        self.seen.append(msgs)
        if self.calls:
            name, args = self.calls.pop(0)
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                   "function": {"name": name, "arguments": json.dumps(args)}}]}
        return {"content": "Done – you're linked!", "tool_calls": []}


def test_ticket_open_link_and_role(world, monkeypatch):  # noqa: F811
    w = world
    run(w.hq.sync())
    start = _channel(w, hq_tickets.START_CHANNEL)
    assert start.sent[0].view.children[0].custom_id == "hq:t:open"
    user = Member(w.guild, USER, "alex")
    user.name = "alex"
    w.guild.members[USER] = user
    i = interaction(w.guild, "hq:t:open", user_id=USER)
    i.user = user
    run(w.hq.on_interaction(i))
    ticket = _channel(w, "ticket-alex")
    assert ticket.overwrites[user].view_channel and ticket.overwrites[w.guild.default_role].view_channel is False
    assert "link code" in ticket.sent[0].content and ticket.mention in i.log[-1]
    i2 = interaction(w.guild, "hq:t:open", user_id=USER)
    i2.user = user
    run(w.hq.on_interaction(i2))
    assert len([c for c in w.guild.categories for ch in c.text_channels if ch.name == "ticket-alex"]) == 1  # one only
    # the assistant links the account; the customer gets their package role
    ai = FakeAI([("link_account", {"code": hq_tickets.link_code(w.cp.settings, w.cid)})])
    monkeypatch.setattr(w.hq, "_ticket_ai", lambda: ai)
    msg = NS(author=user, channel=ticket, content="my code is …", guild=w.guild)

    async def reply(content, mention_author=False):
        return await ticket.send(content)
    msg.reply = reply
    run(w.hq.on_message(msg))
    assert ticket.sent[-1].content == "Done – you're linked!"
    assert hq_tickets.linked_customer(w.cp.db, USER) == w.cid
    assert [r.name for r in user.roles] == ["✅ Customer"]
    # someone else typing in the ticket (staff) doesn't make the assistant answer
    staff = NS(author=NS(id=ADMIN, bot=False, roles=[], guild=w.guild), channel=ticket, content="hi", guild=w.guild)
    n = len(ticket.sent)
    run(w.hq.on_message(staff))
    assert len(ticket.sent) == n
    # closing
    i3 = interaction(w.guild, "hq:t:close", user_id=USER, channel=ticket)
    i3.user = user
    run(w.hq.on_interaction(i3))
    assert getattr(ticket, "deleted", False)
    assert w.cp.db.one("SELECT 1 FROM hq_tickets WHERE user_id = ?", (USER,)) is None


def test_free_week_once_per_discord_account(cp):  # noqa: F811
    import time as _time
    out = hq_tickets.claim_trial(cp.db, cp.box, cp.settings, USER, "Alex The Great")
    assert out["ok"] and out["username"] == "alexthegreat" and len(out["password"]) >= 12
    cid = hq_tickets.linked_customer(cp.db, USER)
    sub = cp.db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (cid,))
    assert sub["status"] == "trial" and 6.9 < (sub["current_period_end"] - _time.time()) / 86400 <= 7
    assert billing.subscription_ok(cp.db, cid) and hq_tickets.subscription_label(cp.db, cid) == "🧪 Free trial"
    from web.users import verify_password
    assert verify_password(out["password"], cp.db.one("SELECT password_hash FROM customers WHERE id = ?",
                                                      (cid,))["password_hash"])
    assert "already had" in hq_tickets.claim_trial(cp.db, cp.box, cp.settings, USER, "x")["error"]
    new_id = int((_time.time() - hq_tickets.DISCORD_EPOCH) * 1000) << 22          # an account made today
    assert "days old" in hq_tickets.claim_trial(cp.db, cp.box, cp.settings, new_id, "fresh")["error"]
    other = hq_tickets.claim_trial(cp.db, cp.box, cp.settings, 778, "Alex The Great")
    assert other["ok"] and other["username"] == "alexthegreat2"                  # names stay unique


def test_trial_button_in_the_ticket_and_roles_made_up_front(world):  # noqa: F811
    w = world
    run(w.hq.sync())
    labels = {r.name for r in w.guild.roles}
    assert "🧪 Free trial" in labels and "✅ Customer" in labels                   # made before anyone has them
    assert all(f"📦 {p['name']}" in labels for p in hq_tickets.plans.listed(w.cp.db))
    user = Member(w.guild, 779, "newbie")
    user.name = "newbie"
    w.guild.members[779] = user
    i = interaction(w.guild, "hq:t:open", user_id=779)
    i.user = user
    run(w.hq.on_interaction(i))
    ticket = _channel(w, "ticket-newbie")
    assert "hq:t:trial" in [c.custom_id for c in ticket.sent[0].view.children]
    i2 = interaction(w.guild, "hq:t:trial", user_id=779, channel=ticket)
    i2.user = user
    run(w.hq.on_interaction(i2))
    assert "free week is on" in i2.log[-1] and "Password: `" in i2.log[-1]
    assert [r.name for r in user.roles] == ["🧪 Free trial"]
    i3 = interaction(w.guild, "hq:t:trial", user_id=779, channel=ticket)
    i3.user = user
    run(w.hq.on_interaction(i3))
    assert "already" in i3.log[-1]
