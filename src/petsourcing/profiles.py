"""Category profiles: the swappable, category-specific knowledge layer.

The core (evidence chain, agents, economics) is category-agnostic. Everything
that differs between e.g. pet supplies and camping gear lives in a TOML file
under ``config/categories/``.
"""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from . import config


class DefaultValue(BaseModel):
    value: float
    note: str


class CategoryProfile(BaseModel):
    key: str
    name: str
    default_market: str = "CA"
    pain_point_taxonomy: list[str] = Field(default_factory=list)
    freshness_days: dict[str, int] = Field(default_factory=dict)
    cost_defaults: dict[str, DefaultValue] = Field(default_factory=dict)
    compliance_checklist: list[str] = Field(default_factory=list)
    challenger_checklist: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)

    def is_stale(self, kind: str, captured_at: date, today: date | None = None) -> bool:
        """True if evidence of this kind, captured on this date, is past its freshness window.

        Kinds without a configured window never go stale.
        """
        days = self.freshness_days.get(kind)
        if days is None:
            return False
        return ((today or date.today()) - captured_at).days > days


def available_profiles(directory: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (directory or config.CATEGORY_DIR).glob("*.toml"))


def load_profile(key: str, directory: Path | None = None) -> CategoryProfile:
    path = (directory or config.CATEGORY_DIR) / f"{key}.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown category profile '{key}'. Available: {available_profiles(directory)}"
        )
    with path.open("rb") as f:
        return CategoryProfile.model_validate(tomllib.load(f))
