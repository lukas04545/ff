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
import logging
import os
import secrets
import time
import webbrowser
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Callable

import aiohttp
import discord
from aiohttp import web

import backups
import settings_schema
from config import ConfigError, load_config, required_intents
from envfile import read_env, update_env
from rce.diagnostics import diagnose_rcon

from logbuffer import memory_log
from web import tunnel as tunnelmod
from web.users import OWNER, UserError, UserStore
from cogs.economy import SHOP_KINDS
from permcheck import check_permissions, explain_forbidden
from rce import items as itemlist
from rce import kits as kitparse
from rce import quickchat
from rce.rcon_client import RconError
from storage import LEADERBOARD_COLUMNS
from support.deepseek import DeepSeekError
from utils import format_minutes, parse_duration, sanitize_ingame_text, sanitize_player_name, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "rce_session"
PUBLIC_LINK_FILE = "PUBLIC_LINK.txt"  # current public address (Cloudflare tunnel), next to start.bat
SESSION_TTL = 12 * 3600
LOGIN_ATTEMPTS_PER_MINUTE = 5
CSRF_HEADER = "X-Requested-With"
# Only for role "owner": settings (tokens/passwords), bot restart, user management
OWNER_ONLY_PREFIXES = ("/api/settings", "/api/restart", "/api/setup", "/api/users", "/api/update", "/api/backups")
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("web_user", default="?")
POLLING_COMMANDS = ("playerlist", "serverinfo")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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
).value


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
                       public_url=(env.get("WEB_PUBLIC_URL") or "").strip())


class WebInterface:
    """bot=None starts setup mode: only login and settings, without Discord/RCON."""

    def __init__(self, bot: "RceBot | None", *, web_settings: WebSettings | None = None,
                 setup_error: str | None = None, setup_fix_url: str | None = None,
                 on_config_saved: Callable[[], None] | None = None,
                 env_path: Path = ENV_PATH, secret_path: Path = Path("data") / "web_secret.key",
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
        self.ws = web_settings
        self.password = web_settings.password
        if not self.password:
            # No fixed password: random password + one-time link (see start())
            self.password = secrets.token_urlsafe(9)
        self.login_token = secrets.token_urlsafe(24)
        self._secret = self._load_secret(secret_path)
        self.users = UserStore(users_path or secret_path.parent / "web_users.json")
        self._revoked: set[str] = set()
        self._login_attempts: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._runner: web.AppRunner | None = None
        self.tunnel: tunnelmod.Tunnel | None = None
        self.tunnel_problem: str | None = None
        self.generated_password: str | None = None  # shown once in the bot window
        self.restart_required = False
        self.restart_for: list[str] = []     # saved settings that only apply after a restart
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
        app = web.Application(middlewares=[self._error_middleware, self._host_middleware, self._auth_middleware],
                              client_max_size=1024 ** 2)
        r = app.router
        r.add_get("/", self._index)
        r.add_static("/static/", STATIC_DIR)
        r.add_get("/auth", self.auth_link)
        r.add_post("/api/login", self.login)
        r.add_post("/api/logout", self.logout)
        r.add_get("/api/me", self.me)
        r.add_get("/api/status", self.status)
        r.add_get("/api/settings", self.get_settings)
        r.add_put("/api/settings", self.put_settings)
        r.add_post("/api/settings/test-rcon", self.settings_test_rcon)
        r.add_post("/api/settings/test-discord", self.settings_test_discord)
        r.add_get("/api/settings/discord-options", self.settings_discord_options)
        r.add_get("/api/users", self.list_users)
        r.add_post("/api/users", self.create_user)
        r.add_put("/api/users/{username}", self.update_user)
        r.add_delete("/api/users/{username}", self.delete_user)
        r.add_post("/api/account/password", self.change_own_password)
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
        r.add_post("/api/kits/give", self.give_kit)
        r.add_post("/api/kit-builder/ingame", self.builder_ingame)
        r.add_post("/api/kit-builder/custom", self.builder_custom)
        r.add_post("/api/custom-kits", self.create_custom_kit)
        r.add_post("/api/custom-kits/import", self.import_custom_kits)
        r.add_delete("/api/custom-kits/{name}", self.delete_custom_kit)
        r.add_put("/api/custom-kits/{name}/claim", self.custom_kit_claim_settings)
        r.add_delete("/api/linked-players/{user_id:\\d+}", self.unlink_player)
        r.add_post("/api/custom-kits/{name}/items", self.add_custom_item)
        r.add_delete("/api/custom-kits/{name}/items/{item_id:\\d+}", self.remove_custom_item)
        r.add_post("/api/autokits", self.create_autokit)
        r.add_post("/api/autokits/reset", self.reset_autokits)
        r.add_post("/api/autokits/{rule_id:\\d+}/toggle", self.toggle_autokit)
        r.add_put("/api/autokits/{rule_id:\\d+}/role", self.autokit_role)
        r.add_delete("/api/autokits/{rule_id:\\d+}", self.delete_autokit)
        r.add_get("/api/meta", self.meta)
        r.add_get("/api/tickets", self.tickets)
        r.add_get("/api/tickets/{ticket_id:\\d+}", self.ticket_detail)
        r.add_get("/api/leaderboard", self.leaderboard)
        r.add_get("/api/knowledge", self.get_knowledge)
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
        r.add_get("/api/shop", self.shop)
        r.add_post("/api/shop/items", self.create_shop_item)
        r.add_put("/api/shop/items/{item_id:\\d+}", self.update_shop_item)
        r.add_delete("/api/shop/items/{item_id:\\d+}", self.delete_shop_item)
        r.add_post("/api/points", self.change_points)
        r.add_get("/api/timed-roles", self.timed_roles)
        r.add_delete("/api/timed-roles/{row_id:\\d+}", self.delete_timed_role)
        r.add_get("/api/backups", self.list_backups)
        r.add_post("/api/backups", self.create_backup)
        r.add_get("/api/backups/{name}", self.download_backup)
        return app

    async def start(self, *, open_browser: bool | None = None) -> bool:
        ws = self.ws
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, ws.host, ws.port).start()
        except OSError as exc:
            log.error("Web interface could not open port %s (%s) – choose a different WEB_PORT.", ws.port, exc)
            return False
        self._start_tunnel()  # may create WEB_PASSWORD, so before the login hints below
        shown_host = "localhost" if ws.host in ("127.0.0.1", "0.0.0.0", "") else ws.host
        url = f"http://{shown_host}:{ws.port}"
        login_url = f"{url}/auth?token={self.login_token}"
        if self.setup_mode:
            log.warning("SETUP: Open %s in your browser to fill in the missing settings.", login_url)
        else:
            log.info("Web interface running: %s", url)
            if not ws.password:
                log.warning("No WEB_PASSWORD set – log in via this link: %s  (or password: %s)",
                            login_url, self.password)
        if ws.host not in ("127.0.0.1", "localhost"):
            log.warning("Web interface is reachable on the network (WEB_HOST=%s). Do not expose it to the internet!",
                        ws.host)
        if ws.open_browser if open_browser is None else open_browser:
            try:
                webbrowser.open(login_url)
            except Exception:  # e.g. no browser on a server
                pass
        return True

    def _start_tunnel(self) -> None:
        """Cloudflare tunnel (reachable from the internet). Keeps running across bot restarts."""
        ws = self.ws
        if not ws.tunnel:
            tunnelmod.stop_tunnel()
            return
        if not ws.password:
            # Public address without a fixed password: create a strong one and keep it in the .env
            password = secrets.token_urlsafe(12)
            try:
                update_env(self.env_path, {"WEB_PASSWORD": password})
            except OSError as exc:
                self.tunnel_problem = f"Not started: could not save a WEB_PASSWORD to the .env ({exc})."
                log.error("Cloudflare tunnel %s", self.tunnel_problem)
                tunnelmod.stop_tunnel()
                return
            ws.password = self.password = self.generated_password = password
            log.warning("Created a web interface password (saved in .env as WEB_PASSWORD): %s", password)
        log.info("Setting up the Cloudflare tunnel – the link follows in a few seconds …")
        self.tunnel = tunnelmod.ensure_tunnel(ws.port, token=ws.tunnel_token, public_url=ws.public_url)
        loop = asyncio.get_running_loop()
        self.tunnel.on_online = lambda url: loop.call_soon_threadsafe(self._tunnel_online, url)
        if self.tunnel.state == "online":  # already running (restart from the web interface)
            self._tunnel_online(self.tunnel.url, announce=False)

    def _tunnel_online(self, url: str | None, announce: bool = True) -> None:
        url = url or self.ws.public_url or "(your fixed tunnel address)"
        if self.tunnel is not None and self.tunnel.adopted:
            announce = False     # same link as before the update restart – no new announcement
        login = (f"owner / {self.generated_password}   (new password, saved in .env as WEB_PASSWORD)"
                 if self.generated_password else "owner / your WEB_PASSWORD   (other admins: their own account)")
        bar = "=" * 72
        for line in ("", bar, "  🌍 YOUR WEB INTERFACE LINK – works from anywhere (phone, other PCs):", "",
                     f"     {url}", "", f"  Login: {login}", "  The link is also saved in PUBLIC_LINK.txt "
                     "and posted in the admin log channel.", bar, ""):
            log.warning(line)
        try:
            (self.env_path.parent / PUBLIC_LINK_FILE).write_text(
                f"{url}\n\nLogin: owner + your WEB_PASSWORD (in the .env), other admins: their own account.\n"
                "This address changes when the bot window is closed.\n", encoding="utf-8")
        except OSError:
            pass
        if announce and self.bot is not None:
            self.bot.audit(f"🌍 Web interface reachable from anywhere: {url}")

    @property
    def public_url(self) -> str | None:
        if self.tunnel is not None and self.tunnel.state == "online":
            return self.tunnel.url or self.ws.public_url or None
        return None

    async def stop(self) -> None:
        if self.tunnel is not None:
            self.tunnel.on_online = None  # the tunnel itself keeps running for the restarted bot
        if self._runner is not None:
            await self._runner.cleanup()

    # ------------------------------------------------------------ Middleware

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
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

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/"):
            return await handler(request)
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
        _current_user.set(session[0])
        return await handler(request)

    def _session_key(self, username: str) -> str | None:
        """Secret part of the signature: the owner's WEB_PASSWORD or the user's password hash.
        Changing the password (or deleting/disabling the account) ends all their sessions."""
        if username == OWNER:
            return self.ws.password
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
        return username, self.users.get(username)["role"]

    def _set_session_cookie(self, resp: web.StreamResponse, username: str = OWNER) -> None:
        # Random nonce: every login gets its own token (logout revokes exactly that one)
        token = self._sign(username, int(time.time()) + SESSION_TTL, secrets.token_hex(8))
        if token:
            resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Strict")

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

    # ------------------------------------------------------------ Pages & login

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    def _rate_limit(self, key: str) -> None:
        attempts = self._login_attempts[key]
        now = time.time()
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS_PER_MINUTE:
            raise ApiError("Too many attempts – please wait a minute.", 429)
        attempts.append(now)

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
        self._rate_limit(ip)
        self._rate_limit("user:" + username.lower())  # also limits guessing one account from many IPs

        if username.lower() == OWNER:
            username = OWNER
            ok = hmac.compare_digest(password.encode(), self.password.encode())
        else:
            user = await asyncio.to_thread(self.users.authenticate, username, password)
            ok = user is not None
            username = user["username"] if user else username
        if not ok:
            log.warning("Failed web login for '%s' from %s", username, ip)
            raise ApiError("Wrong username or password.", 401)
        self._login_attempts[ip].clear()
        self._login_attempts["user:" + username.lower()].clear()
        resp = web.json_response({"ok": True})
        self._set_session_cookie(resp, username)
        log.info("Web login: %s from %s", username, ip)
        if self.bot is not None and username != OWNER:
            self.bot.audit(f"🌐 Web interface: **{username}** logged in")
        return resp

    async def auth_link(self, request: web.Request) -> web.Response:
        """One-time link from the bot window: logs in and opens the matching page."""
        if not hmac.compare_digest(request.query.get("token", ""), self.login_token):
            raise web.HTTPFound("/")
        resp = web.HTTPFound("/#settings" if self.setup_mode else "/#overview")
        self._set_session_cookie(resp)
        raise resp

    async def logout(self, request: web.Request) -> web.Response:
        self._revoked.add(request.cookies.get(SESSION_COOKIE, ""))
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE)
        return resp

    async def me(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "setup_mode": self.setup_mode,
                                  "user": request["user"], "role": request["role"], **self._tunnel_info()})

    def _tunnel_info(self) -> dict:
        t = self.tunnel
        return {"public_url": self.public_url, "tunnel_enabled": self.ws.tunnel,
                "tunnel_state": t.state if t else ("error" if self.tunnel_problem else None),
                "tunnel_error": (t.error if t and t.state != "online" else None) or self.tunnel_problem}

    # ------------------------------------------------------------ Accounts (other admins)

    async def list_users(self, request: web.Request) -> web.Response:
        return web.json_response({"users": self.users.list(), "owner_password_set": bool(self.ws.password)})

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
        self._audit(f"Account **{username}** deleted")
        return web.json_response({"ok": True})

    async def change_own_password(self, request: web.Request) -> web.Response:
        if request["user"] == OWNER:
            raise ApiError("The owner password is WEB_PASSWORD – change it under Settings → Web interface.")
        data = await self._json(request)
        if await asyncio.to_thread(self.users.authenticate, request["user"],
                                   str(data.get("current") or "")) is None:
            raise ApiError("The current password is wrong.", 403)
        try:
            await asyncio.to_thread(self.users.update, request["user"], password=str(data.get("new") or ""))
        except UserError as exc:
            raise ApiError(str(exc)) from None
        resp = web.json_response({"ok": True})
        self._set_session_cookie(resp, request["user"])  # old sessions end, this one continues
        self._audit("changed their own password")
        return resp

    # ------------------------------------------------------------ Settings

    async def get_settings(self, request: web.Request) -> web.Response:
        env = read_env(self.env_path)
        fields = []
        for f in settings_schema.FIELDS:
            item = {"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "help": f.help,
                    "required": f.required, "pick": f.pick, "advanced": f.advanced,
                    "placeholder": f.placeholder, "choices": list(f.choices)}
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
            if f is None:
                continue
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
        if "WEB_PASSWORD" in updates:
            # New password applies immediately; the current session stays logged in
            self.ws.password = self.password = updates["WEB_PASSWORD"]
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
            self._set_session_cookie(resp, request["user"])
        return resp

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

    async def get_knowledge(self, request: web.Request) -> web.Response:
        path: Path = self.bot.config.support_knowledge_file
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return web.json_response({"path": str(path), "text": text})

    async def put_knowledge(self, request: web.Request) -> web.Response:
        text = str((await self._json(request)).get("text", ""))
        if len(text) > 50000:
            raise ApiError("Text too long (max. 50,000 characters).")
        path: Path = self.bot.config.support_knowledge_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Knowledge base edited")
        return web.json_response({"ok": True})

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
        return web.json_response({"enabled": self.bot.config.backup_enabled,
                                  "keep_days": self.bot.config.backup_keep_days,
                                  "backups": await asyncio.to_thread(backups.list_backups)})

    async def create_backup(self, request: web.Request) -> web.Response:
        name = await self._cog("Monitor").backup_now("manual")
        if name is None:
            raise ApiError("Backup failed – see the bot log.", 500)
        self._audit(f"Backup created: {name}")
        return web.json_response({"ok": True, "name": name})

    async def download_backup(self, request: web.Request) -> web.StreamResponse:
        path = backups.backup_path(request.match_info["name"])
        if path is None:
            raise ApiError("Backup not found.", 404)
        self._audit(f"Backup downloaded: {path.name}")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    async def botlog(self, request: web.Request) -> web.Response:
        return web.json_response({"lines": list(memory_log.records)})
