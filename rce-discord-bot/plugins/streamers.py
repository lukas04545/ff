"""Streamers: who streams on the server – live streams on the player website (/streams) and in Discord.

The owner lists the streamers (platform | channel | in-game name). Every few minutes the bot checks who is live:
  * Twitch  – the official API (Client ID + Secret from dev.twitch.tv, free)
  * Kick    – Kick's public channel data
  * YouTube – the channel's /live page (no API key – YouTube's API quota is far too small for this)
When someone goes live, an embed is posted in the chosen Discord channel (optionally pinging a role once per stream);
when the stream ends the message says so. The website shows the live streams embedded (watch right there) and the
other streamers. With "Only while on our server" a stream only counts while the streamer's in-game player is online.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

PLATFORMS = ("twitch", "kick", "youtube")
ICONS = {"twitch": "🟣", "kick": "🟢", "youtube": "🔴"}
COLOURS = {"twitch": 0x9146FF, "kick": 0x53FC18, "youtube": 0xFF0000}
LINE = re.compile(r"^\s*(twitch|kick|youtube)\s*\|\s*([^|]{1,80}?)\s*(?:\|\s*([^|]{0,64}?)\s*)?$", re.I)
CHANNEL_NAME = re.compile(r"^[@\w.\-]{2,80}$")
UA = "Mozilla/5.0 (compatible; RCE-Bot streamers; +https://discord.com)"
OFFLINE_CHECKS = 2              # checks in a row without the stream before it counts as ended (no flapping)
REANNOUNCE_AFTER = 30 * 60      # a stream back within this time continues the old message instead of a new ping


@dataclass
class Streamer:
    platform: str
    channel: str
    player: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.channel.lower()}"

    @property
    def url(self) -> str:
        if self.platform == "twitch":
            return f"https://www.twitch.tv/{self.channel}"
        if self.platform == "kick":
            return f"https://kick.com/{self.channel}"
        return (f"https://www.youtube.com/{self.channel}" if self.channel.startswith("@")
                else f"https://www.youtube.com/channel/{self.channel}")


@dataclass
class Live:
    title: str = ""
    game: str = ""
    viewers: int | None = None
    thumbnail: str = ""
    video_id: str = ""              # YouTube
    started: float = field(default_factory=time.time)


def parse_streamers(text: str) -> tuple[list[Streamer], list[str]]:
    """'platform | channel | in-game name' per line -> streamers, problems."""
    out, problems, seen = [], [], set()
    for n, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = LINE.match(line)
        channel = m.group(2).strip() if m else ""
        if not m or not CHANNEL_NAME.match(channel):
            problems.append(f"Line {n}: use  platform | channel | in-game name  (platform: twitch, kick or youtube).")
            continue
        platform = m.group(1).lower()
        if platform == "youtube" and not (channel.startswith("@") or channel.startswith("UC")):
            channel = "@" + channel                              # a handle without the @
        s = Streamer(platform, channel.removeprefix("@") if platform != "youtube" else channel,
                     (m.group(3) or "").strip())
        if s.key not in seen:
            seen.add(s.key)
            out.append(s)
    return out, problems


def parse_youtube_live(html: str) -> Live | None:
    """From a channel's /live page: live right now? (title, video id). None = not live."""
    if '"isLiveNow":true' not in html and '"isLive":true' not in html:
        return None
    vid = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{6,20})"', html) \
        or re.search(r'"videoId":"([\w-]{6,20})"', html)
    if vid is None:
        return None
    title = re.search(r'<meta (?:property|name)="og:title" content="([^"]{0,300})"', html)
    viewers = re.search(r'"concurrentViewers":"(\d+)"', html)
    return Live(title=_unescape(title.group(1)) if title else "", video_id=vid.group(1),
                thumbnail=f"https://i.ytimg.com/vi/{vid.group(1)}/hqdefault_live.jpg",
                viewers=int(viewers.group(1)) if viewers else None)


def _unescape(text: str) -> str:
    return (text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<")
            .replace("&gt;", ">"))


def parse_kick(data) -> Live | None:
    stream = data.get("livestream") if isinstance(data, dict) else None
    if not isinstance(stream, dict) or not stream.get("is_live", True):
        return None
    cats = stream.get("categories") or []
    thumb = stream.get("thumbnail") or {}
    return Live(title=str(stream.get("session_title") or ""), viewers=stream.get("viewer_count"),
                game=str(cats[0].get("name") or "") if cats and isinstance(cats[0], dict) else "",
                thumbnail=str(thumb.get("url") or "") if isinstance(thumb, dict) else "")


def parse_twitch(data) -> dict[str, Live]:
    out = {}
    for s in (data.get("data") or []) if isinstance(data, dict) else []:
        if s.get("type") != "live":
            continue
        out[str(s.get("user_login") or "").lower()] = Live(
            title=str(s.get("title") or ""), game=str(s.get("game_name") or ""), viewers=s.get("viewer_count"),
            thumbnail=str(s.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360"))
    return out


class Streamers(Plugin):
    NAME = "Streamers"
    ICON = "📺"
    DESCRIPTION = ("Who streams on your server: live streams on the player website (/streams, watch right there) and "
                   "an announcement in Discord when someone goes live – Twitch, Kick and YouTube.")
    SETTINGS = (
        Setting("streamers", "Streamers", "textarea", "",
                help="One per line: platform | channel | in-game name (optional), e.g.  twitch | kryvon_live | "
                     "Gamer7  ·  kick | kryvon  ·  youtube | @KryvonRust. Platforms: twitch, kick, youtube."),
        Setting("only_on_server", "Only while they play on our server", "bool", False,
                help="A stream only counts while the streamer's in-game player (3rd column) is online on the server."),
        Setting("channel", "Discord channel for live announcements", "channel", None,
                help="Empty = no announcements (the website and /streams still show who's live)."),
        Setting("role", "Role to ping when someone goes live (optional)", "role", None,
                help="Pinged once per stream (not again if the stream comes back within 30 minutes)."),
        Setting("website", "Show the Streams page on the website", "bool", True),
        Setting("twitch_client_id", "Twitch Client ID", "text", "",
                help="Free: dev.twitch.tv/console → Register Your Application (category: Other, OAuth redirect "
                     "http://localhost) → Client ID + New Secret. Needed for Twitch only."),
        Setting("twitch_client_secret", "Twitch Client Secret", "text", ""),
        Setting("minutes", "Check every … minutes", "int", 2, min=1, max=30),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.live: dict[str, Live] = {}              # key -> live stream (counts: on server if required)
        self._misses: dict[str, int] = {}
        self._twitch_token: tuple[str, float] | None = None
        self._last_check = 0.0
        self._problem = ""
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------ Card

    def streamers(self) -> list[Streamer]:
        return parse_streamers(self.setting("streamers"))[0]

    def problems(self) -> list[str]:
        out = parse_streamers(self.setting("streamers"))[1]
        if any(s.platform == "twitch" for s in self.streamers()) and not (
                str(self.setting("twitch_client_id") or "").strip()
                and str(self.setting("twitch_client_secret") or "").strip()):
            out.append("Twitch streamers need the Twitch Client ID and Secret (see the setting's help).")
        if self.setting("only_on_server") and any(not s.player for s in self.streamers()):
            out.append("\"Only while on our server\" needs the in-game name (3rd column) of every streamer.")
        return out + ([self._problem] if self._problem else [])

    def status(self) -> dict:
        return {"Streamers": len(self.streamers()), "Live now": len(self.live)}

    async def on_unload(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------ Checking who's live

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": UA})
        return self._session

    async def _twitch(self, logins: list[str]) -> dict[str, Live]:
        cid = str(self.setting("twitch_client_id") or "").strip()
        secret = str(self.setting("twitch_client_secret") or "").strip()
        if not logins or not cid or not secret:
            return {}
        http = self._http()
        if self._twitch_token is None or self._twitch_token[1] < time.time() + 60:
            async with http.post("https://id.twitch.tv/oauth2/token", params={
                    "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}) as r:
                data = await r.json(content_type=None)
            if "access_token" not in data:
                raise RuntimeError("Twitch refused the Client ID / Secret.")
            self._twitch_token = (data["access_token"], time.time() + int(data.get("expires_in") or 3600))
        out: dict[str, Live] = {}
        for i in range(0, len(logins), 100):
            async with http.get("https://api.twitch.tv/helix/streams",
                                params=[("user_login", login) for login in logins[i:i + 100]],
                                headers={"Client-Id": cid, "Authorization": f"Bearer {self._twitch_token[0]}"}) as r:
                if r.status == 401:
                    self._twitch_token = None
                    raise RuntimeError("Twitch login expired – trying again next time.")
                out.update(parse_twitch(await r.json(content_type=None)))
        return out

    async def _kick(self, slug: str) -> Live | None:
        async with self._http().get(f"https://kick.com/api/v2/channels/{slug}",
                                    headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            return parse_kick(await r.json(content_type=None))

    async def _youtube(self, channel: str) -> Live | None:
        path = channel if channel.startswith("@") else f"channel/{channel}"
        async with self._http().get(f"https://www.youtube.com/{path}/live",
                                    headers={"Accept-Language": "en", "Cookie": "CONSENT=YES+1"}) as r:
            if r.status != 200:
                return None
            return parse_youtube_live(await r.text())

    async def fetch_live(self) -> dict[str, Live]:
        """Asks the platforms who is live now. -> {streamer key: Live}."""
        streamers = self.streamers()
        found: dict[str, Live] = {}
        problems = []
        twitch = [s for s in streamers if s.platform == "twitch"]
        try:
            live = await self._twitch([s.channel.lower() for s in twitch])
            for s in twitch:
                if s.channel.lower() in live:
                    found[s.key] = live[s.channel.lower()]
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            problems.append(f"Twitch: {exc}")
        for s in streamers:
            if s.platform == "twitch":
                continue
            try:
                result = await (self._kick(s.channel) if s.platform == "kick" else self._youtube(s.channel))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                problems.append(f"{s.platform.title()} {s.channel}: {exc}")
                continue
            if result is not None:
                found[s.key] = result
        self._problem = "Last check: " + "; ".join(problems)[:300] if problems else ""
        return found

    def _on_server(self, s: Streamer) -> bool:
        if not s.player:
            return False
        return any(n.lower() == s.player.lower() for n in self.bot.online_players)

    async def check(self) -> None:
        """One round: who is live (and on the server if required); announce starts and ends."""
        streamers = {s.key: s for s in self.streamers()}
        found = await self.fetch_live()
        if self.setting("only_on_server"):
            found = {k: v for k, v in found.items() if k in streamers and self._on_server(streamers[k])}
        for key, info in found.items():
            self._misses.pop(key, None)
            if key in self.live:
                info.started = self.live[key].started
                self.live[key] = info
                await self._announce(streamers[key], info, update=True)
            else:
                self.live[key] = info
                await self._announce(streamers[key], info)
        for key in list(self.live):
            if key in found:
                continue
            self._misses[key] = self._misses.get(key, 0) + 1
            if self._misses[key] >= OFFLINE_CHECKS or key not in streamers:
                ended = self.live.pop(key)
                self._misses.pop(key, None)
                if key in streamers:
                    await self._ended(streamers[key], ended)

    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        if time.monotonic() - self._last_check < self.setting("minutes") * 60 - 5:
            return
        self._last_check = time.monotonic()
        try:
            await self.check()
        except Exception as exc:                         # never let one bad answer stop the loop
            self.log.warning("Streamers: %s", exc)

    async def on_settings_changed(self) -> None:
        self._last_check = 0.0
        self._twitch_token = None

    # ------------------------------------------------------------ Discord

    def embed(self, s: Streamer, info: Live, *, ended: bool = False) -> discord.Embed:
        e = discord.Embed(title=(info.title or f"{s.channel} is live")[:256], url=s.url,
                          colour=discord.Colour(0x4F545C if ended else COLOURS[s.platform]))
        e.set_author(name=f"{ICONS[s.platform]} {s.channel} " + ("was live" if ended else f"is live on {s.platform.title()}"))
        if info.game:
            e.add_field(name="Playing", value=info.game[:100])
        if info.viewers is not None and not ended:
            e.add_field(name="Viewers", value=f"{info.viewers:,}".replace(",", " "))
        if s.player:
            e.add_field(name="In-game", value=s.player[:64])
        if ended:
            e.description = f"Stream ended after {_duration(time.time() - info.started)}."
        elif info.thumbnail:
            e.set_image(url=info.thumbnail + ("&" if "?" in info.thumbnail else "?") + f"t={int(time.time() // 300)}")
        return e

    async def _announce(self, s: Streamer, info: Live, *, update: bool = False) -> None:
        channel = self.channel(self.setting("channel"))
        if channel is None:
            return
        saved = self.data.get(f"msg:{s.key}") or {}
        msg = None
        recent = saved and time.time() - saved.get("ended", time.time()) < REANNOUNCE_AFTER
        if saved.get("id") and (update or recent):
            try:
                msg = await channel.fetch_message(int(saved["id"]))
                await msg.edit(embed=self.embed(s, info))
            except discord.HTTPException:
                msg = None
            if msg is not None:
                if recent and not update:
                    info.started = saved.get("started", info.started)
                self.data.set(f"msg:{s.key}", {"id": msg.id, "started": info.started})
                return
        if update:
            return
        role = self.setting("role")
        msg = await self.post(channel, f"{ICONS[s.platform]} **{discord.utils.escape_markdown(s.channel)}** is live!",
                              embed=self.embed(s, info), ping_role=int(role) if role else None)
        if msg is not None:
            self.data.set(f"msg:{s.key}", {"id": msg.id, "started": info.started})

    async def _ended(self, s: Streamer, info: Live) -> None:
        channel = self.channel(self.setting("channel"))
        saved = self.data.get(f"msg:{s.key}") or {}
        if channel is None or not saved.get("id"):
            return
        try:
            msg = await channel.fetch_message(int(saved["id"]))
            await msg.edit(content=None, embed=self.embed(s, info, ended=True))
        except discord.HTTPException:
            pass
        self.data.set(f"msg:{s.key}", {**saved, "ended": time.time()})

    @app_commands.command(name="streams", description="Who is streaming on the server right now.")
    async def streams_command(self, interaction: discord.Interaction) -> None:
        streamers = self.streamers()
        live = [(s, self.live[s.key]) for s in streamers if s.key in self.live]
        if not live:
            await interaction.response.send_message("📺 Nobody is live right now." + (
                f" Our streamers: {', '.join(f'[{s.channel}]({s.url})' for s in streamers[:15])}" if streamers else ""),
                ephemeral=True, suppress_embeds=True)
            return
        e = discord.Embed(title="📺 Live now", colour=discord.Colour(0x9146FF))
        for s, info in live[:20]:
            viewers = f" · {info.viewers} watching" if info.viewers is not None else ""
            e.add_field(name=f"{ICONS[s.platform]} {s.channel}"[:256],
                        value=f"[{(info.title or 'Watch')[:150]}]({s.url}){viewers}", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ------------------------------------------------------------ Website

    def site_data(self) -> dict:
        """For the website's Streams page: live streams first, then the others."""
        streamers = self.streamers()
        live = [(s, self.live[s.key]) for s in streamers if s.key in self.live]
        live.sort(key=lambda x: -(x[1].viewers or 0))
        return {"live": live, "offline": [s for s in streamers if s.key not in self.live]}


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{max(1, minutes)} min"

