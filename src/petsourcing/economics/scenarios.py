"""Load cost scenarios from a TOML file and fill gaps from the category profile.

File format (see examples/economics_demo.toml):

    [meta]
    name = "..."
    category = "pet_supplies"

    [base]                    # required parameters + any overrides
    price = 29.99
    ...

    [sources.unit_cost]       # optional: where a value came from
    kind = "quote"
    ref = "quote:1"

    [scenarios.pessimistic]   # optional: overrides on top of base
    unit_cost = 22

Any CostInputs field missing from [base] but present in the profile's
``cost_defaults`` is filled in and marked ``profile_default`` — so assumptions
are always visible, never silent. Fields missing from both use the engine
default (0 / 1.0) and are marked ``assumption`` with an explicit note.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from ..profiles import CategoryProfile, load_profile
from .engine import CostInputs, CostResult, Source, compute

REQUIRED = ("price", "unit_cost", "order_qty")
_INPUT_FIELDS = {f.name: f for f in fields(CostInputs)}


@dataclass
class Scenario:
    name: str
    inputs: CostInputs
    sources: dict[str, Source]
    result: CostResult


@dataclass
class ScenarioSet:
    title: str
    category: str | None
    scenarios: list[Scenario]


def build_inputs(
    values: dict,
    sources: dict[str, Source] | None = None,
    profile: CategoryProfile | None = None,
) -> tuple[CostInputs, dict[str, Source]]:
    unknown = set(values) - set(_INPUT_FIELDS)
    if unknown:
        raise ValueError(f"Unknown cost parameter(s): {sorted(unknown)}")
    missing = [k for k in REQUIRED if k not in values]
    if missing:
        raise ValueError(f"Missing required cost parameter(s): {missing}")

    resolved = dict(values)
    src: dict[str, Source] = dict(sources or {})
    for name, f in _INPUT_FIELDS.items():
        if name in resolved:
            src.setdefault(name, Source("assumption", note="已提供，但未注明来源"))
        elif profile and name in profile.cost_defaults:
            d = profile.cost_defaults[name]
            resolved[name] = d.value
            src[name] = Source("profile_default", ref=f"profile:{profile.key}", note=d.note)
        else:
            src[name] = Source("assumption", note=f"未提供，使用引擎默认值 {f.default}")
    if "order_qty" in resolved:
        resolved["order_qty"] = int(resolved["order_qty"])
    return CostInputs(**resolved), src


def load_scenarios(path: Path, profile_dir: Path | None = None) -> ScenarioSet:
    with Path(path).open("rb") as fh:
        doc = tomllib.load(fh)

    meta = doc.get("meta", {})
    category = meta.get("category")
    profile = load_profile(category, profile_dir) if category else None
    base = doc.get("base", {})
    base_sources = {
        k: Source(v["kind"], v.get("ref"), v.get("note")) for k, v in doc.get("sources", {}).items()
    }

    out: list[Scenario] = []
    variants = {"base": {}, **doc.get("scenarios", {})}
    for name, overrides in variants.items():
        values = {**base, **overrides}
        srcs = dict(base_sources)
        for k in overrides:  # an override replaces the base value, so its source too
            srcs[k] = Source("assumption", note=f"情景 '{name}' 的覆盖值")
        inputs, resolved_sources = build_inputs(values, srcs, profile)
        out.append(Scenario(name, inputs, resolved_sources, compute(inputs)))
    return ScenarioSet(meta.get("name", Path(path).stem), category, out)
