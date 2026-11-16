"""Paths and settings.

Real data lives under ``private/`` (git-ignored) by default, so that running the
tool never accidentally produces files that end up on GitHub.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR = REPO_ROOT / "private"
CATEGORY_DIR = REPO_ROOT / "config" / "categories"
SAMPLES_DIR = REPO_ROOT / "data" / "samples"

DEFAULT_DB_PATH = PRIVATE_DIR / "data" / "petsourcing.db"
DEMO_DB_PATH = PRIVATE_DIR / "data" / "demo.db"


def db_path() -> Path:
    """Database file path. Override with the PETSRC_DB environment variable."""
    env = os.environ.get("PETSRC_DB")
    return Path(env).expanduser().resolve() if env else DEFAULT_DB_PATH


def db_url(path: Path | None = None) -> str:
    return f"sqlite:///{path or db_path()}"
