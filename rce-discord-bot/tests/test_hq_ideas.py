"""The suggestions AI in the HQ customers' chat: it saves ideas (or counts votes), keeps a pinned board, staff change
statuses (AI or owner panel) and the person who suggested it gets a notice."""
import asyncio
import json
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from control_plane import hq_ideas
from tests.test_control_plane import cp, csrf_of, customer, paid  # noqa: F401
from tests.test_hq import ADMIN, Member, Msg, _channel, world  # noqa: F401

USER, OTHER = 701, 702


def run(coro):
    return asyncio.run(coro)


class FakeAI:
    """Tool calls first (one per round), then a text answer."""

    def __init__(self, calls, final="📝 Saved as #1 – thanks!"):
        self.calls, self.final, self.seen = list(calls), final, []

    async def chat(self, msgs, tools=None, json_mode=True, max_tokens=0):
        self.seen.append((msgs, [t["function"]["name"] for t in tools or []]))
        if self.calls:
            name, args = self.calls.pop(0)
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                   "function": {"name": name, "arguments": json.dumps(args)}}]}
        return {"content": self.final, "tool_calls": []}


def say(w, ch, author, content, mentions=()):
    msg = NS(author=author, channel=ch, content=content, guild=w.guild, mentions=list(mentions))

    async def reply(text, mention_author=False):
        return await ch.send(text)
    msg.reply = reply
    run(w.hq.on_message(msg))


def test_ideas_are_collected_voted_and_their_status_announced(world, monkeypatch):  # noqa: F811
    w = world
    pins = []

    async def pin(self, reason=None):
        pins.append(self.id)
    monkeypatch.setattr(Msg, "pin", pin, raising=False)
    run(w.hq.sync())
    ch = _channel(w, "customer-chat")
    assert ch.overwrites[w.guild.default_role].view_channel is False              # customers + staff only
    board = ch.sent[0]
    assert "Suggestions" in board.embed.title and pins == [board.id]
    alex, sam = Member(w.guild, USER, "alex"), Member(w.guild, OTHER, "sam")
    # an idea → saved
    ai = FakeAI([("list_suggestions", {"query": "raid alerts"}),
                 ("add_suggestion", {"text": "Raid alerts in a Discord DM"})])
    monkeypatch.setattr(w.hq, "_ticket_ai", lambda: ai)
    say(w, ch, alex, "it would be cool to get raid alerts by DM")
    assert ch.sent[-1].content.startswith("📝 Saved")
    s = hq_ideas.listing(w.cp.db)[0]
    assert s["text"] == "Raid alerts in a Discord DM" and s["status"] == "new" and s["author_id"] == USER
    assert "set_status" not in ai.seen[0][1]                                       # customers can't change statuses
    assert "Raid alerts" in board.embed.fields[0].value                            # the board updates in place
    # someone else wants it too → a vote
    monkeypatch.setattr(w.hq, "_ticket_ai", lambda: FakeAI([("support_suggestion", {"id": s["id"]})], "👍 Counted!"))
    say(w, ch, sam, "yes please, raid DMs!")
    assert hq_ideas.get(w.cp.db, s["id"])["votes"] == 2 and "👍 2" in board.embed.fields[0].value
    # chatter: the AI stays silent
    n = len(ch.sent)
    monkeypatch.setattr(w.hq, "_ticket_ai", lambda: FakeAI([], hq_ideas.SILENT))
    say(w, ch, sam, "gg alex")
    assert len(ch.sent) == n
    # a customer asking the AI to change a status gets nothing done
    out = hq_ideas.IdeaTools(w.cp.db, USER, "alex").run("set_status", {"id": s["id"], "status": "done"})
    assert not out["ok"] and hq_ideas.get(w.cp.db, s["id"])["status"] == "new"
    # staff talking in the channel without @mentioning the bot: ignored
    staff = NS(id=ADMIN, bot=False, roles=[], guild=w.guild, display_name="Lukas")
    ai = FakeAI([("set_status", {"id": s["id"], "status": "in_progress", "note": "coming next update"})], "Done.")
    monkeypatch.setattr(w.hq, "_ticket_ai", lambda: ai)
    say(w, ch, staff, "@Customer ask me anything")
    assert ai.seen == []
    # … with the mention: staff can set the status; the author gets a notice
    w.hq.client.user = me = NS(id=2)
    say(w, ch, staff, "<@2> #1 is in progress", mentions=[me])
    assert "set_status" in ai.seen[0][1]
    assert hq_ideas.get(w.cp.db, s["id"])["status"] == "in_progress"
    notice = next(m for m in ch.sent if m.content and m.content.startswith("🛠️ In progress"))
    assert f"<@{USER}>" in notice.content and "coming next update" in notice.content
    assert board.embed.fields[0].name.startswith("🛠️ In progress")
    run(w.hq.sync())                                                               # announced once only
    assert sum(1 for m in ch.sent if m.content and m.content.startswith("🛠️")) == 1


def test_owner_panel_changes_a_status(cp):  # noqa: F811
    from control_plane import websettings
    from control_plane.web import build_owner
    s = hq_ideas.add(cp.db, USER, "alex", "A wipe countdown on the website")

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await c.get("/suggestions")).text()
            assert "A wipe countdown on the website" in page and "Suggestions" in page
            r = await c.post(f"/suggestions/{s['id']}", data={"csrf": csrf_of(page), "status": "done",
                                                               "note": "Live now"}, allow_redirects=False)
            assert r.status == 303
            got = hq_ideas.get(cp.db, s["id"])
            assert got["status"] == "done" and got["note"] == "Live now"
            assert [x["id"] for x in hq_ideas.unannounced(cp.db)] == [s["id"]]     # the HQ posts the notice
        finally:
            await c.close()
    run(scenario())


def test_docs_search_finds_the_readme_section():
    hits = hq_ideas.search_docs("battle royale")
    assert hits and "Battle royale" in hits[0]["section"]
    assert hq_ideas.search_docs("") == []
