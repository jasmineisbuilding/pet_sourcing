"""Versioned agent prompts.

A prompt's version is a short hash of its exact text, so every run records which
prompt produced it without anyone having to remember to bump a number. When an
eval regresses, `run list` shows which prompt_version changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


def available_prompts() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.md") if p.stem != "common")


def load_prompt(agent: str) -> tuple[str, str]:
    """Return (system prompt, version) for an agent."""
    path = PROMPT_DIR / f"{agent}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt for agent '{agent}'. Available: {available_prompts()}")
    text = (PROMPT_DIR / "common.md").read_text(encoding="utf-8").strip() + "\n\n" + \
        path.read_text(encoding="utf-8").strip()
    version = f"{agent}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    return text, version
