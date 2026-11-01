"""python3 -m control_plane with a Python that lacks the bot's packages switches to the bot's .venv."""
import subprocess
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent


def test_without_packages_it_explains_or_switches(tmp_path):
    # a Python that can't see the installed packages (isolated, no site-packages) and no .venv next to the code
    code = tmp_path / "code"
    code.mkdir()
    (code / "control_plane").symlink_to(BOT / "control_plane", target_is_directory=True)
    r = subprocess.run([sys.executable, "-S", "-m", "control_plane", "links"], cwd=code, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1 and "missing the bot's packages" in r.stderr and "requirements.txt" in r.stderr
