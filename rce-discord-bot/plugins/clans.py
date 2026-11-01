"""Clans: groups of members with a name and tag, invites, roles and a clan leaderboard (from the in-game stats of
the members' linked players).

/clan create <name> <tag> · /clan invite <member> · /clan accept <clan> · /clan leave · /clan kick <member>
/clan disband · /clan info [clan] · /clan list · /clan top
Optional: a Discord role per clan (made and handed out by the bot).
"""
from __future__ import annotations

import re

import discord
from discord import app_commands

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0x8E44AD)
TAG = re.compile(r"^[A-Za-z0-9]{2,5}$")


class Clans(Plugin):
    NAME = "Clans"
    ICON = "🛡️"
    DESCRIPTION = "Members form clans (name + tag), invite each other and compete on a clan leaderboard."
    SETTINGS = (
        Setting("max_members", "Max members per clan", "int", 8, min=2, max=100),
        Setting("roles", "Give every clan its own Discord role", "bool", False,
                help="The bot creates the role (needs Manage Roles) and gives it to the members."),
        Setting("cost", "Cost to create a clan (points)", "int", 0, min=0, max=10_000_000),
    )

    clan = app_commands.Group(name="clan", description="Clans", guild_only=True)

    # ------------------------------------------------------------ Data

    def clans(self) -> dict[str, dict]:
        return dict(self.data.get("clans", {}))

    def _save(self, clans: dict) -> None:
        self.data.set("clans", clans)

    def clan_of(self, user_id: int) -> tuple[str, dict] | None:
        for key, c in self.clans().items():
            if user_id in c["members"]:
                return key, c
        return None

    def find(self, name: str) -> tuple[str, dict] | None:
        q = (name or "").strip().lower()
        for key, c in self.clans().items():
            if q in (key, c["tag"].lower()):
                return key, c
        return None

    def score(self, c: dict) -> dict:
        kills = deaths = playtime = 0
        for uid in c["members"]:
            player = self.linked_player(uid)
            row = self.bot.db.player(player) if player else None
            if row is not None:
                kills += row["kills"]
                deaths += row["deaths"]
                playtime += row["playtime_seconds"]
        return {"kills": kills, "deaths": deaths, "kd": round(kills / max(deaths, 1), 2),
                "hours": round(playtime / 3600, 1)}

    # ------------------------------------------------------------ Actions

    def create(self, user_id: int, name: str, tag: str) -> tuple[bool, str]:
        name, tag = " ".join(str(name).split())[:32], str(tag).strip()
        if len(name) < 3:
            return False, "The clan name needs at least 3 characters."
        if not TAG.match(tag):
            return False, "The tag must be 2–5 letters or numbers."
        if self.clan_of(user_id):
            return False, "You're already in a clan – leave it first."
        if self.find(name) or self.find(tag):
            return False, "That name or tag is taken."
        cost = self.setting("cost")
        if cost and self.take_points(user_id, cost, f"clan {name}") is None:
            return False, f"Creating a clan costs {self.fmt_points(cost)}."
        clans = self.clans()
        clans[name.lower()] = {"name": name, "tag": tag.upper(), "owner": user_id, "members": [user_id],
                               "invites": [], "role": None, "created": self.now()}
        self._save(clans)
        return True, f"Clan **{self.md(name)}** [{tag.upper()}] founded."

    def invite(self, user_id: int, target: int) -> tuple[bool, str]:
        mine = self.clan_of(user_id)
        if mine is None or mine[1]["owner"] != user_id:
            return False, "Only a clan leader can invite."
        key, c = mine
        if self.clan_of(target):
            return False, "They're already in a clan."
        if len(c["members"]) >= self.setting("max_members"):
            return False, "Your clan is full."
        clans = self.clans()
        if target not in clans[key]["invites"]:
            clans[key]["invites"].append(target)
        self._save(clans)
        return True, f"Invited <@{target}> to **{self.md(c['name'])}** – they join with `/clan accept {c['tag']}`."

    def accept(self, user_id: int, clan: str) -> tuple[bool, str]:
        found = self.find(clan)
        if found is None or user_id not in found[1]["invites"]:
            return False, "You have no invite from that clan."
        if self.clan_of(user_id):
            return False, "Leave your clan first."
        key, c = found
        if len(c["members"]) >= self.setting("max_members"):
            return False, "That clan is full."
        clans = self.clans()
        clans[key]["invites"].remove(user_id)
        clans[key]["members"].append(user_id)
        self._save(clans)
        return True, f"Welcome to **{self.md(c['name'])}** [{c['tag']}]!"

    def leave(self, user_id: int) -> tuple[bool, str]:
        mine = self.clan_of(user_id)
        if mine is None:
            return False, "You're not in a clan."
        key, c = mine
        clans = self.clans()
        clans[key]["members"].remove(user_id)
        if not clans[key]["members"]:
            clans.pop(key)
            self._save(clans)
            return True, f"You left – **{self.md(c['name'])}** is disbanded (no members left)."
        if c["owner"] == user_id:
            clans[key]["owner"] = clans[key]["members"][0]
        self._save(clans)
        return True, f"You left **{self.md(c['name'])}**."

    def kick(self, user_id: int, target: int) -> tuple[bool, str]:
        mine = self.clan_of(user_id)
        if mine is None or mine[1]["owner"] != user_id:
            return False, "Only the clan leader can kick."
        if target == user_id or target not in mine[1]["members"]:
            return False, "They're not a member (and you can't kick yourself)."
        clans = self.clans()
        clans[mine[0]]["members"].remove(target)
        self._save(clans)
        return True, f"<@{target}> was removed from the clan."

    def disband(self, user_id: int) -> tuple[bool, str, dict | None]:
        mine = self.clan_of(user_id)
        if mine is None or mine[1]["owner"] != user_id:
            return False, "Only the clan leader can disband it.", None
        clans = self.clans()
        c = clans.pop(mine[0])
        self._save(clans)
        return True, f"**{self.md(c['name'])}** is disbanded.", c

    # ------------------------------------------------------------ Discord roles

    async def _sync_role(self, key: str | None = None, *, delete: dict | None = None) -> None:
        guild = self.guild
        if guild is None or not self.setting("roles"):
            return
        try:
            if delete is not None:
                role = guild.get_role(delete.get("role") or 0)
                if role is not None:
                    await role.delete(reason="Clan disbanded")
                return
            clans = self.clans()
            c = clans.get(key or "")
            if c is None:
                return
            role = guild.get_role(c.get("role") or 0)
            if role is None:
                role = await guild.create_role(name=f"[{c['tag']}] {c['name']}"[:100], reason="Clan role")
                clans[key]["role"] = role.id
                self._save(clans)
            for member in list(getattr(role, "members", [])):
                if member.id not in c["members"]:
                    await member.remove_roles(role, reason="Left the clan")
            for uid in c["members"]:
                member = guild.get_member(uid)
                if member is not None and role not in member.roles:
                    await member.add_roles(role, reason="Clan member")
        except discord.HTTPException as exc:
            self.log.warning("Clan role: %s", exc)

    # ------------------------------------------------------------ Commands

    async def _send(self, interaction, ok: bool, text: str, *, public: bool = False) -> None:
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + text, ephemeral=not (ok and public),
                                                allowed_mentions=discord.AllowedMentions.none())

    @clan.command(name="create", description="Found a clan.")
    @app_commands.describe(name="Clan name", tag="2–5 letters, e.g. KRV")
    async def create_cmd(self, interaction: discord.Interaction, name: str, tag: str) -> None:
        ok, text = self.create(interaction.user.id, name, tag)
        await self._send(interaction, ok, text, public=True)
        if ok:
            await self._sync_role(name.strip().lower())

    @clan.command(name="invite", description="Invite a member to your clan.")
    async def invite_cmd(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._send(interaction, *self.invite(interaction.user.id, member.id))

    @clan.command(name="accept", description="Join a clan that invited you.")
    async def accept_cmd(self, interaction: discord.Interaction, clan: str) -> None:
        ok, text = self.accept(interaction.user.id, clan)
        await self._send(interaction, ok, text, public=True)
        if ok:
            await self._sync_role(self.find(clan)[0])

    @clan.command(name="leave", description="Leave your clan.")
    async def leave_cmd(self, interaction: discord.Interaction) -> None:
        mine = self.clan_of(interaction.user.id)
        ok, text = self.leave(interaction.user.id)
        await self._send(interaction, ok, text)
        if ok and mine:
            await self._sync_role(mine[0]) if mine[0] in self.clans() else await self._sync_role(delete=mine[1])

    @clan.command(name="kick", description="Remove a member (clan leader).")
    async def kick_cmd(self, interaction: discord.Interaction, member: discord.Member) -> None:
        ok, text = self.kick(interaction.user.id, member.id)
        await self._send(interaction, ok, text)
        if ok:
            await self._sync_role(self.clan_of(interaction.user.id)[0])

    @clan.command(name="disband", description="Disband your clan (clan leader).")
    async def disband_cmd(self, interaction: discord.Interaction) -> None:
        ok, text, c = self.disband(interaction.user.id)
        await self._send(interaction, ok, text)
        if ok:
            await self._sync_role(delete=c)

    @clan.command(name="info", description="A clan's members and stats.")
    async def info_cmd(self, interaction: discord.Interaction, clan: str | None = None) -> None:
        found = self.find(clan) if clan else self.clan_of(interaction.user.id)
        if found is None:
            await self._send(interaction, False, "No such clan." if clan else "You're not in a clan.")
            return
        c, s = found[1], self.score(found[1])
        embed = discord.Embed(title=f"🛡️ [{c['tag']}] {c['name']}", colour=COLOUR,
                              description="\n".join(("👑 " if uid == c["owner"] else "• ") + f"<@{uid}>"
                                                    + (f" – {self.md(p)}" if (p := self.linked_player(uid)) else "")
                                                    for uid in c["members"]))
        embed.add_field(name="Kills", value=str(s["kills"]))
        embed.add_field(name="K/D", value=str(s["kd"]))
        embed.add_field(name="Hours played", value=str(s["hours"]))
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @clan.command(name="list", description="All clans.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        lines = [f"**[{c['tag']}] {self.md(c['name'])}** – {len(c['members'])} member(s)" for c in self.clans().values()]
        await interaction.response.send_message(embed=discord.Embed(
            title="🛡️ Clans", colour=COLOUR, description="\n".join(lines)[:4000] or "No clans yet – /clan create."),
            ephemeral=True)

    @clan.command(name="top", description="Clan leaderboard.")
    @app_commands.choices(by=[app_commands.Choice(name=n, value=v) for n, v in
                              (("Kills", "kills"), ("K/D", "kd"), ("Hours played", "hours"))])
    async def top_cmd(self, interaction: discord.Interaction, by: app_commands.Choice[str] | None = None) -> None:
        key = by.value if by else "kills"
        rows = sorted(((c, self.score(c)) for c in self.clans().values()), key=lambda r: -r[1][key])[:15]
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} **[{c['tag']}] {self.md(c['name'])}** – {s[key]}"
                 for i, (c, s) in enumerate(rows)]
        await interaction.response.send_message(embed=discord.Embed(
            title=f"🛡️ Clan leaderboard – {key}", colour=COLOUR, description="\n".join(lines) or "No clans yet."))

    # ------------------------------------------------------------ Panel

    def status(self) -> dict:
        clans = self.clans()
        return {"Clans": len(clans), "Members": sum(len(c["members"]) for c in clans.values())}

    def profile_fields(self, member) -> list[tuple[str, str]]:
        mine = self.clan_of(member.id)
        return [("🛡️ Clan", f"[{mine[1]['tag']}] {mine[1]['name']}")] if mine else []
