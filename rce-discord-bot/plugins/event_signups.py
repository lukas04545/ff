"""Event sign-ups: players register for a raid / PvP / custom event (alone or as a team) before it starts.

  /signup create <title> <starts_in_minutes> [team_size] [max] [info]   posts the event with Join / Leave (staff)
  Join → solo: you're in · teams: a form asks your team name (others join the same name, up to the team size)
  /signup winners <event> <winner1> [winner2 …]   records winners (they get the points reward) (staff)
  /signup leaderboard                            who won the most events · /signup list   events and their sign-ups

The post shows a live countdown and the participants. When it starts, every participant gets a DM (no mass pings).
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import admin_only


class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"signup:(?P<act>join|leave):(?P<id>\d+)"):
    def __init__(self, act: str, event_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Join" if act == "join" else "Leave", emoji="✅" if act == "join" else "🚪",
            style=discord.ButtonStyle.success if act == "join" else discord.ButtonStyle.secondary,
            custom_id=f"signup:{act}:{event_id}"))
        self.act, self.event_id = act, event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        plugin = interaction.client.plugins.running("event_signups") if getattr(interaction.client, "plugins", None) \
            else None
        if plugin is None:
            await interaction.response.send_message("Event sign-ups are switched off.", ephemeral=True)
            return
        if self.act == "leave":
            await interaction.response.send_message(await plugin.leave(self.event_id, interaction.user.id),
                                                    ephemeral=True)
            return
        ev = plugin.event(self.event_id)
        if ev and ev["team_size"] > 1:
            await interaction.response.send_modal(TeamForm(plugin, self.event_id))
            return
        await interaction.response.send_message(await plugin.join(self.event_id, interaction.user), ephemeral=True)


class TeamForm(discord.ui.Modal, title="Your team"):
    team = discord.ui.TextInput(label="Team name (same name = same team)", max_length=32)

    def __init__(self, plugin: "EventSignups", event_id: int) -> None:
        super().__init__(timeout=600)
        self.plugin, self.event_id = plugin, event_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            await self.plugin.join(self.event_id, interaction.user, str(self.team.value)), ephemeral=True)


class EventSignups(Plugin):
    NAME = "Event sign-ups"
    ICON = "🏆"
    DESCRIPTION = ("Players sign up for raid / PvP / custom events alone or as teams, with a countdown, winners, "
                   "point rewards and an event leaderboard.")
    SETTINGS = (
        Setting("channel", "Event channel", "channel", help="Where events are posted. Empty = the events channel."),
        Setting("points", "Points for each winner", "int", 0, min=0, max=1_000_000),
        Setting("remind", "DM participants when it starts", "bool", True),
    )

    signup = app_commands.Group(name="signup", description="Event sign-ups", guild_only=True)

    async def on_load(self) -> None:
        self.bot.add_dynamic_items(SignupButton)

    async def on_unload(self) -> None:
        self.bot.remove_dynamic_items(SignupButton)

    # ------------------------------------------------------------ data

    def event(self, event_id: int) -> dict | None:
        return self.data.get(f"ev:{int(event_id)}")

    def _save(self, event_id: int, ev: dict) -> None:
        self.data.set(f"ev:{int(event_id)}", ev)

    def create_event(self, title: str, starts_at: int, team_size: int = 1, max_players: int = 0, info: str = "") -> int:
        n = int(self.data.get("next", 0)) + 1
        self.data.set("next", n)
        self._save(n, {"title": title[:100], "starts": int(starts_at), "team_size": max(1, int(team_size)),
                       "max": max(0, int(max_players)), "info": info[:500], "players": {}, "winners": [],
                       "message": None, "channel": None, "reminded": False, "closed": False})
        return n

    async def join(self, event_id: int, user, team: str = "") -> str:
        ev = self.event(event_id)
        if not ev or ev["closed"] or ev["starts"] <= self.now():
            return "Sign-ups for this event are closed."
        uid = str(user.id)
        if uid in ev["players"]:
            return "You're signed up already."
        if ev["max"] and len(ev["players"]) >= ev["max"]:
            return "The event is full."
        team = " ".join(team.split())[:32]
        if ev["team_size"] > 1:
            if not team:
                return "Enter a team name."
            mates = [p for p in ev["players"].values() if p["team"].lower() == team.lower()]
            if len(mates) >= ev["team_size"]:
                return f"Team **{self.md(team)}** is full ({ev['team_size']} players)."
            if mates:
                team = mates[0]["team"]                    # same spelling as the first member
        ev["players"][uid] = {"name": getattr(user, "display_name", str(user))[:40], "team": team,
                              "player": self.linked_player(user.id) or ""}
        self._save(event_id, ev)
        await self.refresh(event_id)
        return f"✅ You're in **{self.md(ev['title'])}**" + (f" with team **{self.md(team)}**" if team else "") + \
            f" – it starts <t:{ev['starts']}:R>."

    async def leave(self, event_id: int, user_id: int) -> str:
        ev = self.event(event_id)
        if not ev or str(user_id) not in ev["players"]:
            return "You aren't signed up."
        del ev["players"][str(user_id)]
        self._save(event_id, ev)
        await self.refresh(event_id)
        return "You left the event."

    def embed(self, event_id: int) -> discord.Embed:
        ev = self.event(event_id)
        e = discord.Embed(title=f"🏆 {ev['title']}", colour=0xF1C40F,
                          description=(ev["info"] + "\n\n" if ev["info"] else "")
                          + f"Starts <t:{ev['starts']}:F> (<t:{ev['starts']}:R>)"
                          + (f" · teams of {ev['team_size']}" if ev["team_size"] > 1 else "")
                          + (f" · max {ev['max']} players" if ev["max"] else ""))
        players = list(ev["players"].values())
        if ev["team_size"] > 1:
            teams: dict[str, list[str]] = {}
            for p in players:
                teams.setdefault(p["team"], []).append(p["name"])
            body = "\n".join(f"**{self.md(t)}** – {', '.join(self.md(x) for x in names)}" for t, names in teams.items())
        else:
            body = ", ".join(self.md(p["name"]) for p in players)
        e.add_field(name=f"Signed up ({len(players)})", value=body[:1020] or "Nobody yet – be the first!", inline=False)
        if ev["winners"]:
            e.add_field(name="🥇 Winners", value=", ".join(self.md(w) for w in ev["winners"])[:1020], inline=False)
        return e

    def view(self, event_id: int) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(SignupButton("join", event_id))
        v.add_item(SignupButton("leave", event_id))
        return v

    async def refresh(self, event_id: int) -> None:
        ev = self.event(event_id)
        ch = self.bot.get_channel(ev["channel"]) if ev and ev["channel"] else None
        if ch is None or not ev["message"]:
            return
        try:
            msg = await ch.fetch_message(ev["message"])
            await msg.edit(embed=self.embed(event_id), view=None if ev["closed"] else self.view(event_id))
        except (discord.HTTPException, AttributeError):
            pass

    def leaderboard(self) -> list[tuple[str, int]]:
        wins: dict[str, int] = {}
        for ev in self.data.items("ev:").values():
            for w in ev["winners"]:
                wins[w] = wins.get(w, 0) + 1
        return sorted(wins.items(), key=lambda x: (-x[1], x[0].lower()))

    def find(self, text: str) -> int | None:
        text = str(text).strip().lstrip("#")
        if text.isdigit() and self.event(int(text)):
            return int(text)
        for k, ev in sorted(self.data.items("ev:").items(), key=lambda kv: -int(kv[0].split(":")[1])):
            if ev["title"].lower() == text.lower():
                return int(k.split(":")[1])
        return None

    # ------------------------------------------------------------ the start

    @tasks.loop(seconds=30)
    async def start_loop(self) -> None:
        await self.start_due()

    async def start_due(self) -> int:
        started = 0
        for key, ev in self.data.items("ev:").items():
            if ev["reminded"] or ev["closed"] or ev["starts"] > self.now():
                continue
            event_id = int(key.split(":")[1])
            ev["reminded"] = ev["closed"] = True
            self._save(event_id, ev)
            started += 1
            if self.setting("remind"):
                for uid in ev["players"]:
                    try:
                        user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                        await user.send(f"🏆 **{self.md(ev['title'])}** starts now on {self.md(self.server_name)}!")
                    except (discord.HTTPException, AttributeError):
                        pass
            await self.refresh(event_id)
        return started

    # ------------------------------------------------------------ commands

    @signup.command(name="create", description="Post an event players can sign up for.")
    @app_commands.describe(starts_in_minutes="When it starts (minutes from now)", team_size="1 = solo",
                           max_players="0 = no limit", info="What, where, rules")
    @admin_only()
    async def create_cmd(self, interaction: discord.Interaction, title: str, starts_in_minutes: app_commands.Range[int, 1, 43200],
                         team_size: app_commands.Range[int, 1, 20] = 1, max_players: app_commands.Range[int, 0, 500] = 0,
                         info: str = "") -> None:
        ch = self.channel(self.setting("channel"), self.bot.config.events_channel_id) or interaction.channel
        event_id = self.create_event(title, self.now() + starts_in_minutes * 60, team_size, max_players, info)
        msg = await self.post(ch, embed=self.embed(event_id), view=self.view(event_id))
        if msg is None:
            await interaction.response.send_message("I can't post in the event channel.", ephemeral=True)
            return
        ev = self.event(event_id)
        ev["message"], ev["channel"] = msg.id, ch.id
        self._save(event_id, ev)
        self.audit(f"Event **{self.md(title)}** (#{event_id}) posted – starts <t:{ev['starts']}:R>")
        await interaction.response.send_message(f"✅ Event #{event_id} posted in <#{ch.id}>.", ephemeral=True)

    @signup.command(name="winners", description="Record the winners of an event (they get the reward).")
    @app_commands.describe(event="Event number or title", winner1="Player or team name")
    @admin_only()
    async def winners_cmd(self, interaction: discord.Interaction, event: str, winner1: str, winner2: str = "",
                          winner3: str = "") -> None:
        event_id = self.find(event)
        if event_id is None:
            await interaction.response.send_message("No such event.", ephemeral=True)
            return
        await interaction.response.send_message(await self.set_winners(event_id, [winner1, winner2, winner3]),
                                                ephemeral=True)

    async def set_winners(self, event_id: int, names: list[str]) -> str:
        ev = self.event(event_id)
        names = [" ".join(n.split())[:40] for n in names if n and n.strip()]
        ev["winners"], ev["closed"] = names, True
        self._save(event_id, ev)
        paid = []
        if self.setting("points") and self.points_enabled:
            lows = {n.lower() for n in names}
            for uid, p in ev["players"].items():      # a winning player, or every member of a winning team
                if {p["name"].lower(), p["team"].lower(), p["player"].lower()} & lows:
                    if self.give_points(int(uid), self.setting("points"), f"won {ev['title']}") is not None:
                        paid.append(p["name"])
        await self.refresh(event_id)
        self.audit(f"Event **{self.md(ev['title'])}** winners: {', '.join(names)}"
                   + (f" – {self.fmt_points(self.setting('points'))} each to {', '.join(paid)}" if paid else ""))
        return f"🥇 Winners saved: {', '.join(names)}" + (f" · rewarded: {', '.join(paid)}" if paid else "")

    @signup.command(name="leaderboard", description="Who won the most events.")
    async def leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        rows = self.leaderboard()[:15]
        await interaction.response.send_message(
            "🏆 **Event leaderboard**\n" + "\n".join(f"{i}. **{self.md(n)}** – {w} win{'s' if w != 1 else ''}"
                                                   for i, (n, w) in enumerate(rows, 1)) if rows else
            "No winners yet.", ephemeral=True)

    @signup.command(name="list", description="Upcoming events and their sign-ups.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = sorted(((int(k.split(":")[1]), ev) for k, ev in self.data.items("ev:").items() if not ev["closed"]),
                      key=lambda x: x[1]["starts"])
        await interaction.response.send_message(
            "\n".join(f"#{i} **{self.md(ev['title'])}** <t:{ev['starts']}:R> – {len(ev['players'])} signed up"
                      for i, ev in rows) or "No upcoming events.", ephemeral=True)

    def status(self) -> dict:
        evs = list(self.data.items("ev:").values())
        return {"Upcoming": sum(not e["closed"] for e in evs), "Events": len(evs)}
