"""Tests für Prompt-Logik und DeepSeek-Client (gegen einen lokalen Fake-Server)."""
import asyncio

import pytest
from aiohttp import web

from storage import Storage
from support.deepseek import DeepSeekClient, DeepSeekError
from support.prompt import build_messages, build_system_prompt, parse_answer


def test_parse_answer_variants():
    a = parse_answer('{"reply": "Wipe ist donnerstags.", "escalate": false, "reason": ""}')
    assert (a.reply, a.escalate) == ("Wipe ist donnerstags.", False)
    b = parse_answer('{"reply": "Ich hole das Team.", "escalate": "true", "reason": "Ban-Einspruch"}')
    assert b.escalate and b.reason == "Ban-Einspruch"
    # Leere Antwort -> immer Staff
    c = parse_answer('{"reply": "", "escalate": false}')
    assert c.escalate and c.reason
    # Kein JSON -> Text wird genutzt, ohne Eskalation
    d = parse_answer("Einfach Text")
    assert d.reply == "Einfach Text" and not d.escalate
    assert parse_answer("[1, 2]").escalate


def test_build_messages_merges_and_ends_with_user():
    system = build_system_prompt(server_name="Test", topic="Hilfe", ingame_name=None, status="", knowledge="")
    assert "json" in system.lower()  # DeepSeek-JSON-Modus verlangt das Wort "json" im Prompt
    msgs = build_messages(system, [("user", "a"), ("user", "b"), ("assistant", "c")])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "a\nb"


async def _fake_api(handler, balance_handler=None):
    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    if balance_handler is not None:
        app.router.add_get("/user/balance", balance_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def test_deepseek_client_success_and_errors():
    seen = {}

    async def ok(request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"message": {"role": "assistant",
                                                            "content": '{"reply": "Hi", "escalate": false}'},
                                               "finish_reason": "stop"}]})

    async def balance(request):
        return web.json_response({"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "4.20", "granted_balance": "0.00", "topped_up_balance": "4.20"}]})

    async def broke(request):
        return web.json_response({"error": {"message": "Insufficient Balance"}}, status=402)

    async def scenario():
        runner, url = await _fake_api(ok, balance)
        client = DeepSeekClient("sk-test", model="deepseek-flash", base_url=url)
        assert (await client.balance())["balance_infos"][0]["total_balance"] == "4.20"
        assert parse_answer(await client.chat_json([{"role": "user", "content": "json bitte"}])).reply == "Hi"
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"]["response_format"] == {"type": "json_object"}
        assert seen["body"]["model"] == "deepseek-flash"
        await client.close()
        await runner.cleanup()

        runner, url = await _fake_api(broke)
        client = DeepSeekClient("sk-test", model="m", base_url=url)
        with pytest.raises(DeepSeekError, match="402"):
            await client.chat_json([{"role": "user", "content": "x"}])
        await client.close()
        await runner.cleanup()

        with pytest.raises(DeepSeekError):
            await DeepSeekClient("", model="m", base_url=url).chat_json([])

    asyncio.run(scenario())


def test_ticket_storage(tmp_path):
    db = Storage(tmp_path / "bot.db")
    tid = db.create_ticket(42, "Ich komme nicht auf den Server", "Bob")
    assert db.open_ticket_of_user(42) is None  # noch kein Channel
    db.update_ticket(tid, channel_id=1000)
    assert db.ticket_by_channel(1000)["id"] == tid
    assert db.open_ticket_of_user(42)["id"] == tid
    assert db.increment_ticket_ai_replies(tid) == 1
    with pytest.raises(ValueError):
        db.update_ticket(tid, status="closed")  # nur erlaubte Felder
    db.close_ticket(tid, "Staff#1", "gelöst")
    assert db.ticket_by_channel(1000) is None and db.ticket(tid)["close_reason"] == "gelöst"
    db.close()
