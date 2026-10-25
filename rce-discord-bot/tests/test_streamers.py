"""Streamers: who's live (Twitch API, Kick, YouTube /live page), Discord announcements, the website's Streams page."""
import asyncio
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

from plugins import streamers as sm
from tests.test_plugins import pb  # noqa: F401

H = {"X-Requested-With": "rce-web"}
LIST = "twitch | KryvonLive | Gamer7\nkick | kryvon\nyoutube | KryvonRust\nnope | x\n# comment"


def test_streamer_list_and_parsers():
    streamers, problems = sm.parse_streamers(LIST)
    assert [(s.platform, s.channel, s.player) for s in streamers] == [
        ("twitch", "KryvonLive", "Gamer7"), ("kick", "kryvon", ""), ("youtube", "@KryvonRust", "")]
    assert len(problems) == 1 and "Line 4" in problems[0]
    assert streamers[2].url == "https://www.youtube.com/@KryvonRust" and streamers[0].key == "twitch:kryvonlive"
    html = ('<link rel="canonical" href="https://www.youtube.com/watch?v=abcDEF12345">'
            '<meta property="og:title" content="Raiding &amp; farming"> "isLiveNow":true "concurrentViewers":"42"')
    live = sm.parse_youtube_live(html)
    assert live.video_id == "abcDEF12345" and live.title == "Raiding & farming" and live.viewers == 42
    assert sm.parse_youtube_live(html.replace('"isLiveNow":true', '"isLiveNow":false')) is None
    k = sm.parse_kick({"livestream": {"is_live": True, "session_title": "Wipe day", "viewer_count": 7,
                                      "categories": [{"name": "Rust"}], "thumbnail": {"url": "https://x.kick.com/t.jpg"}}})
    assert (k.title, k.viewers, k.game) == ("Wipe day", 7, "Rust") and sm.parse_kick({"livestream": None}) is None
    t = sm.parse_twitch({"data": [{"type": "live", "user_login": "KryvonLive", "title": "hi", "game_name": "Rust",
                                   "viewer_count": 3, "thumbnail_url": "https://static-cdn.jtvnw.net/{width}x{height}.jpg"}]})
    assert t["kryvonlive"].thumbnail.endswith("640x360.jpg")


@pytest.fixture
def st(pb, monkeypatch):  # noqa: F811
    posted, edits = [], []

    class Msg:
        def __init__(self, mid, content, embed):
            self.id, self.content, self.embed = mid, content, embed

        async def edit(self, content="same", embed=None):
            edits.append((self.id, embed))
            self.embed = embed

    class Channel:
        id = 77
        msgs = {}

        async def send(self, content=None, embed=None, allowed_mentions=None, **kw):
            m = Msg(1000 + len(posted), content, embed)
            posted.append((content, embed, allowed_mentions))
            self.msgs[m.id] = m
            return m

        async def fetch_message(self, mid):
            return self.msgs[mid]
    channel = Channel()
    monkeypatch.setattr(pb.bot, "get_channel", lambda cid: channel if int(cid) == 77 else None)

    async def setup():
        await pb.m.load_all()
        await pb.m.set_enabled("streamers", True)
        await pb.m.save_settings("streamers", {"streamers": LIST, "channel": "77", "role": "55"})
    asyncio.run(setup())
    p = pb.m.running("streamers")
    state = {"live": {}}

    async def fake_fetch():
        return dict(state["live"])
    monkeypatch.setattr(p, "fetch_live", fake_fetch)
    return NS(pb=pb, p=p, posted=posted, edits=edits, state=state)


def test_go_live_update_and_end_on_discord(st):
    p, state = st.p, st.state
    state["live"] = {"twitch:kryvonlive": sm.Live(title="Wipe day!", game="Rust", viewers=12,
                                                  thumbnail="https://static-cdn.jtvnw.net/x.jpg")}
    asyncio.run(p.check())
    content, embed, mentions = st.posted[0]
    assert "KryvonLive" in content and "<@&55>" in content and embed.title == "Wipe day!"
    assert [r.id for r in mentions.roles] == [55] and not mentions.everyone       # only the chosen role
    assert p.status()["Live now"] == 1
    state["live"]["twitch:kryvonlive"].viewers = 30
    asyncio.run(p.check())
    assert len(st.posted) == 1 and st.edits[-1][1].fields[1].value == "30"       # updated, no new message
    state["live"] = {}
    asyncio.run(p.check())
    assert p.status()["Live now"] == 1                                          # one miss: not ended yet
    asyncio.run(p.check())
    assert p.status()["Live now"] == 0 and "ended" in st.edits[-1][1].description
    state["live"] = {"twitch:kryvonlive": sm.Live(title="back")}                 # back within 30 min
    asyncio.run(p.check())
    assert len(st.posted) == 1                                                   # same message, no second ping


def test_only_while_on_our_server(st):
    p, state = st.p, st.state
    asyncio.run(st.pb.m.save_settings("streamers", {"only_on_server": True}))
    state["live"] = {"twitch:kryvonlive": sm.Live(title="x")}
    st.pb.bot.online_players = {}
    asyncio.run(p.check())
    assert not p.live and not st.posted                                          # live, but not on our server
    st.pb.bot.online_players = {"Gamer7": {}}
    asyncio.run(p.check())
    assert "twitch:kryvonlive" in p.live and len(st.posted) == 1
    assert any("in-game name" in x for x in p.problems())                       # kick/youtube lines have no name


def test_streams_page_on_the_website(st):
    p, state = st.p, st.state
    state["live"] = {"twitch:kryvonlive": sm.Live(title="Wipe day!", viewers=5,
                                                  thumbnail="https://static-cdn.jtvnw.net/x.jpg"),
                     "youtube:@kryvonrust": sm.Live(title="YT", video_id="abcDEF12345",
                                                    thumbnail="https://i.ytimg.com/vi/abcDEF12345/hq.jpg")}
    asyncio.run(p.check())

    async def main():
        from web.server import WebInterface
        await st.pb.m.set_enabled("website", True)
        web = WebInterface(st.pb.bot, env_path=st.pb.tmp / ".env", secret_path=st.pb.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            r = await c.get("/streams")
            text = await r.text()
            assert r.status == 200 and 'href="/streams"' in text
            assert "player.twitch.tv/?channel=KryvonLive&amp;parent=127.0.0.1" in text   # biggest audience first
            assert "Also live" in text and "watch=youtube%3A%40kryvonrust" in text and "kryvon" in text
            csp = r.headers["Content-Security-Policy"]
            assert "frame-src https://player.twitch.tv" in csp and "https://static-cdn.jtvnw.net" in csp
            text = await (await c.get("/streams?watch=youtube:@kryvonrust")).text()
            assert "youtube-nocookie.com/embed/abcDEF12345" in text
            await st.pb.m.save_settings("streamers", {"website": False})
            assert (await c.get("/streams")).status == 404
        finally:
            await c.close()
            await st.pb.bot.rcon.close()
    asyncio.run(main())
