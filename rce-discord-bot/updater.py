"""Automatic updates from GitHub – the bot updates itself when the repository's branch changes.

Two install types are supported:

* **ZIP install** (downloaded from GitHub): the bot asks the GitHub API for the newest commit of the branch,
  downloads exactly that commit as a ZIP, checks it (Python syntax, new requirements installable) and only
  then writes the changed files. A manifest in data/update_state.json remembers what was installed, so
  files removed upstream are deleted too – but never your own files.
* **Git checkout** (git clone): git fetch + a fast-forward merge (only if you have no local changes).

Never touched: .env, data/, logs/, bin/, .venv/, PUBLIC_LINK.txt. rules.md, support_knowledge.md and prices.txt are
only replaced if you never edited them.

After an update the bot exits with EXIT_UPDATED; start.bat/start.sh start it again right away.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("rce.updater")

BASE_DIR = Path(__file__).resolve().parent
SUBDIR = "rce-discord-bot"                   # the bot's folder inside the repository
STATE_FILE = Path("data") / "update_state.json"
# Never written or deleted by the updater (relative to the bot folder; folders end with "/")
PROTECTED = (".env", "PUBLIC_LINK.txt", "data/", "logs/", "bin/", ".venv/", "venv/", ".git/", ".pytest_cache/")
# Files the owner edits (also in the web interface): only replaced while still unchanged
USER_EDITABLE = ("rules.md", "support_knowledge.md", "prices.txt")
# Start scripts run while the bot runs: only replaced if they are the update-safe version (marker inside)
START_SCRIPTS = ("start.bat", "start.sh")
SAFE_MARKER = b"update-safe"
USER_AGENT = "rce-discord-bot-updater"


class UpdateError(Exception):
    pass


@dataclass
class Result:
    status: str                    # up_to_date / updated / skipped / error
    message: str = ""
    sha: str | None = None
    title: str = ""                # first line of the commit message
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    requirements_changed: bool = False

    @property
    def restart_needed(self) -> bool:
        return self.status == "updated"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_protected(rel: str) -> bool:
    parts = rel.split("/")
    return (rel in PROTECTED or any(p.endswith("/") and rel.startswith(p) for p in PROTECTED)
            or "__pycache__" in parts or rel.endswith(".pyc"))


def _http_get(url: str, *, timeout: int = 60, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Updater:
    def __init__(self, repo: str, branch: str = "main", *, base_dir: Path = BASE_DIR,
                 http_get: Callable[..., bytes] = _http_get,
                 pip_install: Callable[[Path], None] | None = None,
                 before_apply: Callable[[], None] | None = None) -> None:
        self.repo = repo.strip().strip("/")
        self.branch = branch.strip() or "main"
        self.base = Path(base_dir)
        self.http_get = http_get
        self.pip_install = pip_install or self._pip_install
        self.before_apply = before_apply      # e.g. a backup right before files are replaced
        self.last_check: float | None = None
        self.last_result: Result | None = None

    # ------------------------------------------------------------ Info

    @property
    def state_path(self) -> Path:
        return self.base / STATE_FILE

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def git_root(self) -> Path | None:
        """Repository root if the bot runs from a git checkout (and git is installed)."""
        for folder in (self.base, self.base.parent):
            if (folder / ".git").exists() and shutil.which("git"):
                return folder
        return None

    @property
    def mode(self) -> str:
        return "git" if self.git_root() else "zip"

    def current_version(self) -> dict:
        """{"sha", "title", "installed_at"} of the installed version (as far as known)."""
        root = self.git_root()
        if root:
            try:
                sha = self._git(root, "rev-parse", "HEAD").strip()
                title = self._git(root, "log", "-1", "--format=%s").strip()
                return {"sha": sha, "title": title, "installed_at": None}
            except UpdateError:
                return {}
        state = self.load_state()
        return {k: state.get(k) for k in ("sha", "title", "installed_at")}

    # ------------------------------------------------------------ Check + apply

    def check(self) -> Result:
        """Looks for a newer version and installs it. Result.restart_needed -> restart the bot."""
        self.last_check = time.time()
        try:
            root = self.git_root()
            result = self._update_git(root) if root else self._update_zip()
        except UpdateError as exc:
            result = Result("error", str(exc))
        except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile) as exc:
            result = Result("error", f"Update check failed: {exc}")
        self.last_result = result
        return result

    def latest_commit(self) -> tuple[str, str]:
        """Newest commit (sha, title) of the branch.

        Uses git's own protocol (what `git ls-remote` does) – unlike the GitHub API it has no limit of 60
        requests per hour per IP, which shared networks (university, VPN) run into quickly. The title comes from
        the commits feed; the API is only the last fallback."""
        try:
            sha = self._ls_remote()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 404):
                raise UpdateError(f"GitHub: repository {self.repo} not found (private repositories are not "
                                  "supported).") from exc
            log.debug("ls-remote failed (%s) – trying the GitHub API", exc)
            return self._latest_commit_api()
        return sha, self._title(sha)

    def _ls_remote(self) -> str:
        raw = self.http_get(f"https://github.com/{self.repo}.git/info/refs?service=git-upload-pack", timeout=20)
        m = re.search(rb"([0-9a-f]{40}) refs/heads/" + re.escape(self.branch.encode()) + rb"[\x00\n]", raw)
        if not m:
            raise UpdateError(f"GitHub: branch {self.branch} not found in {self.repo}.")
        return m.group(1).decode()

    def _title(self, sha: str) -> str:
        """Commit title from the public commits feed (best effort – empty if unavailable)."""
        try:
            feed = self.http_get(f"https://github.com/{self.repo}/commits/{self.branch}.atom", timeout=10)
        except (urllib.error.URLError, OSError, ValueError):
            return ""
        m = re.search(rb"Commit/" + sha.encode() + rb"</id>.*?<title>\s*(.*?)\s*</title>", feed, re.DOTALL)
        if not m:
            return ""
        import html
        return html.unescape(m.group(1).decode(errors="replace")).splitlines()[0][:200]

    def _latest_commit_api(self) -> tuple[str, str]:
        url = f"https://api.github.com/repos/{self.repo}/commits/{self.branch}"
        try:
            data = json.loads(self.http_get(url, timeout=20, accept="application/vnd.github+json"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError(f"GitHub: repository {self.repo} or branch {self.branch} not found "
                                  "(private repositories are not supported).") from exc
            if exc.code in (403, 429):
                raise UpdateError("GitHub rate limit reached – trying again later.") from exc
            raise
        return data["sha"], (data.get("commit", {}).get("message") or "").splitlines()[0][:200]

    def _update_zip(self) -> Result:
        sha, title = self.latest_commit()
        state = self.load_state()
        if state.get("sha") == sha:
            return Result("up_to_date", "Already the newest version.", sha, title)

        archive = self.http_get(f"https://codeload.github.com/{self.repo}/zip/{sha}", timeout=120)
        new_files = self._files_from_zip(archive)
        if not new_files:
            raise UpdateError(f"The download contains no {SUBDIR}/ folder.")
        manifest: dict[str, str] = state.get("files") or {}

        # What would change? (byte comparison – only differing files are written)
        changed, skipped = [], []
        for rel, data in sorted(new_files.items()):
            if _is_protected(rel):
                continue
            target = self.base / rel
            old = target.read_bytes() if target.exists() else None
            if old == data:
                continue
            if old is not None and rel in USER_EDITABLE and manifest.get(rel) != _sha256(old):
                skipped.append(rel)          # edited by the owner -> keep
                continue
            if old is not None and rel in START_SCRIPTS and SAFE_MARKER not in old:
                skipped.append(rel)          # old start script is running -> never overwrite it in place
                (self.base / (rel + ".new")).write_bytes(data)
                continue
            changed.append(rel)
        removed = [rel for rel in manifest if rel not in new_files and not _is_protected(rel)
                   and rel not in USER_EDITABLE and (self.base / rel).is_file()]

        new_state = {"sha": sha, "title": title, "installed_at": int(time.time()),
                     "files": {rel: _sha256(data) for rel, data in new_files.items()}}
        if not changed and not removed:
            self._save_state(new_state)      # same content (e.g. first check after a manual install)
            return Result("up_to_date", "Already the newest version.", sha, title)

        # Checks BEFORE anything is written: Python syntax, then the (possibly new) requirements
        for rel in changed:
            if rel.endswith(".py"):
                try:
                    compile(new_files[rel], rel, "exec")
                except SyntaxError as exc:
                    raise UpdateError(f"Update {sha[:7]} skipped: syntax error in {rel} ({exc.msg}, line {exc.lineno}).")
        req_changed = "requirements.txt" in changed
        if req_changed:
            with tempfile.TemporaryDirectory() as tmp:
                req = Path(tmp) / "requirements.txt"
                req.write_bytes(new_files["requirements.txt"])
                self.pip_install(req)

        self._before_apply()
        for rel in changed:
            target = self.base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".updating")
            tmp.write_bytes(new_files[rel])
            os.replace(tmp, target)
        for rel in removed:
            try:
                (self.base / rel).unlink()
            except OSError:
                pass
        self._save_state(new_state)
        note = f" Kept: {', '.join(skipped)}." if skipped else ""
        return Result("updated", f"Installed {len(changed)} changed file(s).{note}", sha, title, changed, removed,
                      req_changed)

    def _files_from_zip(self, archive: bytes) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                parts = info.filename.split("/")
                # <repo>-<sha>/rce-discord-bot/<path>
                if info.is_dir() or len(parts) < 3 or parts[1] != SUBDIR:
                    continue
                rel = "/".join(parts[2:])
                if not rel or ".." in parts or rel.startswith("/"):
                    continue
                files[rel] = zf.read(info)
        return files

    def _update_git(self, root: Path) -> Result:
        self._git(root, "fetch", "--quiet", "origin", self.branch, timeout=120)
        head = self._git(root, "rev-parse", "HEAD").strip()
        new = self._git(root, "rev-parse", "FETCH_HEAD").strip()
        title = self._git(root, "log", "-1", "--format=%s", new).strip()
        if head == new or self._git_ok(root, "merge-base", "--is-ancestor", new, head):
            return Result("up_to_date", "Already the newest version.", head, title)
        if not self._git_ok(root, "merge-base", "--is-ancestor", head, new):
            return Result("skipped", "Your checkout has its own commits – update it yourself with git pull.", new, title)
        if self._git(root, "status", "--porcelain", "--untracked-files=no").strip():
            return Result("skipped", "Local changes in the bot files – update it yourself with git pull.", new, title)
        prefix = "" if root == self.base else f"{SUBDIR}/"
        changed = [line[len(prefix):] for line in self._git(root, "diff", "--name-only", head, new).splitlines()
                   if line.startswith(prefix)]
        req_changed = "requirements.txt" in changed
        if req_changed:
            with tempfile.TemporaryDirectory() as tmp:
                req = Path(tmp) / "requirements.txt"
                req.write_text(self._git(root, "show", f"{new}:{prefix}requirements.txt"), encoding="utf-8")
                self.pip_install(req)
        self._before_apply()
        self._git(root, "merge", "--ff-only", "--quiet", new)
        return Result("updated", f"Updated to {new[:7]} ({len(changed)} changed file(s)).", new, title, changed,
                      requirements_changed=req_changed)

    # ------------------------------------------------------------ Helpers

    def _before_apply(self) -> None:
        if self.before_apply is not None:
            try:
                self.before_apply()
            except Exception as exc:  # a failed backup must not block the update
                log.warning("Before update: %s", exc)

    @staticmethod
    def _git(root: Path, *args: str, timeout: int = 30) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"git {args[0]} failed: {exc}") from exc
        if proc.returncode != 0:
            raise UpdateError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc.stdout

    @staticmethod
    def _git_ok(root: Path, *args: str) -> bool:
        try:
            Updater._git(root, *args)
            return True
        except UpdateError:
            return False

    @staticmethod
    def _pip_install(requirements: Path) -> None:
        log.info("Update: installing new Python packages …")
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                               "-r", str(requirements)], capture_output=True, text=True, timeout=600,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            raise UpdateError("Update skipped: installing the new Python packages failed: "
                              + (proc.stderr or proc.stdout).strip()[-400:])
