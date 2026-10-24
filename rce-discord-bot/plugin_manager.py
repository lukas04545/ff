"""Finds, loads and unloads plugins, and keeps their on/off state and settings.

Built-in plugins live in plugins/ (updated together with the bot), your own in data/plugins/ (never touched by
updates). A file in data/plugins/ with the same name as a built-in REPLACES the built-in – that's how you keep a
changed built-in across updates (delete your copy to get the built-in back). State and settings are kept in
data/plugins.json:

    {"enabled": {"bounties": true}, "settings": {"bounties": {"min_amount": 50}},
     "known": {"invites": {"intents": ["members"], "default": false}}}

Switching a plugin on/off, changing its settings or reloading it works live – no restart needed.
"""
from __future__ import annotations

import ast
import importlib
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from plugin_api import Plugin

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.plugins")

BASE_DIR = Path(__file__).resolve().parent
BUILTIN_DIR = BASE_DIR / "plugins"
BUILTIN_PACKAGE = "plugins"
USER_PACKAGE = "rce_user_plugins"
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_UPLOAD = 300_000          # bytes of source code
MAX_IMAGE = 700_000           # bytes of an uploaded picture ("image" settings)
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
KNOWN_INTENTS = ("members", "message_content")
# Main features of the bot that use the plugin engine (settings, storage, listeners) but aren't plugins for the owner:
# their own fixed place in the control panel, not on the Plugins page or in the plugin menu, can't be replaced by an
# uploaded file. Their on/off state, settings and data stay where they are (data/plugins.json, the plugin storage).
CORE_FEATURES = frozenset({"offline_protection"})


class PluginError(Exception):
    pass


def state_path_for(database_path: Path) -> Path:
    return Path(database_path).parent / "plugins.json"


def user_dir_for(database_path: Path) -> Path:
    return Path(database_path).parent / "plugins"


def files_dir_for(database_path: Path) -> Path:
    return Path(database_path).parent / "plugin_files"


def read_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled_plugin_intents(database_path: Path) -> dict[str, list[str]]:
    """Privileged intents -> enabled plugins that need them (read from data/plugins.json; no imports)."""
    state = read_state(state_path_for(database_path))
    enabled, known = state.get("enabled") or {}, state.get("known") or {}
    out: dict[str, list[str]] = {}
    for pid, meta in known.items():
        if not isinstance(meta, dict) or not enabled.get(pid, bool(meta.get("default"))):
            continue
        for intent in meta.get("intents") or []:
            if intent in KNOWN_INTENTS:
                out.setdefault(intent, []).append(f"Plugin {meta.get('name') or pid}")
    return out


@dataclass
class PluginInfo:
    id: str
    path: Path
    builtin: bool
    cls: type[Plugin] | None = None
    error: str | None = None
    mtime: float = 0.0               # file time when it was read (changed file -> read again)
    overrides: bool = False          # your own file in data/plugins/ that replaces the built-in with this name

    @property
    def module_name(self) -> str:
        return f"{BUILTIN_PACKAGE if self.builtin else USER_PACKAGE}.{self.id}"


def _short_error(exc: BaseException) -> str:
    tb = traceback.extract_tb(exc.__traceback__)
    where = next((f" ({Path(f.filename).name}, line {f.lineno})" for f in reversed(tb)
                  if "plugins" in f.filename), "")
    return f"{type(exc).__name__}: {exc}{where}"[:500]


def _plugin_class(module) -> type[Plugin]:
    found = [obj for obj in vars(module).values()
             if isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin
             and obj.__module__ == module.__name__]
    if len(found) != 1:
        raise PluginError("The file must contain exactly one class derived from Plugin "
                          f"(found {len(found)}).")
    keys = [s.key for s in found[0].SETTINGS]
    if dupes := sorted({k for k in keys if keys.count(k) > 1}):
        raise PluginError(f"Two settings use the same key: {', '.join(dupes)}.")
    return found[0]


class PluginManager:
    def __init__(self, bot: "RceBot", *, builtin_dir: Path = BUILTIN_DIR, user_dir: Path | None = None,
                 state_path: Path | None = None) -> None:
        self.bot = bot
        self.builtin_dir = Path(builtin_dir)
        self.user_dir = Path(user_dir) if user_dir else user_dir_for(bot.config.database_path)
        self.state_path = Path(state_path) if state_path else state_path_for(bot.config.database_path)
        self.files_dir = files_dir_for(bot.config.database_path)
        self.state = read_state(self.state_path)
        for key in ("enabled", "settings", "known"):
            if not isinstance(self.state.get(key), dict):
                self.state[key] = {}
        self.infos: dict[str, PluginInfo] = {}
        self.instances: dict[str, Plugin] = {}
        self.load_errors: dict[str, str] = {}
        self.startup_failures: list[tuple[str, str]] = []    # (plugin, error) of switched-on plugins at the start

    # ------------------------------------------------------------ State

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def is_enabled(self, pid: str) -> bool:
        return bool(self.state["enabled"].get(pid, self._default_on(self.infos.get(pid))))

    def _keep_on_off(self, pid: str) -> None:
        """Your copy is replacing the built-in: it keeps the on/off state the built-in had (also its "on by
        default" – known from data/plugins.json after a restart)."""
        if pid in self.state["enabled"]:
            return
        prev = self.infos.get(pid)
        if prev is not None and prev.builtin:
            on = self._default_on(prev)
        else:
            on = bool((self.state["known"].get(pid) or {}).get("default"))
        self.state["enabled"][pid] = on
        try:
            self._save()
        except OSError as exc:
            log.warning("Could not save %s: %s", self.state_path, exc)

    @staticmethod
    def _default_on(info: "PluginInfo | None") -> bool:
        """Built-in plugins (and your own copy replacing one) may be on without the owner switching them on."""
        return bool(info and info.cls and info.cls.DEFAULT_ENABLED and (info.builtin or info.overrides))

    # ------------------------------------------------------------ Control panel menu

    MENU_GROUPS = ("server", "kits", "community", "plugins", "admin")    # the sidebar categories

    def menu_entries(self) -> list[dict]:
        """Plugins with their own tab in the control panel menu, in order, each with its sidebar category:
        [{"id", "group"}]. Until the owner changes it: the built-ins that ask for a tab (Plugin.TAB), under Plugins."""
        saved = self.state.get("menu")
        if not isinstance(saved, list):
            saved = sorted(pid for pid, info in self.infos.items() if info.cls is not None and info.cls.TAB)
        out = []
        for entry in saved:                                  # older files: a plain list of plugin ids
            pid, group = (entry, "plugins") if isinstance(entry, str) else (entry.get("id"), entry.get("group"))
            if pid in self.infos and pid not in CORE_FEATURES:            # main features have a fixed place
                out.append({"id": pid, "group": group if group in self.MENU_GROUPS else "plugins"})
        return out

    def menu(self) -> list[str]:
        return [e["id"] for e in self.menu_entries()]

    def set_menu(self, entries) -> list[dict]:
        """entries: plugin ids (category Plugins) or {"id", "group"} – in menu order."""
        if not isinstance(entries, list):
            raise PluginError("Invalid menu.")
        clean, seen = [], set()
        for entry in entries:
            if isinstance(entry, str):
                entry = {"id": entry, "group": "plugins"}
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise PluginError("Invalid menu.")
            if entry["id"] not in self.infos or entry["id"] in CORE_FEATURES:
                raise PluginError(f"Unknown plugin: {entry['id']}")
            group = entry.get("group") or "plugins"
            if group not in self.MENU_GROUPS:
                raise PluginError(f"Unknown menu category: {group}")
            if entry["id"] not in seen:                          # no doubles, order kept
                seen.add(entry["id"])
                clean.append({"id": entry["id"], "group": group})
        self.state["menu"] = clean
        self._save()
        return self.menu_entries()

    def settings_of(self, pid: str) -> dict[str, Any]:
        values = self.state["settings"].get(pid)
        return dict(values) if isinstance(values, dict) else {}

    def running(self, pid: str) -> Plugin | None:
        return self.instances.get(pid)

    # ------------------------------------------------------------ Discovery

    def discover(self) -> None:
        """Finds every plugin file and reads its class (imports it). Your own file in data/plugins/ wins over a
        built-in with the same name (so an update that adds a built-in called like your plugin can't hide it)."""
        candidates: dict[str, tuple[Path, bool]] = {}
        overrides: set[str] = set()
        for builtin, folder in ((True, self.builtin_dir), (False, self.user_dir)):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.py")):
                pid = path.stem
                if pid.startswith("_"):
                    continue
                if not PLUGIN_ID.match(pid):
                    log.warning("Plugin file %s ignored: names may only use a-z, 0-9 and _.", path.name)
                    continue
                if pid in CORE_FEATURES and not builtin:
                    log.warning("%s ignored: %s is a main feature of the bot and can't be replaced.", path, pid)
                    continue
                if pid in candidates and not builtin:
                    overrides.add(pid)
                candidates[pid] = (path, builtin)
        found: dict[str, PluginInfo] = {}
        for pid, (path, builtin) in candidates.items():
            info = self.infos.get(pid)
            if info is not None and pid in self.instances:
                found[pid] = info                  # running: switched over on its next reload / restart
                continue
            if info is None or info.path != path or info.builtin != builtin:
                info = PluginInfo(pid, path, builtin)
            if pid in overrides and not info.overrides:
                log.info("Plugin %s: your own copy in %s replaces the built-in.", pid, self.user_dir)
                self._keep_on_off(pid)
            info.overrides = pid in overrides
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if info.mtime != mtime or (info.cls is None and info.error is None):
                self._import(info, fresh=info.mtime != 0.0)
            found[pid] = info
        self.infos = found
        self._remember_meta()

    def _import(self, info: PluginInfo, *, fresh: bool = False) -> None:
        info.error = None
        try:
            info.mtime = info.path.stat().st_mtime
        except OSError:
            pass
        try:
            if info.builtin and self.builtin_dir == BUILTIN_DIR:
                name = info.module_name
                module = (importlib.reload(sys.modules[name]) if fresh and name in sys.modules
                          else importlib.import_module(name))
            else:     # your own plugins (and built-ins from another folder, e.g. in tests)
                package = USER_PACKAGE if not info.builtin else "rce_other_plugins"
                module = self._import_file(f"{package}.{info.id}", info.path)
            info.cls = _plugin_class(module)
        except Exception as exc:          # a broken plugin must never stop the bot
            info.cls = None
            info.error = _short_error(exc) if not isinstance(exc, PluginError) else str(exc)
            log.warning("Plugin %s could not be read: %s", info.id, info.error)

    @staticmethod
    def _import_file(name: str, path: Path):
        package = name.rsplit(".", 1)[0]
        if package not in sys.modules:
            pkg = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(package, None, is_package=True))
            pkg.__path__ = []
            sys.modules[package] = pkg
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"Can't load {path.name}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    def _remember_meta(self) -> None:
        """Keeps name/intents/default in the state file, so the bot knows the needed intents before importing."""
        known = {}
        for pid, info in self.infos.items():
            if info.cls is not None:
                known[pid] = {"name": info.cls.NAME, "intents": [i for i in info.cls.INTENTS if i in KNOWN_INTENTS],
                              "default": self._default_on(info)}
        if known != self.state["known"]:
            self.state["known"] = known
            try:
                self._save()
            except OSError as exc:
                log.warning("Could not save %s: %s", self.state_path, exc)

    # ------------------------------------------------------------ Loading

    async def load_all(self) -> None:
        """Bot start: loads every plugin that is on (no command sync – the bot syncs once afterwards)."""
        self.discover()
        for pid in self.infos:
            if self.is_enabled(pid):
                await self._start(pid)
        if self.instances:
            log.info("Plugins running: %s", ", ".join(sorted(self.instances)))
        # Switched-on plugins that couldn't start (e.g. written for an older bot version) – reported once ready
        self.startup_failures = [(pid, info.error or self.load_errors.get(pid) or "unknown error")
                                 for pid, info in sorted(self.infos.items())
                                 if self.state["enabled"].get(pid) and pid not in self.instances]

    async def _start(self, pid: str) -> bool:
        info = self.infos.get(pid)
        if info is None or info.cls is None:
            return False
        if pid in self.instances:
            return True
        self.load_errors.pop(pid, None)
        try:
            cog_name = info.cls.__cog_name__
            if self.bot.get_cog(cog_name) is not None:
                raise PluginError(f"Another part of the bot is already called {cog_name} – rename the class.")
            plugin = info.cls(self.bot, pid, self)
            await self.bot.add_cog(plugin)
        except Exception as exc:
            self.load_errors[pid] = _short_error(exc) if not isinstance(exc, PluginError) else str(exc)
            log.warning("Plugin %s could not be started: %s", pid, self.load_errors[pid])
            return False
        self.instances[pid] = plugin
        return True

    async def _stop(self, pid: str) -> None:
        plugin = self.instances.pop(pid, None)
        if plugin is None:
            return
        try:
            await self.bot.remove_cog(plugin.__cog_name__)
        except Exception:
            log.exception("Stopping plugin %s failed", pid)

    async def unload_all(self) -> None:
        for pid in list(self.instances):
            await self._stop(pid)

    async def _sync(self) -> None:
        sync = getattr(self.bot, "sync_commands", None)
        if sync is None or not self.bot.is_ready():
            return
        try:
            await sync()
        except Exception as exc:
            log.warning("Slash commands could not be updated: %s", exc)

    async def set_enabled(self, pid: str, enabled: bool) -> dict:
        info = self.infos.get(pid)
        if info is None:
            raise PluginError("Unknown plugin.")
        if enabled and info.cls is None:
            raise PluginError(f"The plugin has an error and can't be switched on: {info.error}")
        self.state["enabled"][pid] = bool(enabled)
        self._save()
        changed = False
        if enabled and pid not in self.instances:
            changed = await self._start(pid)
        elif not enabled and pid in self.instances:
            await self._stop(pid)
            changed = True
        if changed:
            await self._sync()
        return self.describe(pid)

    async def reload(self, pid: str) -> dict:
        """Reads the plugin file again (after an edit or upload) and restarts it if it is on."""
        info = self.infos.get(pid)
        if info is None:
            raise PluginError("Unknown plugin.")
        was_running = pid in self.instances
        await self._stop(pid)
        self._import(info, fresh=True)
        self._remember_meta()
        if self.is_enabled(pid) and info.cls is not None:
            await self._start(pid)
        if was_running or pid in self.instances:
            await self._sync()
        return self.describe(pid)

    # ------------------------------------------------------------ Settings

    async def save_settings(self, pid: str, values: dict) -> dict[str, str]:
        """Validates and saves settings. Returns {key: error} (empty = saved)."""
        info = self.infos.get(pid)
        if info is None or info.cls is None:
            raise PluginError("Unknown plugin or the plugin has an error.")
        specs = {s.key: s for s in info.cls.SETTINGS}
        current = self.settings_of(pid)
        errors: dict[str, str] = {}
        for key, raw in (values or {}).items():
            spec = specs.get(key)
            if spec is None or spec.kind == "image":       # pictures are uploaded with save_file()
                continue
            try:
                value = spec.clean(raw)
            except ValueError as exc:
                errors[key] = str(exc)
                continue
            if value is None or value == spec.default:
                current.pop(key, None)
            else:
                current[key] = value
        if errors:
            return errors
        self.state["settings"][pid] = current
        self._save()
        plugin = self.instances.get(pid)
        if plugin is not None:
            try:
                await plugin.on_settings_changed()
            except Exception:
                log.exception("Plugin %s: applying settings failed", pid)
        return {}

    # ------------------------------------------------------------ Uploaded pictures ("image" settings)

    def _image_spec(self, pid: str, key: str):
        info = self.infos.get(pid)
        spec = next((s for s in info.cls.SETTINGS if s.key == key), None) if info and info.cls else None
        if spec is None or spec.kind != "image":
            raise PluginError("Unknown picture setting.")
        return spec

    def file_path(self, pid: str, key: str) -> Path | None:
        name = self.settings_of(pid).get(key)
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+\.(png|jpg|webp|gif)", name):
            return None
        path = self.files_dir / pid / name
        return path if path.is_file() else None

    async def save_file(self, pid: str, key: str, data: bytes) -> dict:
        from web.item_images import image_type
        self._image_spec(pid, key)
        kind = image_type(data or b"")
        if kind not in IMAGE_EXT:
            raise PluginError("Please choose a PNG, JPG, WebP or GIF picture.")
        if len(data) > MAX_IMAGE:
            raise PluginError("The picture is too big (max. 700 KB).")
        folder = self.files_dir / pid
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{key}.*"):
            old.unlink(missing_ok=True)
        name = f"{key}{IMAGE_EXT[kind]}"
        tmp = folder / (name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, folder / name)
        await self._set_value(pid, key, name)
        return self.describe(pid)

    async def delete_file(self, pid: str, key: str) -> dict:
        self._image_spec(pid, key)
        for old in (self.files_dir / pid).glob(f"{key}.*"):
            old.unlink(missing_ok=True)
        await self._set_value(pid, key, None)
        return self.describe(pid)

    async def _set_value(self, pid: str, key: str, value) -> None:
        current = self.settings_of(pid)
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
        self.state["settings"][pid] = current
        self._save()
        plugin = self.instances.get(pid)
        if plugin is not None:
            try:
                await plugin.on_settings_changed()
            except Exception:
                log.exception("Plugin %s: applying settings failed", pid)

    # ------------------------------------------------------------ Your own plugins

    async def install(self, filename: str, code: str) -> dict:
        """Saves an uploaded plugin file in data/plugins/ (switched off) and reads it."""
        pid = Path(str(filename or "")).name.removesuffix(".py").strip().lower()
        if not PLUGIN_ID.match(pid):
            raise PluginError("File name: use a-z, 0-9 and _ only (2–40 characters), e.g. my_plugin.py.")
        if pid in CORE_FEATURES:
            raise PluginError(f"{pid} is a main feature of the bot – pick another file name.")
        existing = self.infos.get(pid)
        data = (code or "").encode("utf-8")
        if not data.strip():
            raise PluginError("The file is empty.")
        if len(data) > MAX_UPLOAD:
            raise PluginError("The file is too big (max. 300 KB).")
        try:
            ast.parse(code, filename=f"{pid}.py")
        except SyntaxError as exc:
            raise PluginError(f"Python error in line {exc.lineno}: {exc.msg}") from None
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{pid}.py"
        backup = path.read_bytes() if path.exists() else None
        if existing is not None and existing.builtin:
            self._keep_on_off(pid)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        await self._stop(pid)
        os.replace(tmp, path)
        info = existing if existing is not None and not existing.builtin else PluginInfo(pid, path, False)
        info.overrides = (self.builtin_dir / f"{pid}.py").is_file()       # same name as a built-in: yours wins
        self._import(info, fresh=True)
        if info.cls is None:
            error = info.error
            if backup is not None:          # keep the old, working version
                path.write_bytes(backup)
                self._import(info, fresh=True)
                if info.cls is not None and self.is_enabled(pid):
                    await self._start(pid)
            else:
                path.unlink(missing_ok=True)
                if existing is not None and existing.builtin:      # the built-in stays as it was
                    self.infos[pid] = existing
                    if existing.cls is not None and self.is_enabled(pid):
                        await self._start(pid)
                else:
                    self.infos.pop(pid, None)
            raise PluginError(f"The plugin can't be used: {error}")
        self.infos[pid] = info
        self._remember_meta()
        if self.is_enabled(pid):
            await self._start(pid)
        await self._sync()
        return self.describe(pid)

    async def uninstall(self, pid: str) -> None:
        info = self.infos.get(pid)
        if info is None:
            raise PluginError("Unknown plugin.")
        if info.builtin:
            raise PluginError("Built-in plugins can't be deleted – switch them off instead.")
        was_running = pid in self.instances
        await self._stop(pid)
        info.path.unlink(missing_ok=True)
        sys.modules.pop(info.module_name, None)
        self.infos.pop(pid, None)
        if info.overrides:
            # Your copy of a built-in: the built-in comes back with the same on/off state and settings
            self.discover()
            if self.is_enabled(pid) and pid in self.infos:
                await self._start(pid)
        else:
            shutil.rmtree(self.files_dir / pid, ignore_errors=True)
            self.state["enabled"].pop(pid, None)
            self.state["settings"].pop(pid, None)
            self._remember_meta()
        self._save()
        if was_running or pid in self.instances:
            await self._sync()

    # ------------------------------------------------------------ Info

    def missing_intents(self, pid: str) -> list[str]:
        info = self.infos.get(pid)
        if info is None or info.cls is None:
            return []
        return [i for i in info.cls.INTENTS if i in KNOWN_INTENTS and not getattr(self.bot.intents, i, False)]

    def describe(self, pid: str) -> dict:
        info = self.infos[pid]
        cls = info.cls
        plugin = self.instances.get(pid)
        values = self.settings_of(pid)
        out = {"id": pid, "builtin": info.builtin, "overrides": info.overrides, "enabled": self.is_enabled(pid),
               "running": plugin is not None,
               "error": info.error or self.load_errors.get(pid), "file": info.path.name,
               "name": cls.NAME if cls else pid, "description": cls.DESCRIPTION if cls else "",
               "version": cls.VERSION if cls else "", "icon": cls.ICON if cls else "🧩", "tab": cls.TAB if cls else None, "core": pid in CORE_FEATURES,
               "in_menu": pid in self.menu(),
               "author": cls.AUTHOR if cls else "", "intents": list(cls.INTENTS) if cls else [],
               "missing_intents": self.missing_intents(pid), "public": bool(cls and hasattr(cls, "web_public")),
               "public_path": (cls.PUBLIC_PATH or f"/p/{pid}") if cls and hasattr(cls, "web_public") else None,
               "settings": [{**s.describe(), "value": values.get(s.key, s.default)} for s in (cls.SETTINGS if cls else ())],
               "problems": [], "status": {}}
        if plugin is not None:
            for key, fn in (("problems", plugin.problems), ("status", plugin.status)):
                try:
                    out[key] = fn()
                except Exception as exc:
                    log.warning("Plugin %s: %s() failed: %s", pid, fn.__name__, exc)
        return out

    def describe_all(self) -> list[dict]:
        """Everything for the Plugins page – main features (CORE_FEATURES) have their own page instead."""
        return [self.describe(pid) for pid in sorted(self.infos, key=lambda p: (not self.infos[p].builtin, p))
                if pid not in CORE_FEATURES]

    def profile_fields(self, member) -> list[tuple[str, str]]:
        out = []
        for pid, plugin in sorted(self.instances.items()):
            try:
                out.extend(plugin.profile_fields(member) or [])
            except Exception as exc:
                log.warning("Plugin %s: profile fields failed: %s", pid, exc)
        return out
