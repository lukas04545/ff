"""Raid statistics, daily/monthly recaps, website staff/bans/wipes pages, discount codes, TikTok, cross-server
profiles."""
import asyncio
import json
import re
from types import SimpleNamespace as NS

from tests.test_plugins import pb, plug  # noqa: F401
from tests.test_webshop import shop  # noqa: F401
from tests.test_kit_requests import env  # noqa: F401


def run(c):
    return asyncio.run(c)


def test_raid_statistics(pb):  # noqa: F811
    p = plug(pb, "raid_alerts", patterns=r"(?P<player>\w+)'s base was raided")
    run(p.handle("Bob's base was raided by rockets"))
    run(p.handle("Bob's base was raided again"))
    run(p.handle("Ann's base was raided"))
    run(p.handle("nothing to see"))
    st = p.raid_stats(7)
    assert st["total"] == 3 and st["recent"] == 3
    assert [(r["name"], r["count"]) for r in st["most_raided"]] == [("Bob", 2), ("Ann", 1)]


def test_daily_and_monthly_recaps(pb):  # noqa: F811
    import datetime
    p = plug(pb, "weekly_recap", daily=True, monthly=True, hour=18)
    p.add_playtime(["Ann"], 3600)
    assert p._bucket("day")["playtime"]["Ann"] == 3600 and p._bucket("month")["playtime"]["Ann"] == 3600
    assert "Daily recap" in p.build(period="day").title and "Monthly recap" in p.build(period="month").title
    tz = p.tz
    t0 = int(datetime.datetime(2026, 3, 10, 19, 0, tzinfo=tz).timestamp())
    assert p.due_extra("day", t0) is None                        # first look: remembers the slot
    assert p.due_extra("day", t0 + 3600) is None                 # same day
    assert p.due_extra("day", t0 + 86400) is not None            # next day after 18:00
    assert p.due_extra("month", t0) is None
    first = int(datetime.datetime(2026, 4, 1, 18, 30, tzinfo=tz).timestamp())
    assert p.due_extra("month", first) is not None
    assert run(p.post_recap("day")) is True and p._bucket("day")["playtime"] == {}
    assert p._bucket("month")["playtime"]["Ann"] == 3600         # other periods untouched


def test_website_staff_bans_and_wipes(pb):  # noqa: F811
    w = plug(pb, "website", show_bans=True, staff_extra="Kryvon | Owner")
    from rce.log_parser import BanEvent
    run(w.on_rce_ban_reason("Cheater99", "aimbot"))
    run(w.on_rce_ban(BanEvent("admin", "Cheater99", True)))
    run(w.on_rce_ban(BanEvent("admin", "Bob", True)))
    run(w.on_rce_ban(BanEvent("admin", "Bob", False)))            # unbanned: gone
    assert w.render("", "bans", {})[0] == 200
    page = w.render("", "bans", {})[1]
    assert "Cheater99" in page and "aimbot" in page and "Bob" not in page.split("<h1>Bans</h1>")[1]
    assert "Kryvon" in w.render("", "staff", {})[1] and 'href="/staff"' in w.render("", "staff", {})[1]
    run(w.on_rce_wiped(None))
    assert "Past wipes (1)" in w.render("", "wipes", {})[1]
    off = plug(pb, "website", show_bans=False)
    assert off.render("", "bans", {})[0] == 404


def test_discount_code_in_the_basket(shop, monkeypatch):  # noqa: F811
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    db.add_store_discount("SUMMER20", percent=20, uses=1)
    db.add_store_discount("BIG", amount=5, min_total=50)
    row, off = s.discount("summer20", 9.99)
    assert row["code"] == "SUMMER20" and off == 2.0
    try:
        s.discount("BIG", 9.99)
        raise AssertionError
    except CheckoutError as exc:
        assert "at least" in str(exc)
    r = run(s.checkout("cart", base_url="https://x", cart=[f"k{shop.kit_id}"], player="Gamer7",
                       discount_code="SUMMER20"))
    parent = db.store_order_by("token", r["token"])
    assert parent["total"] == 7.99 and parent["discount_code"] == "SUMMER20" and shop.pp.orders["PP1"]["amount"] == 7.99
    assert db.store_order_children(parent["id"])[0]["total"] == 7.99
    try:                                                        # one use only
        run(s.checkout("cart", base_url="https://x", cart=[f"k{shop.kit_id}"], player="Gamer7",
                       discount_code="SUMMER20"))
        raise AssertionError
    except CheckoutError as exc:
        assert "used up" in str(exc)
    run(s.cancel(r["token"]))                                    # cancelled: the use comes back
    assert db.store_discount("SUMMER20")["uses_left"] == 1


def test_tiktok_live_page():
    from plugins.streamers import parse_streamers, parse_tiktok_live
    streamers, problems = parse_streamers("tiktok | @kryvonrust | Gamer7")
    assert not problems and streamers[0].url == "https://www.tiktok.com/@kryvonrust/live"
    live = parse_tiktok_live('{"liveRoomUserInfo":{"liveRoom":{"status":2,"title":"Raiding!","userCount":42}}}')
    assert live.title == "Raiding!" and live.viewers == 42
    assert parse_tiktok_live('{"liveRoomUserInfo":{"liveRoom":{"status":4}}}') is None


def test_cross_server_profile(env, monkeypatch):  # noqa: F811
    import servers
    from cogs.economy import Economy
    eco = Economy.__new__(Economy)
    eco.bot = env.bot

    async def stats(sid, player):
        return {"kills": 10, "deaths": 5, "playtime_seconds": 7200} if sid == "eu2" else None
    monkeypatch.setattr(servers, "player_stats_on", stats)
    monkeypatch.setattr(servers, "server_name", lambda sid: {"eu2": "EU 2", "us": "US"}[sid])
    import discord
    embed = discord.Embed()
    run(eco.add_other_servers(embed, "Gamer7", ["eu2", "us"]))
    assert embed.fields[0].name == "🌍 On our other servers" and "**EU 2** – 10 kills" in embed.fields[0].value
    assert "US" not in embed.fields[0].value
