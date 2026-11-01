"""python3 -m control_plane … – also with the system Python: if the bot's packages aren't there, it switches to the
bot's own virtual environment (.venv, made by start.sh / the installer) by itself."""
import os
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
VENVS = (".venv", "venv", "env")


def _missing() -> list[str]:
    import importlib.util
    return [m for m in ("dotenv", "aiohttp", "discord", "cryptography") if importlib.util.find_spec(m) is None]


def _switch_to_venv(missing: list[str]) -> None:
    for name in VENVS:
        python = BOT_DIR / name / "bin" / "python"
        if python.exists() and Path(sys.prefix).resolve() != (BOT_DIR / name).resolve():
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (str(BOT_DIR),
                                                                             os.environ.get("PYTHONPATH")))))
            os.execve(str(python), [str(python), "-m", "control_plane", *sys.argv[1:]], env)
    print(f"This Python ({sys.executable}) is missing the bot's packages: {', '.join(missing)}.\n"
          f"Install them once:\n  cd {BOT_DIR}\n  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
          "(Debian/Ubuntu: `apt install python3-venv` first.) Then run the same command again.", file=sys.stderr)
    raise SystemExit(1)


missing = _missing()
if missing:
    _switch_to_venv(missing)

from control_plane.cli import main  # noqa: E402

raise SystemExit(main())
