"""Reach the web interface from outside your network – via a Cloudflare tunnel.

Your PC opens an outgoing, encrypted connection to Cloudflare; Cloudflare gives you an https:// address
that forwards to the web interface. Nothing has to be opened on the router, and the web interface itself
keeps listening on 127.0.0.1 only.

* Quick tunnel (default): random address like https://xyz.trycloudflare.com. It stays the same while the
  bot runs (also across restarts from the web interface) and changes when the bot window is closed –
  the bot shows the current address in its window, the web interface and the Discord admin log.
* Fixed address: create a tunnel in the Cloudflare dashboard (needs a domain), enter its token as
  WEB_TUNNEL_TOKEN and the address as WEB_PUBLIC_URL.

cloudflared runs in a background thread (the bot restarts its asyncio loop on every restart, the tunnel
must outlive that). If it isn't installed, it is downloaded once from Cloudflare's official GitHub releases
into bin/.

The link also survives automatic updates: cloudflared writes to logs/cloudflared.log (not to a pipe of this
process), its PID and address are kept in data/tunnel_state.json, and before an update restart the bot only
detaches from it. The new bot process adopts the still-running cloudflared – same link.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger("rce.tunnel")

BOT_DIR = Path(__file__).resolve().parents[1]
BIN_DIR = BOT_DIR / "bin"
STATE_FILE = BOT_DIR / "data" / "tunnel_state.json"
LOG_FILE = BOT_DIR / "logs" / "cloudflared.log"
RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CONNECTED = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered", re.IGNORECASE)


def _asset_name() -> str | None:
    """File name of the cloudflared release for this computer (None = no single-file download)."""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64",
            "i386": "386", "i686": "386", "x86": "386"}.get(machine)
    if arch is None:
        return None
    if sys.platform == "win32":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform.startswith("linux"):
        return f"cloudflared-linux-{arch}"
    return None  # macOS: "brew install cloudflared"


def find_binary() -> Path | None:
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    for name in ("cloudflared.exe", "cloudflared"):
        if (BIN_DIR / name).exists():
            return BIN_DIR / name
    return None


def download_binary() -> Path:
    asset = _asset_name()
    if asset is None:
        raise RuntimeError("No automatic download for this system – install cloudflared yourself "
                           "(macOS: brew install cloudflared).")
    target = BIN_DIR / ("cloudflared.exe" if asset.endswith(".exe") else "cloudflared")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Downloading cloudflared once (~40 MB) from Cloudflare's GitHub releases …")
    tmp = target.with_suffix(".download")
    with urllib.request.urlopen(RELEASE_URL + asset, timeout=60) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 16)
    os.replace(tmp, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    log.info("cloudflared saved to %s", target)
    return target


# ------------------------------------------------------------ Process helpers (also for adopted processes)

def _pid_command(pid: int) -> str | None:
    """Executable/command line of a running process, None if it isn't running."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=,command="], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    stat_, _, command = out.partition(" ")
    if not out or stat_.startswith("Z"):   # gone or a zombie (ended, not yet reaped)
        return None
    return command.strip()


def _is_cloudflared(pid: int) -> bool:
    command = _pid_command(pid)
    return bool(command) and "cloudflared" in command.lower()


def _kill_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
            if handle:
                kernel32.TerminateProcess(handle, 1)
                kernel32.CloseHandle(handle)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _key_id(key: tuple) -> str:
    """Fingerprint of the tunnel settings for the state file (the token itself is never written)."""
    return hashlib.sha256(repr(key).encode()).hexdigest()[:24]


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Tunnel:
    def __init__(self, port: int, *, token: str = "", public_url: str = "", binary: Path | None = None,
                 state_path: Path | None = None, log_path: Path | None = None) -> None:
        self.port = port
        self.token = token
        self.fixed_url = public_url.rstrip("/")
        self.binary = binary
        self.state_path = state_path or STATE_FILE
        self.log_path = log_path or LOG_FILE
        self.url: str | None = self.fixed_url or None
        self.state = "starting"          # starting / online / error / stopped
        self.error: str | None = None
        self.adopted = False             # cloudflared from the previous bot process (after an update)
        # Called (from the tunnel thread) when the tunnel is online – the web interface hands it on to its loop
        self.on_online: Callable[[str | None], None] | None = None
        self._proc: subprocess.Popen | None = None
        self._pid: int | None = None     # running cloudflared (own or adopted)
        self._detached = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def key(self) -> tuple:
        return self.port, self.token, self.fixed_url

    @property
    def host(self) -> str | None:
        return urlparse(self.url).hostname if self.url else None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_forever, name="cloudflare-tunnel", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.state = "stopped"
        if self._detached:
            return                       # keeps running for the next bot process (update restart)
        self._kill()
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def detach(self) -> None:
        """Before an update restart: leave cloudflared running so the new bot process can adopt it."""
        if self._pid and self.state == "online":
            self._detached = True
            self._save_state()
        self._stop.set()

    # ------------------------------------------------------------ Thread

    def _command(self) -> list[str]:
        base = [str(self.binary), "tunnel", "--no-autoupdate"]
        if self.token:
            return base + ["run", "--token", self.token]
        return base + ["--url", f"http://127.0.0.1:{self.port}"]

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif proc is None and self._pid and _is_cloudflared(self._pid):
            _kill_pid(self._pid)         # adopted process
        self._pid = None

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"pid": self._pid, "url": self.url, "key": _key_id(self.key),
                                                   "saved": int(time.time())}), encoding="utf-8")
        except OSError as exc:
            log.debug("Tunnel state not saved: %s", exc)

    def _try_adopt(self) -> bool:
        """cloudflared of the previous bot process still running with the same settings -> take it over."""
        state = _read_state(self.state_path)
        pid = int(state.get("pid") or 0)
        if not pid:
            return False
        if state.get("key") != _key_id(self.key) or not state.get("url") and not self.token:
            if _is_cloudflared(pid):
                _kill_pid(pid)           # leftover with other settings
            return False
        if not _is_cloudflared(pid):
            return False
        self._pid, self.adopted = pid, True
        self.url = self.fixed_url or state.get("url")
        self.state, self.error = "online", None
        log.info("Tunnel: still running from before the restart – same link: %s", self.url)
        if self.on_online:
            try:
                self.on_online(self.url)
            except Exception:
                log.exception("Error in tunnel callback")
        return True

    def _run_forever(self) -> None:
        delay = 5
        if self._try_adopt():
            while not self._stop.is_set() and _pid_command(self._pid or 0):
                self._stop.wait(3)
            if self._stop.is_set():
                return
            log.warning("Tunnel: the adopted cloudflared has stopped – starting a new one.")
            self._pid, self.adopted = None, False
            if not self.fixed_url:
                self.url = None
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if self.binary is None:
                    self.binary = find_binary() or download_binary()
                self._run_once()
            except Exception as exc:  # missing binary, download error, …
                self.error = str(exc)
                log.error("Tunnel: %s", exc)
            if self._stop.is_set():
                break
            self.state = "error"
            if not self.fixed_url:
                self.url = None
            if time.monotonic() - started > 300:
                delay = 5  # it ran fine for a while -> retry quickly
            log.warning("Tunnel stopped (%s) – next attempt in %s s.", self.error, delay)
            self._stop.wait(delay)
            delay = min(delay * 2, 300)

    def _run_once(self) -> None:
        self.state, self.error = "starting", None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # No console window; own process group so Ctrl+C in the bot window doesn't hit it
            options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        else:
            options = {"start_new_session": True}
        # Output goes to a file, not a pipe: cloudflared keeps working when this process exits (update restart)
        with self.log_path.open("wb") as log_file:
            self._proc = proc = subprocess.Popen(self._command(), stdin=subprocess.DEVNULL, stdout=log_file,
                                                 stderr=log_file, **options)
        self._pid = proc.pid
        last: list[str] = []
        pending = b""
        with self.log_path.open("rb") as fh:
            while True:
                chunk = fh.readline()
                if not chunk or not chunk.endswith(b"\n"):
                    pending += chunk
                    if self._stop.is_set():
                        if not self._detached:
                            self._kill()     # also if stop() came while cloudflared was being started
                        return           # detach() leaves it running
                    if proc.poll() is not None and not chunk:
                        break
                    time.sleep(0.2)
                    continue
                line = (pending + chunk).decode(errors="replace").strip()
                pending = b""
                last = (last + [line])[-3:]
                found = QUICK_URL.search(line)
                if found and not self.token:
                    self.url = found.group(0)
                if self.state != "online" and CONNECTED.search(line) and (self.url or self.token):
                    self._save_state()
                    self.state = "online"
                    log.warning("🌍 Web interface reachable from anywhere: %s",
                                self.url or "your WEB_PUBLIC_URL (set it in the settings)")
                    if self.on_online:
                        try:
                            self.on_online(self.url)
                        except Exception:
                            log.exception("Error in tunnel callback")
        proc.wait()
        self._pid = None
        self.error = f"cloudflared exited (code {proc.returncode}): " + (" | ".join(last[-2:]) or "no output")


# One tunnel per bot process – it outlives restarts from the web interface
_current: Tunnel | None = None


def ensure_tunnel(port: int, *, token: str = "", public_url: str = "", binary: Path | None = None) -> Tunnel:
    global _current
    wanted = Tunnel(port, token=token, public_url=public_url, binary=binary)
    if _current is not None and _current.key == wanted.key and _current.state != "stopped":
        return _current
    if _current is not None:
        _current.stop()
    _current = wanted       # adopts a cloudflared left running by the previous process (see _try_adopt)
    _current.start()
    return _current


def stop_tunnel() -> None:
    global _current
    if _current is not None:
        _current.stop()
        _current = None
    else:
        # Tunnel off: also end a cloudflared left running by the previous bot process
        state = _read_state(STATE_FILE)
        pid = int(state.get("pid") or 0)
        if pid and _is_cloudflared(pid):
            _kill_pid(pid)
        if state:
            try:
                STATE_FILE.unlink()
            except OSError:
                pass


def detach_tunnel() -> None:
    """Before an update restart: keep cloudflared (and with it the link) running for the new process."""
    if _current is not None:
        _current.detach()


atexit.register(stop_tunnel)
