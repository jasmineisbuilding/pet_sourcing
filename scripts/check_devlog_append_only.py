#!/usr/bin/env python3
"""Pre-commit check: DEVLOG.md may only be appended to, never edited or truncated.

Compares the staged DEVLOG.md with the last committed version. Install with:
    sh scripts/install_hooks.sh
"""

from __future__ import annotations

import subprocess
import sys

PATH = "DEVLOG.md"


def is_append_only(old: str, new: str) -> bool:
    return new.startswith(old)


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main() -> int:
    old = _git("show", f"HEAD:{PATH}")
    if old is None:  # first commit or file is new
        return 0
    new = _git("show", f":{PATH}")
    if new is None:
        print(f"✗ {PATH} was deleted. It is append-only.", file=sys.stderr)
        return 1
    if not is_append_only(old, new):
        print(f"✗ {PATH} is append-only: earlier entries were modified.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
