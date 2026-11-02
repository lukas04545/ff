"""Panel themes ("skins") the owner picks for each account – Users page → Panel themes.

modern: the default look – midnight base, aurora colours, gradient accents, a colour per stat card.
rust: charcoal, Rust orange, striped card headers. rustgame: styled after the game Rust's own
interface. rustmetal: rusty metal and smoked glass. liquid: liquid glass. waterfall: liquid glass with a waterfall
behind semi-transparent panels. ember: dark graphite with Rust orange. neon: the earlier violet/teal look. jumpscare: a dark horror look with a scary face and scream now and then. retro: an early-2000s
home page with 20 popup ads. The admin can't change their own theme; only the owner can.
Saved in data/web_themes.json (username in lower case -> theme). Accounts without a choice get the default: on a hosted
server the platform owner sets it for every server (owner panel → Settings → Default panel theme), which the control
plane writes to <RCE_HOME>/panel_default_theme – read live, so no restart is needed.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

THEMES = {"modern": "Modern ✨ (default)", "rust": "Rust", "rustgame": "Rust game UI", "rustmetal": "Rusty metal",
          "liquid": "Liquid glass", "waterfall": "Liquid glass + waterfall", "ember": "Ember", "neon": "Neon",
          "jumpscare": "Jump scares 👻", "retro": "Early 2000s 💾"}
DEFAULT = "modern"
LEGACY = {"glass": "rust"}                 # old theme names -> current ones
DEFAULT_FILE = "panel_default_theme"


def default_file() -> Path | None:
    home = os.environ.get("RCE_HOME")
    return Path(home) / DEFAULT_FILE if home else None


def write_default(home: Path, theme: str) -> None:
    """Control plane: the global default theme for one hosted server (unknown names fall back to DEFAULT)."""
    theme = theme if theme in THEMES else DEFAULT
    path = Path(home) / DEFAULT_FILE
    try:
        if path.read_text(encoding="utf-8").strip() == theme:
            return
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(theme, encoding="utf-8")
    os.replace(tmp, path)
# Early 2000s: popup ads at login, how often (minutes, 0 = never) and how many come back, at most this many open
POPUP_OPTIONS = {"popups_start": (20, 0, 100), "popups_every": (3, 0, 120), "popups_wave": (2, 1, 50),
                 "popups_max": (6, 1, 100)}            # key: (default, min, max)


class ThemeStore:
    def __init__(self, path: Path, default_path: Path | None = None) -> None:
        self.path = Path(path)
        self.default_path = default_path
        self._default = (None, DEFAULT)                  # (mtime, theme) of default_path
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data = {k: LEGACY.get(v, v) for k, v in data.items()} if isinstance(data, dict) else data
            self._themes = {str(k).lower(): str(v) for k, v in data.items() if v in THEMES} \
                if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._themes = {}
        self.options_path = self.path.with_name(self.path.stem + "_options.json")
        try:
            saved = json.loads(self.options_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self._options = self._clean(saved if isinstance(saved, dict) else {})

    @staticmethod
    def _clean(values: dict, base: dict | None = None) -> dict:
        out = dict(base or {k: d for k, (d, _, _) in POPUP_OPTIONS.items()})
        for key, (_, low, high) in POPUP_OPTIONS.items():
            if key in values:
                try:
                    out[key] = max(low, min(high, int(values[key])))
                except (TypeError, ValueError):
                    pass
        return out

    def options(self) -> dict:
        return dict(self._options)

    def set_options(self, values: dict) -> dict:
        with self._lock:
            self._options = self._clean(values, self._options)
            self.options_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.options_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._options, indent=1), encoding="utf-8")
            os.replace(tmp, self.options_path)
        return self.options()

    def default(self) -> str:
        """The theme of accounts without their own: the platform's global default, else DEFAULT."""
        if self.default_path is None:
            return DEFAULT
        try:
            mtime = self.default_path.stat().st_mtime_ns
            if mtime != self._default[0]:
                value = self.default_path.read_text(encoding="utf-8").strip()
                value = LEGACY.get(value, value)
                self._default = (mtime, value if value in THEMES else DEFAULT)
        except OSError:
            self._default = (None, DEFAULT)
        return self._default[1]

    def get(self, username: str) -> str:
        return self._themes.get(username.lower()) or self.default()

    def set(self, username: str, theme: str) -> None:
        if theme not in THEMES:
            raise ValueError(theme)
        with self._lock:
            self._themes[username.lower()] = theme       # an explicit choice stays, whatever the default becomes
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._themes, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
