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
"""
from __future__ import annotations

import atexit
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

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
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


class Tunnel:
    def __init__(self, port: int, *, token: str = "", public_url: str = "", binary: Path | None = None) -> None:
        self.port = port
        self.token = token
        self.fixed_url = public_url.rstrip("/")
        self.binary = binary
        self.url: str | None = self.fixed_url or None
        self.state = "starting"          # starting / online / error / stopped
        self.error: str | None = None
        # Called (from the tunnel thread) when the tunnel is online – the web interface hands it on to its loop
        self.on_online: Callable[[str | None], None] | None = None
        self._proc: subprocess.Popen | None = None
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
        self._kill()

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

    def _run_forever(self) -> None:
        delay = 5
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
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no extra console window
        self._proc = proc = subprocess.Popen(self._command(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                             creationflags=flags)
        last: list[str] = []
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            last = (last + [line])[-3:]
            found = QUICK_URL.search(line)
            if found and not self.token:
                self.url = found.group(0)
            if self.state != "online" and (CONNECTED.search(line) or (found and not self.token)):
                self.state = "online"
                log.warning("🌍 Web interface reachable from anywhere: %s",
                            self.url or "your WEB_PUBLIC_URL (set it in the settings)")
                if self.on_online:
                    try:
                        self.on_online(self.url)
                    except Exception:
                        log.exception("Error in tunnel callback")
        proc.wait()
        self.error = f"cloudflared exited (code {proc.returncode}): " + (" | ".join(last[-2:]) or "no output")


# One tunnel per bot process – it outlives restarts from the web interface
_current: Tunnel | None = None


def ensure_tunnel(port: int, *, token: str = "", public_url: str = "", binary: Path | None = None) -> Tunnel:
    global _current
    wanted = Tunnel(port, token=token, public_url=public_url, binary=binary)
    if _current is not None and _current.key == wanted.key and _current.state != "stopped":
        return _current
    stop_tunnel()
    _current = wanted
    _current.start()
    return _current


def stop_tunnel() -> None:
    global _current
    if _current is not None:
        _current.stop()
        _current = None


atexit.register(stop_tunnel)
