"""Role menus: a message with buttons – members click to take or drop a role (e.g. "Wipe pings", "EU", "Console").

  /rolemenu create <channel> <title> <role1> [role2 … role5] [text]   posts the menu (staff)
  /rolemenu list                                                     the menus and their roles

Safe by design: only roles without any permissions can be in a menu (no admin, moderator or manage roles), and only
roles below the bot's own. The buttons keep working after a restart.
"""
from __future__ import annotations

import discord
from discord import app_commands

from plugin_api import Plugin
from utils import admin_only


def role_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    """Why a role can't be in a menu (None = it can)."""
    if role.is_default() or role.managed:
        return f"{role.name} is managed by Discord or a bot"
    if role.permissions.value:
        return f"{role.name} has permissions – only roles without any permissions can be self-assigned"
    me = guild.me
    if me is not None and role >= me.top_role:
        return f"{role.name} is above the bot's role – move the bot's role higher"
    return None


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolemenu:(?P<role>\d+)"):
    def __init__(self, role_id: int, label: str = "", emoji: str | None = None) -> None:
        super().__init__(discord.ui.Button(label=label[:80] or "Role", emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"rolemenu:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        plugin = interaction.client.plugins.running("role_menus") if getattr(interaction.client, "plugins", None) \
            else None
        if plugin is None or not plugin.known(self.role_id):
            await interaction.response.send_message("This role menu isn't active any more.", ephemeral=True)
            return
        await plugin.toggle(interaction, self.role_id)


class RoleMenus(Plugin):
    NAME = "Role menus"
    ICON = "🎭"
    DESCRIPTION = "Buttons where members pick their own roles (pings, region, platform …) – /rolemenu create."

    rolemenu = app_commands.Group(name="rolemenu", description="Self-assign role menus", guild_only=True)

    async def on_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton)

    async def on_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton)

    def known(self, role_id: int) -> bool:
        return any(role_id in m["roles"] for m in self.data.items("menu:").values())

    async def toggle(self, interaction: discord.Interaction, role_id: int) -> None:
        guild, member = interaction.guild, interaction.user
        role = guild.get_role(role_id) if guild else None
        if role is None:
            await interaction.response.send_message("That role doesn't exist any more.", ephemeral=True)
            return
        if (why := role_problem(guild, role)) is not None:                    # checked again on every click
            await interaction.response.send_message(f"Can't give that role: {why}.", ephemeral=True)
            return
        try:
            if role in getattr(member, "roles", []):
                await member.remove_roles(role, reason="Role menu")
                text = f"Removed **{role.name}**."
            else:
                await member.add_roles(role, reason="Role menu")
                text = f"You have **{role.name}** now."
        except discord.HTTPException as exc:
            text = f"Discord refused: {exc}"
        await interaction.response.send_message(text, ephemeral=True)

    @rolemenu.command(name="create", description="Post a menu where members pick roles (up to 5).")
    @app_commands.describe(channel="Where to post it", title="Heading", text="Optional text above the buttons")
    @admin_only()
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str,
                     role1: discord.Role, role2: discord.Role | None = None, role3: discord.Role | None = None,
                     role4: discord.Role | None = None, role5: discord.Role | None = None, text: str = "") -> None:
        roles = list(dict.fromkeys(r for r in (role1, role2, role3, role4, role5) if r is not None))
        bad = [why for r in roles if (why := role_problem(interaction.guild, r))]
        if bad:
            await interaction.response.send_message("Not possible: " + "; ".join(bad) + ".", ephemeral=True)
            return
        view = discord.ui.View(timeout=None)
        for r in roles:
            view.add_item(RoleButton(r.id, r.name))
        e = discord.Embed(title=title[:250], colour=0x9B59B6,
                          description=(text[:1500] + "\n\n" if text else "") + "Click a button to take or drop the role.")
        msg = await self.post(channel, embed=e, view=view)
        if msg is None:
            await interaction.response.send_message("I can't post in that channel.", ephemeral=True)
            return
        self.data.set(f"menu:{msg.id}", {"channel": channel.id, "title": title[:250], "roles": [r.id for r in roles]})
        self.audit(f"Role menu **{self.md(title)}** posted in <#{channel.id}> ({', '.join(r.name for r in roles)})")
        await interaction.response.send_message(f"✅ Posted in {channel.mention}.", ephemeral=True)

    @rolemenu.command(name="list", description="The role menus.")
    @admin_only()
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        lines = []
        for mid, m in self.data.items("menu:").items():
            names = [getattr(guild.get_role(r), "name", f"#{r}") for r in m["roles"]] if guild else m["roles"]
            lines.append(f"**{self.md(m['title'])}** in <#{m['channel']}> – {', '.join(map(str, names))}")
        await interaction.response.send_message("\n".join(lines) or "No role menus yet – /rolemenu create.",
                                                ephemeral=True)

    def status(self) -> dict:
        return {"Menus": len(self.data.items("menu:"))}
