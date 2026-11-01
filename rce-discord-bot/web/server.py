"""Web interface: runs in the same process as the bot (aiohttp, no extra packages).

Security:
  * By default only reachable on 127.0.0.1 (this PC only). For access from your home
    network set WEB_HOST=0.0.0.0 – and then be sure to pick a strong WEB_PASSWORD.
    Do NOT expose it to the internet via port forwarding (no HTTPS).
  * Login by password or one-time link from the bot window; signed session cookie
    (HttpOnly, SameSite=Strict) – survives bot restarts, invalidated when the password changes.
  * Login rate limit, host check against DNS rebinding (when running on this PC only).
  * Write requests need the X-Requested-With header (CSRF protection).
  * Every action is logged to the admin log channel.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Callable

import aiohttp
import discord
from aiohttp import web

import backups
import servers
import settings_schema
from config import ConfigError, load_config, required_intents
from envfile import read_env, update_env
from rce.diagnostics import diagnose_rcon

from logbuffer import memory_log
from web import tunnel as tunnelmod
from web.caching import YEAR, conditional, maybe_compress
from web.item_images import THUMB_SIZES, WAIT_FOR_DOWNLOAD
from web.ratelimit import RateLimiter
from web.totp import TwoFactorStore, matching_step, new_secret, otpauth_uri
from web import honeypot
from web.honeypot import HoneypotLog
from web.system_stats import SystemMonitor
from web.panel_themes import THEMES, ThemeStore
from web.users import OWNER, UserError, UserStore, hash_password, is_hashed, same_secret, verify_password
from cogs.economy import SHOP_KINDS
from permcheck import check_permissions, explain_forbidden
from rce import items as itemlist
from rce import kits as kitparse
from rce import quickchat
from rce.rcon_client import RconError
from storage import LEADERBOARD_COLUMNS
from support import pricing
from support.deepseek import DeepSeekError
from utils import format_minutes, parse_duration, sanitize_ingame_text, sanitize_player_name, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png",
                ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon"}
STATIC_NAME = re.compile(r"^[\w-]+(\.[\w-]+)*$")
STATIC_LINK = re.compile(r'(/static/([\w.-]+))\?v=[\w]+')     # index.html: ?v=… becomes the file's content hash
SESSION_COOKIE = "rce_session"
PUBLIC_LINK_FILE = "PUBLIC_LINK.txt"  # current public address (Cloudflare tunnel), next to start.bat
SESSION_TTL = 12 * 3600
SERVER_COOKIE = "rce_server"      # which server the panel shows (its server drop-down, several servers)
MAIN_ONLY_API = frozenset({"/api/switch", "/api/login", "/api/logout",   # never passed on to the other server
                           "/api/system"})                     # one PC: the main bot sees every server's process
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
                         "transfer-encoding", "upgrade", "host"})
GATE_COOKIE = "rce_gate"          # set when the panel is opened at its secret address: the panel's files/API answer
GATE_TTL = 30 * 86400             # … only with it (or a login, or on this PC) – everyone else gets "not found"
PANEL_PATH = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
HONEYPOT_TELL_EVERY = 6 * 3600    # admin log: one line per IP (visit / login try) per 6 hours
SHARED_SETTINGS = frozenset(servers.SHARED_KEYS + servers.LOGIN_KEYS)   # extra servers: from the main server


def locked_settings() -> frozenset[str]:
    """Settings an extra server takes from the main server (shown, but not changeable on its panel). Hosted
    (SaaS) tenants: everything the control plane sets or that could reach beyond the tenant's folder."""
    import saas_mode
    if saas_mode.enabled():
        return saas_mode.locked_settings()
    if not servers.is_child():
        return frozenset()
    import discord_router
    locked = SHARED_SETTINGS - frozenset(servers.SHARED_KEYS) if servers.own_abuse_settings(servers.server_id()) \
        else SHARED_SETTINGS                                      # own abuse protection settings: editable here
    return locked | frozenset(servers.DISCORD_KEYS) if discord_router.uses_main_bot() else locked
KNOCK_MIN = 8                     # WEB_PANEL_KNOCK shorter than this is off (too easy to guess)
KNOCK_ROUNDS = 100_000            # hash rounds of WEB_PANEL_KNOCK: every long player search is checked against it
REAUTH_SECONDS = 15 * 60          # after logging in / confirming the password, risky actions need no new confirmation
# Risky actions (run code, change secrets or accounts): need a password confirmation in the last REAUTH_SECONDS
REAUTH_PATHS = (("POST", "/api/plugins/upload"), ("POST", "/api/allplugins/copy"), ("POST", "/api/servers"), ("DELETE", "/api/servers"), ("PUT", "/api/settings"), ("POST", "/api/users"),
                ("PUT", "/api/users"), ("DELETE", "/api/users"), ("POST", "/api/restart"), ("POST", "/api/update"),
                ("POST", "/api/backups"), ("DELETE", "/api/plugins"), ("POST", "/api/2fa/disable"),
                ("POST", "/api/webshop/giftcards"))
# The control panel: only its own scripts/styles/pictures, never framed by another site
PANEL_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "connect-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                    "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-site",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                                          "interest-cohort=()"}
LOGIN_ATTEMPTS_PER_MINUTE = 5          # per IP and per username (every attempt counts)
LOGIN_FAILURES_PER_15_MIN = 15         # failed attempts -> locked for up to 15 minutes
PUBLIC_PAGES_PER_MINUTE = 60           # player website per visitor
PUBLIC_IMAGES_PER_MINUTE = 1200        # item pictures per visitor (the store's kit builder shows ~570)
PUBLIC_PAGES_TOTAL_PER_MINUTE = 1200   # player website, all visitors together
HONEYPOT_PER_IP_PER_MINUTE = 10        # /admin from outside: visits + login tries per visitor
HONEYPOT_LOGINS_PER_IP_PER_MINUTE = 5  # of those, login tries
HONEYPOT_TOTAL_PER_MINUTE = 120        # everyone together – a flood can't push real entries out of the list
CSRF_HEADER = "X-Requested-With"
# Only for role "owner": settings (tokens/passwords), bot restart, user management
# First path segments that are never a page of the player website (it lives at the site root)
ROOT_RESERVED = {"api", "static", "img", "hooks", "admin", "auth", "p", "play", "internal"}
OWNER_ONLY_PREFIXES = ("/api/settings", "/api/restart", "/api/setup", "/api/users", "/api/update", "/api/backups",
                       "/api/plugins", "/api/webshop", "/api/item-images", "/api/protection", "/api/br",
                       "/api/honeypot", "/api/themes", "/api/allplugins", "/api/shield")
# The management AI (cogs/manager_ai.py) uses the panel's own API with a session of its own ("Management AI",
# owner rights, a per-start secret key – nobody can log in as it). Never for these (any method):
AI_USER = "Management AI"
AI_BLOCKED = ("/api/login", "/api/logout", "/api/reauth", "/api/2fa", "/api/account", "/api/users", "/api/restart",
              "/api/update", "/api/backups", "/api/setup", "/api/honeypot", "/api/switch", "/api/command")
# … and these only for reading (no changes): servers, the domain, gift cards (money), plugin code/files
AI_BLOCKED_WRITES = ("/api/servers", "/api/settings/cloudflare-domain", "/api/webshop/giftcards", "/api/plugins/upload",
                     "/api/plugins/generate", "/api/allplugins/copy")
AI_BLOCKED_WRITE_RE = re.compile(r"^/api/plugins/[^/]+(/file/.*)?$")      # delete a plugin, upload plugin files
# Settings it may never change (besides every secret): access, ports, who is admin, its own permissions
AI_LOCKED_SETTINGS = {"WEB_PASSWORD", "WEB_PANEL_KNOCK", "WEB_HOST", "WEB_PORT", "WEB_PANEL_PATH", "WEB_ENABLED",
                      "WEB_PUBLIC_URL", "ALLOW_RAW_RCON", "ADMIN_ROLE_IDS", "ADMIN_USER_IDS", "GUILD_ID", "RCON_HOST",
                      "RCON_PORT", "DEEPSEEK_BASE_URL", "MANAGER_AI_CHANNEL_IDS", "MANAGER_AI_CONSOLE",
                      "MANAGER_AI_WEB", "MANAGER_AI_INSTRUCTIONS"}
AI_SECRET_KEY = re.compile(r"token|secret|passw|api_?key|cookie|session|webhook|private", re.I)
AI_MAX_RESULT = 14000
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("web_user", default="?")
POLLING_COMMANDS = ("playerlist", "serverinfo")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **data) -> None:
        super().__init__(message)
        self.status = status
        self.data = data          # extra fields for the panel, e.g. need_2fa / reauth


def _row(row) -> dict:
    return dict(row) if row is not None else None


LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
ENV_PATH = Path(".env")
ENV_TEMPLATE = Path(".env.example")
DISCORD_API = "https://discord.com/api/v10"
# Bot permissions for the invite link (every feature of this bot)
BUILDER_MAX_ITEMS = 37  # 24 inventory + 6 hotbar + 7 clothing slots
INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
    add_reactions=True, manage_channels=True, manage_roles=True, moderate_members=True, kick_members=True,
    ban_members=True, manage_messages=True, mention_everyone=True,
    manage_guild=True, send_polls=True,      # plugins: invite rewards (read invites), wipe feedback polls
).value


def placeholder_svg(shortname: str) -> str:
    """Grey tile with the item's shortname (item without a picture)."""
    import html
    words = (shortname[:40] or "?").replace(".", ". ").split()
    lines, line = [], ""
    for w in words:
        if line and len(line) + len(w) > 11:
            lines.append(line)
            line = w
        else:
            line = f"{line}{w}"
    lines = (lines + [line])[:4]
    text = "".join(f'<tspan x="50" dy="{0 if i == 0 else 13}">{html.escape(t)}</tspan>' for i, t in enumerate(lines))
    top = 54 - 6.5 * (len(lines) - 1)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="12" '
            'fill="#8884"/><text x="50" y="' + f"{top:.1f}" + '" font-family="sans-serif" font-size="12" fill="#999" '
            'text-anchor="middle">' + text + '</text></svg>')


def invite_url(app_id) -> str | None:
    """Invite link with all required permissions. Using it again updates the bot role's permissions."""
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}") if app_id else None


INTENT_LABELS = {"message_content": "Message Content Intent", "members": "Server Members Intent"}


async def check_discord(token: str) -> dict:
    """Checks the bot token directly with Discord and reads which privileged intents are enabled."""
    result = {"reachable": False, "token_ok": None, "bot_name": None, "app_id": None, "intents": None, "error": None}
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
                result["reachable"] = True
                if resp.status == 401:
                    result["token_ok"] = False
                    result["error"] = ("Discord rejected the bot token. Generate a new one in the Developer Portal under "
                                       "Bot → Reset Token and enter it here.")
                    return result
                if resp.status != 200:
                    result["error"] = f"Discord responded with HTTP {resp.status}."
                    return result
                user = await resp.json()
            async with session.get(f"{DISCORD_API}/applications/@me", headers=headers) as resp:
                app_info = await resp.json() if resp.status == 200 else {}
    except (aiohttp.ClientError, TimeoutError) as exc:
        result["error"] = f"Discord unreachable: {exc}"
        return result
    flags = int(app_info.get("flags") or 0)
    result.update(token_ok=True, bot_name=user.get("username"), app_id=app_info.get("id") or user.get("id"))
    if app_info:
        # Application-Flags: GATEWAY_GUILD_MEMBERS(_LIMITED) = 1<<14/1<<15, MESSAGE_CONTENT(_LIMITED) = 1<<18/1<<19
        result["intents"] = {"members": bool(flags & (1 << 14 | 1 << 15)),
                             "message_content": bool(flags & (1 << 18 | 1 << 19))}
    return result


def intent_warning(cfg, missing: list[str]) -> str:
    """Explains which intents are missing and which features are paused because of it."""
    needed = required_intents(cfg)
    parts = [f'"{INTENT_LABELS[k]}" (affects: {", ".join(needed[k]) or "–"})' for k in missing]
    verb = "is" if len(parts) == 1 else "are"
    return ("Limited mode: In the Discord Developer Portal, " + " and ".join(parts)
            + f" {verb} turned off. All commands, RCON, kits and feeds work normally – only the features "
              'listed are paused. Fix: Developer Portal → Bot → turn on "Privileged Gateway Intents" → '
              '"Save Changes" → restart the bot.')


def intents_fix_url(app_id: str | None) -> str:
    return (f"https://discord.com/developers/applications/{app_id}/bot" if app_id
            else "https://discord.com/developers/applications")


async def discord_preflight(cfg) -> dict | None:
    """Checks before starting how Discord will accept the bot with this configuration.

    None = all good (or Discord currently unreachable). Otherwise a dict with
      preflight_error  – blocking (e.g. invalid token), the bot cannot start
      missing_intents  – missing privileged intents: the bot starts anyway, just without them
      intent_warning, fix_url
    """
    info = await check_discord(cfg.discord_token)
    if not info["reachable"]:
        return None  # e.g. no internet: don't block, the bot retries on its own
    if info["token_ok"] is False:
        return {"preflight_error": info["error"], "missing_intents": [],
                "fix_url": "https://discord.com/developers/applications"}
    if not info["intents"]:
        return None
    missing = [k for k, features in required_intents(cfg).items() if features and not info["intents"].get(k)]
    if not missing:
        return None
    return {"preflight_error": None, "missing_intents": missing,
            "intent_warning": intent_warning(cfg, missing), "fix_url": intents_fix_url(info["app_id"])}


@dataclass
class WebSettings:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""
    open_browser: bool = True
    tunnel: bool = False
    tunnel_token: str = ""
    public_url: str = ""
    panel_path: str = ""          # secret address of the control panel (/<panel_path>); /admin only on this PC
    panel_knock: str = ""         # secret phrase: typed into the website's player search it opens the panel
    honeypot: bool = True         # /admin from outside: fake login that logs every visitor


def web_settings_from_env(path: Path = ENV_PATH) -> WebSettings:
    """WEB_* values straight from .env – even if the rest of the configuration is still incomplete."""
    env = {**read_env(path), **{k: v for k, v in os.environ.items() if k.startswith("WEB_")}}

    def flag(key: str, default: bool) -> bool:
        raw = str(env.get(key) or "").strip().lower()
        return default if not raw else raw in ("1", "true", "yes", "ja", "on")

    try:
        port = int(str(env.get("WEB_PORT") or "8080").strip())
    except ValueError:
        port = 8080
    return WebSettings(enabled=flag("WEB_ENABLED", True), host=(env.get("WEB_HOST") or "127.0.0.1").strip(),
                       port=port, password=(env.get("WEB_PASSWORD") or "").strip(),
                       open_browser=flag("WEB_OPEN_BROWSER", True), tunnel=flag("WEB_TUNNEL", True),
                       tunnel_token=(env.get("WEB_TUNNEL_TOKEN") or "").strip(),
                       public_url=(env.get("WEB_PUBLIC_URL") or "").strip(),
                       panel_path=(env.get("WEB_PANEL_PATH") or "").strip().strip("/"),
                       panel_knock=(env.get("WEB_PANEL_KNOCK") or "").strip(),
                       honeypot=flag("WEB_HONEYPOT", True))


class WebInterface:
    """bot=None starts setup mode: only login and settings, without Discord/RCON."""

    def __init__(self, bot: "RceBot | None", *, web_settings: WebSettings | None = None,
                 setup_error: str | None = None, setup_fix_url: str | None = None,
                 on_config_saved: Callable[[], None] | None = None,
                 env_path: Path = ENV_PATH, secret_path: Path | None = None,
                 users_path: Path | None = None) -> None:
        self.bot = bot
        self.setup_mode = bot is None
        self.setup_error = setup_error
        self.setup_fix_url = setup_fix_url
        self.on_config_saved = on_config_saved
        self.env_path = env_path
        if web_settings is None:
            cfg = bot.config
            web_settings = WebSettings(True, cfg.web_host, cfg.web_port, cfg.web_password, cfg.web_open_browser,
                                       cfg.web_tunnel, cfg.web_tunnel_token, cfg.web_public_url)
            saved = read_env(env_path)                              # .env-only values (not in the bot's config)
            web_settings.panel_path = (saved.get("WEB_PANEL_PATH") or "").strip().strip("/")
            web_settings.panel_knock = (saved.get("WEB_PANEL_KNOCK") or "").strip()
            web_settings.honeypot = (saved.get("WEB_HONEYPOT") or "true").strip().lower() in ("1", "true", "yes",
                                                                                              "ja", "on")
        self.ws = web_settings
        # The .env keeps only salted hashes of WEB_PASSWORD / WEB_PANEL_KNOCK – a plain value (typed into the .env
        # by hand) is hashed and replaced on the start. A login hashes what was typed and compares the hashes.
        self.password = None                 # a random password made right now (shown in the bot window), else None
        if not web_settings.password:
            # No fixed password: random password + one-time link (see start())
            self.password = secrets.token_urlsafe(9)
        self.password_hash = self._hashed_env("WEB_PASSWORD", web_settings.password or self.password,
                                              save=bool(web_settings.password))
        if web_settings.password:
            web_settings.password = self.password_hash
        if len(web_settings.panel_knock) >= KNOCK_MIN:
            web_settings.panel_knock = self._hashed_env("WEB_PANEL_KNOCK", web_settings.panel_knock,
                                                        rounds=KNOCK_ROUNDS)
        self.login_token = secrets.token_urlsafe(24)
        # Logins (key, accounts, 2FA, themes) are the main server's – the same for every server (servers.py)
        secret_path = secret_path or servers.shared_data() / "web_secret.key"
        self._secret = self._load_secret(secret_path)
        self.users = UserStore(users_path or secret_path.parent / "web_users.json")
        self.two_factor = TwoFactorStore(secret_path.parent / "web_2fa.json")
        self.honeypot = HoneypotLog(secret_path.parent / "web_honeypot.json")
        self.system = SystemMonitor()                            # CPU / memory for the System page
        self.themes = ThemeStore(secret_path.parent / "web_themes.json")
        self._proxy_session: aiohttp.ClientSession | None = None   # other servers' panels through this address
        self._honeypot_told: dict[str, float] = {}               # ip:kind -> last admin log line (no flooding)
        self._ensure_panel_path()
        self._pending_2fa: dict[str, tuple[str, float]] = {}     # username -> (new secret, valid until)
        self._ai_key = secrets.token_hex(32)                      # the management AI's session key (per start)
        self._sudo: dict[str, float] = {}                        # session token -> confirmed until
        self._revoked: set[str] = set()
        self._login_minute = RateLimiter(LOGIN_ATTEMPTS_PER_MINUTE, 60)
        self._login_failures = RateLimiter(LOGIN_FAILURES_PER_15_MIN, 900)
        from web.shield import Shield
        cfg = getattr(bot, "config", None)
        self.shield_on = bool(getattr(cfg, "ddos_protection", True))
        self.shield = Shield(per_ip=int(getattr(cfg, "ddos_requests_per_ip", 150) or 150),
                             ban_minutes=int(getattr(cfg, "ddos_ban_minutes", 15) or 15))
        if bot is not None:
            self.shield.on_event = lambda text: bot.audit(text)
        self._public_pages = RateLimiter(PUBLIC_PAGES_PER_MINUTE, 60)
        self._public_images = RateLimiter(PUBLIC_IMAGES_PER_MINUTE, 60)
        self._public_total = RateLimiter(PUBLIC_PAGES_TOTAL_PER_MINUTE, 60)
        self._honeypot_ip = RateLimiter(HONEYPOT_PER_IP_PER_MINUTE, 60)
        self._honeypot_logins = RateLimiter(HONEYPOT_LOGINS_PER_IP_PER_MINUTE, 60)
        self._honeypot_total = RateLimiter(HONEYPOT_TOTAL_PER_MINUTE, 60)
        self._honeypot_blocked = 0                                # requests turned away by the limits (since start)
        self._static_cache: dict[str, tuple[int, bytes, str]] = {}
        self._runner: web.AppRunner | None = None
        self.tunnel: tunnelmod.Tunnel | None = None
        self.tunnel_problem: str | None = None
        self.generated_password: str | None = None  # shown once in the bot window
        self.restart_required = False
        self.restart_for: list[str] = []     # saved settings that only apply after a restart
        from web.item_images import ItemImages
        self._scan: dict = {}
        self._scan_task = None
        self._warm_task: asyncio.Task | None = None
        self.item_images = ItemImages((bot.config.database_path.parent if bot else Path("data")) / "item_images")
        self.app = self._build_app()

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        """Secret key for the session cookies – stays the same across restarts."""
        try:
            return bytes.fromhex(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            secret = secrets.token_bytes(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secret.hex(), encoding="utf-8")
            except OSError:
                log.warning("Could not save %s – logins will only last until the next restart.", path)
            return secret

    # ------------------------------------------------------------ Start/stop

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._shield_middleware, self._compress_middleware, self._error_middleware, self._host_middleware,
                                           self._proxy_middleware, self._gate_middleware, self._origin_middleware, self._auth_middleware],
                              client_max_size=1024 ** 2)
        app.on_response_prepare.append(self._security_headers)
        r = app.router
        r.add_get("/", self._root)                                  # player website (or -> /admin if it's off)
        r.add_get("/admin", self._admin)                            # the control panel – only on this PC; from
        r.add_get("/admin/", self._admin)                           # anywhere else: /<WEB_PANEL_PATH>
        r.add_post("/admin", self._admin)                           # the honeypot's fake login form
        r.add_post("/admin/", self._admin)
        r.add_get("/api/honeypot", self.honeypot_list)
        r.add_get("/api/system", self.system_info)
        r.add_post("/api/switch", self.switch_server)
        r.add_post("/internal/{action:online|give|status|kits|kitinfo}", self.internal)          # other servers of this bot (signed)
        r.add_get("/api/servers", self.servers_list)                     # several Rust servers (servers.py)
        r.add_post("/api/servers", self.servers_create)
        r.add_post("/api/servers/{sid}/{action:start|stop|restart}", self.servers_action)
        r.add_post("/api/servers/{sid}/use-main-bot", self.servers_use_main_bot)
        r.add_delete("/api/servers/{sid}", self.servers_remove)
        r.add_delete("/api/servers/removed/{name}", self.servers_delete_removed)
        r.add_get("/api/servers/{sid}/log", self.servers_log)
        r.add_post("/api/kits/copy", self.kits_copy)
        r.add_get("/api/themes", self.themes_list)
        r.add_put("/api/themes", self.themes_set)
        r.add_put("/api/themes/options", self.themes_options)
        r.add_delete("/api/honeypot", self.honeypot_clear)
        r.add_get("/static/{name}", self._static)
        r.add_post("/api/item-images/scan", self.item_images_scan)
        r.add_get("/api/item-images/scan", self.item_images_scan_state)
        r.add_get("/img/items/{name}", self.item_image)            # public: item pictures (cached by the bot)
        r.add_get("/img/kits/{kit_id:\\d+}.png", self.store_kit_image)  # public: picture of a kit for sale
        r.add_get("/img/bot-avatar.png", self.bot_avatar)          # public: the Discord bot's picture (panel logo)
        r.add_get("/auth", self.auth_link)
        r.add_post("/api/login", self.login)
        r.add_post("/api/logout", self.logout)
        r.add_post("/api/reauth", self.reauth)
        r.add_get("/api/2fa", self.two_factor_state)
        r.add_post("/api/2fa/setup", self.two_factor_setup)
        r.add_post("/api/2fa/enable", self.two_factor_enable)
        r.add_post("/api/2fa/disable", self.two_factor_disable)
        r.add_get("/api/me", self.me)
        r.add_get("/api/status", self.status)
        r.add_get("/api/settings", self.get_settings)
        r.add_put("/api/settings", self.put_settings)
        r.add_post("/api/settings/test-rcon", self.settings_test_rcon)
        r.add_post("/api/settings/test-discord", self.settings_test_discord)
        r.add_post("/api/settings/cloudflare-domain", self.cloudflare_domain)              # own domain (owner)
        r.add_post("/api/settings/cloudflare-domain/check", self.cloudflare_domain_check)
        r.add_get("/api/settings/discord-options", self.settings_discord_options)
        r.add_get("/api/users", self.list_users)
        r.add_post("/api/users", self.create_user)
        r.add_put("/api/users/{username}", self.update_user)
        r.add_delete("/api/users/{username}", self.delete_user)
        r.add_delete("/api/users/{username}/2fa", self.reset_user_2fa)
        r.add_post("/api/account/password", self.change_own_password)
        r.add_get("/saas/health", self.saas_health)              # hosted mode: also while in setup mode
        if self.setup_mode:
            r.add_post("/api/setup/continue", self.setup_continue)
            return app
        r.add_post("/api/restart", self.restart)
        r.add_get("/api/update", self.update_info)
        r.add_post("/api/update/check", self.update_check)
        r.add_get("/api/console", self.console)
        r.add_post("/api/command", self.command)
        r.add_post("/api/say", self.say)
        r.add_post("/api/players/{action:kick|ban|unban}", self.player_action)
        r.add_get("/api/kits", self.kits)
        r.add_get("/api/kits/ingame/{name}", self.ingame_kit_info)
        r.add_get("/api/kits/ingame/{name}/image.png", self.ingame_kit_image)
        r.add_get("/api/custom-kits/{name}/image.png", self.custom_kit_image)
        r.add_post("/api/kit-image", self.kit_builder_image)
        r.add_post("/api/kits/give", self.give_kit)
        r.add_post("/api/kit-builder/ingame", self.builder_ingame)
        r.add_post("/api/kit-builder/custom", self.builder_custom)
        r.add_post("/api/custom-kits", self.create_custom_kit)
        r.add_post("/api/custom-kits/import", self.import_custom_kits)
        r.add_delete("/api/custom-kits/{name}", self.delete_custom_kit)
        r.add_put("/api/custom-kits/{name}/claim", self.custom_kit_claim_settings)
        r.add_put("/api/custom-kits/{name}/disabled", self.custom_kit_disabled)
        r.add_put("/api/custom-kits-disabled", self.custom_kits_disabled_all)       # every custom kit at once
        r.add_get("/api/kit-db", self.kit_db)
        r.add_get("/api/kit-roles", self.kit_roles)
        r.add_put("/api/kit-roles", self.kit_roles_update)
        r.add_post("/api/kit-roles/sync", self.kit_roles_sync)
        r.add_post("/api/kit-roles/give", self.kit_roles_give)
        r.add_post("/api/kit-roles/match", self.kit_roles_match)
        r.add_post("/api/kit-roles/apply", self.kit_roles_apply)
        r.add_post("/api/kit-roles/undo", self.kit_roles_undo)
        r.add_post("/api/linked-players", self.link_player)
        r.add_post("/api/linked-players/sync-nicknames", self.sync_nicknames)
        r.add_delete("/api/linked-players/{user_id:\\d+}", self.unlink_player)
        r.add_post("/api/custom-kits/{name}/items", self.add_custom_item)
        r.add_delete("/api/custom-kits/{name}/items/{item_id:\\d+}", self.remove_custom_item)
        r.add_post("/api/autokits", self.create_autokit)
        r.add_post("/api/autokits/reset", self.reset_autokits)
        r.add_post("/api/autokits/{rule_id:\\d+}/toggle", self.toggle_autokit)
        r.add_put("/api/autokits/{rule_id:\\d+}/role", self.autokit_role)
        r.add_delete("/api/autokits/{rule_id:\\d+}", self.delete_autokit)
        r.add_get("/api/emote-binds", self.emote_binds)
        r.add_put("/api/emote-binds", self.set_emote_bind)
        r.add_post("/api/emote-binds/roles", self.emote_bind_roles)
        r.add_get("/api/shield", self.shield_status)
        r.add_post("/api/shield", self.shield_ban)
        r.add_get("/api/teleports", self.teleports)
        r.add_put("/api/teleports", self.set_teleport)
        r.add_post("/api/teleports/capture", self.capture_teleport)
        r.add_post("/api/teleports/clear", self.clear_teleport)
        r.add_get("/api/meta", self.meta)
        r.add_get("/api/tickets", self.tickets)
        r.add_get("/api/tickets/{ticket_id:\\d+}", self.ticket_detail)
        r.add_get("/api/leaderboard", self.leaderboard)
        r.add_get("/api/knowledge", self.get_knowledge)
        r.add_get("/api/knowledge/search", self.search_knowledge)
        r.add_get("/api/prices", self.get_prices)
        r.add_put("/api/prices", self.put_prices)
        r.add_get("/api/store", self.store_status)
        r.add_post("/hooks/tip4serv/{token}", self.tip4serv_webhook)   # public: Tip4Serv "live payments"
        r.add_put("/api/knowledge", self.put_knowledge)
        r.add_get("/api/ai", self.ai_status)
        r.add_get("/api/botlog", self.botlog)
        r.add_get("/api/rules", self.get_rules)
        r.add_put("/api/rules", self.put_rules)
        r.add_get("/api/community", self.community)
        r.add_post("/api/giveaways/{giveaway_id:\\d+}/{action:end|reroll}", self.giveaway_action)
        r.add_get("/api/wipe", self.get_wipe)
        r.add_put("/api/wipe", self.put_wipe)
        r.add_delete("/api/wipe", self.clear_wipe)
        r.add_post("/api/wipe/done", self.wipe_done)
        r.add_get("/api/schedules", self.list_schedules)
        r.add_post("/api/schedules", self.create_schedule)
        r.add_put("/api/schedules/{msg_id:\\d+}", self.toggle_schedule)
        r.add_delete("/api/schedules/{msg_id:\\d+}", self.delete_schedule)
        r.add_get("/api/reports", self.list_reports)
        r.add_post("/api/reports/{report_id:\\d+}/handled", self.handle_report)
        r.add_post("/api/watchlist", self.add_watch)
        r.add_delete("/api/watchlist/{player}", self.remove_watch)
        r.add_get("/api/population", self.population)
        r.add_get("/api/abuse", self.abuse_status)
        r.add_post("/api/abuse/cases/{case_id:\\d+}/{action:approve|reject}", self.abuse_decide)
        r.add_post("/api/abuse/loops/stop", self.abuse_loop_stop)
        r.add_get("/api/shop", self.shop)
        r.add_post("/api/shop/items", self.create_shop_item)
        r.add_put("/api/shop/items/{item_id:\\d+}", self.update_shop_item)
        r.add_delete("/api/shop/items/{item_id:\\d+}", self.delete_shop_item)
        r.add_post("/api/points", self.change_points)
        r.add_get("/api/timed-roles", self.timed_roles)
        r.add_delete("/api/timed-roles/{row_id:\\d+}", self.delete_timed_role)
        r.add_get("/api/backups", self.list_backups)
        r.add_post("/api/backups", self.create_backup)
        r.add_get("/api/backups/full", self.full_backups)
        r.add_post("/api/backups/db-key", self.db_recovery_key)
        r.add_post("/api/backups/full", self.create_full_backup)
        r.add_post("/api/backups/full/{name}/restore", self.emergency_restore)
        r.add_get("/api/backups/{name}", self.download_backup)
        r.add_get("/api/webshop", self.webshop)
        r.add_post("/api/webshop/kits", self.webshop_kit_create)
        r.add_put("/api/webshop/kits/{kit_id:\\d+}", self.webshop_kit_update)
        r.add_delete("/api/webshop/kits/{kit_id:\\d+}", self.webshop_kit_delete)
        r.add_post("/api/webshop/giftcards", self.webshop_giftcards_create)
        r.add_post("/api/webshop/giftcards/{code}/toggle", self.webshop_giftcard_toggle)
        r.add_post("/api/webshop/orders/{order_id:\\d+}/retry", self.webshop_retry)
        r.add_get("/api/allplugins", self.allplugins_list)                 # every server's plugins (main server)
        r.add_post("/api/allplugins/{sid}/{pid}/toggle", self.allplugins_toggle)
        r.add_post("/api/allplugins/copy", self.allplugins_copy)
        r.add_get("/api/plugins", self.plugins_list)
        r.add_get("/api/plugins/menu", self.plugins_menu)
        r.add_put("/api/plugins/menu", self.plugins_menu_save)
        r.add_post("/api/plugins/upload", self.plugin_upload)
        r.add_post("/api/plugins/generate", self.plugin_generate)
        r.add_post("/api/plugins/{pid}/toggle", self.plugin_toggle)
        r.add_put("/api/plugins/{pid}/settings", self.plugin_settings)
        r.add_post("/api/plugins/{pid}/reload", self.plugin_reload)
        r.add_delete("/api/plugins/{pid}", self.plugin_delete)
        r.add_get("/api/protection", self.protection_get)
        r.add_get("/api/br", self.br_get)
        r.add_post("/api/br/{action:test|shrink|stop|marker-test|clear-markers|forget-markers|delete-zones}", self.br_action)
        r.add_delete("/api/protection/zones/{team}", self.protection_delete_zone)
        r.add_get("/api/plugins/{pid}/file/{key}", self.plugin_file_get)
        r.add_post("/api/plugins/{pid}/file/{key}", self.plugin_file_upload)
        r.add_delete("/api/plugins/{pid}/file/{key}", self.plugin_file_delete)
        r.add_get("/play", self._old_play)                          # old address of the website -> the same page at /
        r.add_get("/play/{tail:.*}", self._old_play)
        r.add_post("/play/{tail:.*}", self.player_website)          # forms on a page opened before the move
        r.add_get("/p/{pid}", self.plugin_public)                  # public plugin pages (no login)
        r.add_get("/p/{pid}/{tail:.*}", self.plugin_public)
        r.add_post("/p/{pid}/{tail:.*}", self.plugin_public)
        # Last: everything else is a page of the player website at the root (/leaderboard, /store, /account …)
        r.add_get("/{tail:.+}", self._root)
        r.add_post("/{tail:.+}", self._root)
        return app

    async def start(self, *, open_browser: bool | None = None) -> bool:
        ws = self.ws
        # keepalive 15 s (not 75): idle connections don't pile up during a flood
        self._runner = web.AppRunner(self.app, access_log=None, keepalive_timeout=15)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, ws.host, ws.port).start()
        except OSError as exc:
            log.error("Web interface could not open port %s (%s) – choose a different WEB_PORT.", ws.port, exc)
            return False
        self._start_tunnel()  # may create WEB_PASSWORD, so before the login hints below
        self.system.start()
        shown_host = "localhost" if ws.host in ("127.0.0.1", "0.0.0.0", "") else ws.host
        url = f"http://{shown_host}:{ws.port}"
        login_url = f"{url}/auth?token={self.login_token}"
        if self.setup_mode:
            log.warning("SETUP: Open %s in your browser to fill in the missing settings.", login_url)
        else:
            log.info("Web interface running: %s/admin (player website: %s/)", url, url)
            if not ws.password:
                log.warning("No WEB_PASSWORD set – log in via this link: %s  (or password: %s)",
                            login_url, self.password)
        if self.bot is not None:
            self._warm_task = asyncio.create_task(self._warm_item_images())
        if ws.host not in ("127.0.0.1", "localhost"):
            log.warning("Web interface is reachable on the network (WEB_HOST=%s). Do not expose it to the internet!",
                        ws.host)
        if ws.open_browser if open_browser is None else open_browser:
            try:
                webbrowser.open(login_url)
            except Exception:  # e.g. no browser on a server
                pass
        return True

    async def _warm_item_images(self) -> None:
        """At the start: delete old/broken downloaded pictures (the bundled ones replace them) and kit pictures made
        from them; then fetch the pictures of catalogue items the bot doesn't have yet, in the background."""
        try:
            removed = await asyncio.to_thread(self.item_images.cleanup)
            if removed["replaced"] or removed["broken"]:
                log.info("Item pictures: deleted %d old downloads (bundled pictures are used) and %d broken ones",
                         removed["replaced"], removed["broken"])
            kit_cache = self.bot.config.database_path.parent / "kit_images"
            if (removed["replaced"] or removed["broken"]) and kit_cache.is_dir():
                for old in kit_cache.glob("*.png"):
                    old.unlink(missing_ok=True)
        except Exception:
            log.exception("Cleaning up item pictures failed")
        await asyncio.sleep(30)
        template = self.bot.config.item_image_url or ""
        if not template or template.lower() == "off":
            return
        todo = [s for s in itemlist.COMMON_ITEMS if self.item_images.cached(s) is None]
        if not todo:
            return
        self.item_images.url_template = template
        try:
            result = await self.item_images.scan(todo, retry=False)
        except Exception:
            log.exception("Fetching item pictures failed")
            return
        trouble = {h: s["last_error"] for h, s in self.item_images.sources.items() if s["errors"] and not s["ok"]}
        log.info("Item pictures: %d new, %d still without a picture%s", len(result["found"]), len(result["missing"]),
                 "".join(f" – {h} refused ({e})" for h, e in trouble.items()))

    def _start_tunnel(self) -> None:
        """Cloudflare tunnel (reachable from the internet). Keeps running across bot restarts."""
        ws = self.ws
        if not ws.tunnel:
            tunnelmod.stop_tunnel()
            return
        if not ws.password:
            # Public address without a fixed password: create a strong one and keep it in the .env
            password = secrets.token_urlsafe(12)
            hashed = hash_password(password)
            try:
                update_env(self.env_path, {"WEB_PASSWORD": hashed})
            except OSError as exc:
                self.tunnel_problem = f"Not started: could not save a WEB_PASSWORD to the .env ({exc})."
                log.error("Cloudflare tunnel %s", self.tunnel_problem)
                tunnelmod.stop_tunnel()
                return
            ws.password = self.password_hash = hashed
            self.password = self.generated_password = password
            log.warning("Created a web interface password (its hash is saved in .env as WEB_PASSWORD): %s",
                        password)
        log.info("Setting up the Cloudflare tunnel – the link follows in a few seconds …")
        self.tunnel = tunnelmod.ensure_tunnel(ws.port, token=ws.tunnel_token, public_url=ws.public_url)
        loop = asyncio.get_running_loop()
        self.tunnel.on_online = lambda url: loop.call_soon_threadsafe(self._tunnel_online, url)
        if self.tunnel.state == "online":  # already running (restart from the web interface)
            self._tunnel_online(self.tunnel.url, announce=False)

    def _tunnel_online(self, url: str | None, announce: bool = True) -> None:
        url = (url or self.ws.public_url or "(your fixed tunnel address)").rstrip("/")
        panel = f"{url}{self.panel_path}"
        if self.tunnel is not None and self.tunnel.adopted:
            announce = False     # same link as before the update restart – no new announcement
        login = (f"owner / {self.generated_password}   (new password, saved in .env as WEB_PASSWORD)"
                 if self.generated_password else "owner / your WEB_PASSWORD   (other admins: their own account)")
        bar = "=" * 72
        for line in ("", bar, "  🌍 YOUR CONTROL PANEL – works from anywhere (phone, other PCs):", "",
                     f"     {panel}", "", f"  Login: {login}", f"  Player website: {url}/",
                     "  The links are also saved in PUBLIC_LINK.txt and posted in the admin log channel.", bar, ""):
            log.warning(line)
        try:
            (self.env_path.parent / PUBLIC_LINK_FILE).write_text(
                f"Control panel: {panel}\nPlayer website: {url}/\n\n"
                "Login: owner + your WEB_PASSWORD (in the .env), other admins: their own account.\n"
                + ("This is your own domain – it stays the same.\n" if self.ws.public_url
                   else "This address changes when the bot window is closed.\n"), encoding="utf-8")
        except OSError:
            pass
        if announce and self.bot is not None:
            self.bot.audit(f"🌍 Control panel reachable from anywhere: {panel} – player website: {url}/")

    @property
    def public_url(self) -> str | None:
        if self.tunnel is not None and self.tunnel.state == "online":
            return self.tunnel.url or self.ws.public_url or None
        return None

    async def stop(self) -> None:
        self.system.stop()
        if self._warm_task is not None:
            self._warm_task.cancel()
        await self.item_images.close()
        if self.tunnel is not None:
            self.tunnel.on_online = None  # the tunnel itself keeps running for the restarted bot
        if self._runner is not None:
            await self._runner.cleanup()
        if self._proxy_session is not None:
            await self._proxy_session.close()

    # ------------------------------------------------------------ Hidden panel address

    def _ensure_panel_path(self) -> None:
        """The panel's secret address: WEB_PANEL_PATH, else a random one – saved in the .env (shown in Settings)."""
        if PANEL_PATH.match(self.ws.panel_path or ""):
            return
        self.ws.panel_path = "panel-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y")
        try:
            update_env(self.env_path, {"WEB_PANEL_PATH": self.ws.panel_path})
        except OSError as exc:
            log.warning("Could not save WEB_PANEL_PATH to the .env (%s) – the panel address changes on restart.",
                        exc)

    def _hashed_env(self, key: str, value: str, *, save: bool = True, rounds: int | None = None) -> str:
        """The hash of a secret from the .env; a plain value is hashed and the .env updated."""
        if is_hashed(value):
            return value
        hashed = hash_password(value, **({"rounds": rounds} if rounds else {}))
        if save:
            try:
                update_env(self.env_path, {key: hashed})
            except OSError as exc:
                log.warning("Could not save the hashed %s to the .env (%s).", key, exc)
        return hashed

    @property
    def panel_path(self) -> str:
        return "/" + self.ws.panel_path

    @staticmethod
    def _is_local(request: web.Request) -> bool:
        """A request from the bot's own PC – not through the public tunnel or a proxy."""
        return request.remote in ("127.0.0.1", "::1") and not request.headers.get("CF-Connecting-IP") \
            and not request.headers.get("X-Forwarded-For")

    def _gate_value(self) -> str:
        return hmac.new(self._secret, f"gate:{self.ws.panel_path}".encode(), hashlib.sha256).hexdigest()[:40]

    def _may_see_panel(self, request: web.Request) -> bool:
        """Knows the panel: on this PC, opened its secret address (gate cookie) or is logged in."""
        return self._is_local(request) or hmac.compare_digest(request.cookies.get(GATE_COOKIE, ""), self._gate_value()) \
            or self._session_user(request) is not None

    @web.middleware
    async def _gate_middleware(self, request: web.Request, handler):
        """Without knowing the panel's address, its files, login and API don't exist (plain "not found")."""
        if request.path.startswith(("/api/", "/static/")) and not self._may_see_panel(request):
            raise web.HTTPNotFound(text="Not found.")
        return await handler(request)

    async def _panel(self, request: web.Request) -> web.StreamResponse:
        """The control panel at its secret address – remembers the visitor may see the panel (gate cookie)."""
        resp = await self._index(request)
        resp.set_cookie(GATE_COOKIE, self._gate_value(), max_age=GATE_TTL, httponly=True, samesite="Strict",
                        secure=self._is_https(request), path="/")
        return resp

    # ------------------------------------------------------------ Other servers' panels through this address

    async def switch_server(self, request: web.Request) -> web.Response:
        """The panel's server drop-down: which server this panel shows – same address, same page. Afterwards the
        main server passes the panel's API requests on to that server's web interface on this PC."""
        if servers.is_child():
            raise ApiError("Switch servers on the main server's panel.")
        sid = self._text(await self._json(request), "server", limit=40)
        if sid not in {s["id"] for s in servers.all_servers()}:
            raise ApiError("No such server.", 404)
        resp = web.json_response({"ok": True, "server": sid})
        # Set (never just deleted – some browsers keep a Secure cookie a plain delete doesn't match): "main" = this
        # server's own panel. Until the browser closes.
        resp.set_cookie(SERVER_COOKIE, sid, httponly=True, samesite="Lax", secure=self._is_https(request), path="/")
        return resp

    async def internal(self, request: web.Request) -> web.Response:
        """Another server of this bot asks (servers.ask_server): who's online here, or give an item / in-game kit to
        an online player. Only from this PC and signed with the servers' shared key."""
        body = await request.read()
        if not self._is_local(request) or not servers.internal_ok(
                self._secret, request.headers.get("X-RCE-Time", ""), request.path, body,
                request.headers.get("X-RCE-Sig", "")):
            raise web.HTTPNotFound(text="Not found.")
        if self.bot is None:
            return web.json_response({"players": [], "result": "unreachable"})
        action = request.match_info["action"]
        if action == "online":
            return web.json_response({"players": list(self.bot.online_players)})
        if action == "status":                       # the player website shows every server
            info = self.bot.server_info or {}
            online = bool(self.bot.rcon.connected and info)
            return web.json_response({"online": online,
                                      "players": int(info.get("Players") or len(self.bot.online_players)) if online
                                      else 0, "max_players": int(info.get("MaxPlayers") or 0) if online else 0,
                                      "queued": int(info.get("Queued") or 0) if online else 0})
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise web.HTTPBadRequest() from None
        if action in ("kits", "kitinfo"):            # its in-game kits, for the main server's web store
            from rce import kits as kitparse
            from rce.rcon_client import RconError
            kits_cog = self.bot.get_cog("Kits")
            if kits_cog is None or not self.bot.rcon.connected:
                return web.json_response({"kits": None, "items": None})
            try:
                if action == "kits":
                    return web.json_response({"kits": list(await kits_cog.kit_names.get(max_wait=5))})
                raw = await self.bot.rcon.command(f'kit info "{kitparse.clean_arg(str(data.get("kit") or ""))}"',
                                                  timeout=4)
            except RconError:
                return web.json_response({"kits": None, "items": None})
            return web.json_response({"items": [{"short": i.short_name, "amount": i.amount, "container": i.container}
                                                for i in kitparse.parse_kit_info(raw)]})
        from rce import kits as kitparse
        from utils import sanitize_player_name
        wanted = str(data.get("player") or "").lower()
        name = next((n for n in self.bot.online_players if n.lower() == wanted), None)
        if name is None or not self.bot.rcon.connected:
            return web.json_response({"result": "offline"})
        cfg = self.bot.config
        if data.get("kit"):
            command = cfg.kit_give_template.format(kit=kitparse.clean_arg(str(data["kit"])), name=name)
        else:
            item = re.sub(r"[^\w.\-]", "", str(data.get("item") or ""))[:60]
            try:
                amount = max(1, min(int(data.get("amount") or 1), 100_000))
            except (TypeError, ValueError):
                amount = 1
            if not item:
                raise web.HTTPBadRequest()
            command = cfg.item_give_template.format(name=sanitize_player_name(name), item=item, amount=amount)
        try:
            await self.bot.rcon.command(command, expect_response=False)
        except Exception as exc:                     # RCON down
            log.warning("Give for another server failed: %s", exc)
            return web.json_response({"result": "offline"})
        log.info("Given for another server: %s", command)
        return web.json_response({"result": "given"})

    def _proxy_target(self, request: web.Request) -> dict | None:
        """The extra server this panel shows (picked in the drop-down) for an API request, else None."""
        sid = request.cookies.get(SERVER_COOKIE)
        if not sid or servers.is_child() or sid == servers.MAIN_ID:
            return None
        if not request.path.startswith("/api/") or request.path in MAIN_ONLY_API \
                or request.path.startswith(("/api/servers", "/api/allplugins")):
            return None                     # the server list and managing servers stay the main server's
        return next((s for s in servers.all_servers() if s["id"] == sid), None)

    @web.middleware
    async def _proxy_middleware(self, request: web.Request, handler):
        target = self._proxy_target(request)
        if target is None:
            return await handler(request)
        return await self._proxy(request, target)

    async def _proxy(self, request: web.Request, target: dict) -> web.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        headers["Host"] = f"127.0.0.1:{target['port']}"
        headers["Accept-Encoding"] = "identity"           # this server compresses the answer itself
        if not request.headers.get("CF-Connecting-IP") and not self._is_local(request):
            headers["X-Forwarded-For"] = self._client_ip(request)   # the other server must not think it's local
        headers.setdefault("X-Forwarded-Proto", "https" if self._is_https(request) else "http")
        if self._proxy_session is None or self._proxy_session.closed:
            self._proxy_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120),
                                                        auto_decompress=False)
        url = f"http://127.0.0.1:{target['port']}{request.path_qs}"
        try:
            async with self._proxy_session.request(request.method, url, headers=headers, data=await request.read(),
                                                   allow_redirects=False) as upstream:
                body = await upstream.read()
                resp = web.Response(status=upstream.status, body=body)
                for key, value in upstream.headers.items():
                    if key.lower() not in HOP_HEADERS | {"content-length", "content-encoding", "server"}:
                        resp.headers.add(key, value)
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({"error": f"{target['name']} isn't running right now – start it on the Servers "
                                               "page, or pick another server.", "server_down": True}, status=502)

    async def _knock(self, request: web.Request, tail: str) -> None:
        """The secret way in from the player website: WEB_PANEL_KNOCK typed into the player search opens the panel.
        Nothing on the website gives it away – any other search is just a search."""
        knock = self.ws.panel_knock
        if (not is_hashed(knock) or request.method != "GET" or tail not in ("player", "p/website/player")
                or not self._website_running()):
            return
        typed = request.query.get("name", "").strip()[:256]
        if len(typed) >= KNOCK_MIN and await asyncio.to_thread(verify_password, typed, knock):
            log.info("Control panel opened from the website's secret search by %s", self._client_ip(request))
            raise web.HTTPSeeOther(self.panel_path)

    async def _admin(self, request: web.Request) -> web.StreamResponse:
        """/admin: the panel on the bot's own PC only – from anywhere else a honeypot (fake login, every visit
        logged) or, with the honeypot off, it doesn't exist."""
        if self._is_local(request):
            if request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET"])
            return await self._panel(request)
        if self.ws.honeypot:
            return await self._honeypot(request)
        if request.method != "GET":
            raise web.HTTPNotFound(text="Not found.")
        if self._website_running():
            return await self._serve_plugin(request, "website")      # the website's normal "not found" page
        raise web.HTTPNotFound(text="Not found.")

    def _honeypot_limited(self, request: web.Request) -> float | None:
        """Seconds to wait if this visitor (or everyone together) is over the honeypot's limits."""
        ip = self._client_ip(request)
        if request.method == "POST" and (wait := self._honeypot_logins.retry_after(ip)) is not None:
            return wait
        for limiter, key in ((self._honeypot_ip, ip), (self._honeypot_total, "all")):
            if (wait := limiter.retry_after(key)) is not None:
                return wait
        self._honeypot_ip.hit(ip)
        self._honeypot_total.hit("all")
        if request.method == "POST":
            self._honeypot_logins.hit(ip)
        return None

    def _real_password_of(self, password: str) -> str | None:
        """Account whose real panel password this is (owner or another account) – or None."""
        if self.password_hash and verify_password(password, self.password_hash):
            return OWNER
        return self.users.password_of(password)

    async def _honeypot(self, request: web.Request) -> web.Response:
        """The fake login at /admin: notes who came (IP, location, browser, the login tried …) – never lets anyone
        in. Rate limited: over the limit it answers "too many attempts" and logs nothing (no flooding the list)."""
        wait = self._honeypot_limited(request)
        if wait is not None:
            self._honeypot_blocked += 1
            return web.Response(text=honeypot.fake_login_page(error="Too many login attempts. Try again later."),
                                content_type="text/html", status=429,
                                headers={"Retry-After": str(int(wait) + 1), "Cache-Control": "no-store",
                                         "X-Robots-Tag": "noindex"})
        entry = honeypot.visitor(request, self._client_ip(request))
        entry["server"] = servers.server_name(servers.server_id())     # whose /admin it was (several servers)
        if request.method == "POST":
            form = await request.post()
            password = str(form.get("password") or "")[:128]
            entry["username"] = str(form.get("username") or "")[:64]
            entry["password_given"] = bool(password)
            real = await asyncio.to_thread(self._real_password_of, password) if password else None
            if real:                                                    # an admin at the wrong address: not saved
                entry["real_password_of"] = real
                if self.bot is not None:
                    self.bot.audit(f"⚠️ Honeypot: someone typed the real panel password of **{real}** into the "
                                   f"fake /admin login (IP {entry['ip']}). It was not saved – if that wasn't one "
                                   "of your admins, change that password.")
            elif password:
                entry["password"] = password
        self.honeypot.add(entry)
        log.warning("Honeypot: %s %s from %s (%s, %s on %s)", request.method, request.path, entry["ip"],
                    entry["location"], entry["browser"], entry["os"])
        told_key, now = f"{entry['ip']}:{request.method}", time.time()
        if self.bot is not None and now - self._honeypot_told.get(told_key, 0) > HONEYPOT_TELL_EVERY:
            self._honeypot_told[told_key] = now
            self.bot.audit(honeypot.summary(entry))                     # never contains the password
        if request.method == "POST":
            await asyncio.sleep(1.5)                                    # like a real login check – slows guessing
        return web.Response(text=honeypot.fake_login_page(error=request.method == "POST"), content_type="text/html",
                            status=401 if request.method == "POST" else 200,
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    async def system_info(self, request: web.Request) -> web.Response:
        """System page: CPU, memory, disk of this PC and the memory of the bot's processes (every server)."""
        procs: dict[str, int | None] = {f"{servers.server_name(servers.server_id())} (this bot)": os.getpid()}
        manager = getattr(servers, "manager", None)
        if manager is not None and not servers.is_child():
            for sid, proc in sorted(manager.procs.items()):
                procs[servers.server_name(sid)] = proc.info().get("pid")
        disk = self.bot.config.database_path.parent if self.bot is not None else Path(".")
        return web.json_response(self.system.snapshot(procs, disk if disk.exists() else Path(".")))

    async def honeypot_list(self, request: web.Request) -> web.Response:
        entries = self.honeypot.entries()
        return web.json_response({"enabled": self.ws.honeypot, "entries": entries, "blocked": self._honeypot_blocked,
                                  "limits": {"per_ip": HONEYPOT_PER_IP_PER_MINUTE,
                                             "logins_per_ip": HONEYPOT_LOGINS_PER_IP_PER_MINUTE,
                                             "total": HONEYPOT_TOTAL_PER_MINUTE},
                                  "ips": len({e.get("ip") for e in entries}),
                                  "logins": sum(1 for e in entries if e.get("method") == "POST")})

    async def honeypot_clear(self, request: web.Request) -> web.Response:
        self.honeypot.clear()
        self._audit("Honeypot log cleared")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ Middleware

    @staticmethod
    def _is_https(request: web.Request) -> bool:
        """Served over HTTPS – directly, or through the local Cloudflare tunnel (which says so in a header)."""
        if request.secure:
            return True
        return request.remote in ("127.0.0.1", "::1") and \
            request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"

    async def _security_headers(self, request: web.Request, resp: web.StreamResponse) -> None:
        """Every answer (pages, API, errors): browser protections; the panel also gets a strict CSP."""
        for key, value in SECURITY_HEADERS.items():
            resp.headers.setdefault(key, value)
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers["Server"] = "RCE-Bot"                     # no version numbers for scanners
        path = request.path
        if path.startswith(("/admin", "/static/", "/api/", "/auth")) or path.rstrip("/") == self.panel_path:
            resp.headers["Content-Security-Policy"] = PANEL_CSP
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        if self._is_https(request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")

    @web.middleware
    async def _compress_middleware(self, request: web.Request, handler):
        resp = await handler(request)
        maybe_compress(request, resp)
        return resp

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as exc:
            return web.json_response({"error": str(exc), **exc.data}, status=exc.status)
        except RconError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except web.HTTPException:
            raise
        except Exception:
            log.exception("Error in web interface (%s %s)", request.method, request.path)
            return web.json_response({"error": "Internal error – see the bot log for details."}, status=500)

    @web.middleware
    async def _host_middleware(self, request: web.Request, handler):
        # DNS rebinding protection: if the web interface only runs locally, the host must be local too.
        if self.ws.host in ("127.0.0.1", "localhost"):
            host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") \
                else (request.host or "").split("]")[0] + "]"
            allowed = LOCAL_HOSTS | {h for h in (self.tunnel.host if self.tunnel else None,
                                                 urlparse(self.ws.public_url).hostname) if h}
            if host.lower() not in allowed:
                raise web.HTTPForbidden(text="Unknown host.")
        return await handler(request)

    @staticmethod
    def _same_site_form(request: web.Request) -> bool:
        """A form POST comes from this website itself. Browsers name the sender: Origin, else (no Origin, or "null"
        – privacy settings send that on the site's own forms, a sandboxed frame of another site too) Sec-Fetch-Site,
        else the Referer. Without any of them it's no browser (curl, a script) – those carry no visitor's cookies,
        so there's nothing to forge."""
        host = (request.host or "").lower()
        origin = request.headers.get("Origin")
        if origin is not None and origin != "null":
            return (urlparse(origin).netloc or "").lower() == host
        fetch_site = request.headers.get("Sec-Fetch-Site")      # set by the browser itself, pages can't fake it
        if fetch_site is not None:
            return fetch_site in ("same-origin", "none")
        referer = request.headers.get("Referer")
        if referer:
            return (urlparse(referer).netloc or "").lower() == host
        return origin is None                                    # "null" and nothing else to go by: refused

    @web.middleware
    async def _origin_middleware(self, request: web.Request, handler):
        """Forms of the player website (store, account, casino, battle pass …) may only be sent from the site
        itself (CSRF): a POST from another site is refused (webhooks under /hooks/, the signed /internal/ calls of
        the other servers, the panel API – own header check – and the honeypot's fake login, which never lets anyone
        in and should log attempts from anywhere, excepted)."""
        if request.method == "POST" and not request.path.startswith(("/api/", "/hooks/", "/internal/")) \
                and request.path.rstrip("/") != "/admin" and not self._same_site_form(request):
            raise web.HTTPForbidden(text="Forms can only be sent from this website.")
        return await handler(request)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/"):
            return await handler(request)
        import saas_mode
        if saas_mode.blocked(request.method, path):
            raise ApiError("Not available on hosted servers.", 403)
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "rce-web":
            raise ApiError("Invalid request.", 403)
        if path == "/api/login":
            return await handler(request)
        session = self._session_user(request)
        if session is None:
            raise ApiError("Not logged in.", 401)
        request["user"], request["role"] = session
        if request["role"] != "owner" and path.startswith(OWNER_ONLY_PREFIXES):
            raise ApiError("Only owners can do this (ask the server owner).", 403)
        if any(request.method == m and path.startswith(p) for m, p in REAUTH_PATHS) \
                and self._sudo.get(request.cookies.get(SESSION_COOKIE, ""), 0) < time.time():
            raise ApiError("Please confirm your password for this.", 403, reauth=True,
                           need_2fa=self.two_factor.enabled(session[0]))
        _current_user.set(session[0])
        return await handler(request)

    # ------------------------------------------------------------ The management AI using the panel

    @staticmethod
    def ai_blocked(method: str, path: str) -> str | None:
        """Why the management AI may not make this request (None = allowed)."""
        method, path = method.upper(), "/" + path.lstrip("/")
        if not path.startswith("/api/"):
            return "Only the panel API (/api/…) can be used."
        if path.startswith(AI_BLOCKED):
            return "Logins, accounts, 2FA, restart, updates, backups, setup and raw console are off-limits for the AI."
        if method not in ("GET", "HEAD") and (path.startswith(AI_BLOCKED_WRITES) or AI_BLOCKED_WRITE_RE.match(path)):
            return "The AI may only look at this (servers, domain, gift cards, plugin code), not change it."
        return None

    @staticmethod
    def ai_clean_settings(body) -> tuple[dict | None, list[str]]:
        """PUT /api/settings from the AI: secrets and access settings are dropped. (values, dropped keys)."""
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            return None, []
        kept, dropped = {}, []
        for key, value in values.items():
            f = settings_schema.FIELDS_BY_KEY.get(key)
            if f is None or f.kind == "secret" or key in AI_LOCKED_SETTINGS:
                dropped.append(str(key))
            else:
                kept[key] = value
        return kept, dropped

    def ai_endpoints(self, query: str = "") -> list[dict]:
        """The panel's API the AI may use: method, path and what it does (first line of the handler's docstring)."""
        out, q = [], (query or "").lower()
        for route in self.app.router.routes():
            info = route.resource.get_info() if route.resource is not None else {}
            path = info.get("path") or info.get("formatter") or ""
            if route.method == "HEAD" or not path.startswith("/api/") or self.ai_blocked(route.method, path):
                continue
            doc = (getattr(route.handler, "__doc__", None) or "").strip().split("\n")[0][:140]
            item = {"method": route.method, "path": path, **({"about": doc} if doc else {})}
            if not q or q in (path + " " + doc).lower():
                out.append(item)
        return out

    @staticmethod
    def _ai_redact(value):
        if isinstance(value, dict):
            return {k: ("[hidden]" if AI_SECRET_KEY.search(str(k)) and isinstance(v, str) and v
                        else WebInterface._ai_redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [WebInterface._ai_redact(v) for v in value]
        return value

    async def ai_request(self, method: str, path: str, body=None) -> dict:
        """One panel API request as the "Management AI" user (logged in the admin log like any panel user).
        The caller (cogs/manager_ai.py) asks the admin before anything that changes something."""
        method = (method or "GET").upper()
        path = "/" + str(path or "").lstrip("/")
        if (why := self.ai_blocked(method, path.split("?")[0])) is not None:
            return {"ok": False, "status": 403, "error": why}
        dropped: list[str] = []
        if method == "PUT" and path.split("?")[0] == "/api/settings":
            values, dropped = self.ai_clean_settings(body)
            if not values:
                return {"ok": False, "status": 403, "error": "None of these settings may be changed by the AI "
                                                             "(secrets and access settings are off-limits).",
                        "not_allowed": dropped}
            body = {"values": values}
        token = self._sign(AI_USER, int(time.time()) + 120, secrets.token_hex(8))
        if method == "PUT" and path.split("?")[0] == "/api/settings":
            self._sudo[token] = time.time() + 60            # already confirmed by the admin in Discord
        host = self.ws.host if self.ws.host not in ("", "0.0.0.0", "::", "localhost") else "127.0.0.1"
        url = f"http://{'[' + host + ']' if ':' in host else host}:{self.ws.port}{path}"
        headers = {CSRF_HEADER: "rce-web", "Host": f"127.0.0.1:{self.ws.port}"}
        cookies = {SESSION_COOKIE: token, GATE_COOKIE: self._gate_value()}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), cookies=cookies) as session:
                kwargs = {"json": body} if body is not None and method not in ("GET", "HEAD") else {}
                async with session.request(method, url, headers=headers, **kwargs) as resp:
                    kind = resp.content_type or ""
                    if "json" in kind:
                        data = self._ai_redact(await resp.json(content_type=None))
                    elif kind.startswith("text/") or not kind:
                        data = (await resp.text())[:AI_MAX_RESULT]
                    else:
                        data = f"({kind}, {len(await resp.read())} bytes – not shown)"
                    status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"ok": False, "status": 0, "error": f"The panel did not answer: {exc}"}
        finally:
            self._sudo.pop(token, None)          # the token never leaves the bot and expires after 2 minutes
        text = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")) if not isinstance(data, str) else data
        out = {"ok": 200 <= status < 300, "status": status}
        if len(text) > AI_MAX_RESULT:
            out["result_truncated"] = text[:AI_MAX_RESULT]
            out["note"] = "Result was cut off – ask for something narrower."
        else:
            out["result"] = data
        if dropped:
            out["not_changed"] = dropped
            out["note_settings"] = "Secrets and access settings can't be changed by the AI."
        return out

    def _session_key(self, username: str) -> str | None:
        """Secret part of the signature: the owner's WEB_PASSWORD or the user's password hash.
        Changing the password (or deleting/disabling the account) ends all their sessions."""
        if username == OWNER:
            return self.password_hash
        if username == AI_USER:
            return self._ai_key
        return self.users.session_key(username)

    def _sign(self, username: str, expires: int, nonce: str) -> str | None:
        key = self._session_key(username)
        if key is None:
            return None
        msg = f"{username}:{expires}:{nonce}:{key}".encode()
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return f"{username.encode().hex()}.{expires}.{nonce}.{sig}"

    def _session_user(self, request: web.Request) -> tuple[str, str] | None:
        """(username, role) of a valid session cookie, else None."""
        token = request.cookies.get(SESSION_COOKIE, "")
        parts = token.split(".")
        if token in self._revoked or len(parts) != 4 or not parts[1].isdigit() or int(parts[1]) < time.time():
            return None
        try:
            username = bytes.fromhex(parts[0]).decode()
        except ValueError:
            return None
        expected = self._sign(username, int(parts[1]), parts[2])
        if expected is None or not hmac.compare_digest(token, expected):
            return None
        if username == OWNER:
            return OWNER, "owner"
        if username == AI_USER:
            return AI_USER, "owner"
        return username, self.users.get(username)["role"]

    def _set_session_cookie(self, resp: web.StreamResponse, username: str = OWNER,
                            request: web.Request | None = None) -> None:
        # Random nonce: every login gets its own token (logout revokes exactly that one)
        token = self._sign(username, int(time.time()) + SESSION_TTL, secrets.token_hex(16))
        if token:
            resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Strict",
                            secure=bool(request is not None and self._is_https(request)), path="/")
            self._sudo[token] = time.time() + REAUTH_SECONDS      # just logged in: risky actions OK for a while
            now = time.time()
            for t in [t for t, until in self._sudo.items() if until < now]:
                self._sudo.pop(t, None)

    # ------------------------------------------------------------ Helpers

    async def _json(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise ApiError("Invalid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError("Invalid JSON.")
        return data

    @staticmethod
    def _text(data: dict, key: str, *, required: bool = True, limit: int = 200) -> str:
        value = str(data.get(key) or "").strip()[:limit]
        if required and not value:
            raise ApiError(f'Field "{key}" is missing.')
        return value

    @staticmethod
    def _int(data: dict, key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            raise ApiError(f'Field "{key}" must be a number.') from None
        return max(lo, min(hi, value))

    def _audit(self, text: str) -> None:
        user = _current_user.get()
        if self.bot is not None:
            self.bot.audit(f"🌐 Web interface ({user}): {text}")
        log.info("Web action by %s: %s", user, text)

    def _cog(self, name: str):
        cog = self.bot.get_cog(name)
        if cog is None:
            raise ApiError(f"Module {name} is not loaded.", 503)
        return cog

    # ------------------------------------------------------------ Web store (owner)

    async def webshop(self, request: web.Request) -> web.Response:
        shop = self._cog("WebShop")
        db = self.bot.db
        syncing = shop.start_sync()           # new bot / in-game kits – in the background, the page doesn't wait
        order_cols = ("id", "kind", "title", "player", "user_id", "total", "currency", "gift_code", "gift_amount",
                      "paypal_amount", "paypal_order", "capture_id", "payer_email", "admin_key", "status", "result", "request_id",
                      "created_at", "paid_at")
        orders = [{k: (str(o[k]) if k == "user_id" and o[k] else o[k]) for k in order_cols}
                  for o in db.store_orders(limit=150)]
        return web.json_response({
            "status": shop.status(), "syncing": syncing,
            "kits": [{**dict(r), "items": len(shop.kit_items(r)), "for_servers": shop.kit_servers(r)}
                     for r in db.store_kits()],
            "servers": [{"id": s["id"], "name": s["name"]} for s in servers.all_servers()],
            "this_server": servers.server_id(),
            "custom_kits": [k["name"] for k in db.custom_kits()],
            "gift_cards": [dict(g) for g in db.gift_cards()], "orders": orders,
            "price_list": bool(shop.price_list())})

    def _store_kit_fields(self, data: dict, *, partial: bool = False, source: str = "custom") -> dict:
        out = {}
        if (not partial or "kit" in data) and source == "custom":
            kit = self.bot.db.custom_kit(self._text(data, "kit", limit=60))
            if kit is None:
                raise ApiError("Choose an existing custom kit (create one in the kit builder first).")
            out["kit"] = kit["name"]
        if "servers" in data:                  # several servers: which one(s) the kit is for
            wanted = [str(s) for s in data.get("servers") or [] if isinstance(s, str)]
            known = [s["id"] for s in servers.all_servers()]
            picked = [s for s in known if s in wanted]
            if wanted and not picked:
                raise ApiError("Pick at least one server for the kit.")
            out["servers"] = "" if picked in ([], [servers.server_id()]) else ",".join(picked)
        if not partial or "name" in data:
            out["name"] = self._text(data, "name", limit=60)
        if not partial or "price" in data:
            try:
                price = round(float(data.get("price")), 2)
            except (TypeError, ValueError):
                raise ApiError("The price must be a number.") from None
            if not 0.01 <= price <= 100_000:
                raise ApiError("The price must be between 0.01 and 100 000.")
            out["price"] = price
        if not partial or "days" in data:
            try:
                days = int(data.get("days") or 0)
            except (TypeError, ValueError):
                raise ApiError("Days must be a whole number.") from None
            if not 0 <= days <= 3650:
                raise ApiError("Days: 0 (permanent) to 3650.")
            out["days"] = days
        if not partial or "description" in data:
            out["description"] = str(data.get("description") or "").strip()[:500] or None
        if not partial or "category" in data:
            out["category"] = " ".join(str(data.get("category") or "").split())[:40] or None
        if "enabled" in data:
            out["enabled"] = 1 if data.get("enabled") else 0
        return out

    async def webshop_kit_create(self, request: web.Request) -> web.Response:
        f = self._store_kit_fields(await self._json(request))
        kit_id = self.bot.db.add_store_kit(f["name"], f["kit"], f["price"], f["days"], f["description"],
                                           category=f["category"])
        if f.get("servers"):
            self.bot.db.update_store_kit(kit_id, servers=f["servers"])
            await self._cog("WebShop").sync_kit_servers(self.bot.db.store_kit(kit_id))
        self._audit(f"store: kit **{f['name']}** for sale ({f['kit']}, {f['price']})")
        return web.json_response({"id": kit_id})

    async def webshop_kit_update(self, request: web.Request) -> web.Response:
        kit_id = int(request.match_info["kit_id"])
        row = self.bot.db.store_kit(kit_id)
        if row is None:
            raise ApiError("Kit not found.", 404)
        f = self._store_kit_fields(await self._json(request), partial=True, source=row["source"])
        if f.get("price", 0) > 0 and row["price"] <= 0 and "enabled" not in f:
            f["enabled"] = 1                  # automatically added kit got its first price: on sale now
        if f.get("enabled") and (f.get("price") or row["price"]) <= 0:
            raise ApiError("Set a price first.")
        self.bot.db.update_store_kit(kit_id, **f)
        if "servers" in f:
            await self._cog("WebShop").sync_kit_servers(self.bot.db.store_kit(kit_id))
        self._audit(f"store: kit #{kit_id} changed")
        return web.json_response({"ok": True})

    async def webshop_kit_delete(self, request: web.Request) -> web.Response:
        if not self._cog("WebShop").remove_kit(int(request.match_info["kit_id"])):
            raise ApiError("Kit not found.", 404)
        self._audit(f"store: kit #{request.match_info['kit_id']} removed")
        return web.json_response({"ok": True})

    async def webshop_giftcards_create(self, request: web.Request) -> web.Response:
        from cogs.webshop import CheckoutError
        data = await self._json(request)
        try:
            amount, count, days = float(data.get("amount")), int(data.get("count") or 1), int(data.get("days") or 0)
        except (TypeError, ValueError):
            raise ApiError("Value, number and days must be numbers.") from None
        if not 0 <= days <= 3650:
            raise ApiError("Valid for: 0 (no end) to 3650 days.")
        try:
            codes = self._cog("WebShop").create_gift_cards(amount, count, request["user"], days=days,
                                                           note=str(data.get("note") or ""))
        except CheckoutError as exc:
            raise ApiError(str(exc)) from None
        self._audit(f"store: {len(codes)} gift card(s) of {amount:.2f} created")
        return web.json_response({"codes": codes})

    async def webshop_giftcard_toggle(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        code = request.match_info["code"]
        if not self.bot.db.set_gift_card_disabled(code, bool(data.get("disabled"))):
            raise ApiError("Gift card not found.", 404)
        self._audit(f"store: gift card {code[:4]}… {'disabled' if data.get('disabled') else 'enabled'}")
        return web.json_response({"ok": True})

    async def webshop_retry(self, request: web.Request) -> web.Response:
        order_id = int(request.match_info["order_id"])
        shop = self._cog("WebShop")
        ok = await shop.retry(order_id)
        order = self.bot.db.store_order(order_id)
        if order is None:
            raise ApiError("Order not found.", 404)
        self._audit(f"store: order #{order_id} delivery retried ({order['status']})")
        return web.json_response({"ok": ok, "status": order["status"], "result": order["result"]})

    # ------------------------------------------------------------ Item pictures

    async def bot_avatar(self, request: web.Request) -> web.Response:
        """The Discord bot's profile picture (128 px PNG) – the control panel's logo. Fetched once per picture and
        kept in memory, so browsers never load it from Discord. 404 while the bot isn't logged in to Discord."""
        user = getattr(self.bot, "user", None) if self.bot is not None else None
        if user is None:
            raise web.HTTPNotFound()
        asset = user.display_avatar.replace(size=128, format="png")
        cached = getattr(self, "_avatar", None)
        if cached is None or cached[0] != asset.url:
            try:
                data = await asset.read()
            except (discord.DiscordException, aiohttp.ClientError, OSError) as exc:
                log.debug("Bot avatar: %s", exc)
                raise web.HTTPNotFound() from None
            self._avatar = cached = (asset.url, data, hashlib.sha1(data).hexdigest()[:16])
        if request.headers.get("If-None-Match") == f'"{cached[2]}"':
            return web.Response(status=304)
        return web.Response(body=cached[1], content_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600", "ETag": f'"{cached[2]}"'})

    async def item_image(self, request: web.Request) -> web.Response:
        self._public_limit(request, images=True)
        template = (self.bot.config.item_image_url if self.bot is not None else "") or ""
        self.item_images.url_template = template or "off"     # "off": bundled/own pictures only, no downloads
        name = request.match_info["name"]
        for ext in (".png", ".jpg", ".webp"):
            name = name.removesuffix(ext)
        size = int(request.query["s"]) if request.query.get("s", "").isdigit() else None
        found = await self.item_images.get(name, size if size in THUMB_SIZES else None, wait=WAIT_FOR_DOWNLOAD)
        if found is None:
            # Never cached (not by the browser, not by Cloudflare): the picture may arrive a moment later
            if request.query.get("ph"):        # player website: a neutral tile with the shortname instead of a 404
                return web.Response(text=placeholder_svg(name), content_type="image/svg+xml",
                                    headers={"Cache-Control": "no-store",
                                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})
        data, kind = found
        return conditional(request, data, kind, "public, max-age=604800")

    async def item_images_scan(self, request: web.Request) -> web.Response:
        """Owner: fetch the pictures of every catalogue item now and report which ones have none."""
        template = self.bot.config.item_image_url if self.bot is not None else ""
        if not template or template.lower() == "off":
            raise ApiError("Item pictures are switched off (Settings → Web → Item pictures from).")
        state = self._scan
        if state.get("running"):
            return web.json_response(state)
        self.item_images.url_template = template
        state.clear()
        state.update(running=True, done=0, total=len(itemlist.COMMON_ITEMS), found=0, missing=[])

        def progress(done: int, total: int) -> None:
            state.update(done=done, total=total)

        async def run() -> None:
            try:
                result = await self.item_images.scan(itemlist.COMMON_ITEMS, progress=progress)
                state.update(found=len(result["found"]), missing=result["missing"])
                state["sources"] = self.item_images.sources
            except Exception as exc:                 # never leave the page waiting forever
                log.exception("Item picture scan failed")
                state.update(error=str(exc)[:200])
            finally:
                state["running"] = False
        self._scan_task = asyncio.create_task(run())
        self._audit("item picture scan started")
        return web.json_response(state)

    async def item_images_scan_state(self, request: web.Request) -> web.Response:
        return web.json_response(self._scan or {"running": False, "done": 0, "total": 0, "found": 0, "missing": [],
                                                "never": True})

    # ------------------------------------------------------------ Plugins

    def _plugins(self):
        manager = getattr(self.bot, "plugins", None) if self.bot is not None else None
        if manager is None:
            raise ApiError("Plugins are not loaded (is the bot running?).", 503)
        return manager

    async def _plugin_call(self, coro):
        from plugin_manager import PluginError
        try:
            return await coro
        except PluginError as exc:
            raise ApiError(str(exc)) from None

    async def plugins_list(self, request: web.Request) -> web.Response:
        manager = self._plugins()
        manager.discover()          # picks up files added by hand
        return web.json_response({"plugins": manager.describe_all(),
                                  "kits": [k["name"] for k in self.bot.db.custom_kits()],
                                  "intents": {"members": self.bot.intents.members,
                                              "message_content": self.bot.intents.message_content}})

    # Plugins in the control panel menu (sidebar), chosen by the owner

    def _menu_items(self) -> list[dict]:
        manager = self._plugins()
        out = []
        for entry in manager.menu_entries():
            info = manager.describe(entry["id"])
            out.append({"id": entry["id"], "group": entry["group"], "name": info["name"], "icon": info["icon"],
                        "enabled": info["enabled"]})
        return out

    async def plugins_menu(self, request: web.Request) -> web.Response:
        return web.json_response({"menu": self._menu_items()})

    async def plugins_menu_save(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        manager = self._plugins()
        try:
            manager.set_menu(data.get("plugins"))
        except Exception as exc:
            raise ApiError(str(exc)) from None
        self._audit("control panel menu: " + (", ".join(manager.menu()) or "no plugins"))
        return web.json_response({"menu": self._menu_items()})

    # Offline protection zones have their own tab (plugins/offline_protection.py)

    async def protection_get(self, request: web.Request) -> web.Response:
        manager = self._plugins()
        if "offline_protection" not in manager.infos:
            raise ApiError("Offline protection isn't installed.", 404)
        plugin = manager.running("offline_protection")
        return web.json_response({"plugin": manager.describe("offline_protection"),
                                  "zones": plugin.zone_list() if plugin is not None else [],
                                  "kits": [k["name"] for k in self.bot.db.custom_kits()], "now": time.time()})

    # Battle royale page: state, test round, shrink now, stop, marker test (plugins/battle_royale.py)
    async def br_get(self, request: web.Request) -> web.Response:
        manager = self._plugins()
        if "battle_royale" not in manager.infos:
            raise ApiError("Battle royale isn't installed.", 404)
        plugin = manager.running("battle_royale")
        return web.json_response({"plugin": manager.describe("battle_royale"),
                                  "game": plugin.panel_state() if plugin is not None else None,
                                  "kits": [k["name"] for k in self.bot.db.custom_kits()], "now": time.time()})

    async def br_action(self, request: web.Request) -> web.Response:
        plugin = self._plugins().running("battle_royale")
        if plugin is None:
            raise ApiError("Switch battle royale on first.")
        action, user = request.match_info["action"], request.get("user", "?")
        if action == "delete-zones":
            count = await plugin.delete_all_zones()
            self._audit(f"battle royale: deleted all zones ({user})")
            return web.json_response({"ok": True, "message": f"Deleted all battle royale zones ({count} names tried).",
                                      "game": plugin.panel_state()})
        if action == "clear-markers":
            result = await plugin.clear_markers()
            self._audit(f"battle royale: cleared {result['count']} vending machine(s)")
            return web.json_response({"ok": True, "message": f"Delete sent for {result['count']} vending machine(s).",
                                      "marker_clear": result})
        if action == "forget-markers":
            count = plugin.forget_markers()
            self._audit(f"battle royale: forgot {count} vending machine ID(s)")
            return web.json_response({"ok": True, "message": f"Forgot {count} vending machine ID(s)."})
        if action == "marker-test":
            result = await plugin.test_marker()
            self._audit(f"battle royale marker test (ID {result['id'] or 'none'})")
            return web.json_response({"ok": True, "marker_test": result})
        if action == "stop":
            if plugin.game["state"] == "idle":
                raise ApiError("No round running.")
            await plugin.end_round()
            ok, msg = True, "Round stopped – zones and vending machines deleted."
        elif action == "test":
            ok, msg = await plugin.start_test()
        else:
            ok, msg = await plugin.shrink_now()
        if not ok:
            raise ApiError(msg)
        self._audit(f"battle royale: {action} ({user})")
        return web.json_response({"ok": True, "message": msg, "game": plugin.panel_state()})

    async def protection_delete_zone(self, request: web.Request) -> web.Response:
        plugin = self._plugins().running("offline_protection")
        if plugin is None:
            raise ApiError("Switch offline protection on first.")
        try:
            team_id = int(request.match_info["team"])
        except ValueError:
            raise ApiError("Unknown team.") from None
        if not await plugin.remove_zone(team_id, "deleted in the control panel"):
            raise ApiError("This team has no zone.", 404)
        self._audit(f"offline protection zone of team {team_id} deleted")
        return web.json_response({"ok": True})

    async def plugin_toggle(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        pid = request.match_info["pid"]
        info = await self._plugin_call(self._plugins().set_enabled(pid, bool(data.get("enabled"))))
        self._audit(f"plugin {pid} switched {'on' if info['enabled'] else 'off'}")
        return web.json_response({"plugin": info})

    async def plugin_settings(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        pid = request.match_info["pid"]
        values = data.get("values")
        if not isinstance(values, dict):
            raise ApiError("Invalid settings.")
        errors = await self._plugin_call(self._plugins().save_settings(pid, values))
        if errors:
            return web.json_response({"error": "Please check the highlighted settings.", "errors": errors}, status=400)
        self._audit(f"plugin {pid}: settings saved")
        return web.json_response({"plugin": self._plugins().describe(pid)})

    async def plugin_reload(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        info = await self._plugin_call(self._plugins().reload(pid))
        self._audit(f"plugin {pid} reloaded")
        return web.json_response({"plugin": info})

    # ------------------------------------------------------------ Plugins on every server (main server's panel)

    def _main_only(self) -> None:
        if servers.is_child():
            raise ApiError("This works on the main server's panel.")

    async def _server_api(self, request: web.Request, sid: str, method: str, path: str, payload=None):
        """Calls another server's own web interface on this PC as the logged-in owner (same login on every server)."""
        target = next((s for s in servers.all_servers() if s["id"] == sid), None)
        if target is None:
            raise ApiError("No such server.", 404)
        if self._proxy_session is None or self._proxy_session.closed:
            self._proxy_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120),
                                                        auto_decompress=False)
        headers = {CSRF_HEADER: "rce-web", "Cookie": f"{SESSION_COOKIE}={request.cookies.get(SESSION_COOKIE, '')}"}
        try:
            async with self._proxy_session.request(method, f"http://127.0.0.1:{target['port']}{path}", json=payload,
                                                   headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise ApiError(f"{target['name']}: {(data or {}).get('error') or f'HTTP {resp.status}'}",
                                   resp.status if resp.status < 500 else 502)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise ApiError(f"{target['name']} isn't running right now.", 502) from None

    async def _plugins_of(self, request: web.Request, sid: str) -> list[dict]:
        if sid == servers.MAIN_ID:
            manager = self._plugins()
            manager.discover()
            return manager.describe_all()
        return (await self._server_api(request, sid, "GET", "/api/plugins"))["plugins"]

    async def allplugins_list(self, request: web.Request) -> web.Response:
        """Every plugin on every server: on/off per server – each server is managed on its own."""
        self._main_only()
        out = []
        for s in servers.all_servers():
            try:
                plugins = await self._plugins_of(request, s["id"])
                out.append({"id": s["id"], "name": s["name"], "ok": True, "plugins": [
                    {k: p.get(k) for k in ("id", "name", "icon", "builtin", "overrides", "enabled", "running", "error",
                                           "version", "description")} for p in plugins]})
            except ApiError as exc:
                out.append({"id": s["id"], "name": s["name"], "ok": False, "error": str(exc), "plugins": []})
        return web.json_response({"servers": out})

    async def allplugins_toggle(self, request: web.Request) -> web.Response:
        self._main_only()
        sid, pid = request.match_info["sid"], request.match_info["pid"]
        enabled = bool((await self._json(request)).get("enabled"))
        if sid == servers.MAIN_ID:
            info = await self._plugin_call(self._plugins().set_enabled(pid, enabled))
        else:
            info = (await self._server_api(request, sid, "POST", f"/api/plugins/{pid}/toggle",
                                           {"enabled": enabled}))["plugin"]
        self._audit(f"plugin {pid} on {servers.server_name(sid)} switched {'on' if info['enabled'] else 'off'}")
        return web.json_response({"plugin": info})

    async def allplugins_copy(self, request: web.Request) -> web.Response:
        """Installs one of your plugins (file, settings, pictures) from one server onto others – off until you
        switch it on there (or with "enable")."""
        import ast
        import shutil
        from plugin_manager import CORE_FEATURES, MAX_UPLOAD, PLUGIN_ID, files_dir_for, read_state, \
            state_path_for, user_dir_for
        self._main_only()
        data = await self._json(request)
        pid, source = self._text(data, "plugin", limit=40), self._text(data, "from", limit=40)
        targets = [str(t) for t in data.get("to") or [] if str(t) != source]
        known = {s["id"] for s in servers.all_servers()}
        if not PLUGIN_ID.match(pid) or pid in CORE_FEATURES or source not in known or not targets \
                or any(t not in known for t in targets):
            raise ApiError("Pick a plugin, the server it comes from and where it should go.")
        src_db = servers.database_file(source)
        path = user_dir_for(src_db) / f"{pid}.py"
        if not path.is_file():
            raise ApiError("Only plugins you uploaded or made with the AI can be copied – the built-in ones are on "
                           "every server already (just switch them on there).")
        code = path.read_text(encoding="utf-8")
        if len(code.encode("utf-8")) > MAX_UPLOAD:
            raise ApiError("The plugin file is too big.")
        try:
            ast.parse(code, filename=path.name)
        except SyntaxError as exc:
            raise ApiError(f"{path.name} has a Python error in line {exc.lineno} – fix it first.") from None
        settings = (read_state(state_path_for(src_db)).get("settings") or {}).get(pid) if data.get("settings") else None
        files = files_dir_for(src_db) / pid
        done = []
        for target in targets:
            dst_db = servers.database_file(target)
            dest = user_dir_for(dst_db)
            dest.mkdir(parents=True, exist_ok=True)
            tmp = dest / f"{pid}.tmp"
            tmp.write_text(code, encoding="utf-8")
            os.replace(tmp, dest / f"{pid}.py")
            if files.is_dir():
                shutil.copytree(files, files_dir_for(dst_db) / pid, dirs_exist_ok=True)
            # the server picks the file up (new: switched off; already there: reloaded with the new code)
            if target == servers.MAIN_ID:
                manager = self._plugins()
                manager.discover()
                if manager.running(pid) is not None:
                    await self._plugin_call(manager.reload(pid))
            else:
                listed = await self._server_api(request, target, "GET", "/api/plugins")
                if any(p["id"] == pid and p.get("running") for p in listed["plugins"]):
                    await self._server_api(request, target, "POST", f"/api/plugins/{pid}/reload")
            if isinstance(settings, dict) and settings:
                if target == servers.MAIN_ID:
                    await self._plugin_call(self._plugins().save_settings(pid, settings))
                else:
                    await self._server_api(request, target, "PUT", f"/api/plugins/{pid}/settings",
                                           {"values": settings})
            if data.get("enable"):
                if target == servers.MAIN_ID:
                    await self._plugin_call(self._plugins().set_enabled(pid, True))
                else:
                    await self._server_api(request, target, "POST", f"/api/plugins/{pid}/toggle", {"enabled": True})
            done.append(servers.server_name(target))
        self._audit(f"plugin {pid} copied from {servers.server_name(source)} to " + ", ".join(done))
        return web.json_response({"ok": True, "copied_to": done})

    async def plugin_upload(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        name = self._text(data, "filename", limit=60)
        code = data.get("code")
        if not isinstance(code, str):
            raise ApiError("Choose a .py file.")
        info = await self._plugin_call(self._plugins().install(name, code))
        self._audit(f"plugin file {info['file']} uploaded")
        return web.json_response({"plugin": info})

    def _ai_client(self):
        """The shared DeepSeek client (owned by the Tickets cog). Raises ApiError if the bot/AI isn't ready."""
        tickets = self.bot.get_cog("Tickets") if self.bot is not None else None
        ai = getattr(tickets, "ai", None)
        if ai is None or not getattr(ai, "enabled", False):
            raise ApiError("The AI is off – set a DeepSeek API key in Settings → 🎫 Support first.")
        return ai

    async def plugin_generate(self, request: web.Request) -> web.Response:
        """AI writes a plugin from a description. Returns the code for review – it is NOT installed here."""
        from support.plugin_ai import generate_plugin
        data = await self._json(request)
        description = self._text(data, "description", limit=2000)
        if not description:
            raise ApiError("Describe the plugin you want.")
        self._plugins()                                   # 503 early if plugins aren't loaded
        ai = self._ai_client()
        try:
            result = await generate_plugin(ai, description)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        self._audit(f"AI drafted a plugin ({result['filename']})")
        return web.json_response({"draft": result})

    async def plugin_file_get(self, request: web.Request) -> web.Response:
        path = self._plugins().file_path(request.match_info["pid"], request.match_info["key"])
        if path is None:
            raise web.HTTPNotFound()
        from web.item_images import image_type
        data = path.read_bytes()
        return web.Response(body=data, content_type=image_type(data) or "application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def plugin_file_upload(self, request: web.Request) -> web.Response:
        import base64
        import binascii
        data = await self._json(request)
        pid, key = request.match_info["pid"], request.match_info["key"]
        try:
            raw = base64.b64decode(str(data.get("data") or "").split(",")[-1], validate=True)
        except (binascii.Error, ValueError):
            raise ApiError("Invalid file.") from None
        info = await self._plugin_call(self._plugins().save_file(pid, key, raw))
        self._audit(f"plugin {pid}: picture {key} uploaded")
        return web.json_response({"plugin": info})

    async def plugin_file_delete(self, request: web.Request) -> web.Response:
        pid, key = request.match_info["pid"], request.match_info["key"]
        info = await self._plugin_call(self._plugins().delete_file(pid, key))
        self._audit(f"plugin {pid}: picture {key} removed")
        return web.json_response({"plugin": info})

    async def plugin_delete(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        await self._plugin_call(self._plugins().uninstall(pid))
        self._audit(f"plugin {pid} deleted")
        return web.json_response({"ok": True})

    async def player_website(self, request: web.Request) -> web.StreamResponse:
        return await self.plugin_public(request, pid="website")

    def _website_running(self) -> bool:
        manager = getattr(self.bot, "plugins", None) if self.bot is not None else None
        return bool(manager is not None and manager.running("website") is not None)

    async def _root(self, request: web.Request) -> web.StreamResponse:
        """The site root is the player website; the control panel lives at /admin."""
        tail = request.match_info.get("tail", "").strip("/")
        if tail and tail == self.ws.panel_path and request.method == "GET":
            return await self._panel(request)                 # the control panel's secret address
        if tail and request.method == "GET" and not servers.is_child() and self._session_user(request) is not None \
                and any(s["panel_path"] == f"/{tail}" for s in servers.all_servers() if not s["main"]):
            # an extra server's panel address (an older version opened it here): the panel lives at one address now
            resp = web.HTTPFound(self.panel_path)
            resp.set_cookie(SERVER_COOKIE, servers.MAIN_ID, httponly=True, samesite="Lax",
                            secure=self._is_https(request), path="/")
            raise resp
        if tail.split("/", 1)[0] in ROOT_RESERVED:
            raise web.HTTPNotFound(text="Not found.")       # e.g. a wrong method on an API address
        self._public_limit(request)                          # every public page counts, also with the website off
        await self._knock(request, tail)
        if not self._website_running():
            if not tail and self._is_local(request):
                raise web.HTTPFound("/admin")                # no website: on this PC the address opens the panel
            raise web.HTTPNotFound(text="Not found.")
        return await self._serve_plugin(request, "website")

    async def _old_play(self, request: web.Request) -> web.Response:
        """/play/… was the website's old address: same page at the root (old links in Discord keep working)."""
        self._public_limit(request)
        tail = request.match_info.get("tail", "").strip("/")
        query = f"?{request.query_string}" if request.query_string else ""
        raise web.HTTPMovedPermanently(f"/{tail}{query}")

    async def plugin_public(self, request: web.Request, pid: str | None = None) -> web.StreamResponse:
        self._public_limit(request)
        await self._knock(request, request.path.strip("/"))
        return await self._serve_plugin(request, pid or request.match_info["pid"])

    async def _serve_plugin(self, request: web.Request, pid: str) -> web.StreamResponse:
        manager = getattr(self.bot, "plugins", None) if self.bot is not None else None
        plugin = manager.running(pid) if manager is not None else None
        handler = getattr(plugin, "web_public", None)
        if handler is None:
            raise web.HTTPNotFound(text="Not found.")
        try:
            return await handler(request, request.match_info.get("tail", "").strip("/"))
        except web.HTTPException:
            raise
        except Exception:
            log.exception("Public page of plugin %s failed", pid)
            raise web.HTTPInternalServerError(text="Error – see the bot log.") from None

    # ------------------------------------------------------------ Pages & login

    def _static_file(self, name: str) -> tuple[bytes, str] | None:
        """(content, short hash) of a file in web/static, kept in memory until the file changes."""
        path = STATIC_DIR / name
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._static_cache.get(name)
        if cached is None or cached[0] != mtime:
            data = path.read_bytes()
            cached = (mtime, data, hashlib.blake2s(data, digest_size=6).hexdigest())
            self._static_cache[name] = cached
        return cached[1], cached[2]

    async def _static(self, request: web.Request) -> web.Response:
        """Panel files. With ?v=… (index.html links them by content hash) the browser keeps them for a year."""
        name = request.match_info["name"]
        kind = STATIC_TYPES.get(Path(name).suffix.lower())
        found = self._static_file(name) if kind and STATIC_NAME.match(name) else None
        if found is None:
            raise web.HTTPNotFound()
        cache = YEAR if request.query.get("v") else "no-cache"
        charset = "utf-8" if kind.startswith(("text/", "application/")) else None
        return conditional(request, found[0], kind, cache, charset=charset)

    async def _index(self, request: web.Request) -> web.Response:
        found = self._static_file("index.html")
        if found is None:
            raise web.HTTPNotFound()

        def versioned(m: re.Match) -> str:
            linked = self._static_file(m[2])
            return f"{m[1]}?v={linked[1] if linked else '0'}"
        page = STATIC_LINK.sub(versioned, found[0].decode("utf-8"))
        return conditional(request, page.encode("utf-8"), "text/html", "no-cache", charset="utf-8")

    def _login_blocked(self, *keys: str, count: bool = True) -> float | None:
        """Seconds to wait if one of the keys (IP, username …) is locked. count=True counts this attempt."""
        for key in keys:
            wait = self._login_failures.retry_after(key)
            if wait is None and count:
                wait = self._login_minute.hit(key)
            elif wait is None:
                wait = self._login_minute.retry_after(key)
            if wait is not None:
                return wait
        return None

    def _rate_limit(self, *keys: str) -> None:
        wait = self._login_blocked(*keys)
        if wait is not None:
            minutes = max(1, round(wait / 60))
            raise ApiError(f"Too many attempts – please wait {minutes} minute{'s' if minutes != 1 else ''}.", 429)

    def _login_failed(self, *keys: str) -> None:
        for key in keys:
            self._login_failures.hit(key)

    def _login_ok(self, *keys: str) -> None:
        for key in keys:
            self._login_minute.clear(key)
            self._login_failures.clear(key)

    def _public_limit(self, request: web.Request, *, images: bool = False) -> None:
        """Player website and item pictures: limits per visitor (logged-in admins aren't limited)."""
        if self._session_user(request) is not None:
            return
        ip = self._client_ip(request)
        wait = (self._public_images if images else self._public_pages).hit(ip)
        if wait is None and not images:
            wait = self._public_total.hit("all")
        if wait is not None:
            raise web.HTTPTooManyRequests(text="Too many requests – please wait a moment and reload.",
                                          headers={"Retry-After": str(int(wait) + 1)})

    @web.middleware
    async def _shield_middleware(self, request: web.Request, handler):
        """Flood shield (web/shield.py): before anything else – limits, bans, busy cap, under-attack mode."""
        if not self.shield_on:
            return await handler(request)
        local = self._is_local(request)
        trusted = local or (request.cookies.get(SESSION_COOKIE) and self._session_user(request) is not None)
        path = request.path
        asset = path.startswith(("/static/", "/img/", "/favicon"))
        blocked = self.shield.check(self._client_ip(request), trusted=bool(trusted), asset=asset)
        if blocked is not None:
            status, text, retry = blocked
            error = web.HTTPTooManyRequests if status == 429 else web.HTTPServiceUnavailable
            raise error(text=text, headers={"Retry-After": str(retry), "Cache-Control": "no-store"})
        self.shield.inflight += 1
        try:
            return await handler(request)
        finally:
            self.shield.inflight -= 1

    async def saas_health(self, request: web.Request) -> web.Response:
        """Hosted mode: a small status for the control plane (Bearer SAAS_HEALTH_TOKEN). No tenant data."""
        import saas_mode
        if not saas_mode.enabled() or not saas_mode.health_ok(request.headers.get("Authorization", "")):
            raise web.HTTPNotFound()
        bot = self.bot
        guild = None
        if bot is not None:
            from permcheck import main_guild
            guild = main_guild(bot)
        return web.json_response({
            "ok": True, "setup_mode": self.setup_mode,
            "discord_ready": bool(bot is not None and bot.is_ready()),
            "bot_user": str(bot.user) if bot is not None and bot.user else None,
            "guild_id": str(guild.id) if guild else None, "guild_name": guild.name if guild else None,
            "rcon_connected": bool(bot is not None and bot.rcon.connected),
            "players_online": len(bot.online_players) if bot is not None else 0,
            "latency_ms": round(bot.latency * 1000) if bot is not None and bot.is_ready() else None,
        }, headers={"Cache-Control": "no-store"})

    async def shield_status(self, request: web.Request) -> web.Response:
        """The flood shield: traffic, blocked requests, banned IPs, under-attack mode."""
        exposed = self.ws.host not in ("127.0.0.1", "localhost", "::1")
        return web.json_response({"enabled": self.shield_on, **self.shield.status(),
                                  "tunnel": bool(self.tunnel), "exposed": exposed, "host": self.ws.host})

    async def shield_ban(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        ip = self._text(data, "ip", limit=64)
        if data.get("unban"):
            ok = self.shield.unban(ip)
            self._audit(f"Flood shield: {ip} unblocked")
            return web.json_response({"ok": ok})
        self.shield.ban(ip, self._int(data, "minutes", 60, 1, 10080), why=f"by {_current_user.get()}")
        self._audit(f"Flood shield: {ip} blocked by hand")
        return web.json_response({"ok": True})

    @staticmethod
    def _client_ip(request: web.Request) -> str:
        """Real visitor IP – behind the Cloudflare tunnel every request comes from 127.0.0.1."""
        remote = request.remote or "?"
        forwarded = request.headers.get("CF-Connecting-IP")
        if forwarded and remote in ("127.0.0.1", "::1"):
            return forwarded.strip()[:64]
        return remote

    async def login(self, request: web.Request) -> web.Response:
        ip = self._client_ip(request)
        data = await self._json(request)
        username = str(data.get("username") or OWNER).strip()[:32] or OWNER
        password = str(data.get("password", ""))[:256]
        keys = (ip, "user:" + username.lower())     # per IP, and per account (guessing one account from many IPs)
        self._rate_limit(*keys)

        if username.lower() == OWNER:
            username = OWNER
            ok = await asyncio.to_thread(verify_password, password, self.password_hash)
        else:
            user = await asyncio.to_thread(self.users.authenticate, username, password)
            ok = user is not None
            username = user["username"] if user else username
        if not ok:
            self._login_failed(*keys)
            log.warning("Failed web login for '%s' from %s", username, ip)
            raise ApiError("Wrong username or password.", 401)
        if self.two_factor.enabled(username):
            code = str(data.get("code") or "").strip()
            if not code:
                raise ApiError("Enter the 6-digit code from your authenticator app.", 401, need_2fa=True)
            if not self.two_factor.verify(username, code):
                self._login_failed(*keys)
                log.warning("Wrong 2FA code for '%s' from %s", username, ip)
                raise ApiError("Wrong or used code – wait for the next one.", 401, need_2fa=True)
        self._login_ok(*keys)
        resp = web.json_response({"ok": True})
        self._set_session_cookie(resp, username, request)
        log.info("Web login: %s from %s", username, ip)
        if self.bot is not None:
            self.bot.audit(f"🌐 Web interface: **{username}** logged in"
                           + (" (2FA)" if self.two_factor.enabled(username) else "") + f" from {ip}")
        return resp

    def _check_password(self, username: str, password: str) -> bool:
        if username == OWNER:
            return verify_password(password, self.password_hash)
        return self.users.authenticate(username, password) is not None

    async def reauth(self, request: web.Request) -> web.Response:
        """Confirms the password (and 2FA code) before a risky action – valid for REAUTH_SECONDS."""
        data = await self._json(request)
        user = request["user"]
        keys = (self._client_ip(request), "user:" + user.lower())
        self._rate_limit(*keys)
        ok = await asyncio.to_thread(self._check_password, user, str(data.get("password", ""))[:256])
        if ok and self.two_factor.enabled(user):
            ok = self.two_factor.verify(user, str(data.get("code") or ""))
        if not ok:
            self._login_failed(*keys)
            raise ApiError("Wrong password" + (" or code." if self.two_factor.enabled(user) else "."), 403,
                           reauth=True, need_2fa=self.two_factor.enabled(user))
        self._login_ok(*keys)
        self._sudo[request.cookies.get(SESSION_COOKIE, "")] = time.time() + REAUTH_SECONDS
        return web.json_response({"ok": True})

    # Two-factor login (authenticator app) for the logged-in account

    async def two_factor_state(self, request: web.Request) -> web.Response:
        return web.json_response({"enabled": self.two_factor.enabled(request["user"])})

    async def two_factor_setup(self, request: web.Request) -> web.Response:
        user = request["user"]
        if self.two_factor.enabled(user):
            raise ApiError("Two-factor login is already on.")
        secret = new_secret()
        self._pending_2fa[user.lower()] = (secret, time.time() + 600)
        issuer = (self.bot.config.server_display_name if self.bot is not None else "") or "RCE Bot"
        return web.json_response({"secret": secret, "uri": otpauth_uri(secret, user, f"{issuer} panel")})

    async def two_factor_enable(self, request: web.Request) -> web.Response:
        user = request["user"]
        pending = self._pending_2fa.get(user.lower())
        if pending is None or pending[1] < time.time():
            raise ApiError("Start again – the setup expired.")
        if matching_step(pending[0], str((await self._json(request)).get("code") or "")) is None:
            raise ApiError("That code doesn't match – check the time on your phone and try the next code.")
        self.two_factor.enable(user, pending[0])
        self._pending_2fa.pop(user.lower(), None)
        self._audit("two-factor login switched on")
        return web.json_response({"enabled": True})

    async def two_factor_disable(self, request: web.Request) -> web.Response:
        self.two_factor.disable(request["user"])
        self._audit("two-factor login switched off")
        return web.json_response({"enabled": False})

    async def auth_link(self, request: web.Request) -> web.Response:
        """One-time link from the bot window: logs in and opens the matching page."""
        key = "link:" + self._client_ip(request)
        if not self._is_local(request):
            raise web.HTTPNotFound(text="Not found.")         # the bot window's link: only on this PC
        if self._login_blocked(key) is not None or not same_secret(request.query.get("token", ""), self.login_token):
            self._login_failed(key)
            raise web.HTTPFound("/admin")
        self._login_ok(key)
        resp = web.HTTPFound("/admin#settings" if self.setup_mode else "/admin#overview")
        self._set_session_cookie(resp, OWNER, request)
        raise resp

    async def logout(self, request: web.Request) -> web.Response:
        self._revoked.add(request.cookies.get(SESSION_COOKIE, ""))
        self._sudo.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE)
        resp.set_cookie(SERVER_COOKIE, servers.MAIN_ID, httponly=True, samesite="Lax",  # next login: the main server
                        secure=self._is_https(request), path="/")
        return resp

    async def me(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "setup_mode": self.setup_mode, "panel_path": self.panel_path,
                                  "user": request["user"], "role": request["role"],
                                  "skin": self.themes.get(request["user"]), "skin_options": self.themes.options(),
                                  **self._tunnel_info()})

    def _tunnel_info(self) -> dict:
        t = self.tunnel
        return {"public_url": self.public_url, "tunnel_enabled": self.ws.tunnel,
                "tunnel_state": t.state if t else ("error" if self.tunnel_problem else None),
                "tunnel_error": (t.error if t and t.state != "online" else None) or self.tunnel_problem}

    # ------------------------------------------------------------ Several Rust servers (servers.py)

    def _servers_owner(self, request: web.Request):
        """Adding, starting and removing servers: the owner, on the main server's panel."""
        if request["role"] != "owner":
            raise ApiError("Only the owner can manage servers.", 403)
        if servers.is_child() or servers.manager is None:
            raise ApiError("Servers are managed on the main server's panel.")
        return servers.manager

    def _known_server(self, sid: str) -> str:
        if not any(s["id"] == sid for s in servers.load_registry()):
            raise ApiError("No such server.", 404)
        return sid

    async def servers_list(self, request: web.Request) -> web.Response:
        manager = servers.manager
        out = []
        for s in await asyncio.to_thread(servers.all_servers):
            if s["main"]:
                s["state"] = "running"
            elif manager is not None:
                s.update(manager.info(s["id"]))
            out.append(s)
        shown = request.cookies.get(SERVER_COOKIE) if not servers.is_child() else None
        current = shown if shown in {s["id"] for s in out} else servers.server_id()
        manage = servers.manager is not None and not servers.is_child()
        return web.json_response({
            "current": current, "servers": out, "manage": manage, "owner": request.get("role") == "owner",
            "child": servers.is_child(),
            "removed": await asyncio.to_thread(servers.removed_folders) if manage else []})

    async def servers_create(self, request: web.Request) -> web.Response:
        manager = self._servers_owner(request)
        data = await self._json(request)
        try:
            info = await asyncio.to_thread(
                servers.create_server, data.get("name", ""), data.get("rcon_host", ""), data.get("rcon_port", ""),
                str(data.get("rcon_password") or ""), data.get("discord_token", ""), data.get("guild_id", ""))
        except ValueError as exc:
            raise ApiError(str(exc))
        manager.start(info["id"])
        self._audit(f"Server added: {info['name']} (RCON {info['rcon']}, panel port {info['port']}, Discord: "
                    + ("the main bot" if info["discord"] == "shared" else "its own bot") + ")")
        return web.json_response({"ok": True, "server": info})

    async def servers_action(self, request: web.Request) -> web.Response:
        manager = self._servers_owner(request)
        sid, action = self._known_server(request.match_info["sid"]), request.match_info["action"]
        await asyncio.to_thread(getattr(manager, action), sid)
        self._audit(f"Server {servers.server_name(sid)}: {action}")
        return web.json_response({"ok": True, **manager.info(sid)})

    async def servers_use_main_bot(self, request: web.Request) -> web.Response:
        """Switches an extra server from its own Discord bot to the main server's (one bot for every server)."""
        manager = self._servers_owner(request)
        sid = self._known_server(request.match_info["sid"])
        await asyncio.to_thread(servers.use_main_bot, sid)
        manager.restart(sid)
        self._audit(f"Server {servers.server_name(sid)} now uses the main Discord bot")
        return web.json_response({"ok": True})

    async def servers_remove(self, request: web.Request) -> web.Response:
        """Removes an extra server: ?delete=1 deletes its folder for good, else it's kept as a backup."""
        manager = self._servers_owner(request)
        sid = self._known_server(request.match_info["sid"])
        if sid == servers.MAIN_ID:
            raise ApiError("The main server can't be removed.")
        name = servers.server_name(sid)
        delete = request.query.get("delete") == "1"
        if delete and request.query.get("confirm", "").strip().lower() != name.strip().lower():
            raise ApiError(f"Type the server's name ({name}) to delete it for good.")
        try:
            await asyncio.to_thread(manager.remove, sid, keep=not delete)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        self._audit(f"Server deleted for good: {name}" if delete
                    else f"Server removed: {name} (its folder was kept as servers/_removed-{sid}-…)")
        return web.json_response({"ok": True, "deleted": delete})

    async def servers_delete_removed(self, request: web.Request) -> web.Response:
        """Deletes the backup folder of a server removed earlier."""
        self._servers_owner(request)
        name = request.match_info["name"]
        try:
            await asyncio.to_thread(servers.delete_removed_folder, name)
        except (ValueError, OSError) as exc:
            raise ApiError(str(exc)) from exc
        self._audit(f"Removed server's backup deleted: {name}")
        return web.json_response({"ok": True})

    async def servers_log(self, request: web.Request) -> web.Response:
        self._servers_owner(request)
        sid = self._known_server(request.match_info["sid"])
        return web.json_response({"log": await asyncio.to_thread(servers.log_tail, sid)})

    async def kits_copy(self, request: web.Request) -> web.Response:
        """Copies custom kits of this server to another one (items, description, claim cooldown)."""
        data = await self._json(request)
        names = data.get("kits")
        if not isinstance(names, list) or not names or len(names) > 500:
            raise ApiError("Pick the kits to copy.")
        target = self._text(data, "target", limit=40)
        kits, missing = servers.export_custom_kits(self.bot.db, [str(n)[:60] for n in names])   # this thread's DB
        try:
            result = await asyncio.to_thread(servers.import_custom_kits, kits, target,
                                             overwrite=bool(data.get("overwrite")), missing=missing)
        except ValueError as exc:
            raise ApiError(str(exc))
        if result["copied"]:
            self._audit(f"Kits copied to {servers.server_name(target)}: " + ", ".join(result["copied"]))
        return web.json_response({"ok": True, **result})

    # ------------------------------------------------------------ Panel themes (owner picks one per account)

    async def themes_list(self, request: web.Request) -> web.Response:
        names = [OWNER] + [u["username"] for u in self.users.list()]
        return web.json_response({"themes": [{"id": k, "label": v} for k, v in THEMES.items()],
                                  "options": self.themes.options(),
                                  "users": [{"username": n, "theme": self.themes.get(n)} for n in names]})

    async def themes_set(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        username, theme = str(data.get("username") or ""), str(data.get("theme") or "")
        if theme not in THEMES:
            raise ApiError("Unknown theme.")
        if username != OWNER:
            user = self.users.get(username)
            if user is None:
                raise ApiError("No such account.", 404)
            username = user["username"]
        self.themes.set(username, theme)
        self._audit(f"Panel theme of {username}: {THEMES[theme]}")
        return web.json_response({"ok": True, "username": username, "theme": theme})

    async def themes_options(self, request: web.Request) -> web.Response:
        """Early 2000s theme: how many popup ads, how often they come back (out-of-range values are clamped)."""
        data = await self._json(request)
        options = self.themes.set_options(data if isinstance(data, dict) else {})
        self._audit("Popup ads: {popups_start} at login, {popups_wave} more every {popups_every} min "
                    "(at most {popups_max} open)".format(**options))
        return web.json_response({"ok": True, "options": options})

    # ------------------------------------------------------------ Accounts (other admins)

    async def list_users(self, request: web.Request) -> web.Response:
        users = [{**u, "two_factor": self.two_factor.enabled(u["username"])} for u in self.users.list()]
        return web.json_response({"users": users, "owner_password_set": bool(self.ws.password),
                                  "owner_two_factor": self.two_factor.enabled(OWNER)})

    async def reset_user_2fa(self, request: web.Request) -> web.Response:
        """Owner: switch off someone's two-factor login (lost phone)."""
        username = request.match_info["username"]
        if not self.two_factor.enabled(username):
            raise ApiError("Two-factor login isn't on for this account.", 404)
        self.two_factor.disable(username)
        self._audit(f"Two-factor login of **{username}** switched off")
        return web.json_response({"ok": True})

    async def create_user(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        try:
            user = await asyncio.to_thread(
                self.users.create, self._text(data, "username", limit=32), str(data.get("password") or ""),
                str(data.get("role") or "admin"), request["user"])
        except UserError as exc:
            raise ApiError(str(exc)) from None
        self._audit(f"Account **{user['username']}** created (role {user['role']})")
        return web.json_response({"ok": True})

    async def update_user(self, request: web.Request) -> web.Response:
        username = request.match_info["username"]
        data = await self._json(request)
        password = data.get("password")
        try:
            user = await asyncio.to_thread(
                self.users.update, username, role=data.get("role"), disabled=data.get("disabled"),
                password=str(password) if password else None)
        except UserError as exc:
            raise ApiError(str(exc), 404 if "not found" in str(exc) else 400) from None
        changes = [k for k in ("role", "disabled", "password") if data.get(k) is not None]
        self._audit(f"Account **{user['username']}** changed ({', '.join(changes) or 'nothing'})")
        return web.json_response({"ok": True})

    async def delete_user(self, request: web.Request) -> web.Response:
        username = request.match_info["username"]
        if username.lower() == request["user"].lower():
            raise ApiError("You can't delete your own account.")
        if not self.users.delete(username):
            raise ApiError("User not found.", 404)
        self.two_factor.disable(username)
        self._audit(f"Account **{username}** deleted")
        return web.json_response({"ok": True})

    async def change_own_password(self, request: web.Request) -> web.Response:
        if request["user"] == OWNER:
            raise ApiError("The owner password is WEB_PASSWORD – change it under Settings → Web interface.")
        data = await self._json(request)
        key = "pw:" + request["user"].lower()
        self._rate_limit(key)
        if await asyncio.to_thread(self.users.authenticate, request["user"],
                                   str(data.get("current") or "")) is None:
            self._login_failed(key)
            raise ApiError("The current password is wrong.", 403)
        self._login_ok(key)
        try:
            await asyncio.to_thread(self.users.update, request["user"], password=str(data.get("new") or ""))
        except UserError as exc:
            raise ApiError(str(exc)) from None
        resp = web.json_response({"ok": True})
        self._set_session_cookie(resp, request["user"], request)  # old sessions end, this one continues
        self._audit("changed their own password")
        return resp

    # ------------------------------------------------------------ Settings

    async def get_settings(self, request: web.Request) -> web.Response:
        env = read_env(self.env_path)
        locked = locked_settings()
        fields = []
        for f in settings_schema.FIELDS:
            if f.key == servers.OWN_ABUSE_KEY and not servers.is_child():
                continue                        # only extra servers choose between the main's settings and their own
            item = {"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "help": f.help,
                    "required": f.required, "pick": f.pick, "advanced": f.advanced,
                    "placeholder": f.placeholder, "choices": list(f.choices)}
            if f.key in locked:
                item["locked"] = {"WEB_PASSWORD": "Same as on the main server – change it there.",
                                  "DISCORD_TOKEN": "This server uses the main server's Discord bot.",
                                  "GUILD_ID": "This server uses the main server's Discord bot."}.get(
                    f.key, "Set on the main server's panel – the same for every server.")
            if f.kind == "secret":
                item["is_set"] = bool((env.get(f.key) or "").strip())  # never send secrets out
            else:
                item["value"] = env.get(f.key, "")
            fields.append(item)
        try:
            load_config(self.env_path)
            config_error = None
        except ConfigError as exc:
            config_error = str(exc)
        return web.json_response({
            "groups": [{"id": g, "label": label} for g, label in settings_schema.GROUPS],
            "fields": fields, "setup_mode": self.setup_mode, "setup_error": self.setup_error,
            "setup_fix_url": self.setup_fix_url,
            "config_error": config_error, "restart_required": self.restart_required, "restart_for": self.restart_for,
        })

    async def put_settings(self, request: web.Request) -> web.Response:
        values = (await self._json(request)).get("values")
        if not isinstance(values, dict):
            raise ApiError("Invalid data.")
        current = read_env(self.env_path)
        updates, errors = {}, {}
        for key, raw in values.items():
            f = settings_schema.FIELDS_BY_KEY.get(key)
            if f is None or key in locked_settings():
                continue                     # an extra server takes these from the main server (servers.py)
            value = "" if raw is None else str(raw)
            if f.kind != "textarea":
                value = value.strip()
            if f.kind == "secret" and not value:
                continue  # empty = leave unchanged
            if f.kind == "ids":
                value = ",".join(p.strip() for p in value.split(",") if p.strip())
            if value == (current.get(key) or ""):
                continue  # unchanged – nothing to write
            if error := settings_schema.validate(key, value):
                errors[key] = error
            else:
                updates[key] = value
        if errors:
            return web.json_response({"error": "Please fix the highlighted fields.", "errors": errors},
                                     status=400)
        for key in ("WEB_PASSWORD", "WEB_PANEL_KNOCK"):     # only their hashes go into the .env
            if key in updates and (key == "WEB_PASSWORD" or len(updates[key]) >= KNOCK_MIN):
                updates[key] = await asyncio.to_thread(
                    hash_password, updates[key], **({"rounds": KNOCK_ROUNDS} if key == "WEB_PANEL_KNOCK" else {}))
        if updates:
            update_env(self.env_path, updates, template=ENV_TEMPLATE)
            self._audit("Settings changed: " + ", ".join(sorted(updates)))
        rcon_test = None
        if updates.keys() & {"RCON_HOST", "RCON_PORT", "RCON_PASSWORD"}:
            # RCON details changed: test right away so errors show up immediately
            saved = read_env(self.env_path)
            result = await diagnose_rcon(saved.get("RCON_HOST") or "", saved.get("RCON_PORT") or "",
                                         saved.get("RCON_PASSWORD") or "")
            rcon_test = {"ok": result.ok, "steps": result.steps}
        if "WEB_PANEL_PATH" in updates and PANEL_PATH.match(updates["WEB_PANEL_PATH"].strip("/")):
            self.ws.panel_path = updates["WEB_PANEL_PATH"].strip("/")     # the new address works right away
        if "WEB_HONEYPOT" in updates:
            self.ws.honeypot = updates["WEB_HONEYPOT"].lower() in ("1", "true", "yes", "on")
        if "WEB_PANEL_KNOCK" in updates:
            self.ws.panel_knock = updates["WEB_PANEL_KNOCK"]
        if "WEB_PASSWORD" in updates:
            # New password applies immediately; the current session stays logged in
            self.ws.password = self.password_hash = updates["WEB_PASSWORD"]
            self.password = None
        try:
            load_config(self.env_path)
        except ConfigError as exc:
            resp = web.json_response({"ok": True, "saved": sorted(updates), "config_error": str(exc),
                                      "rcon_test": rcon_test})
        else:
            preflight = await self._preflight()
            if self.setup_mode:
                if preflight and preflight.get("preflight_error"):
                    resp = web.json_response({"ok": True, "saved": sorted(updates), "rcon_test": rcon_test,
                                              **preflight})
                else:
                    resp = web.json_response({"ok": True, "saved": sorted(updates), "starting": True,
                                              "rcon_test": rcon_test, **(preflight or {})})
                    if self.on_config_saved:
                        self.on_config_saved()
            else:
                # Apply right away – only a few settings (token, Discord server, web address …) need a restart
                needs = await self._apply_live(load_config(self.env_path)) if updates else []
                self.restart_for = sorted(set(self.restart_for) | set(needs))
                self.restart_required = bool(self.restart_for)
                resp = web.json_response({"ok": True, "saved": sorted(updates), "rcon_test": rcon_test,
                                          "applied": bool(updates), "restart_for": self.restart_for,
                                          "restart_required": self.restart_required, **(preflight or {})})
        if "WEB_PASSWORD" in updates:
            self._set_session_cookie(resp, request["user"], request)
        return resp

    async def cloudflare_domain(self, request: web.Request) -> web.Response:
        """Owner: use your own Cloudflare domain – creates tunnel + route + DNS record via the Cloudflare API and
        saves the tunnel token / address. The API token itself is not stored."""
        from support.cloudflare import CloudflareAPI, CloudflareError
        data = await self._json(request)
        try:
            api = CloudflareAPI(str(data.get("api_token") or ""))
            setup = await api.setup(str(data.get("hostname") or ""), self.ws.port)
        except CloudflareError as exc:
            raise ApiError(str(exc)) from None
        update_env(self.env_path, {"WEB_TUNNEL": "true", "WEB_TUNNEL_TOKEN": setup.token, "WEB_PUBLIC_URL": setup.url},
                   template=ENV_TEMPLATE)
        self._audit(f"own domain set up: {setup.url} (tunnel {setup.tunnel}, DNS record {setup.dns})")
        applied = False
        if self.bot is not None and not self.setup_mode:
            try:
                await self._apply_live(load_config(self.env_path))
                applied = True
            except ConfigError:
                pass
        if not applied:                                # setup mode / config not complete: just switch the tunnel
            self.ws.tunnel, self.ws.tunnel_token, self.ws.public_url = True, setup.token, setup.url
            self._start_tunnel()
        return web.json_response({"ok": True, "url": setup.url, "hostname": setup.hostname, "dns": setup.dns,
                                  "tunnel": setup.tunnel, "tunnel_name": setup.tunnel_name})

    async def cloudflare_domain_check(self, request: web.Request) -> web.Response:
        """Opens the fixed public address like a visitor would and says whether it reaches this bot."""
        from support.cloudflare import check_reachable
        url = self.ws.public_url
        if not url:
            raise ApiError("No own domain set up yet.")
        ok, message = await check_reachable(url)
        return web.json_response({"ok": ok, "url": url, "message": message,
                                  "tunnel_state": getattr(self.tunnel, "state", None)})

    async def _apply_live(self, cfg) -> list[str]:
        """New settings without a restart: bot + cogs, RCON connection, tunnel. Returns what needs a restart."""
        needs = await self.bot.apply_config(cfg)
        ws = self.ws
        tunnel_before = (ws.tunnel, ws.tunnel_token, ws.public_url)
        ws.tunnel, ws.tunnel_token, ws.public_url = cfg.web_tunnel, cfg.web_tunnel_token, cfg.web_public_url
        ws.open_browser = cfg.web_open_browser
        if tunnel_before != (ws.tunnel, ws.tunnel_token, ws.public_url):
            self._start_tunnel()
        return needs

    def _saved(self, key: str) -> str:
        return (read_env(self.env_path).get(key) or "").strip()

    async def settings_test_rcon(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        host = str(data.get("host") or self._saved("RCON_HOST"))
        port = str(data.get("port") or self._saved("RCON_PORT"))
        password = str(data.get("password") or self._saved("RCON_PASSWORD"))
        result = await diagnose_rcon(host, port, password)
        return web.json_response({"ok": result.ok, "steps": result.steps})

    async def settings_test_discord(self, request: web.Request) -> web.Response:
        token = str((await self._json(request)).get("token") or self._saved("DISCORD_TOKEN")).strip()
        if not token:
            raise ApiError("No bot token entered.")
        info = await check_discord(token)
        if not info["token_ok"]:
            return web.json_response({"ok": False, "error": info["error"]})
        app_id = info["app_id"]
        return web.json_response({
            "ok": True, "bot_name": info["bot_name"], "app_id": app_id, "intents": info["intents"],
            "invite_url": invite_url(app_id),
        })

    async def _preflight(self) -> dict | None:
        """Before (re)starting, check that Discord accepts the bot – otherwise it ends up in a start loop.

        Returns None if Discord is currently unreachable, otherwise the result of
        discord_preflight(); only "preflight_error" blocks the start.
        """
        try:
            cfg = load_config(self.env_path)
        except ConfigError as exc:
            return {"preflight_error": f"Not complete yet: {exc}", "fix_url": None}
        return await discord_preflight(cfg)

    async def settings_discord_options(self, request: web.Request) -> web.Response:
        """Channels/roles/servers to pick from – only when the bot is connected to Discord."""
        bot = self.bot
        if bot is None or not bot.is_ready():
            return web.json_response({"available": False})
        guilds = [{"id": str(g.id), "name": g.name} for g in bot.guilds]
        guild = bot.get_guild(bot.config.guild_id) if bot.config.guild_id else None
        guild = guild or (bot.guilds[0] if len(bot.guilds) == 1 else None)
        result = {"available": True, "guilds": guilds, "guild": guild.name if guild else None,
                  "text_channels": [], "categories": [], "roles": []}
        if guild is not None:
            result["text_channels"] = [
                {"id": str(c.id), "name": f"#{c.name}" + (f" ({c.category.name})" if c.category else "")}
                for c in sorted(guild.text_channels, key=lambda c: (c.category.position if c.category else -1,
                                                                    c.position))]
            result["categories"] = [{"id": str(c.id), "name": c.name} for c in guild.categories]
            result["roles"] = [{"id": str(r.id), "name": r.name} for r in reversed(guild.roles)
                               if not r.is_default() and not r.managed]
        return web.json_response(result)

    async def restart(self, request: web.Request) -> web.Response:
        preflight = await self._preflight()
        if preflight and preflight.get("preflight_error"):
            # Don't restart – otherwise the bot would fall back into setup mode
            return web.json_response({"ok": False, **preflight})
        self._audit("Bot restart requested")
        self.bot.request_restart()
        return web.json_response({"ok": True, **(preflight or {})})

    async def update_info(self, request: web.Request) -> web.Response:
        import saas_mode
        if saas_mode.enabled():                                   # hosted: the host installs updates for everyone
            return web.json_response({"available": False, "managed": True,
                                      "message": "Updates are installed by your host for all servers."})
        updates = self.bot.get_cog("Updates")
        return web.json_response(updates.info() if updates else {"available": False})

    async def update_check(self, request: web.Request) -> web.Response:
        updates = self._cog("Updates")
        self._audit("Update check requested")
        result = await updates.check_now()
        return web.json_response({"status": result.status, "message": result.message, "sha": result.sha,
                                  "title": result.title, "restarting": result.restart_needed, **updates.info()})

    async def setup_continue(self, request: web.Request) -> web.Response:
        """Setup: start the bot with the saved configuration (e.g. after enabling intents)."""
        preflight = await self._preflight()
        if preflight and preflight.get("preflight_error"):
            return web.json_response({"ok": False, **preflight})
        if self.on_config_saved:
            self.on_config_saved()
        return web.json_response({"ok": True, "starting": True, **(preflight or {})})

    # ------------------------------------------------------------ Status & console

    async def status(self, request: web.Request) -> web.Response:
        if self.setup_mode:
            return web.json_response({"setup_mode": True, "setup_error": self.setup_error,
                                      "rcon_connected": False, "discord_ready": False, "players": []})
        bot, cfg = self.bot, self.bot.config
        info = bot.server_info or {}
        players = [
            {"name": name, "ping": p.get("Ping"), "connected": p.get("ConnectedSeconds"), "health": p.get("Health")}
            for name, p in sorted(bot.online_players.items(), key=lambda kv: kv[0].lower())
        ]
        return web.json_response({
            "setup_mode": False,
            "restart_required": self.restart_required,
            "rcon_connected": bot.rcon.connected,
            "rcon_last_error": bot.rcon.last_error,
            "intent_warning": bot.intent_warning,
            "intent_fix_url": bot.intent_fix_url,
            "rcon_diagnosis": bot.rcon_diagnosis,
            "rcon_target": f"{cfg.rcon_host}:{cfg.rcon_port}",
            "discord_ready": bot.is_ready(),
            "discord_user": str(bot.user) if bot.user else None,
            "permission_problems": check_permissions(bot) if bot.is_ready() else [],
            **self._tunnel_info(),
            "invite_url": invite_url(bot.application_id),
            "server": {
                "name": cfg.server_display_name or strip_rich_text(str(info.get("Hostname", ""))),
                "players": info.get("Players"), "max_players": info.get("MaxPlayers"),
                "queued": info.get("Queued"), "map": info.get("Map"), "uptime": info.get("Uptime"),
                "fps": info.get("Framerate"), "game_time": info.get("GameTime"), "entities": info.get("EntityCount"),
            } if info else None,
            "players": players,
            "allow_raw_rcon": cfg.allow_raw_rcon,
            "tickets_enabled": bool(cfg.ticket_category_id),
            "open_tickets": len(bot.db.tickets(status="open")),
        })

    async def console(self, request: web.Request) -> web.Response:
        try:
            after = int(request.query.get("after", 0))
        except ValueError:
            after = 0
        lines = [line for line in self.bot.console_buffer
                 if line["id"] > after and not any(f"command '{c}" in line["text"] for c in POLLING_COMMANDS)]
        return web.json_response({"lines": lines[-500:]})

    async def command(self, request: web.Request) -> web.Response:
        if not self.bot.config.allow_raw_rcon:
            raise ApiError("Arbitrary RCON commands are disabled (ALLOW_RAW_RCON=false).", 403)
        cmd = self._text(await self._json(request), "command", limit=500)
        self._audit(f"RCON `{cmd[:200]}`")
        response = await self.bot.rcon.command(cmd, timeout=10, expect_response=False)
        return web.json_response({"response": response})

    async def say(self, request: web.Request) -> web.Response:
        text = " ".join(self._text(await self._json(request), "message", limit=400).splitlines())
        await self.bot.rcon.command(f"say {text}", expect_response=False)
        self._audit(f"Say: {sanitize_ingame_text(text, 200)}")
        return web.json_response({"ok": True})

    async def player_action(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        data = await self._json(request)
        name = sanitize_player_name(self._text(data, "name", limit=64))
        reason = self._text(data, "reason", required=False)
        cfg = self.bot.config
        template = {"kick": cfg.kick_template, "ban": cfg.ban_template, "unban": cfg.unban_template}[action]
        response = await self.bot.rcon.command(template.format(name=name), expect_response=False)
        self._audit(f"{action.capitalize()} `{name}`" + (f" – reason: {reason}" if reason else ""))
        return web.json_response({"response": response})

    # ------------------------------------------------------------ Kits

    async def kits(self, request: web.Request) -> web.Response:
        kits_cog = self.bot.get_cog("Kits")
        ingame = await kits_cog.kit_names.get(max_wait=5) if kits_cog else []
        custom = []
        for row in self.bot.db.custom_kits():
            item_rows = self.bot.db.custom_kit_items(row["name"])
            custom.append({**dict(row), "items": [dict(i) for i in item_rows]})
        rules = [dict(r) for r in self.bot.db.kit_rules()]
        linked = [{"user_id": str(r["user_id"]), "player": r["player"], "linked_at": r["linked_at"],
                   "user": str(self.bot.get_user(r["user_id"]) or r["user_id"])}
                  for r in self.bot.db.linked_players()]
        return web.json_response({"ingame": ingame, "custom": custom, "rules": rules, "linked": linked,
                                  "history": [dict(r) for r in self.bot.db.kit_claim_history(limit=30)]})

    # ------------------------------------------------------------ Kit pictures (like the in-game inventory screen)

    async def kit_png(self, items: list[tuple], title: str) -> bytes:
        """The picture of a kit (support/kit_image.py) – item pictures from the same cache as the kit builder."""
        from support import kit_image

        template = (self.bot.config.item_image_url if self.bot is not None else "") or ""
        self.item_images.url_template = template or "off"     # "off": bundled/own pictures only, no downloads
        images = self.item_images
        footer = (self.bot.config.server_display_name or "") if self.bot is not None else ""
        logo, logo_key = self._server_logo()
        cache = (self.bot.config.database_path.parent if self.bot else Path("data")) / "kit_images"
        return await kit_image.kit_picture(items, images, cache, title=title, footer=footer,
                                           logo=logo, logo_key=logo_key)

    def _server_logo(self) -> tuple[bytes | None, str]:
        """The server logo for the kit picture's background: the website's logo (uploaded or bundled), else none.
        Returns (bytes, a key that changes when the logo changes)."""
        manager = getattr(self.bot, "plugins", None) if self.bot is not None else None
        site = manager.running("website") if manager is not None else None
        if site is None:
            return None, ""
        try:
            data, _kind = site.picture("logo")
            return data, site.picture_version()
        except Exception:
            return None, ""

    @staticmethod
    def _png(data: bytes, filename: str, *, download: bool = True, cache: str = "no-store") -> web.Response:
        name = re.sub(r"[^A-Za-z0-9._ -]+", "", filename).strip() or "kit"
        headers = {"Cache-Control": cache}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{name}.png"'
        return web.Response(body=data, content_type="image/png", headers=headers)

    async def kit_builder_image(self, request: web.Request) -> web.Response:
        """Kit builder: the picture of the kit being built (slots with their container) – as a download."""
        from support.kit_image import SIZES
        data = await self._json(request)
        name = self._text(data, "name", required=False, limit=40) or "Kit"
        items = []
        for slot in (data.get("slots") or [])[:80]:
            if not isinstance(slot, dict):
                continue
            short = itemlist.normalize_shortname(str(slot.get("item", "")))
            container = str(slot.get("container", "")).title()
            try:
                amount = max(1, min(int(slot.get("amount", 1)), 1_000_000))
            except (TypeError, ValueError):
                amount = 1
            if short and container in SIZES:
                items.append((short, amount, container))
        if not items:
            raise ApiError("The kit is empty – put some items in it first.")
        return self._png(await self.kit_png(items, name), name)

    async def custom_kit_image(self, request: web.Request) -> web.Response:
        kit = self.bot.db.custom_kit(request.match_info["name"])
        if kit is None:
            raise ApiError("Custom kit not found.", 404)
        items = [(i["shortname"], i["amount"]) for i in self.bot.db.custom_kit_items(kit["name"])]
        if not items:
            raise ApiError("This kit is empty.")
        return self._png(await self.kit_png(items, kit["name"]), kit["name"])

    async def ingame_kit_image(self, request: web.Request) -> web.Response:
        name = kitparse.clean_arg(request.match_info["name"])
        raw = await self.bot.rcon.command(f'kit info "{name}"')
        items = [(i.short_name, i.amount, i.container) for i in kitparse.parse_kit_info(raw)]
        if not items:
            raise ApiError("This kit has no items (or the server didn't say).")
        return self._png(await self.kit_png(items, name), name)

    async def store_kit_image(self, request: web.Request) -> web.Response:
        """Public: the picture on a kit's card in the web store (only kits that are for sale)."""
        self._public_limit(request, images=True)
        shop = self.bot.get_cog("WebShop") if self.bot is not None else None
        row = self.bot.db.store_kit(int(request.match_info["kit_id"])) if shop is not None else None
        if row is None or not row["enabled"] or (row["source"] == "custom" and self.bot.db.custom_kit(row["kit"]) is None):
            raise web.HTTPNotFound()
        items = shop.kit_items_full(row)
        if not items:
            raise web.HTTPNotFound()
        return self._png(await self.kit_png(items, row["name"]), row["name"], download="dl" in request.query,
                         cache="public, max-age=600")

    async def ingame_kit_info(self, request: web.Request) -> web.Response:
        name = kitparse.clean_arg(request.match_info["name"])
        raw = await self.bot.rcon.command(f'kit info "{name}"')
        items = [{"short_name": i.short_name, "amount": i.amount, "condition": i.condition,
                  "container": i.container, "item_id": i.item_id} for i in kitparse.parse_kit_info(raw)]
        return web.json_response({"name": name, "items": items, "raw": raw if not items else ""})

    async def give_kit(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        kit = kitparse.clean_arg(self._text(data, "kit"))
        kit_type = data.get("type", "ingame")
        player = self._text(data, "player", required=False, limit=64)
        everyone = bool(data.get("all"))
        if not everyone and not player:
            raise ApiError("Player is missing.")
        player = sanitize_player_name(player)
        cfg = self.bot.config

        if kit_type == "custom":
            custom = self._cog("CustomKits")
            if self.bot.db.custom_kit(kit) is None:
                raise ApiError("Custom kit not found.", 404)
            targets = sorted(self.bot.online_players) if everyone else [player]
            ok = 0
            for target in targets:
                sent, total = await custom.deliver(kit, target)
                if sent:
                    ok += 1
                    self.bot.db.record_kit_claim(None, kit, target, "web (custom)")
            self._audit(f"Custom kit **{kit}** to {'everyone' if everyone else player} ({ok} players)")
            return web.json_response({"ok": True, "players": ok})

        if everyone:
            await self.bot.rcon.command(cfg.kit_give_all_template.format(kit=kit), expect_response=False)
        else:
            await self.bot.rcon.command(cfg.kit_give_template.format(kit=kit, name=player), expect_response=False)
            self.bot.db.record_kit_claim(None, kit, player, "web")
        self._audit(f"Kit **{kit}** to {'everyone' if everyone else player}")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ Kit builder

    def _builder_payload(self, data: dict) -> tuple[str, list[dict]]:
        """Kit name + validated items {item, amount, condition, container} from the kit builder."""
        kit = kitparse.clean_arg(self._text(data, "kit", limit=40), limit=40)
        raw_items = data.get("items")
        if not kit:
            raise ApiError("Please enter a kit name.")
        if not isinstance(raw_items, list) or not raw_items:
            raise ApiError("The kit is empty – add some items first.")
        if len(raw_items) > BUILDER_MAX_ITEMS:
            raise ApiError(f"At most {BUILDER_MAX_ITEMS} items.")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ApiError("Invalid item.")
            short = " ".join(str(raw.get("item") or "").split()).lower()
            if len(short) > 64 or not kitparse.valid_shortname(short):
                raise ApiError(f"Invalid item shortname: {short[:40] or '(empty)'}")
            container = str(raw.get("container") or "Main").capitalize()
            if container not in kitparse.CONTAINERS:
                raise ApiError("Container must be Main, Belt or Wear.")
            items.append({"item": short, "amount": self._int(raw, "amount", 1, 1, 100000),
                          "condition": self._int(raw, "condition", 100, 1, 100), "container": container})
        return kit, items

    async def builder_ingame(self, request: web.Request) -> web.Response:
        """Creates/extends an in-game kit on the server (one "kit add" per item, in order)."""
        kit, items = self._builder_payload(await self._json(request))
        template = self.bot.config.kit_add_template
        for i in items:
            await self.bot.rcon.command(template.format(kit=kit, item=i["item"], amount=i["amount"],
                                                        condition=i["condition"], container=i["container"]),
                                        expect_response=False)
        kits_cog = self.bot.get_cog("Kits")
        if kits_cog:
            kits_cog.kit_names.invalidate()
        self._audit(f"Kit builder: in-game kit **{kit}** – {len(items)} items added")
        return web.json_response({"ok": True, "kit": kit, "added": len(items)})

    async def builder_custom(self, request: web.Request) -> web.Response:
        """Saves the kit as a custom kit (bot only). Condition/container are not supported there."""
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        data = await self._json(request)
        kit, items = self._builder_payload(data)
        if len(items) > MAX_ITEMS_PER_KIT:
            raise ApiError(f"Custom kits hold at most {MAX_ITEMS_PER_KIT} items.")
        db = self.bot.db
        created = db.create_custom_kit(kit, None, "Web interface (kit builder)")
        name = db.custom_kit(kit)["name"]
        if not created and data.get("replace", True) is not False:
            db.clear_custom_kit_items(name)
        if len(db.custom_kit_items(name)) + len(items) > MAX_ITEMS_PER_KIT:
            raise ApiError(f"Custom kits hold at most {MAX_ITEMS_PER_KIT} items – turn on \"replace\".")
        for i in items:
            db.add_custom_kit_item(name, i["item"], i["amount"])
        self._audit(f"Kit builder: custom kit **{name}** saved ({len(items)} items)")
        return web.json_response({"ok": True, "kit": name, "created": created, "added": len(items)})

    async def create_custom_kit(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        name = kitparse.clean_arg(self._text(data, "name", limit=40), limit=40)
        description = self._text(data, "description", required=False, limit=300) or None
        if not name:
            raise ApiError("Invalid name.")
        if not self.bot.db.create_custom_kit(name, description, "Web interface"):
            raise ApiError("A custom kit with this name already exists.", 409)
        self._audit(f"Custom kit **{name}** created")
        return web.json_response({"ok": True})

    async def import_custom_kits(self, request: web.Request) -> web.Response:
        """Kit builder output ('kit add "Kit" "item" amount ...') -> custom kits."""
        from cogs.custom_kits import MAX_IMPORT_BYTES, import_kit_commands
        data = await self._json(request)
        text = self._text(data, "text", limit=MAX_IMPORT_BYTES)
        kit = self._text(data, "kit", required=False, limit=40) or None
        result = import_kit_commands(self.bot.db, text, kit_name=kit, replace=data.get("replace", True) is not False,
                                     created_by="Web interface")
        if not result["kits"]:
            raise ApiError('No "kit add" commands found.')
        for k in result["kits"]:
            self._audit(f"Custom kit **{k['name']}** imported from kit builder ({k['added']} items)")
        return web.json_response({"ok": True, **result})

    async def delete_custom_kit(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if self.bot.db.custom_kit(name) is None:
            raise ApiError("Custom kit not found.", 404)
        rules = self.bot.db.delete_custom_kit(name)
        self._audit(f"Custom kit **{name}** deleted ({rules} autokit rules)")
        return web.json_response({"ok": True, "rules_deleted": rules})

    # Kit roles: every kit next to its Discord role and who has it; auto-give (cogs/custom_kits.py)

    def _custom_kits_cog(self):
        cog = self.bot.get_cog("CustomKits") if self.bot is not None else None
        if cog is None:
            raise ApiError("Custom kits are not loaded (is the bot running?).", 503)
        return cog

    def _kit_ref(self, data: dict) -> tuple[str, str]:
        kind, kit = str(data.get("kind") or ""), kitparse.clean_arg(str(data.get("kit") or ""))
        known = (self.bot.db.custom_kit(kit) if kind == "custom" else
                 self.bot.db.ingame_kit_role(kit) if kind == "ingame" else None)
        if known is None and kind == "ingame":                # an in-game kit that has no role yet
            kits_cog = self.bot.get_cog("Kits")
            live = {k.lower(): k for k in (getattr(kits_cog.kit_names, "items", None) or [] if kits_cog else [])}
            if kit.lower() in live:
                return kind, live[kit.lower()]
        if known is None:
            raise ApiError("Kit not found.", 404)
        return kind, known["name"] if kind == "custom" else known["kit"]

    async def kit_db(self, request: web.Request) -> web.Response:
        """Kit database: every kit (the bot's custom kits and the server's in-game kits) with its contents, role,
        cooldown, auto-give, store listing, autokits and claim numbers – one list for searching and managing."""
        db = self.bot.db
        kits_cog = self.bot.get_cog("Kits")
        ingame = await kits_cog.kit_names.get(max_wait=5) if kits_cog else []
        custom_cog = self.bot.get_cog("CustomKits")
        guild = custom_cog._guild() if custom_cog is not None else None
        stats = db.kit_claim_stats()
        auto = db.auto_give_kits()
        store = {(r["source"], r["kit"].lower()): r for r in db.store_kits()}
        rules: dict[str, list] = {}
        for r in db.kit_rules():
            rules.setdefault(r["kit"].lower(), []).append({"id": r["id"], "trigger": r["trigger"],
                                                           "enabled": bool(r["enabled"])})
        ingame_roles = {r["kit"].lower(): r for r in db.ingame_kit_roles()}

        def roles_of(ids):
            out = []
            for rid in ids:
                role = guild.get_role(rid) if guild is not None else None
                out.append({"id": str(rid), "name": role.name if role else None,
                            "members": len(getattr(role, "members", []) or []) if role else 0})
            return out

        def entry(kind, name, items, role_ids, cooldown, description=None):
            s = store.get((kind, name.lower()))
            return {"kind": kind, "name": name, "description": description,
                    "items": items, "item_count": len(items) if items is not None else None,
                    "roles": roles_of(role_ids), "cooldown_minutes": cooldown,
                    "auto_give": (kind, name.lower()) in auto,
                    "store": None if s is None else {"id": s["id"], "name": s["name"], "price": s["price"],
                                                     "enabled": bool(s["enabled"]) and s["price"] > 0,
                                                     "days": s["days"]},
                    "autokits": rules.get(name.lower(), []),
                    "claims": stats.get(name.lower(), {"total": 0, "players": 0, "last": None})}

        out = []
        for row in db.custom_kits():
            items = [{"short": i["shortname"], "amount": i["amount"], "id": i["id"],
                      "label": itemlist.COMMON_ITEMS.get(i["shortname"], i["shortname"])}
                     for i in db.custom_kit_items(row["name"])]
            out.append(entry("custom", row["name"], items, db.claim_roles(row), int(row["cooldown_minutes"]),
                             row["description"]))
        names = list(dict.fromkeys([*ingame, *(r["kit"] for r in db.ingame_kit_roles())]))
        for name in names:
            s = store.get(("ingame", name.lower()))
            try:
                cached = json.loads(s["items"]) if s is not None and s["items"] else None
            except ValueError:
                cached = None
            items = [{"short": i["short"], "amount": i["amount"],
                      "label": itemlist.COMMON_ITEMS.get(i["short"], i["short"])} for i in cached] if cached else None
            role = ingame_roles.get(name.lower())
            out.append(entry("ingame", name, items, [role["role_id"]] if role else [],
                             int(role["cooldown_minutes"]) if role else None))
        roles = [{"id": str(r.id), "name": r.name} for r in custom_cog._assignable(guild)] \
            if custom_cog is not None and guild is not None else []
        return web.json_response({"kits": out, "roles": roles, "server_online": self.bot.rcon.connected,
                                  "store": self.bot.get_cog("WebShop") is not None})

    async def kit_roles(self, request: web.Request) -> web.Response:
        return web.json_response(self._custom_kits_cog().kit_role_table())

    async def kit_roles_update(self, request: web.Request) -> web.Response:
        from cogs.custom_kits import MAX_COOLDOWN_MINUTES
        data = await self._json(request)
        kind, kit = self._kit_ref(data)
        if "cooldown" in data:
            minutes = parse_duration(str(data.get("cooldown", "")))
            if minutes is None or minutes > MAX_COOLDOWN_MINUTES:
                raise ApiError("Invalid cooldown. Examples: 30m, 12h, 1d, 1d 12h, 0 (no cooldown). Max. 365 days.")
            if kind == "custom":
                self.bot.db.set_custom_kit_cooldown(kit, minutes)
            elif not self.bot.db.set_ingame_kit_cooldown(kit, minutes):
                raise ApiError("Give the kit a role first – the cooldown is for claiming with the role.")
            self._audit(f"Kit **{kit}** – cooldown {format_minutes(minutes)}")
        if "role_id" in data:                            # the kit's role (kit database)
            rid = str(data.get("role_id") or "")
            cog = self._custom_kits_cog()
            guild = cog._guild()
            role = guild.get_role(int(rid)) if guild is not None and rid.isdigit() else None
            if rid and (role is None or role not in cog._assignable(guild)):
                raise ApiError("That role can't be used (gone, a bot role, or above the bot's own role).")
            if kind == "custom":
                self.bot.db.set_custom_kit_roles(kit, [role.id] if role else [])
            elif role is not None:
                self.bot.db.set_ingame_kit_role(kit, role.id)
            else:
                self.bot.db.delete_ingame_kit_role(kit)
            self._audit(f"Kit **{kit}** – role: {role.name if role else 'none'}")
        if "auto_give" in data:
            self.bot.db.set_auto_give(kind, kit, bool(data["auto_give"]))
            self._audit(f"Kit **{kit}** – auto-give to its role {'on' if data['auto_give'] else 'off'}")
        return web.json_response(self._custom_kits_cog().kit_role_table())

    async def kit_roles_sync(self, request: web.Request) -> web.Response:
        try:
            done = await self._custom_kits_cog().sync_kit_roles()
        except discord.HTTPException as exc:
            raise ApiError(f"Discord refused it ({exc}) – the bot needs \"Manage Roles\".") from None
        return web.json_response({"done": done, **self._custom_kits_cog().kit_role_table()})

    async def kit_roles_give(self, request: web.Request) -> web.Response:
        kind, kit = self._kit_ref(await self._json(request))
        result = await self._custom_kits_cog().auto_give(kits=[(kind, kit)])
        self._audit(f"Kit **{kit}** given to {len(result['given'])} online member(s) with its role")
        return web.json_response(result)

    async def kit_roles_match(self, request: web.Request) -> web.Response:
        """Proposes the existing role for every kit (loose names, then the AI) – nothing is changed yet."""
        try:
            return web.json_response(await self._custom_kits_cog().propose_matches())
        except ValueError as exc:
            raise ApiError(str(exc)) from None

    async def kit_roles_apply(self, request: web.Request) -> web.Response:
        choices = (await self._json(request)).get("choices")
        if not isinstance(choices, list) or not all(isinstance(c, dict) for c in choices):
            raise ApiError("Invalid choices.")
        try:
            result = await self._custom_kits_cog().apply_matches(choices[:500])
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        except discord.HTTPException as exc:
            raise ApiError(f"Discord refused it ({exc}) – the bot needs \"Manage Roles\".") from None
        self._audit(f"Kit roles matched: {result['linked']} connected, {result['moved']} member(s) moved, "
                    f"{len(result['deleted'])} duplicate role(s) deleted")
        return web.json_response(result)

    async def kit_roles_undo(self, request: web.Request) -> web.Response:
        try:
            result = await self._custom_kits_cog().undo_created()
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        self._audit(f"Kit roles: {len(result['deleted'])} role(s) the bot created were deleted")
        return web.json_response(result)

    async def custom_kit_claim_settings(self, request: web.Request) -> web.Response:
        """Self-service claim: which roles may claim the kit and how often."""
        from cogs.custom_kits import MAX_COOLDOWN_MINUTES
        kit = self.bot.db.custom_kit(request.match_info["name"])
        if kit is None:
            raise ApiError("Custom kit not found.", 404)
        data = await self._json(request)
        minutes = parse_duration(str(data.get("cooldown", "")))
        if minutes is None or minutes > MAX_COOLDOWN_MINUTES:
            raise ApiError("Invalid cooldown. Examples: 30m, 12h, 1d, 1d 12h, 0 (no cooldown). Max. 365 days.")
        raw_roles = data.get("role_ids") or []
        if not isinstance(raw_roles, list) or not all(str(r).isdigit() for r in raw_roles):
            raise ApiError("Invalid roles.")
        role_ids = [int(r) for r in raw_roles][:25]
        self.bot.db.set_custom_kit_cooldown(kit["name"], minutes)
        self.bot.db.set_custom_kit_roles(kit["name"], role_ids)
        self._audit(f"Custom kit **{kit['name']}** – claim roles: {len(role_ids)}, "
                    f"cooldown {format_minutes(minutes)}")
        return web.json_response({"ok": True, "cooldown_minutes": minutes})

    async def custom_kit_disabled(self, request: web.Request) -> web.Response:
        """Switch a custom kit off (kept, but nobody can claim it) or back on."""
        kit = self.bot.db.custom_kit(request.match_info["name"])
        if kit is None:
            raise ApiError("Custom kit not found.", 404)
        disabled = bool((await self._json(request)).get("disabled"))
        self.bot.db.set_custom_kit_disabled(kit["name"], disabled)
        self._audit(f"Custom kit **{kit['name']}** {'switched off (not claimable)' if disabled else 'switched back on'}")
        return web.json_response({"ok": True, "disabled": disabled})

    async def custom_kits_disabled_all(self, request: web.Request) -> web.Response:
        """Switch every custom kit off (kept, not claimable) or every one back on."""
        disabled = bool((await self._json(request)).get("disabled"))
        changed = self.bot.db.set_all_custom_kits_disabled(disabled)
        total = len(self.bot.db.custom_kits())
        self._audit(f"All custom kits {'switched off (not claimable)' if disabled else 'switched back on'} "
                    f"({changed} of {total} changed)")
        return web.json_response({"ok": True, "disabled": disabled, "changed": changed, "total": total})

    async def link_player(self, request: web.Request) -> web.Response:
        """Admin links a member to a player (also offline). member: Discord user ID, @name or display name."""
        body = await self._json(request)
        cog = self.bot.get_cog("CustomKits") if self.bot is not None else None
        if cog is None:
            raise ApiError("The bot isn't ready yet.", 503)
        who = str(body.get("member", "")).strip().lstrip("@")
        if not who:
            raise ApiError("Enter the Discord member (ID or name).")
        user_id = int(who) if who.isdigit() else None
        if user_id is None:
            guild = cog.bot.get_guild(self.bot.config.guild_id) if self.bot.config.guild_id else \
                (self.bot.guilds[0] if self.bot.guilds else None)
            matches = [m for m in (guild.members if guild else [])
                       if who.lower() in (m.name.lower(), (m.global_name or "").lower(), m.display_name.lower())]
            if len(matches) != 1:
                raise ApiError(f"No Discord member called {who}." if not matches else
                               f"{len(matches)} members are called {who} – use the Discord user ID.")
            user_id = matches[0].id
        try:
            name, previous = cog.admin_link(user_id, str(body.get("player", "")), force=bool(body.get("force")))
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        nick = await cog.set_member_nickname(user_id, name)
        moved = f" (moved from Discord user {previous})" if previous else ""
        self._audit(f"Linked Discord user {user_id} to **{name}**{moved}")
        return web.json_response({"ok": True, "player": name, "user_id": str(user_id),
                                  "seen": bool(self.bot.db.player(name)), "nickname": nick})

    async def sync_nicknames(self, request: web.Request) -> web.Response:
        """Set every linked member's Discord nickname to their in-game name (the "for the past" button)."""
        cog = self.bot.get_cog("CustomKits") if self.bot is not None else None
        if cog is None:
            raise ApiError("The bot isn't ready yet.", 503)
        counts = await cog.sync_all_nicknames()
        if "error" in counts:
            raise ApiError(counts["error"])
        self._audit(f"Set nicknames for linked players ({counts['changed']}/{counts['total']} changed)")
        return web.json_response({"ok": True, **counts})

    async def unlink_player(self, request: web.Request) -> web.Response:
        player = self.bot.db.unlink_player(int(request.match_info["user_id"]))
        if player is None:
            raise ApiError("Not linked.", 404)
        self._audit(f"Unlinked Discord user {request.match_info['user_id']} from **{player}**")
        return web.json_response({"ok": True})

    async def add_custom_item(self, request: web.Request) -> web.Response:
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        name = request.match_info["name"]
        kit = self.bot.db.custom_kit(name)
        if kit is None:
            raise ApiError("Custom kit not found.", 404)
        data = await self._json(request)
        short = itemlist.normalize_shortname(kitparse.clean_arg(self._text(data, "item", limit=64)))
        amount = self._int(data, "amount", 1, 1, 100000)
        if len(self.bot.db.custom_kit_items(kit["name"])) >= MAX_ITEMS_PER_KIT:
            raise ApiError(f"At most {MAX_ITEMS_PER_KIT} items per kit.")
        self.bot.db.add_custom_kit_item(kit["name"], short, amount)
        self._audit(f"Custom kit **{kit['name']}** + `{short}` × {amount}")
        return web.json_response({"ok": True, "known_item": short in itemlist.COMMON_ITEMS})

    async def remove_custom_item(self, request: web.Request) -> web.Response:
        name, item_id = request.match_info["name"], int(request.match_info["item_id"])
        if not self.bot.db.remove_custom_kit_item(name, item_id):
            raise ApiError("Item not found.", 404)
        self._audit(f"Custom kit **{name}** – item #{item_id} removed")
        return web.json_response({"ok": True})

    async def create_autokit(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        kit = kitparse.clean_arg(self._text(data, "kit"))
        kit_type = "custom" if data.get("kit_type") == "custom" else "ingame"
        trigger = data.get("trigger")
        if trigger not in ("respawn", "quickchat"):
            raise ApiError("Trigger must be respawn or quickchat.")
        phrase = self._text(data, "phrase", required=trigger == "quickchat") or None
        from support import teleports as tp
        if trigger == "quickchat" and tp.is_location(phrase):
            raise ApiError("Location emotes are teleports only – set them under 📍 Teleports.")
        if kit_type == "custom":
            row = self.bot.db.custom_kit(kit)
            if row is None:
                raise ApiError("Custom kit not found.", 404)
            kit = row["name"]
        rule_id = self.bot.db.add_kit_rule(
            kit, trigger, phrase if trigger == "quickchat" else None,
            self._int(data, "cooldown", 0, 0, 525600), self._int(data, "max_claims", 0, 0, 10000),
            "Web interface", kit_type, self._role_id(data))
        self._audit(f"Autokit #{rule_id} created ({kit}, {trigger})")
        return web.json_response({"ok": True, "id": rule_id})

    @staticmethod
    def _role_id(data: dict) -> int | None:
        raw = str(data.get("required_role_id") or "").strip()
        if raw and not raw.isdigit():
            raise ApiError("Invalid role.")
        return int(raw) if raw else None

    async def autokit_role(self, request: web.Request) -> web.Response:
        rule_id = int(request.match_info["rule_id"])
        role_id = self._role_id(await self._json(request))
        if not self.bot.db.set_kit_rule_role(rule_id, role_id):
            raise ApiError("Rule not found.", 404)
        self._audit(f"Autokit #{rule_id} – required role {role_id or 'removed'}")
        return web.json_response({"ok": True})

    async def toggle_autokit(self, request: web.Request) -> web.Response:
        rule_id = int(request.match_info["rule_id"])
        enabled = bool((await self._json(request)).get("enabled"))
        if not self.bot.db.set_kit_rule_enabled(rule_id, enabled):
            raise ApiError("Rule not found.", 404)
        self._audit(f"Autokit #{rule_id} {'enabled' if enabled else 'paused'}")
        return web.json_response({"ok": True})

    async def delete_autokit(self, request: web.Request) -> web.Response:
        rule_id = int(request.match_info["rule_id"])
        if not self.bot.db.delete_kit_rule(rule_id):
            raise ApiError("Rule not found.", 404)
        self._audit(f"Autokit #{rule_id} deleted")
        return web.json_response({"ok": True})

    async def emote_binds(self, request: web.Request) -> web.Response:
        """The emote board: every quick chat emote with the kit it gives (if any)."""
        from support import emote_binds as eb
        bound = {}
        for b in eb.binds(self.bot.db):
            bound.setdefault(b["raw"], b)
        emotes = [{**e, "bind": bound.get(e["raw"])} for e in eb.emotes()]
        return web.json_response({"emotes": emotes, "binds": len(bound)})

    async def emote_bind_roles(self, request: web.Request) -> web.Response:
        """Each emote bind (or every autokit) needs its kit's own Discord role (linked kit role, else same name)."""
        data = await self._json(request)
        if data.get("remove"):
            count = self._cog("Kits").clear_bind_roles(emotes_only=not data.get("all_autokits"),
                                                       by=f"Web interface ({_current_user.get()})")
            return web.json_response({"ok": True, "removed": count})
        result = self._cog("Kits").roles_to_binds(emotes_only=not data.get("all_autokits"),
                                                  only_missing=bool(data.get("only_missing")),
                                                  by=f"Web interface ({_current_user.get()})")
        if not result["ok"]:
            raise ApiError(result["error"], 503)
        return web.json_response(result)

    async def set_emote_bind(self, request: web.Request) -> web.Response:
        """Bind a kit to an emote (replaces what it gave before) or, with an empty kit, unbind it."""
        from support import emote_binds as eb
        data = await self._json(request)
        raw, suggestions = eb.find(self._text(data, "emote"))
        if raw is None:
            raise ApiError("Unknown emote." + (f" Did you mean: {', '.join(suggestions)}" if suggestions else ""))
        kit = kitparse.clean_arg(str(data.get("kit") or ""))
        if not kit:
            removed = eb.unbind(self.bot.db, raw)
            if removed:
                self._audit(f"Emote {eb.label(raw)} unbound ({', '.join(removed)})")
            return web.json_response({"ok": True, "removed": removed})
        kit_type = "custom" if data.get("kit_type") == "custom" else "ingame"
        if kit_type == "custom":
            row = self.bot.db.custom_kit(kit)
            if row is None:
                raise ApiError("Custom kit not found.", 404)
            kit = row["name"]
        rule_id, replaced = eb.bind(self.bot.db, raw, kit, kit_type=kit_type,
                                    cooldown_minutes=self._int(data, "cooldown", 0, 0, 525600),
                                    max_claims=self._int(data, "max_claims", 0, 0, 10000),
                                    required_role_id=self._role_id(data), by="Web interface")
        self._audit(f"Emote {eb.label(raw)} → kit {kit} (autokit #{rule_id})")
        return web.json_response({"ok": True, "id": rule_id, "replaced": replaced})

    # ------------------------------------------------------------ Location teleports (Location emote -> coordinates)

    def _teleports_cog(self):
        cog = self.bot.get_cog("Teleports") if self.bot is not None else None
        if cog is None:
            raise ApiError("The teleport module is not loaded.", 503)
        return cog

    def _location(self, data: dict) -> str:
        from support import teleports as tp
        raw = tp.find(self._text(data, "location"))
        if raw is None:
            raise ApiError("Unknown location – North, Northeast … Northwest.")
        return raw

    async def teleports(self, request: web.Request) -> web.Response:
        cog = self._teleports_cog()
        return web.json_response({"teleports": cog.all(), "online": sorted(self.bot.online_players)})

    async def set_teleport(self, request: web.Request) -> web.Response:
        from cogs.teleports import TeleportError
        cog = self._teleports_cog()
        data = await self._json(request)
        raw = self._location(data)
        try:
            t = cog.set(raw, data.get("x"), data.get("y"), data.get("z"),
                        name=str(data.get("name") or "")[:40], cooldown_minutes=self._int(data, "cooldown", 0, 0, 525600),
                        required_role_id=self._role_id(data), by="Web interface")
        except TeleportError as exc:
            raise ApiError(str(exc)) from exc
        if "enabled" in data:
            self.bot.db.update_teleport(raw, enabled=bool(data["enabled"]))
            t = cog.info(raw)
        self._audit(f"Teleport {t['location']} → ({t['x']:.1f}, {t['y']:.1f}, {t['z']:.1f})")
        return web.json_response({"ok": True, "teleport": t})

    async def capture_teleport(self, request: web.Request) -> web.Response:
        """Reads where a player (the admin, in game) stands; with a location it saves it there too."""
        from cogs.teleports import TeleportError
        cog = self._teleports_cog()
        data = await self._json(request)
        try:
            player, (x, y, z) = await cog.capture(self._text(data, "player"))
            t = None
            if data.get("location"):
                t = cog.set(self._location(data), x, y, z, by=f"Web interface (position of {player})")
                self._audit(f"Teleport {t['location']} captured from {player} → ({x:.1f}, {y:.1f}, {z:.1f})")
        except TeleportError as exc:
            raise ApiError(str(exc).replace("**", "")) from exc
        return web.json_response({"ok": True, "player": player, "x": x, "y": y, "z": z, "teleport": t})

    async def clear_teleport(self, request: web.Request) -> web.Response:
        self._teleports_cog()
        raw = self._location(await self._json(request))
        if not self.bot.db.delete_teleport(raw):
            raise ApiError("That location has no teleport.", 404)
        self._audit(f"Teleport {raw} removed")
        return web.json_response({"ok": True})

    async def reset_autokits(self, request: web.Request) -> web.Response:
        count = self.bot.db.reset_kit_claims()
        self._audit(f"Autokit history reset ({count} entries)")
        return web.json_response({"ok": True, "count": count})

    async def meta(self, request: web.Request) -> web.Response:
        """Option lists for forms: quick chat phrases and item suggestions."""
        return web.json_response({
            "phrases": [{"raw": raw, "text": text} for raw, text in quickchat.all_phrases().items()],
            "items": [{"short": s, "label": label} for s, label in itemlist.COMMON_ITEMS.items()],
            "catalogue": [{"category": cat, "icon": icon,
                           "items": [{"short": s, "label": label, "group": itemlist.GROUPS.get(s, "")}
                                     for s, label in entries.items()]}
                          for (cat, icon), entries in itemlist.CATALOGUE.items()],
            "kit_add_template": self.bot.config.kit_add_template,
            "item_images": True,                     # bundled pictures always; "off" only stops downloads
        })

    # ------------------------------------------------------------ Tickets, stats, AI

    async def tickets(self, request: web.Request) -> web.Response:
        status = request.query.get("status") or None
        rows = self.bot.db.tickets(status=status if status in ("open", "closed") else None)
        return web.json_response({"tickets": [{k: r[k] for k in r.keys() if k != "transcript"} for r in rows]})

    async def ticket_detail(self, request: web.Request) -> web.Response:
        row = self.bot.db.ticket(int(request.match_info["ticket_id"]))
        if row is None:
            raise ApiError("Ticket not found.", 404)
        return web.json_response(_row(row))

    async def leaderboard(self, request: web.Request) -> web.Response:
        category = request.query.get("category", "kills")
        if request.query.get("teams"):
            if category not in self.bot.db.TEAM_ORDER:
                raise ApiError("Unknown category.")
            return web.json_response({"rows": [dict(r) for r in self.bot.db.team_leaderboard(category, limit=50)]})
        if category not in LEADERBOARD_COLUMNS:
            raise ApiError("Unknown category.")
        return web.json_response({"rows": [dict(r) for r in self.bot.db.leaderboard(category, limit=50)]})

    def _knowledge_rag(self) -> dict:
        from support.rag import KnowledgeIndex
        cfg = self.bot.config
        tickets = self.bot.get_cog("Tickets")
        path: Path = cfg.support_knowledge_file
        index = tickets.knowledge_index() if tickets is not None else KnowledgeIndex(
            path.read_text(encoding="utf-8") if path.exists() else "")
        return {"enabled": cfg.knowledge_rag, "sections": len(index.chunks), "chars": len(index.text),
                "budget": cfg.knowledge_rag_chars, "whole": len(index.text) <= cfg.knowledge_rag_chars,
                "max": 500_000 if cfg.knowledge_rag else 12_000}

    async def get_knowledge(self, request: web.Request) -> web.Response:
        path: Path = self.bot.config.support_knowledge_file
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return web.json_response({"path": str(path), "text": text, "rag": self._knowledge_rag()})

    async def search_knowledge(self, request: web.Request) -> web.Response:
        """Which sections an AI answer to this question would get (RAG test on the Knowledge base page)."""
        tickets = self.bot.get_cog("Tickets")
        if tickets is None:
            raise ApiError("The ticket module isn't loaded.", 503)
        query = request.query.get("q", "")[:500]
        index = tickets.knowledge_index()
        _, info = tickets.knowledge_for(query)
        hits = index.search(query, 8)
        return web.json_response({"info": info, "results": [
            {"section": c.heading or "(top)", "text": c.text[:400], "score": round(s, 2)} for s, c in hits]})

    async def put_knowledge(self, request: web.Request) -> web.Response:
        text = str((await self._json(request)).get("text", ""))
        if len(text) > 500_000:
            raise ApiError("Text too long (max. 500,000 characters).")
        path: Path = self.bot.config.support_knowledge_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Knowledge base edited")
        return web.json_response({"ok": True})

    async def get_prices(self, request: web.Request) -> web.Response:
        path: Path = self.bot.config.price_list_file
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        pl = pricing.parse(text)
        return web.json_response({"path": str(path), "text": text, "warnings": pl.warnings, "rules": len(pl.rules),
                                  "currency": pl.currency, "can_edit": request["role"] == "owner"})

    async def put_prices(self, request: web.Request) -> web.Response:
        if request["role"] != "owner":       # prices decide what the bot approves by itself
            raise ApiError("Only the owner can change the price list.", status=403)
        text = str((await self._json(request)).get("text", ""))
        if len(text) > 50000:
            raise ApiError("Text too long (max. 50,000 characters).")
        pl = pricing.parse(text)
        unknown = sorted({s for r in pl.rules for s in r.items | r.extras} - set(itemlist.COMMON_ITEMS))
        path: Path = self.bot.config.price_list_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Price list edited")
        return web.json_response({"ok": True, "warnings": pl.warnings, "rules": len(pl.rules),
                                  "unknown_items": unknown})

    @property
    def webhook_token(self) -> str:
        """Secret part of the Tip4Serv webhook URL (derived from the web secret, stays the same)."""
        return hmac.new(self._secret, b"tip4serv-webhook", hashlib.sha256).hexdigest()[:32]

    async def tip4serv_webhook(self, request: web.Request) -> web.Response:
        key = "hook:" + self._client_ip(request)
        if self._login_failures.retry_after(key) is not None:
            raise web.HTTPTooManyRequests()
        if not same_secret(request.match_info["token"], self.webhook_token):
            self._login_failed(key)
            raise web.HTTPNotFound()
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            form = await request.post()
            data = dict(form) if form else None
        store = self.bot.get_cog("Store")
        if store is None or data is None:
            return web.json_response({"ok": False}, status=400 if data is None else 503)
        return web.json_response(await store.on_webhook(data))

    async def store_status(self, request: web.Request) -> web.Response:
        store = self.bot.get_cog("Store")
        payments = [dict(p) for p in self.bot.db.store_payments(limit=100)]
        pending = [{"id": r["id"], "kit": r["kit"], "user_id": str(r["user_id"]), "price": r["price"],
                    "currency": r["currency"], "source": r["source"], "note": r["pay_note"], "created_at": r["created_at"]}
                   for r in self.bot.db.kit_requests(status="pending", limit=50)]
        for p in payments:
            p["discord_id"] = str(p["discord_id"]) if p["discord_id"] else None
        status = store.status() if store else {"enabled": False}
        status["problem"] = store.problem() if store else "store module not loaded"
        if request["role"] == "owner":     # the URL is a secret: only the owner sees it
            base = self.public_url
            status["webhook_url"] = f"{base.rstrip('/')}/hooks/tip4serv/{self.webhook_token}" if base else None
            status["webhook_fixed"] = bool(self.ws.public_url)
        return web.json_response({"status": status,
                                  "payments": payments, "pending": pending})

    async def ai_status(self, request: web.Request) -> web.Response:
        tickets_cog = self.bot.get_cog("Tickets")
        if tickets_cog is None or not tickets_cog.ai.enabled:
            return web.json_response({"enabled": False})
        result = {"enabled": True, "model": tickets_cog.ai.model}
        try:
            result["balance"] = await tickets_cog.ai.balance()
        except DeepSeekError as exc:
            result["error"] = str(exc)
        return web.json_response(result)

    # ------------------------------------------------------------ Community

    async def get_rules(self, request: web.Request) -> web.Response:
        path: Path = self.bot.config.rules_file
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return web.json_response({"path": str(path), "text": text})

    async def put_rules(self, request: web.Request) -> web.Response:
        text = str((await self._json(request)).get("text", ""))
        if len(text) > 20000:
            raise ApiError("Text too long (max. 20,000 characters).")
        path: Path = self.bot.config.rules_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Rules edited")
        return web.json_response({"ok": True})

    async def community(self, request: web.Request) -> web.Response:
        cfg = self.bot.config

        def channel(cid):
            ch = self.bot.get_channel(cid) if cid else None
            return f"#{ch.name}" if ch else (str(cid) if cid else None)

        guild = self.bot.get_guild(cfg.guild_id) if cfg.guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        role = guild.get_role(cfg.auto_role_id) if guild and cfg.auto_role_id else None
        return web.json_response({
            "guild": guild.name if guild else None,
            "members": guild.member_count if guild else None,
            "welcome_channel": channel(cfg.welcome_channel_id),
            "welcome_message": cfg.welcome_message,
            "auto_role": role.name if role else (str(cfg.auto_role_id) if cfg.auto_role_id else None),
            "discord_log_channel": channel(cfg.discord_log_channel_id),
            "rules_channel": channel(cfg.rules_channel_id),
            "giveaways": [dict(r) for r in self.bot.db.giveaways(limit=30)],
        })

    async def giveaway_action(self, request: web.Request) -> web.Response:
        row = self.bot.db.giveaway(int(request.match_info["giveaway_id"]))
        action = request.match_info["action"]
        if row is None:
            raise ApiError("Giveaway not found.", 404)
        cog = self._cog("Giveaways")
        if action == "end" and row["ended"]:
            raise ApiError("Giveaway has already ended.")
        if action == "reroll" and not row["ended"]:
            raise ApiError("Only ended giveaways can be rerolled.")
        if not self.bot.is_ready():
            raise ApiError("Discord is not connected yet.", 503)
        winners = await cog.finish(row, reroll=action == "reroll")
        self._audit(f"Giveaway #{row['id']} {'ended' if action == 'end' else 'rerolled'}")
        return web.json_response({"ok": True, "winners": [str(w) for w in winners]})

    # ------------------------------------------------------------ Wipe, scheduled messages, reports, population

    async def get_wipe(self, request: web.Request) -> web.Response:
        wipe = self._cog("Wipe")
        s = wipe.schedule()
        local = None
        if s["next"]:
            from datetime import datetime
            local = datetime.fromtimestamp(s["next"], wipe.tz).strftime("%Y-%m-%dT%H:%M")
        cfg = self.bot.config
        return web.json_response({**s, "local": local, "repeat_text": wipe.repeat_text(s["repeat"], s["next"]),
                                  "repeats": {k: wipe.repeat_text(k, s["next"]) for k in ("none", "weekly",
                                                                                          "biweekly", "monthly")},
                                  "resets": {"kits": cfg.wipe_reset_kits, "stats": cfg.wipe_reset_stats,
                                             "points": cfg.wipe_reset_points}})

    async def put_wipe(self, request: web.Request) -> web.Response:
        from cogs.wipe import REPEATS, parse_when
        wipe = self._cog("Wipe")
        data = await self._json(request)
        ts = parse_when(self._text(data, "when", limit=30), wipe.tz)
        if ts is None:
            raise ApiError("Invalid date/time.")
        if ts <= time.time():
            raise ApiError("That time is in the past.")
        repeat = self._text(data, "repeat", required=False, limit=20) or "none"
        if repeat not in REPEATS:
            raise ApiError("Unknown repeat.")
        wipe.set_schedule(ts, repeat)
        self._audit(f"Next wipe set: <t:{ts}:F> ({wipe.repeat_text(repeat, ts)})")
        return web.json_response({"ok": True, "next": ts})

    async def clear_wipe(self, request: web.Request) -> web.Response:
        self._cog("Wipe").set_schedule(None)
        self._audit("Wipe schedule cleared")
        return web.json_response({"ok": True})

    async def wipe_done(self, request: web.Request) -> web.Response:
        reset = await self._cog("Wipe").do_wipe(by=f"{_current_user.get()} (web)")
        return web.json_response({"ok": True, "reset": reset})

    def _text_channels(self) -> list[dict]:
        cfg = self.bot.config
        guild = self.bot.get_guild(cfg.guild_id) if cfg.guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        return [{"id": str(c.id), "name": c.name} for c in guild.text_channels] if guild else []

    async def list_schedules(self, request: web.Request) -> web.Response:
        from cogs.schedule import TARGETS
        rows = []
        for r in self.bot.db.scheduled_messages():
            channel = self.bot.get_channel(r["channel_id"]) if r["channel_id"] else None
            rows.append({**dict(r), "channel_id": str(r["channel_id"]) if r["channel_id"] else None,
                         "channel": f"#{channel.name}" if channel else None})
        return web.json_response({"rows": rows, "targets": TARGETS, "channels": self._text_channels()})

    async def create_schedule(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        channel = self._text(data, "channel_id", required=False, limit=25)
        try:
            msg_id = self._cog("Schedule").add(self._text(data, "text", limit=400), self._int(data, "minutes", 0, 0, 100_000),
                                              self._text(data, "target", limit=10), int(channel) if channel.isdigit() else None,
                                              f"{_current_user.get()} (web)")
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        self._audit(f"Scheduled message #{msg_id} added")
        return web.json_response({"ok": True, "id": msg_id})

    async def toggle_schedule(self, request: web.Request) -> web.Response:
        msg_id = int(request.match_info["msg_id"])
        if not self.bot.db.set_scheduled_enabled(msg_id, bool((await self._json(request)).get("enabled"))):
            raise ApiError("Message not found.", 404)
        return web.json_response({"ok": True})

    async def delete_schedule(self, request: web.Request) -> web.Response:
        msg_id = int(request.match_info["msg_id"])
        if not self.bot.db.delete_scheduled_message(msg_id):
            raise ApiError("Message not found.", 404)
        self._audit(f"Scheduled message #{msg_id} deleted")
        return web.json_response({"ok": True})

    async def list_reports(self, request: web.Request) -> web.Response:
        status = request.query.get("status") or None
        reports = []
        for r in self.bot.db.reports(status=status, limit=100):
            user = self.bot.get_user(r["reporter_id"])
            reports.append({**dict(r), "reporter_id": str(r["reporter_id"]), "channel_id": None, "message_id": None,
                            "reporter": str(user) if user else str(r["reporter_id"]),
                            "online": r["player"] in self.bot.online_players})
        watch = [{**dict(w), "online": w["player"] in self.bot.online_players} for w in self.bot.db.watchlist()]
        return web.json_response({"reports": reports, "watchlist": watch})

    async def handle_report(self, request: web.Request) -> web.Response:
        if not await self._cog("Reports").mark_handled(int(request.match_info["report_id"]),
                                                        f"{_current_user.get()} (web)"):
            raise ApiError("Report not found or already handled.", 404)
        return web.json_response({"ok": True})

    async def add_watch(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        player = self._text(data, "player", limit=64)
        self.bot.db.watch(player, self._text(data, "reason", required=False, limit=300) or None,
                          f"{_current_user.get()} (web)")
        self._audit(f"Watchlist: {player} added")
        return web.json_response({"ok": True})

    async def remove_watch(self, request: web.Request) -> web.Response:
        player = request.match_info["player"]
        if not self.bot.db.unwatch(player):
            raise ApiError("Not on the watchlist.", 404)
        self._audit(f"Watchlist: {player} removed")
        return web.json_response({"ok": True})

    async def abuse_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._cog("AbuseGuard").status())

    async def abuse_decide(self, request: web.Request) -> web.Response:
        ok, text = await self._cog("AbuseGuard").decide(int(request.match_info["case_id"]),
                                                       request.match_info["action"] == "approve",
                                                       f"{_current_user.get()} (web)")
        if not ok:
            raise ApiError(text, 409)
        return web.json_response({"ok": True, "message": text})

    async def abuse_loop_stop(self, request: web.Request) -> web.Response:
        player = self._text(await self._json(request), "player", limit=64)
        if not self._cog("AbuseGuard").stop_loop(player, f"by {_current_user.get()} (web)"):
            raise ApiError("That loop isn't running any more.", 409)
        return web.json_response({"ok": True})

    async def population(self, request: web.Request) -> web.Response:
        try:
            days = max(1, min(int(request.query.get("days", "1")), 30))
        except ValueError:
            raise ApiError("Invalid range.") from None
        since = int(time.time()) - days * 86400
        bucket = 0 if days == 1 else (1800 if days <= 7 else 3600)
        points = self.bot.db.population(since, bucket)
        return web.json_response({"days": days, "points": points,
                                  "online_now": len(self.bot.online_players)})

    # ------------------------------------------------------------ Points & shop

    async def shop(self, request: web.Request) -> web.Response:
        eco = self._cog("Economy")
        guild = eco.guild()
        items = [{**dict(r), "summary": eco.describe_item(r, guild)} for r in self.bot.db.shop_items()]
        wallets = []
        for w in self.bot.db.top_wallets(50):
            member = guild.get_member(w["user_id"]) if guild else None
            wallets.append({"user_id": str(w["user_id"]), "user": member.display_name if member else str(w["user_id"]),
                            "player": self.bot.db.linked_player(w["user_id"]), "points": w["points"],
                            "earned_total": w["earned_total"], "daily_streak": w["daily_streak"]})
        roles = [{"id": str(r.id), "name": r.name} for r in reversed(guild.roles)
                 if not r.managed and not r.is_default() and r < guild.me.top_role] if guild else []
        cfg = self.bot.config
        return web.json_response({
            "enabled": eco.enabled, "points_name": cfg.points_name, "items": items, "wallets": wallets,
            "kinds": SHOP_KINDS, "roles": roles, "custom_kits": [r["name"] for r in self.bot.db.custom_kits()],
            "earning": {"per_hour": cfg.points_per_hour, "per_kill": cfg.points_per_kill, "daily": cfg.daily_points,
                        "streak_bonus": cfg.daily_streak_bonus, "reward_hours": cfg.playtime_reward_hours,
                        "reward_kit": cfg.playtime_reward_kit, "reward_points": cfg.playtime_reward_points}})

    def _shop_fields(self, data: dict, eco) -> dict:
        kind = self._text(data, "kind", limit=20)
        amount = self._int(data, "amount", 1, 0, 100_000)
        price = self._int(data, "price", 0, 0, 10_000_000)
        try:
            target = eco.validate_item(kind, self._text(data, "target", limit=100), amount, price)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return {"kind": kind, "target": target, "amount": amount, "price": price,
                "description": self._text(data, "description", required=False, limit=300) or None}

    async def create_shop_item(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        eco = self._cog("Economy")
        name = self._text(data, "name", limit=60)
        if self.bot.db.shop_item_by_name(name):
            raise ApiError(f"There already is an item called {name}.")
        fields = self._shop_fields(data, eco)
        item_id = self.bot.db.add_shop_item(name, **fields)
        self._audit(f"Shop item added: {name} ({fields['price']} {self.bot.config.points_name})")
        return web.json_response({"ok": True, "id": item_id})

    async def update_shop_item(self, request: web.Request) -> web.Response:
        item_id = int(request.match_info["item_id"])
        row = self.bot.db.shop_item(item_id)
        if row is None:
            raise ApiError("Item not found.", 404)
        data = await self._json(request)
        fields: dict = {}
        if "enabled" in data:
            fields["enabled"] = int(bool(data["enabled"]))
        if "kind" in data:
            fields.update(self._shop_fields({**dict(row), **data}, self._cog("Economy")))
        if "name" in data:
            name = self._text(data, "name", limit=60)
            other = self.bot.db.shop_item_by_name(name)
            if other is not None and other["id"] != item_id:
                raise ApiError(f"There already is an item called {name}.")
            fields["name"] = name
        self.bot.db.update_shop_item(item_id, **fields)
        self._audit(f"Shop item changed: {fields.get('name', row['name'])}")
        return web.json_response({"ok": True})

    async def delete_shop_item(self, request: web.Request) -> web.Response:
        row = self.bot.db.shop_item(int(request.match_info["item_id"]))
        if row is None or not self.bot.db.delete_shop_item(row["id"]):
            raise ApiError("Item not found.", 404)
        self._audit(f"Shop item deleted: {row['name']}")
        return web.json_response({"ok": True})

    async def change_points(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        user_id = self._text(data, "user_id", limit=25)
        if not user_id.isdigit():
            raise ApiError("Invalid user ID.")
        delta = self._int(data, "delta", 0, -1_000_000, 1_000_000)
        if not delta:
            raise ApiError("Enter a number other than 0.")
        reason = self._text(data, "reason", required=False, limit=150) or "web interface"
        balance = self.bot.db.add_points(int(user_id), delta, f"by {_current_user.get()} (web): {reason}")
        self._audit(f"Points {'+' if delta > 0 else ''}{delta} for <@{user_id}> ({reason})")
        return web.json_response({"ok": True, "balance": balance})

    async def timed_roles(self, request: web.Request) -> web.Response:
        rows = []
        for r in self.bot.db.timed_roles():
            guild = self.bot.get_guild(r["guild_id"])
            member = guild.get_member(r["user_id"]) if guild else None
            role = guild.get_role(r["role_id"]) if guild else None
            rows.append({"id": r["id"], "user_id": str(r["user_id"]),
                         "user": (member.display_name if member else None) or str(r["user_id"]),
                         "role": role.name if role else str(r["role_id"]), "expires_at": r["expires_at"],
                         "reason": r["reason"], "granted_by": r["granted_by"]})
        return web.json_response({"rows": rows})

    async def delete_timed_role(self, request: web.Request) -> web.Response:
        row = self.bot.db.timed_role_by_id(int(request.match_info["row_id"]))
        if row is None:
            raise ApiError("Not found (already ended?).", 404)
        cog = self._cog("TimedRoles")
        try:
            text = await cog.revoke(row)
        except discord.Forbidden as exc:
            raise ApiError(explain_forbidden(exc, self.bot), 403) from exc
        self._audit(f"Timed role removed: {text}")
        return web.json_response({"ok": True})

    async def list_backups(self, request: web.Request) -> web.Response:
        monitor = self.bot.get_cog("Monitor")
        return web.json_response({"enabled": self.bot.config.backup_enabled,
                                  "keep_days": self.bot.config.backup_keep_days,
                                  "backups": await asyncio.to_thread(backups.list_backups),
                                  "encryption": self._encryption_status(),
                                  **(monitor.backup_status() if monitor else {})})

    async def create_backup(self, request: web.Request) -> web.Response:
        name = await self._cog("Monitor").backup_now("manual")
        if name is None:
            raise ApiError("Backup failed – see the bot log.", 500)
        self._audit(f"Backup created: {name}")
        return web.json_response({"ok": True, "name": name})

    def _encryption_status(self) -> dict:
        import dbcrypt
        return {"on": dbcrypt.active(), "setting": bool(getattr(self.bot.config, "db_encryption", False)),
                "available": dbcrypt.available(), "key_file": str(dbcrypt.key_file())}

    async def db_recovery_key(self, request: web.Request) -> web.Response:
        """The database recovery key – owner, password re-check (POST under /api/backups). Write it down!"""
        import dbcrypt
        if not dbcrypt.active():
            raise ApiError("The databases aren't encrypted (turn on “Encrypt the databases” and restart).")
        try:
            key = dbcrypt.load_key()
        except dbcrypt.KeyError_ as exc:
            raise ApiError(str(exc)) from exc
        self._audit("Database recovery key shown")
        return web.json_response({"key": key, "key_file": str(dbcrypt.key_file())})

    async def full_backups(self, request: web.Request) -> web.Response:
        """Weekly full backups (code + every database + settings) and whether an emergency restore is possible."""
        import full_backup
        cog = self.bot.get_cog("FullBackups") if self.bot else None
        last = float(self.bot.db.get_value("last_full_backup_ts") or 0) if self.bot else 0
        return web.json_response({"enabled": bool(cog and cog.enabled()), "available": cog is not None,
                                  "main": not servers.is_child(), "last": int(last) or None,
                                  "error": getattr(cog, "last_error", None),
                                  "backups": await asyncio.to_thread(full_backup.list_backups)})

    async def create_full_backup(self, request: web.Request) -> web.Response:
        cog = self._cog("FullBackups")
        if servers.is_child():
            raise ApiError("Full backups are made on the main server (they include every server).")
        try:
            name = await cog.make_now("manual")
        except Exception as exc:
            raise ApiError(f"Full backup failed: {exc}", 500) from exc
        self._audit(f"Full backup created: {name}")
        return web.json_response({"ok": True, "name": name})

    async def emergency_restore(self, request: web.Request) -> web.Response:
        """🚨 Saves the current state, then restarts into the chosen full backup (type RESTORE to confirm)."""
        cog = self._cog("FullBackups")
        data = await self._json(request)
        if str(data.get("confirm") or "").strip().upper() != "RESTORE":
            raise ApiError("Type RESTORE to confirm.")
        try:
            safety = await cog.emergency_restore(request.match_info["name"], by=f"web ({_current_user.get()})")
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        return web.json_response({"ok": True, "safety": safety})

    async def download_backup(self, request: web.Request) -> web.StreamResponse:
        path = backups.backup_path(request.match_info["name"])
        if path is None:
            raise ApiError("Backup not found.", 404)
        self._audit(f"Backup downloaded: {path.name}")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    async def botlog(self, request: web.Request) -> web.Response:
        return web.json_response({"lines": list(memory_log.records)})
