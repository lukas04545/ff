"""Staff applications: members apply with a short form, staff accept or decline with a button.

  /apply                  opens the form (your questions, up to 5) – the answers go to the review channel
  ✅ Accept / ❌ Decline   (staff) – accepting gives the role you set; the applicant gets a DM either way

One open application per member, and a waiting time before applying again after a decline.
"""
from __future__ import annotations

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from utils import is_admin

DEFAULT_QUESTIONS = ("How old are you?\nHow many hours have you played Rust?\nWhy do you want to join the staff?\n"
                     "How active can you be (hours per week)?\nAnything else we should know?")


class AppButton(discord.ui.DynamicItem[discord.ui.Button], template=r"staffapp:(?P<act>accept|decline):(?P<id>\d+)"):
    def __init__(self, act: str, app_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Accept" if act == "accept" else "Decline", emoji="✅" if act == "accept" else "❌",
            style=discord.ButtonStyle.success if act == "accept" else discord.ButtonStyle.danger,
            custom_id=f"staffapp:{act}:{app_id}"))
        self.act, self.app_id = act, app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        plugin = interaction.client.plugins.running("staff_applications") if getattr(interaction.client, "plugins",
                                                                                      None) else None
        if plugin is None:
            await interaction.response.send_message("Staff applications are switched off.", ephemeral=True)
            return
        await plugin.decide(interaction, self.app_id, self.act == "accept")


class ApplyForm(discord.ui.Modal):
    def __init__(self, plugin: "StaffApplications", questions: list[str]) -> None:
        super().__init__(title="Staff application"[:45], timeout=900)
        self.plugin, self.fields = plugin, []
        for q in questions[:5]:
            field = discord.ui.TextInput(label=q[:45], style=discord.TextStyle.paragraph, max_length=600,
                                         required=True)
            self.fields.append((q, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.plugin.submit(interaction, [(q, str(f.value)) for q, f in self.fields])


class StaffApplications(Plugin):
    NAME = "Staff applications"
    ICON = "📋"
    DESCRIPTION = "Members apply for staff with /apply (your questions); staff accept or decline with one click."
    SETTINGS = (
        Setting("questions", "Questions (one per line, up to 5)", "textarea", DEFAULT_QUESTIONS),
        Setting("channel", "Review channel (staff only)", "channel", help="Where applications are posted."),
        Setting("role", "Role for accepted applicants", "role", help="Empty = no role (you add them by hand)."),
        Setting("open", "Applications open", "bool", True),
        Setting("wait_days", "Days before applying again after a decline", "int", 14, min=0, max=365),
    )

    async def on_load(self) -> None:
        self.bot.add_dynamic_items(AppButton)

    async def on_unload(self) -> None:
        self.bot.remove_dynamic_items(AppButton)

    def questions(self) -> list[str]:
        return [q.strip() for q in str(self.setting("questions") or "").splitlines() if q.strip()][:5]

    def blocked(self, user_id: int) -> str | None:
        if not self.setting("open"):
            return "Staff applications are closed right now."
        for app in self.data.items("app:").values():
            if app["user"] == user_id and app["state"] == "open":
                return "You already have an open application – the staff will get back to you."
        until = int(self.data.get(f"wait:{user_id}", 0))
        if until > self.now():
            return f"You can apply again <t:{until}:R>."
        return None

    @app_commands.command(name="apply", description="Apply for the staff team.")
    @app_commands.guild_only()
    async def apply_cmd(self, interaction: discord.Interaction) -> None:
        if (why := self.blocked(interaction.user.id)) is not None:
            await interaction.response.send_message(why, ephemeral=True)
            return
        if not self.questions() or self.channel(self.setting("channel")) is None:
            await interaction.response.send_message("Applications aren't set up yet.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyForm(self, self.questions()))

    async def submit(self, interaction: discord.Interaction, answers: list[tuple[str, str]]) -> None:
        if (why := self.blocked(interaction.user.id)) is not None:
            await interaction.response.send_message(why, ephemeral=True)
            return
        n = int(self.data.get("next", 0)) + 1
        self.data.set("next", n)
        user = interaction.user
        self.data.set(f"app:{n}", {"user": user.id, "name": str(user), "state": "open", "at": self.now(),
                                   "answers": answers})
        e = discord.Embed(title=f"📋 Staff application #{n}", colour=0x3498DB,
                          description=f"From {user.mention} ({self.md(str(user))})"
                                      + (f" · in game: **{self.md(p)}**" if (p := self.linked_player(user.id)) else ""))
        for q, a in answers:
            e.add_field(name=q[:250], value=self.md(a)[:1000] or "–", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(AppButton("accept", n))
        view.add_item(AppButton("decline", n))
        await self.post(self.channel(self.setting("channel")), embed=e, view=view)
        await interaction.response.send_message("✅ Thanks – your application was sent to the staff.", ephemeral=True)

    async def decide(self, interaction: discord.Interaction, app_id: int, accept: bool) -> None:
        if not is_admin(self.bot, interaction.user):
            await interaction.response.send_message("Only staff can decide applications.", ephemeral=True)
            return
        app = self.data.get(f"app:{app_id}")
        if not app or app["state"] != "open":
            await interaction.response.send_message("This application was already decided.", ephemeral=True)
            return
        app["state"], app["by"] = ("accepted" if accept else "declined"), str(interaction.user)
        self.data.set(f"app:{app_id}", app)
        note = ""
        guild = interaction.guild
        member = guild.get_member(app["user"]) if guild else None
        role = guild.get_role(int(self.setting("role"))) if guild and accept and self.setting("role") else None
        if role is not None and member is not None:
            try:
                await member.add_roles(role, reason=f"Staff application #{app_id} accepted")
                note = f" and got {role.mention}"
            except discord.HTTPException as exc:
                note = f" – couldn't give {role.mention}: {exc}"
        if not accept and self.setting("wait_days"):
            self.data.set(f"wait:{app['user']}", self.now() + self.setting("wait_days") * 86400)
        try:
            user = member or self.bot.get_user(app["user"]) or await self.bot.fetch_user(app["user"])
            await user.send(f"📋 Your staff application on **{self.md(self.server_name)}** was "
                            + ("**accepted** 🎉 – welcome to the team!" if accept else "declined. Thanks for applying!"))
        except (discord.HTTPException, AttributeError):
            pass
        self.audit(f"📋 Staff application #{app_id} of <@{app['user']}> {app['state']} by "
                   f"{self.md(str(interaction.user))}")
        await interaction.response.send_message(f"{'✅ Accepted' if accept else '❌ Declined'} – <@{app['user']}> was told"
                                                f"{note}.", allowed_mentions=discord.AllowedMentions.none())

    def problems(self) -> list[str]:
        out = []
        if not self.setting("channel"):
            out.append("Pick the review channel.")
        if not self.questions():
            out.append("Add at least one question.")
        return out

    def status(self) -> dict:
        apps = list(self.data.items("app:").values())
        return {"Open": sum(a["state"] == "open" for a in apps), "Accepted": sum(a["state"] == "accepted" for a in apps),
                "Declined": sum(a["state"] == "declined" for a in apps)}
