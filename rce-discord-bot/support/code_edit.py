"""Code changes by the management AI – with a safety net.

Every changed file's original is kept next to it as <file>.bak until an admin confirms the change works
("✅ Keep") – or "↩️ Revert" puts every original back. All changes since the last Keep/Revert form one pending
change set, remembered in data/code_changes.json (so Keep/Revert also work after a restart). If the bot doesn't even
start any more, run `python revert_code.py` in the bot folder: it puts the originals back without the bot.

Checks before a change is written: the path is inside the bot folder and editable, Python still compiles, and the
change doesn't touch the guard rails (secrets, the AI's own limits, who is admin). After writing, a Python module
must still import – otherwise that edit is undone right away.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITABLE = {".py", ".md", ".js", ".css", ".html", ".json", ".txt", ".svg"}
SKIP_DIRS = {"data", ".git", "venv", ".venv", "env", "__pycache__", "servers", "node_modules", ".pytest_cache"}
# Never edited by the AI: its own tools and limits, the safety net itself, logins/2FA
BLOCKED_FILES = {"cogs/manager_ai.py", "cogs/code_changes.py", "support/code_edit.py", "revert_code.py",
                 "web/users.py", "web/totp.py", "envfile.py"}
# Lines the AI may not add, change or remove anywhere: the AI's limits on the admin website, secrets, who is admin
PROTECTED = re.compile(r"AI_BLOCKED|AI_LOCKED|AI_SECRET|AI_USER|AI_MAX|ai_blocked|ai_clean_settings|_ai_redact|_ai_key|"
                       r"admin_user_ids|admin_role_ids|def is_admin|def admin_only|manager_ai_|MANAGER_AI_|"
                       r"REAUTH_PATHS|OWNER_ONLY_PREFIXES|password_hash|web_secret")
# Functions whose code must stay exactly as it is (who is admin, logins/sessions, the AI's limits, servers)
PROTECTED_FUNCS = {"is_admin", "admin_only", "may_edit_code", "_auth_middleware", "_session_user", "_session_key",
                   "_sign", "_gate_middleware", "_may_see_panel", "_servers_owner", "ai_blocked", "ai_clean_settings",
                   "ai_request", "ai_endpoints", "_ai_redact", "check_password", "verify_password", "login",
                   "reauth", "_origin_middleware", "_host_middleware"}
MAX_FILE = 400_000
MAX_READ_LINES = 400


class CodeEditError(Exception):
    """Readable reason a change can't be made."""


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def safe_path(name: str, *, must_exist: bool = True) -> Path:
    """A file inside the bot folder that may be edited – else CodeEditError."""
    name = str(name or "").strip().replace("\\", "/").lstrip("/")
    if not name or ".." in name.split("/"):
        raise CodeEditError("Give a path inside the bot folder, e.g. cogs/kits.py.")
    path = (ROOT / name).resolve()
    try:
        r = rel(path)
    except ValueError:
        raise CodeEditError("Only files inside the bot folder.") from None
    parts = r.split("/")
    if any(p in SKIP_DIRS or (p.startswith(".") and p not in (".",)) for p in parts[:-1]) or parts[-1].startswith("."):
        raise CodeEditError(f"{r} is off-limits (data, settings, git, virtualenv …).")
    if path.suffix not in EDITABLE or r.endswith(".bak"):
        raise CodeEditError(f"Only {', '.join(sorted(EDITABLE))} files can be edited.")
    if r in BLOCKED_FILES:
        raise CodeEditError(f"{r} is the AI's own safety code – it can't be edited by the AI.")
    if must_exist and not path.is_file():
        raise CodeEditError(f"{r} doesn't exist.")
    return path


def list_files(query: str = "") -> list[dict]:
    q = (query or "").lower()
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in EDITABLE:
            continue
        r = rel(path)
        if any(p in SKIP_DIRS or p.startswith(".") for p in r.split("/")):
            continue
        if q and q not in r.lower():
            continue
        out.append({"path": r, "lines": path.read_text(encoding="utf-8", errors="replace").count("\n") + 1,
                    "editable": r not in BLOCKED_FILES})
    return out[:300]


def read(name: str, start: int = 1, count: int = 200) -> dict:
    path = (ROOT / str(name).lstrip("/")).resolve()
    try:
        r = rel(path)
    except ValueError:
        raise CodeEditError("Only files inside the bot folder.") from None
    if not path.is_file() or path.suffix not in EDITABLE or any(p in SKIP_DIRS or p.startswith(".")
                                                                  for p in r.split("/")):
        raise CodeEditError(f"{r} can't be read (missing, or data/settings).")
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    start = max(1, int(start or 1))
    count = max(1, min(int(count or 200), MAX_READ_LINES))
    chunk = lines[start - 1:start - 1 + count]
    return {"path": r, "total_lines": len(lines), "from": start, "to": start + len(chunk) - 1,
            "text": "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))}


def diff(name: str, old: str, new: str, context: int = 2) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{name}", f"b/{name}",
                                        n=context))


def _changed_lines(old: str, new: str) -> list[str]:
    return [line[1:] for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def _protected_sources(text: str) -> dict[str, list[str]]:
    """name -> source of every protected function (also methods) in this Python code."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROTECTED_FUNCS:
            out.setdefault(node.name, []).append(ast.get_source_segment(text, node) or "")
    return out


def prepare(name: str, *, edits: list | None = None, content: str | None = None,
            create: bool = False) -> tuple[Path, str, str]:
    """(path, old text, new text) for a change – checked, nothing written yet. edits: [{"old", "new"}] exact
    replacements (each old text must occur exactly once); content: the whole new file (create=True for a new one)."""
    path = safe_path(name, must_exist=not create)
    exists = path.is_file()
    if create and exists:
        raise CodeEditError(f"{rel(path)} exists already – edit it instead.")
    old = path.read_text(encoding="utf-8") if exists else ""
    if content is not None:
        new = str(content)
    elif edits:
        new = old
        for n, e in enumerate(edits, 1):
            if not isinstance(e, dict) or not isinstance(e.get("old"), str) or not isinstance(e.get("new"), str):
                raise CodeEditError(f"Edit {n}: needs 'old' and 'new' text.")
            hits = new.count(e["old"]) if e["old"] else 0
            if hits != 1:
                raise CodeEditError(f"Edit {n}: the old text was found {hits} times – it must match exactly once "
                                    "(copy it from read_code_file, with enough lines around it).")
            new = new.replace(e["old"], e["new"], 1)
    else:
        raise CodeEditError("Give edits or the new content.")
    if new == old:
        raise CodeEditError("That changes nothing.")
    if len(new) > MAX_FILE:
        raise CodeEditError("The file would be too big.")
    touched = [line for line in _changed_lines(old, new) if PROTECTED.search(line)]
    if touched:
        raise CodeEditError("This change touches protected lines (secrets, admin rights or the AI's own limits): "
                            + touched[0].strip()[:120])
    if path.suffix == ".py":
        try:
            compile(new, rel(path), "exec")
        except SyntaxError as exc:
            raise CodeEditError(f"Python error in the new code, line {exc.lineno}: {exc.msg}") from None
        before, after = _protected_sources(old), _protected_sources(new)
        changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
        if changed:
            raise CodeEditError(f"This change touches protected code ({', '.join(changed)}: admin rights, logins or "
                                "the AI's own limits) – that can't be changed by the AI.")
    return path, old, new


def module_name(path: Path) -> str | None:
    if path.suffix != ".py":
        return None
    return ".".join(Path(rel(path)).with_suffix("").parts)


def import_check(path: Path, timeout: float = 30) -> str | None:
    """Imports the changed module in a separate Python: None if fine, else the error."""
    mod = module_name(path)
    if mod is None or mod in ("bot", "revert_code"):
        return None
    env = {k: v for k, v in os.environ.items()}
    try:
        res = subprocess.run([sys.executable, "-c", f"import importlib; importlib.import_module({mod!r})"],
                             cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "importing it took too long"
    if res.returncode != 0:
        lines = [x for x in (res.stderr or "").strip().splitlines() if x.strip()]
        return (lines[-1] if lines else f"exit code {res.returncode}")[:300]
    return None


# ------------------------------------------------------------ The pending change set (.bak files)

class ChangeSet:
    """All code changes since the last Keep/Revert. state file: {"files": {path: {"created": bool}}, "log": [...]}"""

    def __init__(self, state_file: Path) -> None:
        self.state_file = Path(state_file)

    def load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"files": {}, "log": []}

    def _save(self, data: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def pending(self) -> dict:
        return self.load()

    def apply(self, path: Path, old: str, new: str, *, by: str, why: str = "") -> None:
        """Writes the new text; the first time a file changes in this set, its original goes to <file>.bak."""
        data = self.load()
        r = rel(path)
        if r not in data["files"]:
            created = not path.exists()
            if not created:
                bak = path.with_name(path.name + ".bak")
                bak.write_text(old, encoding="utf-8")
            data["files"][r] = {"created": created}
        path.write_text(new, encoding="utf-8")
        data["log"].append({"at": int(time.time()), "file": r, "by": by[:80], "why": why[:200]})
        data["log"] = data["log"][-100:]
        self._save(data)

    def undo_one(self, path: Path, old: str, created_now: bool) -> None:
        """Puts back the text from before the last edit of this file (a failed import/reload)."""
        data = self.load()
        r = rel(path)
        if created_now:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old, encoding="utf-8")
        entry = data["files"].get(r)
        if entry is not None:
            original = path.with_name(path.name + ".bak")
            same = (not path.exists() and entry["created"]) or (original.exists() and path.exists()
                                                                 and original.read_text(encoding="utf-8") == old)
            if same:                                      # back to the original: not pending any more
                original.unlink(missing_ok=True)
                data["files"].pop(r, None)
        if data["log"] and data["log"][-1]["file"] == r:
            data["log"].pop()
        self._save(data)

    def revert(self) -> list[str]:
        """Every changed file back to its original (.bak), new files deleted. Returns the files."""
        data = self.load()
        done = []
        for r, entry in data["files"].items():
            path = ROOT / r
            bak = path.with_name(path.name + ".bak")
            if entry.get("created"):
                path.unlink(missing_ok=True)
            elif bak.exists():
                bak.replace(path)
            else:
                continue
            done.append(r)
        self._save({"files": {}, "log": []})
        return done

    def keep(self) -> list[str]:
        """Confirmed working: the .bak copies are deleted. Returns the files."""
        data = self.load()
        for r in data["files"]:
            path = ROOT / r
            path.with_name(path.name + ".bak").unlink(missing_ok=True)
        self._save({"files": {}, "log": []})
        return list(data["files"])
