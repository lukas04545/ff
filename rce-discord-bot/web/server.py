"""Webinterface: läuft im selben Prozess wie der Bot (aiohttp, keine Zusatzpakete).

Sicherheit:
  * Standardmäßig nur auf 127.0.0.1 erreichbar (nur dieser PC). Für Zugriff aus dem
    Heimnetz WEB_HOST=0.0.0.0 setzen – dann unbedingt ein starkes WEB_PASSWORD wählen.
    NICHT per Portfreigabe ins Internet stellen (kein HTTPS).
  * Login per Passwort oder Einmal-Link aus dem Bot-Fenster; signiertes Session-Cookie
    (HttpOnly, SameSite=Strict) – übersteht Bot-Neustarts, wird bei Passwortwechsel ungültig.
  * Login-Bremse, Host-Prüfung gegen DNS-Rebinding (bei Betrieb nur auf diesem PC).
  * Schreibende Anfragen brauchen den Header X-Requested-With (Schutz gegen CSRF).
  * Jede Aktion wird im Admin-Log-Channel protokolliert.
"""
from __future__ import annotations

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
from typing import TYPE_CHECKING, Callable

import aiohttp
import discord
from aiohttp import web

import settings_schema
from config import ConfigError, load_config
from envfile import read_env, update_env
from rce.diagnostics import diagnose_rcon

from logbuffer import memory_log
from rce import items as itemlist
from rce import kits as kitparse
from rce import quickchat
from rce.rcon_client import RconError
from storage import LEADERBOARD_COLUMNS
from support.deepseek import DeepSeekError
from utils import sanitize_ingame_text, sanitize_player_name, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "rce_session"
SESSION_TTL = 12 * 3600
LOGIN_ATTEMPTS_PER_MINUTE = 5
CSRF_HEADER = "X-Requested-With"
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
# Bot-Rechte für den Einladungslink (alle Funktionen dieses Bots)
INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
    add_reactions=True, manage_channels=True, manage_roles=True, moderate_members=True, kick_members=True,
    ban_members=True, manage_messages=True, mention_everyone=True,
).value


@dataclass
class WebSettings:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""
    open_browser: bool = True


def web_settings_from_env(path: Path = ENV_PATH) -> WebSettings:
    """WEB_*-Werte direkt aus der .env – auch wenn der Rest der Konfiguration noch unvollständig ist."""
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
                       open_browser=flag("WEB_OPEN_BROWSER", True))


class WebInterface:
    """bot=None startet den Einrichtungsmodus: nur Login und Einstellungen, ohne Discord/RCON."""

    def __init__(self, bot: "RceBot | None", *, web_settings: WebSettings | None = None,
                 setup_error: str | None = None, on_config_saved: Callable[[], None] | None = None,
                 env_path: Path = ENV_PATH, secret_path: Path = Path("data") / "web_secret.key") -> None:
        self.bot = bot
        self.setup_mode = bot is None
        self.setup_error = setup_error
        self.on_config_saved = on_config_saved
        self.env_path = env_path
        if web_settings is None:
            cfg = bot.config
            web_settings = WebSettings(True, cfg.web_host, cfg.web_port, cfg.web_password, cfg.web_open_browser)
        self.ws = web_settings
        self.password = web_settings.password
        if not self.password:
            # Ohne festes Passwort: Zufallspasswort + Einmal-Link (siehe start())
            self.password = secrets.token_urlsafe(9)
        self.login_token = secrets.token_urlsafe(24)
        self._secret = self._load_secret(secret_path)
        self._revoked: set[str] = set()
        self._login_attempts: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._runner: web.AppRunner | None = None
        self.restart_required = False
        self.app = self._build_app()

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        """Geheimer Schlüssel für die Session-Cookies – bleibt über Neustarts gleich."""
        try:
            return bytes.fromhex(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            secret = secrets.token_bytes(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secret.hex(), encoding="utf-8")
            except OSError:
                log.warning("Konnte %s nicht speichern – Logins gelten nur bis zum Neustart.", path)
            return secret

    # ------------------------------------------------------------ Start/Stopp

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
        if self.setup_mode:
            r.add_post("/api/setup/continue", self.setup_continue)
            return app
        r.add_post("/api/restart", self.restart)
        r.add_get("/api/console", self.console)
        r.add_post("/api/command", self.command)
        r.add_post("/api/say", self.say)
        r.add_post("/api/players/{action:kick|ban|unban}", self.player_action)
        r.add_get("/api/kits", self.kits)
        r.add_get("/api/kits/ingame/{name}", self.ingame_kit_info)
        r.add_post("/api/kits/give", self.give_kit)
        r.add_post("/api/custom-kits", self.create_custom_kit)
        r.add_delete("/api/custom-kits/{name}", self.delete_custom_kit)
        r.add_post("/api/custom-kits/{name}/items", self.add_custom_item)
        r.add_delete("/api/custom-kits/{name}/items/{item_id:\\d+}", self.remove_custom_item)
        r.add_post("/api/autokits", self.create_autokit)
        r.add_post("/api/autokits/reset", self.reset_autokits)
        r.add_post("/api/autokits/{rule_id:\\d+}/toggle", self.toggle_autokit)
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
        return app

    async def start(self, *, open_browser: bool | None = None) -> bool:
        ws = self.ws
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, ws.host, ws.port).start()
        except OSError as exc:
            log.error("Webinterface konnte Port %s nicht öffnen (%s) – anderen WEB_PORT wählen.", ws.port, exc)
            return False
        shown_host = "localhost" if ws.host in ("127.0.0.1", "0.0.0.0", "") else ws.host
        url = f"http://{shown_host}:{ws.port}"
        login_url = f"{url}/auth?token={self.login_token}"
        if self.setup_mode:
            log.warning("EINRICHTUNG: Öffne %s im Browser, um die fehlenden Einstellungen einzutragen.", login_url)
        else:
            log.info("Webinterface läuft: %s", url)
            if not ws.password:
                log.warning("Kein WEB_PASSWORD gesetzt – Login über diesen Link: %s  (oder Passwort: %s)",
                            login_url, self.password)
        if ws.host not in ("127.0.0.1", "localhost"):
            log.warning("Webinterface ist im Netzwerk erreichbar (WEB_HOST=%s). Nicht ins Internet freigeben!",
                        ws.host)
        if ws.open_browser if open_browser is None else open_browser:
            try:
                webbrowser.open(login_url)
            except Exception:  # z. B. kein Browser auf einem Server
                pass
        return True

    async def stop(self) -> None:
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
            log.exception("Fehler im Webinterface (%s %s)", request.method, request.path)
            return web.json_response({"error": "Interner Fehler – Details im Bot-Log."}, status=500)

    @web.middleware
    async def _host_middleware(self, request: web.Request, handler):
        # Schutz gegen DNS-Rebinding: Läuft das Webinterface nur lokal, muss der Host auch lokal sein.
        if self.ws.host in ("127.0.0.1", "localhost"):
            host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") \
                else (request.host or "").split("]")[0] + "]"
            if host.lower() not in LOCAL_HOSTS:
                raise web.HTTPForbidden(text="Unbekannter Host.")
        return await handler(request)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/"):
            return await handler(request)
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "rce-web":
            raise ApiError("Ungültige Anfrage.", 403)
        if path == "/api/login":
            return await handler(request)
        if not self._valid_session(request):
            raise ApiError("Nicht angemeldet.", 401)
        return await handler(request)

    def _sign(self, expires: int) -> str:
        # Das konfigurierte Passwort fließt ein: Passwortwechsel meldet alle ab.
        msg = f"{expires}:{self.ws.password}".encode()
        return f"{expires}.{hmac.new(self._secret, msg, hashlib.sha256).hexdigest()}"

    def _new_session(self) -> str:
        return self._sign(int(time.time()) + SESSION_TTL)

    def _valid_session(self, request: web.Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token or token in self._revoked or "." not in token:
            return False
        expires_raw = token.split(".", 1)[0]
        if not expires_raw.isdigit() or int(expires_raw) < time.time():
            return False
        return hmac.compare_digest(token, self._sign(int(expires_raw)))

    def _set_session_cookie(self, resp: web.StreamResponse) -> None:
        resp.set_cookie(SESSION_COOKIE, self._new_session(), max_age=SESSION_TTL, httponly=True, samesite="Strict")

    # ------------------------------------------------------------ Hilfen

    async def _json(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise ApiError("Ungültiges JSON.") from None
        if not isinstance(data, dict):
            raise ApiError("Ungültiges JSON.")
        return data

    @staticmethod
    def _text(data: dict, key: str, *, required: bool = True, limit: int = 200) -> str:
        value = str(data.get(key) or "").strip()[:limit]
        if required and not value:
            raise ApiError(f"Feld „{key}“ fehlt.")
        return value

    @staticmethod
    def _int(data: dict, key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            raise ApiError(f"Feld „{key}“ muss eine Zahl sein.") from None
        return max(lo, min(hi, value))

    def _audit(self, text: str) -> None:
        if self.bot is not None:
            self.bot.audit(f"🌐 Webinterface: {text}")
        log.info("Web-Aktion: %s", text)

    def _cog(self, name: str):
        cog = self.bot.get_cog(name)
        if cog is None:
            raise ApiError(f"Modul {name} ist nicht geladen.", 503)
        return cog

    # ------------------------------------------------------------ Seiten & Login

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def login(self, request: web.Request) -> web.Response:
        ip = request.remote or "?"
        attempts = self._login_attempts[ip]
        now = time.time()
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS_PER_MINUTE:
            raise ApiError("Zu viele Versuche – bitte eine Minute warten.", 429)
        attempts.append(now)

        data = await self._json(request)
        if not hmac.compare_digest(str(data.get("password", "")).encode(), self.password.encode()):
            log.warning("Fehlgeschlagener Web-Login von %s", ip)
            raise ApiError("Falsches Passwort.", 401)
        attempts.clear()
        resp = web.json_response({"ok": True})
        self._set_session_cookie(resp)
        log.info("Web-Login von %s", ip)
        return resp

    async def auth_link(self, request: web.Request) -> web.Response:
        """Einmal-Link aus dem Bot-Fenster: meldet an und öffnet die passende Seite."""
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
        return web.json_response({"ok": True, "setup_mode": self.setup_mode})

    # ------------------------------------------------------------ Einstellungen

    async def get_settings(self, request: web.Request) -> web.Response:
        env = read_env(self.env_path)
        fields = []
        for f in settings_schema.FIELDS:
            item = {"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "help": f.help,
                    "required": f.required, "pick": f.pick, "advanced": f.advanced,
                    "placeholder": f.placeholder, "choices": list(f.choices)}
            if f.kind == "secret":
                item["is_set"] = bool((env.get(f.key) or "").strip())  # Geheimnisse nie ausliefern
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
            "config_error": config_error, "restart_required": self.restart_required,
        })

    async def put_settings(self, request: web.Request) -> web.Response:
        values = (await self._json(request)).get("values")
        if not isinstance(values, dict):
            raise ApiError("Ungültige Daten.")
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
                continue  # leer = unverändert lassen
            if f.kind == "ids":
                value = ",".join(p.strip() for p in value.split(",") if p.strip())
            if value == (current.get(key) or ""):
                continue  # unverändert – nichts schreiben
            if error := settings_schema.validate(key, value):
                errors[key] = error
            else:
                updates[key] = value
        if errors:
            return web.json_response({"error": "Bitte die markierten Felder korrigieren.", "errors": errors},
                                     status=400)
        if updates:
            update_env(self.env_path, updates, template=ENV_TEMPLATE)
            self._audit("Einstellungen geändert: " + ", ".join(sorted(updates)))
        if "WEB_PASSWORD" in updates:
            # Neues Passwort gilt sofort; die aktuelle Sitzung bleibt angemeldet
            self.ws.password = self.password = updates["WEB_PASSWORD"]
        try:
            load_config(self.env_path)
        except ConfigError as exc:
            resp = web.json_response({"ok": True, "saved": sorted(updates), "config_error": str(exc)})
        else:
            if self.setup_mode:
                resp = web.json_response({"ok": True, "saved": sorted(updates), "starting": True})
                if self.on_config_saved:
                    self.on_config_saved()
            else:
                self.restart_required = self.restart_required or bool(updates)
                resp = web.json_response({"ok": True, "saved": sorted(updates),
                                          "restart_required": self.restart_required})
        if "WEB_PASSWORD" in updates:
            self._set_session_cookie(resp)
        return resp

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
            raise ApiError("Kein Bot-Token eingetragen.")
        headers = {"Authorization": f"Bot {token}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
                    if resp.status == 401:
                        return web.json_response({"ok": False, "error": "Discord lehnt den Token ab – im Developer "
                                                  "Portal unter Bot → Reset Token einen neuen erzeugen."})
                    if resp.status != 200:
                        return web.json_response({"ok": False, "error": f"Discord antwortet mit HTTP {resp.status}."})
                    user = await resp.json()
                async with session.get(f"{DISCORD_API}/applications/@me", headers=headers) as resp:
                    app_info = await resp.json() if resp.status == 200 else {}
        except (aiohttp.ClientError, TimeoutError) as exc:
            return web.json_response({"ok": False, "error": f"Discord nicht erreichbar: {exc}"})
        flags = int(app_info.get("flags") or 0)
        app_id = app_info.get("id") or user.get("id")
        return web.json_response({
            "ok": True,
            "bot_name": user.get("username"),
            "app_id": app_id,
            # Application-Flags: GATEWAY_GUILD_MEMBERS(_LIMITED) = 1<<14/1<<15, MESSAGE_CONTENT(_LIMITED) = 1<<18/1<<19
            "intents": {"members": bool(flags & (1 << 14 | 1 << 15)),
                        "message_content": bool(flags & (1 << 18 | 1 << 19))} if app_info else None,
            "invite_url": (f"https://discord.com/oauth2/authorize?client_id={app_id}"
                           f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}") if app_id else None,
        })

    async def settings_discord_options(self, request: web.Request) -> web.Response:
        """Channels/Rollen/Server zur Auswahl – nur wenn der Bot mit Discord verbunden ist."""
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
        self._audit("Bot-Neustart angefordert")
        self.bot.request_restart()
        return web.json_response({"ok": True})

    async def setup_continue(self, request: web.Request) -> web.Response:
        """Einrichtung: Bot mit der gespeicherten Konfiguration starten (z. B. nach Intents-Freigabe)."""
        try:
            load_config(self.env_path)
        except ConfigError as exc:
            raise ApiError(f"Noch nicht vollständig: {exc}")
        if self.on_config_saved:
            self.on_config_saved()
        return web.json_response({"ok": True, "starting": True})

    # ------------------------------------------------------------ Status & Konsole

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
            "rcon_target": f"{cfg.rcon_host}:{cfg.rcon_port}",
            "discord_ready": bot.is_ready(),
            "discord_user": str(bot.user) if bot.user else None,
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
            raise ApiError("Beliebige RCON-Befehle sind deaktiviert (ALLOW_RAW_RCON=false).", 403)
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
        self._audit(f"{action.capitalize()} `{name}`" + (f" – Grund: {reason}" if reason else ""))
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
        return web.json_response({"ingame": ingame, "custom": custom, "rules": rules,
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
            raise ApiError("Spieler fehlt.")
        player = sanitize_player_name(player)
        cfg = self.bot.config

        if kit_type == "custom":
            custom = self._cog("CustomKits")
            if self.bot.db.custom_kit(kit) is None:
                raise ApiError("Custom Kit nicht gefunden.", 404)
            targets = sorted(self.bot.online_players) if everyone else [player]
            ok = 0
            for target in targets:
                sent, total = await custom.deliver(kit, target)
                if sent:
                    ok += 1
                    self.bot.db.record_kit_claim(None, kit, target, "web (custom)")
            self._audit(f"Custom Kit **{kit}** an {'alle' if everyone else player} ({ok} Spieler)")
            return web.json_response({"ok": True, "players": ok})

        if everyone:
            await self.bot.rcon.command(cfg.kit_give_all_template.format(kit=kit), expect_response=False)
        else:
            await self.bot.rcon.command(cfg.kit_give_template.format(kit=kit, name=player), expect_response=False)
            self.bot.db.record_kit_claim(None, kit, player, "web")
        self._audit(f"Kit **{kit}** an {'alle' if everyone else player}")
        return web.json_response({"ok": True})

    async def create_custom_kit(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        name = kitparse.clean_arg(self._text(data, "name", limit=40), limit=40)
        description = self._text(data, "description", required=False, limit=300) or None
        if not name:
            raise ApiError("Ungültiger Name.")
        if not self.bot.db.create_custom_kit(name, description, "Webinterface"):
            raise ApiError("Ein Custom Kit mit diesem Namen gibt es schon.", 409)
        self._audit(f"Custom Kit **{name}** erstellt")
        return web.json_response({"ok": True})

    async def delete_custom_kit(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if self.bot.db.custom_kit(name) is None:
            raise ApiError("Custom Kit nicht gefunden.", 404)
        rules = self.bot.db.delete_custom_kit(name)
        self._audit(f"Custom Kit **{name}** gelöscht ({rules} Autokit-Regeln)")
        return web.json_response({"ok": True, "rules_deleted": rules})

    async def add_custom_item(self, request: web.Request) -> web.Response:
        from cogs.custom_kits import MAX_ITEMS_PER_KIT
        name = request.match_info["name"]
        kit = self.bot.db.custom_kit(name)
        if kit is None:
            raise ApiError("Custom Kit nicht gefunden.", 404)
        data = await self._json(request)
        short = kitparse.clean_arg(self._text(data, "item", limit=64)).replace(" ", "").lower()
        amount = self._int(data, "amount", 1, 1, 100000)
        if len(self.bot.db.custom_kit_items(kit["name"])) >= MAX_ITEMS_PER_KIT:
            raise ApiError(f"Höchstens {MAX_ITEMS_PER_KIT} Items pro Kit.")
        self.bot.db.add_custom_kit_item(kit["name"], short, amount)
        self._audit(f"Custom Kit **{kit['name']}** + `{short}` × {amount}")
        return web.json_response({"ok": True, "known_item": short in itemlist.COMMON_ITEMS})

    async def remove_custom_item(self, request: web.Request) -> web.Response:
        name, item_id = request.match_info["name"], int(request.match_info["item_id"])
        if not self.bot.db.remove_custom_kit_item(name, item_id):
            raise ApiError("Item nicht gefunden.", 404)
        self._audit(f"Custom Kit **{name}** – Item #{item_id} entfernt")
        return web.json_response({"ok": True})

    async def create_autokit(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        kit = kitparse.clean_arg(self._text(data, "kit"))
        kit_type = "custom" if data.get("kit_type") == "custom" else "ingame"
        trigger = data.get("trigger")
        if trigger not in ("respawn", "quickchat"):
            raise ApiError("Auslöser muss respawn oder quickchat sein.")
        phrase = self._text(data, "phrase", required=trigger == "quickchat") or None
        if kit_type == "custom":
            row = self.bot.db.custom_kit(kit)
            if row is None:
                raise ApiError("Custom Kit nicht gefunden.", 404)
            kit = row["name"]
        rule_id = self.bot.db.add_kit_rule(
            kit, trigger, phrase if trigger == "quickchat" else None,
            self._int(data, "cooldown", 0, 0, 525600), self._int(data, "max_claims", 0, 0, 10000),
            "Webinterface", kit_type)
        self._audit(f"Autokit #{rule_id} angelegt ({kit}, {trigger})")
        return web.json_response({"ok": True, "id": rule_id})

    async def toggle_autokit(self, request: web.Request) -> web.Response:
        rule_id = int(request.match_info["rule_id"])
        enabled = bool((await self._json(request)).get("enabled"))
        if not self.bot.db.set_kit_rule_enabled(rule_id, enabled):
            raise ApiError("Regel nicht gefunden.", 404)
        self._audit(f"Autokit #{rule_id} {'aktiviert' if enabled else 'pausiert'}")
        return web.json_response({"ok": True})

    async def delete_autokit(self, request: web.Request) -> web.Response:
        rule_id = int(request.match_info["rule_id"])
        if not self.bot.db.delete_kit_rule(rule_id):
            raise ApiError("Regel nicht gefunden.", 404)
        self._audit(f"Autokit #{rule_id} gelöscht")
        return web.json_response({"ok": True})

    async def reset_autokits(self, request: web.Request) -> web.Response:
        count = self.bot.db.reset_kit_claims()
        self._audit(f"Autokit-Verlauf zurückgesetzt ({count} Einträge)")
        return web.json_response({"ok": True, "count": count})

    async def meta(self, request: web.Request) -> web.Response:
        """Auswahllisten für Formulare: Quick-Chat-Phrasen und Item-Vorschläge."""
        return web.json_response({
            "phrases": [{"raw": raw, "text": text} for raw, text in quickchat.all_phrases().items()],
            "items": [{"short": s, "label": label} for s, label in itemlist.COMMON_ITEMS.items()],
        })

    # ------------------------------------------------------------ Tickets, Statistik, KI

    async def tickets(self, request: web.Request) -> web.Response:
        status = request.query.get("status") or None
        rows = self.bot.db.tickets(status=status if status in ("open", "closed") else None)
        return web.json_response({"tickets": [{k: r[k] for k in r.keys() if k != "transcript"} for r in rows]})

    async def ticket_detail(self, request: web.Request) -> web.Response:
        row = self.bot.db.ticket(int(request.match_info["ticket_id"]))
        if row is None:
            raise ApiError("Ticket nicht gefunden.", 404)
        return web.json_response(_row(row))

    async def leaderboard(self, request: web.Request) -> web.Response:
        category = request.query.get("category", "kills")
        if category not in LEADERBOARD_COLUMNS:
            raise ApiError("Unbekannte Kategorie.")
        return web.json_response({"rows": [dict(r) for r in self.bot.db.leaderboard(category, limit=50)]})

    async def get_knowledge(self, request: web.Request) -> web.Response:
        path: Path = self.bot.config.support_knowledge_file
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return web.json_response({"path": str(path), "text": text})

    async def put_knowledge(self, request: web.Request) -> web.Response:
        text = str((await self._json(request)).get("text", ""))
        if len(text) > 50000:
            raise ApiError("Text zu lang (max. 50.000 Zeichen).")
        path: Path = self.bot.config.support_knowledge_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Wissensbasis bearbeitet")
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
            raise ApiError("Text zu lang (max. 20.000 Zeichen).")
        path: Path = self.bot.config.rules_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._audit("Regeln bearbeitet")
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
            raise ApiError("Giveaway nicht gefunden.", 404)
        cog = self._cog("Giveaways")
        if action == "end" and row["ended"]:
            raise ApiError("Giveaway ist schon beendet.")
        if action == "reroll" and not row["ended"]:
            raise ApiError("Nur beendete Giveaways können neu ausgelost werden.")
        if not self.bot.is_ready():
            raise ApiError("Discord ist noch nicht verbunden.", 503)
        winners = await cog.finish(row, reroll=action == "reroll")
        self._audit(f"Giveaway #{row['id']} {'beendet' if action == 'end' else 'neu ausgelost'}")
        return web.json_response({"ok": True, "winners": [str(w) for w in winners]})

    async def botlog(self, request: web.Request) -> web.Response:
        return web.json_response({"lines": list(memory_log.records)})
