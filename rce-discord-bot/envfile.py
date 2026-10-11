"""Reading and targeted editing of the .env file (comments and order are preserved)."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def format_value(value: str) -> str:
    """Writes values so that python-dotenv reads them back exactly."""
    needs_quotes = (
        value != value.strip() or "\n" in value or " #" in value or "\t#" in value
        or value[:1] in ("'", '"') or "\\" in value
    )
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def update_env(path: Path, updates: dict[str, str], *, template: Path | None = None) -> None:
    """Sets the given keys. A missing file is created from template (.env.example)."""
    if not path.exists():
        if template is not None and template.exists():
            shutil.copyfile(template, path)
        else:
            path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out = []
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={format_value(remaining.pop(key))}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out.append("# ---------- Added via the web interface ----------")
        out.extend(f"{k}={format_value(v)}" for k, v in remaining.items())
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: never a half-written .env
