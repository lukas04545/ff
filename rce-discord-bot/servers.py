"""Several Rust servers from one bot install.

  (bot folder)        the main server, as always: .env, data/ (database), logs/
  servers/<id>/       one folder per extra server: its own .env (RCON, Discord bot, channels …), data/ (its own
                      database: kits, players, stats, tickets …), logs/ and its own control panel and link

The main bot starts every extra server as a second bot process (same code, working folder servers/<id>), restarts it
after a crash and stops it when it stops itself. Each extra server needs its own Discord bot (token): one Discord bot
can only answer the slash commands of one server.

Shared by all servers (kept on the main server):
  * the panel logins – accounts, two-factor login, panel themes (the main server's data/) and the owner password
  * the admin abuse protection – its settings are edited on the main server and copied into every extra server, and
    a punishment on one server removes the abuser's admin on all of them (cogs/multiserver.py)
Kits can be copied from one server to another (copy_custom_kits).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from envfile import read_env, update_env
from settings_schema import FIELDS

log = logging.getLogger("rce.servers")

BASE_DIR = Path(__file__).resolve().parent
MAIN_ID = "main"
MAIN_NAME = "Main server"
ENV_HOME, ENV_ID, ENV_SHARED, ENV_PARENT = "RCE_HOME", "RCE_SERVER_ID", "RCE_SHARED_DATA", "RCE_PARENT_PID"
ENV_SHARED_BOT = "RCE_SHARED_BOT"   # "1": this extra server uses the main server's Discord bot (discord_router.py)
SERVERS_DIR = "servers"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
# Admin abuse protection: one setup for every server (edited on the main server)
OWN_ABUSE_KEY = "ABUSE_OWN_SETTINGS"   # an extra server with its own abuse protection settings (not the main's)
SHARED_KEYS = tuple(f.key for f in FIELDS if f.group == "abuse" and f.key != OWN_ABUSE_KEY)
GUILD_SPECIFIC = frozenset(f.key for f in FIELDS if f.group == "abuse" and f.pick)   # roles/channels of one Discord
LOGIN_KEYS = ("WEB_PASSWORD",)
DISCORD_KEYS = ("DISCORD_TOKEN", "GUILD_ID")      # servers on the main server's Discord bot: taken from the main
EXIT_FATAL = 3                       # bot.py: configuration error – waiting for the settings, no point restarting
RESTART_DELAYS = (5, 15, 60, 300)    # seconds before restarting a crashed server (then every 5 minutes)


# ------------------------------------------------------------ Where am I?

def server_id() -> str:
    return os.environ.get(ENV_ID) or MAIN_ID


def is_child() -> bool:
    return server_id() != MAIN_ID


def main_root() -> Path:
    """The main server's folder (the bot folder)."""
    shared = os.environ.get(ENV_SHARED)
    return Path(shared).parent if shared else Path.cwd()


def shared_data() -> Path:
    """Data every server shares (logins, 2FA, themes, the abuse bus): the main server's data/ folder."""
    shared = os.environ.get(ENV_SHARED)
    return Path(shared) if shared else Path("data")


def home(sid: str) -> Path:
    return main_root() if sid == MAIN_ID else main_root() / SERVERS_DIR / sid


def env_path(sid: str) -> Path:
    return home(sid) / ".env"


# ------------------------------------------------------------ Registry (data/servers.json of the main server)

def registry_path() -> Path:
    return main_root() / "data" / "servers.json"


def load_registry() -> list[dict]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict) and ID_PATTERN.match(str(s.get("id", "")))] \
        if isinstance(data, list) else []


def save_registry(servers: list[dict]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(servers, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def server_name(sid: str) -> str:
    if sid == MAIN_ID:
        return MAIN_NAME
    return next((s.get("name") or sid for s in load_registry() if s["id"] == sid), sid)


def _public_url(sid: str) -> str:
    """The server's public link (its PUBLIC_LINK.txt, written by the web interface when the tunnel is up)."""
    try:
        first = (home(sid) / "PUBLIC_LINK.txt").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""
    match = re.search(r"https://\S+", first)
    return re.sub(r"/[A-Za-z0-9_-]+$", "", match.group(0)) if match else ""   # without the panel address


def describe(sid: str) -> dict:
    """What the panel shows about a server – never its secrets."""
    env = read_env(env_path(sid))
    try:
        port = int(env.get("WEB_PORT") or 8080)
    except ValueError:
        port = 8080
    panel = (env.get("WEB_PANEL_PATH") or "").strip().strip("/")
    return {"id": sid, "name": server_name(sid), "main": sid == MAIN_ID, "port": port,
            "panel_path": f"/{panel}" if panel else "/admin", "public_url": _public_url(sid),
            "rcon": f"{env.get('RCON_HOST', '')}:{env.get('RCON_PORT', '')}".strip(":"),
            "discord": "main" if sid == MAIN_ID else discord_mode(sid)}


def discord_mode(sid: str) -> str:
    """"shared": the extra server uses the main server's Discord bot (pick the server in each command);
    "own": it has a Discord bot of its own."""
    return next((s.get("discord", "own") for s in load_registry() if s["id"] == sid), "own")


def all_servers() -> list[dict]:
    return [describe(MAIN_ID)] + [describe(s["id"]) | {"enabled": s.get("enabled", True)} for s in load_registry()]


# ------------------------------------------------------------ Shared settings

def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "ja", "on")


def own_abuse_settings(sid: str) -> bool:
    """An extra server set to its own abuse protection settings (instead of the main server's)."""
    return sid != MAIN_ID and _truthy(read_env(env_path(sid)).get(OWN_ABUSE_KEY))


def shared_values(child_env: dict) -> dict[str, str]:
    """The main server's abuse protection settings and owner password, for an extra server. Discord roles and
    channels only if both use the same Discord server (IDs of another Discord mean nothing there)."""
    main_env = read_env(env_path(MAIN_ID))
    same_guild = (child_env.get("GUILD_ID") or "").strip() in ("", (main_env.get("GUILD_ID") or "").strip())
    own = _truthy(child_env.get(OWN_ABUSE_KEY))       # its own abuse protection settings: not the main's
    out = {} if own else {k: main_env[k] for k in SHARED_KEYS
                          if k in main_env and (same_guild or k not in GUILD_SPECIFIC)}
    for key in LOGIN_KEYS:
        if main_env.get(key):
            out[key] = main_env[key]
    return out


def hash_main_password() -> None:
    """A plain WEB_PASSWORD in the main server's .env (typed in by hand) becomes its hash before the extra servers
    copy it – every server must have the very same hash, or a login on one panel isn't valid on the others."""
    from web.users import hash_password, is_hashed
    path = env_path(MAIN_ID)
    value = (read_env(path).get("WEB_PASSWORD") or "").strip()
    if value and not is_hashed(value):
        update_env(path, {"WEB_PASSWORD": hash_password(value)})


def sync_env(sid: str) -> dict[str, str]:
    """Copies the shared settings into an extra server's .env. Returns what changed."""
    path = env_path(sid)
    env = read_env(path)
    wanted = shared_values(env)
    if discord_mode(sid) == "shared":
        main_env = read_env(env_path(MAIN_ID))
        wanted.update({k: main_env.get(k, "") for k in DISCORD_KEYS})
    changed = {k: v for k, v in wanted.items() if env.get(k, "") != v}
    if changed:
        update_env(path, changed)
    return changed


# ------------------------------------------------------------ Adding / removing servers

def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "server"
    return slug if slug[0].isalnum() else "s" + slug


def create_server(name: str, rcon_host: str, rcon_port: str, rcon_password: str, discord_token: str,
                  guild_id: str = "") -> dict:
    """Adds an extra server: folder servers/<id> with its own .env. Raises ValueError with a readable message."""
    name = " ".join(str(name or "").split())[:40]
    rcon_host, rcon_port, discord_token = str(rcon_host).strip(), str(rcon_port).strip(), str(discord_token).strip()
    guild_id = str(guild_id or "").strip()
    if not name:
        raise ValueError("Give the server a name.")
    if not rcon_host or not rcon_port.isdigit() or not 0 < int(rcon_port) < 65536:
        raise ValueError("Enter the server's IP and RCON port.")
    if not rcon_password:
        raise ValueError("Enter the RCON password.")
    main_env = read_env(env_path(MAIN_ID))
    shared = not discord_token                # no token: the main server's Discord bot (one bot for all servers)
    if shared:
        discord_token, guild_id = main_env.get("DISCORD_TOKEN", ""), (main_env.get("GUILD_ID") or "").strip()
    else:
        if len(discord_token) < 50:
            raise ValueError("That doesn't look like a Discord bot token (Developer Portal → your application → Bot "
                             "→ Reset Token) – or leave it empty to use the main server's Discord bot.")
        if guild_id and not guild_id.isdigit():
            raise ValueError("The Discord server ID is digits only.")
        taken = [main_env.get("DISCORD_TOKEN", "")] + [read_env(env_path(s["id"])).get("DISCORD_TOKEN", "")
                                                       for s in load_registry() if s.get("discord") != "shared"]
        if discord_token in taken:
            raise ValueError("That Discord bot already runs another server – leave the token empty to share the "
                             "main server's bot, or use a bot of its own.")
    guild_id = guild_id or (main_env.get("GUILD_ID") or "").strip()      # empty: the same Discord as the main
    servers = load_registry()
    ids = {s["id"] for s in servers} | {MAIN_ID}
    sid, n = _slug(name), 2
    while sid in ids or home(sid).exists():
        sid = f"{_slug(name)[:20]}-{n}"
        n += 1
    ports = {describe(MAIN_ID)["port"]} | {describe(s["id"])["port"] for s in servers}
    port = max(ports) + 1
    folder = home(sid)
    (folder / "data").mkdir(parents=True)
    values = {"DISCORD_TOKEN": discord_token, "GUILD_ID": guild_id, "RCON_HOST": rcon_host, "RCON_PORT": rcon_port,
              "RCON_PASSWORD": rcon_password, "WEB_PORT": str(port), "WEB_HOST": main_env.get("WEB_HOST", ""),
              "WEB_TUNNEL": main_env.get("WEB_TUNNEL", "true") or "true", "WEB_OPEN_BROWSER": "false",
              "AUTO_UPDATE": "false"}
    # Who may use admin commands: the same people (user IDs work everywhere, roles only in the same Discord)
    values["ADMIN_USER_IDS"] = main_env.get("ADMIN_USER_IDS", "")
    if guild_id == (main_env.get("GUILD_ID") or "").strip():
        values["ADMIN_ROLE_IDS"] = main_env.get("ADMIN_ROLE_IDS", "")
    update_env(folder / ".env", values, template=BASE_DIR / ".env.example")
    servers.append({"id": sid, "name": name, "enabled": True, "created_at": int(time.time()),
                    "discord": "shared" if shared else "own"})
    save_registry(servers)
    sync_env(sid)
    log.info("Added server %s (%s) – folder %s, panel port %d", name, sid, folder, port)
    return describe(sid)


def remove_server(sid: str) -> None:
    """Takes a server off the list. Its folder is kept (renamed to servers/_removed-<id>-<time>) – nothing is lost."""
    servers = load_registry()
    if not any(s["id"] == sid for s in servers):
        raise ValueError("No such server.")
    save_registry([s for s in servers if s["id"] != sid])
    folder = home(sid)
    if folder.exists():
        shutil.move(str(folder), str(folder.with_name(f"_removed-{sid}-{int(time.time())}")))


def use_main_bot(sid: str) -> None:
    """Switches an extra server to the main server's Discord bot (its own bot token is replaced)."""
    servers = load_registry()
    if not any(s["id"] == sid for s in servers):
        raise ValueError("No such server.")
    for s in servers:
        if s["id"] == sid:
            s["discord"] = "shared"
    save_registry(servers)
    sync_env(sid)


def set_enabled(sid: str, enabled: bool) -> None:
    servers = load_registry()
    for s in servers:
        if s["id"] == sid:
            s["enabled"] = enabled
    save_registry(servers)


# ------------------------------------------------------------ Copying kits between servers

def database_file(sid: str) -> Path:
    raw = (read_env(env_path(sid)).get("DATABASE_PATH") or "data/bot.db").strip()
    path = Path(raw)
    return path if path.is_absolute() else home(sid) / path


def export_custom_kits(source, names: list[str]) -> tuple[list[dict], list[str]]:
    """The custom kits `names` of the database `source` as plain data (read in the database's own thread)."""
    kits, missing = [], []
    for name in dict.fromkeys(names):
        kit = source.custom_kit(name)
        if kit is None:
            missing.append(name)
            continue
        kits.append({"name": kit["name"], "description": kit["description"],
                     "cooldown": int(kit["cooldown_minutes"] or 0), "roles": source.claim_roles(kit),
                     "items": [(i["shortname"], i["amount"]) for i in source.custom_kit_items(kit["name"])]})
    return kits, missing


def copy_custom_kits(source, names: list[str], target_sid: str, *, overwrite: bool = False,
                     source_sid: str | None = None) -> dict:
    """Copies the bot's custom kits (items, description, claim cooldown) from the database `source` into another
    server. Claim roles only when both servers use the same Discord server. Existing kits there are kept unless
    overwrite. -> {"copied": [...], "skipped": [...], "missing": [...]}"""
    kits, missing = export_custom_kits(source, names)
    return import_custom_kits(kits, target_sid, overwrite=overwrite, source_sid=source_sid, missing=missing)


def import_custom_kits(kits: list[dict], target_sid: str, *, overwrite: bool = False, source_sid: str | None = None,
                       missing: list[str] | None = None) -> dict:
    """Writes exported kits into another server's database (its own connection – any thread)."""
    from storage import Storage
    source_sid = source_sid or server_id()
    if target_sid == source_sid:
        raise ValueError("Pick another server.")
    if target_sid != MAIN_ID and not any(s["id"] == target_sid for s in load_registry()):
        raise ValueError("No such server.")
    target_path = database_file(target_sid)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    guilds = {(read_env(env_path(x)).get("GUILD_ID") or "").strip() for x in (source_sid, target_sid)}
    same_guild = len(guilds) == 1 and "" not in guilds
    out: dict[str, list[str]] = {"copied": [], "skipped": [], "missing": list(missing or [])}
    target = Storage(target_path)
    try:
        for kit in kits:
            if target.custom_kit(kit["name"]) is not None:
                if not overwrite:
                    out["skipped"].append(kit["name"])
                    continue
                target.clear_custom_kit_items(kit["name"])
            else:
                target.create_custom_kit(kit["name"], kit["description"], f"copied from {server_name(source_sid)}")
            for shortname, amount in kit["items"]:
                target.add_custom_kit_item(kit["name"], shortname, amount)
            target.set_custom_kit_cooldown(kit["name"], kit["cooldown"])
            if same_guild:
                target.set_custom_kit_roles(kit["name"], kit["roles"])
            out["copied"].append(kit["name"])
    finally:
        target.close()
    return out


# ------------------------------------------------------------ Running the extra servers (main process only)

def child_environment(sid: str) -> dict[str, str]:
    """A clean environment: none of the main server's settings (load_dotenv put them into os.environ)."""
    own = {f.key for f in FIELDS} | set(read_env(env_path(MAIN_ID))) | set(read_env(BASE_DIR / ".env.example"))
    env = {k: v for k, v in os.environ.items() if k not in own and not k.startswith("WEB_")}
    env.pop("RCE_LAUNCHER", None)
    env.update({ENV_HOME: str(home(sid).resolve()), ENV_ID: sid, ENV_SHARED: str((main_root() / "data").resolve()),
                ENV_PARENT: str(os.getpid()), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    if discord_mode(sid) == "shared":
        env[ENV_SHARED_BOT] = "1"
    return env


class ServerProcess:
    """One extra server: started as `python bot.py` in its folder, restarted after a crash."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"          # stopped / starting / running / restarting / error
        self.last_exit: int | None = None
        self.started_at: float | None = None
        self.crashes = 0
        self._want = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._want = True
        self._wake.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name=f"server-{self.sid}", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while self._want:
            folder = home(self.sid)
            try:
                sync_env(self.sid)
                (folder / "logs").mkdir(parents=True, exist_ok=True)
                with open(folder / "logs" / "console.log", "ab") as out:
                    self.state, self.started_at = "starting", time.time()
                    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW: no extra console
                    self.proc = subprocess.Popen([sys.executable, str(BASE_DIR / "bot.py")], cwd=folder,
                                                 env=child_environment(self.sid), stdout=out,
                                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                 creationflags=flags)
                    self.state = "running"
                    log.info("Server %s started (pid %d)", self.sid, self.proc.pid)
                    code = self.proc.wait()
            except OSError as exc:
                log.error("Server %s could not start: %s", self.sid, exc)
                code = -1
            self.last_exit, self.proc = code, None
            if not self._want:
                break
            if code == EXIT_FATAL:
                self.state, self._want = "error", False
                log.error("Server %s stopped: its settings are incomplete – open its panel and fix them.", self.sid)
                break
            ran = time.time() - (self.started_at or time.time())
            self.crashes = 0 if ran > 600 else self.crashes + 1
            wait = RESTART_DELAYS[min(self.crashes, len(RESTART_DELAYS) - 1)]
            self.state = "restarting"
            log.warning("Server %s stopped (exit code %s) – restarting in %d s", self.sid, code, wait)
            self._wake.wait(wait)
        if self.state != "error":
            self.state = "stopped"

    def stop(self, timeout: float = 15) -> None:
        self._want = False
        self._wake.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout)
        self.state = "stopped" if self.state != "error" else self.state

    def info(self) -> dict:
        return {"state": self.state, "pid": self.proc.pid if self.proc else None, "last_exit": self.last_exit,
                "since": int(self.started_at) if self.started_at and self.state == "running" else None}


class ServerManager:
    def __init__(self) -> None:
        self.procs: dict[str, ServerProcess] = {}

    def process(self, sid: str) -> ServerProcess:
        if sid not in self.procs:
            self.procs[sid] = ServerProcess(sid)
        return self.procs[sid]

    def start_all(self) -> None:
        for s in load_registry():
            if s.get("enabled", True):
                self.process(s["id"]).start()

    def start(self, sid: str) -> None:
        set_enabled(sid, True)
        self.process(sid).start()

    def stop(self, sid: str) -> None:
        set_enabled(sid, False)
        self.process(sid).stop()

    def restart(self, sid: str) -> None:
        proc = self.process(sid)
        threading.Thread(target=lambda: (proc.stop(), proc.start()), daemon=True).start()

    def remove(self, sid: str) -> None:
        if sid in self.procs:
            self.procs.pop(sid).stop()
        remove_server(sid)

    def info(self, sid: str) -> dict:
        return self.procs[sid].info() if sid in self.procs else {"state": "stopped", "pid": None,
                                                                  "last_exit": None, "since": None}

    def stop_all(self) -> None:
        for proc in list(self.procs.values()):
            proc.stop(timeout=10)


manager: ServerManager | None = None        # set by bot.main() in the main process


def watch_parent(interval: float = 5) -> None:
    """Extra server: end this process when the main bot is gone (e.g. its window was closed) – also in setup mode,
    so no second bot keeps running on its own."""
    parent = int(os.environ.get(ENV_PARENT) or 0)
    if not parent:
        return

    def watch() -> None:
        from web.tunnel import _pid_command
        while True:
            time.sleep(interval)
            if _pid_command(parent) is None:
                log.warning("The main bot is gone – stopping this server too.")
                logging.shutdown()
                os._exit(0)
    threading.Thread(target=watch, name="watch-main-bot", daemon=True).start()


def log_tail(sid: str, lines: int = 80) -> str:
    """The last lines of a server's log (for the panel)."""
    folder = home(sid) / "logs"
    for name in ("bot.log", "console.log"):
        try:
            text = (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return "\n".join(text.splitlines()[-lines:])
    return ""
