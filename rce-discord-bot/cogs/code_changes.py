"""Code changes by the management AI: apply (with checks and hot reload), ✅ Keep / ↩️ Revert buttons, /code …

Who: MANAGER_AI_CODE must be on, and only the user IDs in ADMIN_USER_IDS (the owner) – or, if that list is
empty, the admins. Only on the main server (the extra servers run the same code).
  /code pending   what is changed and not confirmed yet
  /code keep      everything works: the .bak copies are deleted
  /code revert    every changed file back to its original
Emergency (bot doesn't start): `python revert_code.py` in the bot folder.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import servers
from support import code_edit
from utils import is_admin

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.code_changes")
STATE_FILE = code_edit.ROOT / "data" / "code_changes.json"


def may_edit_code(bot, user) -> bool:
    cfg = bot.config
    import saas_mode
    if not getattr(cfg, "manager_ai_code", True) or servers.is_child() or saas_mode.enabled():
        return False
    if cfg.admin_user_ids:
        return user.id in cfg.admin_user_ids
    return is_admin(bot, user)


class CodeChangeView(discord.ui.View):
    """✅ Keep / ↩️ Revert – persistent (works after a restart too)."""

    def __init__(self, cog: "CodeChanges") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not may_edit_code(self.cog.bot, interaction.user):
            await interaction.response.send_message("Only the owner can keep or revert code changes.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Keep (works)", emoji="✅", style=discord.ButtonStyle.success, custom_id="codechange:keep")
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        files = self.cog.changes.keep()
        self.cog.audit(f"✅ {interaction.user.mention}: code changes kept ({', '.join(files) or 'none pending'})")
        await interaction.response.edit_message(
            content=(f"✅ Kept – the backups (.bak) of {len(files)} file(s) were deleted." if files
                     else "Nothing pending any more."), view=None)

    @discord.ui.button(label="Revert", emoji="↩️", style=discord.ButtonStyle.danger, custom_id="codechange:revert")
    async def revert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        text = await self.cog.revert(str(interaction.user))
        await interaction.edit_original_response(content=text, view=None)


class CodeChanges(commands.Cog):
    code = app_commands.Group(name="code", description="Code changes made by the management AI", guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.changes = code_edit.ChangeSet(STATE_FILE)

    async def cog_load(self) -> None:
        self.bot.add_view(CodeChangeView(self))

    def audit(self, text: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(text)

    def view(self) -> CodeChangeView:
        return CodeChangeView(self)

    def summary(self) -> str:
        data = self.changes.pending()
        if not data["files"]:
            return "No pending code changes."
        lines = [f"• `{f}`" + (" (new file)" if e.get("created") else "") for f, e in data["files"].items()]
        return (f"🧩 **{len(lines)} changed file(s), not confirmed yet** – originals kept as .bak:\n" + "\n".join(lines)
                + "\nTest it, then **✅ Keep** – or **↩️ Revert** to put everything back.")

    # ------------------------------------------------------------ Applying (used by the management AI)

    async def _reload(self, path) -> tuple[str, str | None]:
        """("reloaded" | "restart needed" | "not loaded", error)."""
        mod = code_edit.module_name(path)
        if mod and mod.startswith("cogs.") and mod in self.bot.extensions:
            try:
                await self.bot.reload_extension(mod)
                return "reloaded", None
            except Exception as exc:                       # broken cog: caller undoes the edit
                return "reload failed", f"{type(exc).__name__}: {exc}"[:300]
        if mod and mod.startswith("plugins.") and getattr(self.bot, "plugins", None) is not None:
            pid = mod.split(".", 1)[1]
            running = self.bot.plugins.running(pid) if hasattr(self.bot.plugins, "running") else None
            if running is not None:
                try:
                    await self.bot.plugins.reload(pid)
                    return "reloaded", None
                except Exception as exc:
                    return "reload failed", f"{type(exc).__name__}: {exc}"[:300]
            return "not loaded", None
        if mod:
            return "restart needed", None
        return ("browser reload" if path.suffix in (".js", ".css", ".html", ".svg") else "saved"), None

    async def edit(self, name: str, *, edits=None, content=None, create=False, by: str, why: str = "") -> dict:
        path, old, new = code_edit.prepare(name, edits=edits, content=content, create=create)
        created_now = not path.exists()
        self.changes.apply(path, old, new, by=by, why=why)
        problem = await asyncio.to_thread(code_edit.import_check, path)
        state = None
        if problem is None:
            state, problem = await self._reload(path)
        if problem is not None:
            self.changes.undo_one(path, old, created_now)
            if state == "reload failed":
                try:
                    await self._reload(path)                # the old version back in memory
                except Exception:
                    pass
            raise code_edit.CodeEditError(f"Undone – the new code doesn't work: {problem}")
        self.audit(f"🧩 Code change by {by}: `{code_edit.rel(path)}` ({state})" + (f" – {why}" if why else ""))
        return {"file": code_edit.rel(path), "result": state, "created": created_now}

    async def revert(self, by: str) -> str:
        files = self.changes.revert()
        if not files:
            return "Nothing to revert."
        notes = []
        for f in files:
            path = code_edit.ROOT / f
            if path.exists():
                state, _ = await self._reload(path)
                notes.append(f"`{f}` ({state})")
            else:
                notes.append(f"`{f}` (deleted)")
        self.audit(f"↩️ {by}: code changes reverted – {', '.join(files)}")
        restart = any("restart needed" in n for n in notes)
        return "↩️ Reverted: " + ", ".join(notes) + ("\nRestart the bot so the originals are used." if restart else "")

    # ------------------------------------------------------------ /code

    async def _check(self, interaction: discord.Interaction) -> bool:
        if may_edit_code(self.bot, interaction.user):
            return True
        await interaction.response.send_message("Only the owner (ADMIN_USER_IDS) can manage code changes "
                                                "(and MANAGER_AI_CODE must be on).", ephemeral=True)
        return False

    @code.command(name="pending", description="Shows the code changes that aren't confirmed yet.")
    async def pending_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        data = self.changes.pending()
        files = []
        for f in list(data["files"])[:5]:
            path = code_edit.ROOT / f
            bak = path.with_name(path.name + ".bak")
            old = bak.read_text(encoding="utf-8") if bak.exists() else ""
            new = path.read_text(encoding="utf-8") if path.exists() else ""
            files.append(discord.File(io.BytesIO(code_edit.diff(f, old, new).encode()),
                                      filename=f.replace("/", "_") + ".diff"))
        await interaction.response.send_message(self.summary(), files=files, ephemeral=True,
                                                view=self.view() if data["files"] else discord.utils.MISSING)

    @code.command(name="keep", description="The code changes work: delete the .bak copies.")
    async def keep_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        files = self.changes.keep()
        self.audit(f"✅ {interaction.user.mention}: code changes kept ({', '.join(files) or 'none'})")
        await interaction.response.send_message(f"✅ Kept {len(files)} file(s)." if files else "Nothing pending.",
                                                ephemeral=True)

    @code.command(name="revert", description="Puts every changed file back to its original.")
    async def revert_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.revert(str(interaction.user)), ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(CodeChanges(bot))
