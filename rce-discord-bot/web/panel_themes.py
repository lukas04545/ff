"""Panel themes ("skins") the owner picks for each account – Users page → Panel themes.

neon: the normal look. jumpscare: a dark horror look with a scary face and scream now and then. retro: an early-2000s
home page with 20 popup ads. The admin can't change their own theme; only the owner can.
Saved in data/web_themes.json (username in lower case -> theme).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

THEMES = {"neon": "Neon (default)", "jumpscare": "Jump scares 👻", "retro": "Early 2000s 💾"}
DEFAULT = "neon"
# Early 2000s: popup ads at login, how often (minutes, 0 = never) and how many come back, at most this many open
POPUP_OPTIONS = {"popups_start": (20, 0, 100), "popups_every": (3, 0, 120), "popups_wave": (2, 1, 50),
                 "popups_max": (6, 1, 100)}            # key: (default, min, max)


class ThemeStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
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

    def get(self, username: str) -> str:
        return self._themes.get(username.lower(), DEFAULT)

    def set(self, username: str, theme: str) -> None:
        if theme not in THEMES:
            raise ValueError(theme)
        with self._lock:
            if theme == DEFAULT:
                self._themes.pop(username.lower(), None)
            else:
                self._themes[username.lower()] = theme
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._themes, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
