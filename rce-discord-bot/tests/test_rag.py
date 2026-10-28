"""Knowledge base RAG: split into sections, BM25 search with stemming and typo tolerance, only matching sections in
the prompt (whole file when it's small), the AI's search tool, and the settings switch."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

from support.rag import KnowledgeIndex, split, tokens
from tests.test_kit_requests import env  # noqa: F401

KB = """# Kryvon 10x
Welcome to Kryvon, a 10x server. Join through the Discord.

## Wipes
Map wipes every Thursday at 6pm UTC. Blueprints wipe monthly.

## Rules
No racism. Max team size is 4. No cheating or exploiting.

## Kits
The starter kit is free every 2 hours. VIP kits cost 10 EUR in the store.

## Raiding
Offline raiding is not allowed – bases get a protection zone while the team is offline.
"""
FILLER = "\n".join(f"## Topic {n}\nSomething unrelated number {n} about gardening and cooking recipes.\n"
                   for n in range(60))


def test_split_by_headings_and_size():
    chunks = split(KB)
    assert [c.heading for c in chunks] == ["Kryvon 10x", "Kryvon 10x › Wipes", "Kryvon 10x › Rules",
                                           "Kryvon 10x › Kits", "Kryvon 10x › Raiding"]
    long = "# Big\n" + "\n\n".join("Paragraph %d. " % i + "word " * 60 for i in range(10))
    parts = split(long, max_chars=500)
    assert len(parts) > 3 and all(len(c.text) <= 500 for c in parts) and all(c.heading == "Big" for c in parts)
    assert split("") == [] and split("just text, no heading")[0].heading == ""


def test_search_stemming_typos_and_accents():
    index = KnowledgeIndex(KB)
    best = lambda q: index.search(q, 1)[0][1].heading.split(" › ")[-1]
    assert best("when is the next wipe?") == "Wipes"
    assert best("wipee") == "Wipes"                                        # typo
    assert best("how much does VIP cost") == "Kits"
    assert best("can I offline raid someone") == "Raiding"
    assert best("max team size") == "Rules"
    assert index.search("banana spaceship", 3) == []
    assert tokens("Wipes wiped Räume") == ["wip", "wip", "raum"]


def test_select_sends_whole_small_file_and_only_matches_of_a_big_one():
    small = KnowledgeIndex(KB)
    text, info = small.select("wipe", budget=6000)
    assert text == KB and info["mode"] == "all"
    big = KnowledgeIndex(KB + FILLER)
    text, info = big.select("when is the next wipe", budget=1500)
    assert info["mode"] == "rag" and info["used"] < info["total"] and len(text) <= 1500 + 300
    assert "Thursday at 6pm" in text and "Welcome to Kryvon" in text            # the match + the first section
    assert "gardening" not in text and "search_knowledge_base" in text
    assert text.index("Welcome") < text.index("Thursday")                      # in the file's order
    assert "search_knowledge_base" not in big.select("wipe", budget=1500, tool=False)[0]
    found = big.lookup("vip price")
    assert found[0]["section"].endswith("Kits") and "10 EUR" in found[0]["text"]


def test_ticket_ai_gets_matching_sections_and_the_search_tool(env):  # noqa: F811
    env.bot.config.support_knowledge_file.write_text(KB + FILLER, encoding="utf-8")
    env.bot.config = replace(env.bot.config, knowledge_rag=True, knowledge_rag_chars=1500)
    seen = {}
    replies = iter([
        {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "search_knowledge_base", "arguments": '{"query": "vip price"}'}}]},
        {"content": '{"reply": "Wipes are on Thursdays, VIP is 10 EUR.", "escalate": false, "reason": ""}',
         "tool_calls": []},
    ])
    tool_results = []

    async def chat(messages, tools=None, json_mode=True):
        seen.setdefault("system", messages[0]["content"])
        seen["tools"] = {t["function"]["name"] for t in tools or []}
        tool_results.extend(m["content"] for m in messages if m.get("role") == "tool")
        return next(replies)
    env.tickets.ai = SimpleNamespace(enabled=True, model="test-model", chat=chat)
    sent = []

    async def defer(**kw):
        return None

    async def followup_send(text, **kw):
        sent.append(text)
    interaction = SimpleNamespace(user=env.staff, guild=env.channel.guild,
                                  response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send))
    env.staff.display_name = "Mod"
    asyncio.run(env.tickets.ai_test.callback(env.tickets, interaction, "when is the next wipe?"))
    assert "Thursday at 6pm" in seen["system"] and "gardening" not in seen["system"]
    assert "search_knowledge_base" in seen["tools"]
    assert any("10 EUR" in r for r in tool_results)                           # the tool really searched
    assert "search_knowledge_base `vip price`" in sent[0] and "sections (RAG)" in sent[0]
    # RAG off: the whole file again (as before), no search tool
    env.bot.config = replace(env.bot.config, knowledge_rag=False)
    text, info = env.tickets.knowledge_for("wipe")
    assert info["mode"] == "all" and "Welcome to Kryvon" in text and "Topic 0" in text


def test_knowledge_page_shows_rag_and_tests_a_question(env):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    env.bot.config.support_knowledge_file.write_text(KB + FILLER, encoding="utf-8")
    env.bot.config = replace(env.bot.config, knowledge_rag=True, knowledge_rag_chars=1500, web_password="secret123")
    H = {"X-Requested-With": "rce-web"}

    async def main():
        from web.server import WebInterface
        tmp = env.bot.config.database_path.parent
        w = WebInterface(env.bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/knowledge")).json()
            assert d["rag"]["enabled"] and d["rag"]["sections"] > 60 and not d["rag"]["whole"]
            s = await (await c.get("/api/knowledge/search", params={"q": "when is the wipe"})).json()
            assert s["info"]["mode"] == "rag" and s["results"][0]["section"].endswith("Wipes")
            big = "# Big\n" + "fact. " * 20000                               # > 50,000 – fine with RAG now
            assert (await c.put("/api/knowledge", json={"text": big}, headers=H)).status == 200
        finally:
            await c.close()
            await env.bot.rcon.close()
    asyncio.run(main())
