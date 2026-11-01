"""Emergency undo for code changes made by the management AI – works even if the bot no longer starts.

Run in the bot folder:   python revert_code.py
It puts every file changed since the last "✅ Keep" back to its original (<file>.bak) and deletes files the AI
created. Then start the bot again.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from support.code_edit import ChangeSet  # noqa: E402  (only standard library inside)


def main() -> int:
    state = ROOT / "data" / "code_changes.json"
    files = ChangeSet(state).revert()
    if files:
        print("Put back the original of:")
        for f in files:
            print("  -", f)
    else:
        print("No pending code changes – nothing to revert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
