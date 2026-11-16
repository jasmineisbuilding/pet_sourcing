"""Unit economics engine — deterministic, no LLM involved.

The cost agent (phase 1) and the UI cost simulator both call these same
functions, so a number shown anywhere can be reproduced exactly.

All money is CAD unless noted. Prices are *excluding* sales tax (GST/HST/PST):
tax collected from customers is passed through, not revenue.

Model
-----
Per unit purchased (landed cost):
    product_cad   = unit_cost * fx_to_cad
    freight       = inbound_freight_total / order_qty
    duty          = product_cad * duty_rate          (duty base simplified, verify with CBSA)
    landed        = product_cad + freight + duty + packaging + labeling

Per unit sold (selling costs):
    platform      = price * (referral_rate + payment_rate) + fulfillment_fee
    returns       = return_rate * loss_per_return
    net_per_sale  = price - platform - returns - cac_per_order

    contribution  = net_per_sale - landed           (profit per unit if everything sells)

Test batch:
    cash_needed     = landed * order_qty + fixed_costs
    units_to_recover= ceil(cash_needed / net_per_sale)
    profit          = floor(order_qty * sell_through) * net_per_sale - cash_needed
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Literal

SourceKind = Literal["quote", "policy", "assumption", "profile_default", "measured"]


@dataclass(frozen=True)
class Source:
    """Where a parameter value came from. Shown next to every number in the UI."""

    kind: SourceKind
    ref: str | None = None  # e.g. "quote:3", "evidence:17", a URL
    note: str | None = None


RATE_FIELDS = ("duty_rate", "referral_rate", "payment_rate", "return_rate", "sell_through")


@dataclass(frozen=True)
class CostInputs:
    price: float  # selling price, CAD, excl. sales tax
    unit_cost: float  # supplier unit price, in cost currency
    order_qty: int
    fx_to_cad: float = 1.0  # CAD per 1 unit of cost currency
    inbound_freight_total: float = 0.0  # shipment total to your warehouse/FBA, CAD
    duty_rate: float = 0.0
    packaging_per_unit: float = 0.0
    labeling_per_unit: float = 0.0  # e.g. bilingual labels / inserts
    referral_rate: float = 0.0  # marketplace commission
    fulfillment_fee_per_unit: float = 0.0  # pick/pack/ship per order
    payment_rate: float = 0.0
    return_rate: float = 0.0
    loss_per_return: float = 0.0  # CAD lost per returned unit
    cac_per_order: float = 0.0  # ad / acquisition cost per order
    fixed_costs: float = 0.0  # samples, photos, listing setup, ...
    sell_through: float = 1.0  # share of the batch sold in the test window

    def __post_init__(self) -> None:
        errors = validate_inputs(self)
        if errors:
            raise ValueError("; ".join(errors))


def validate_inputs(p: CostInputs) -> list[str]:
    errors: list[str] = []
    if p.price <= 0:
        errors.append("price must be > 0")
    if p.order_qty <= 0 or int(p.order_qty) != p.order_qty:
        errors.append("order_qty must be a positive integer")
    if p.fx_to_cad <= 0:
        errors.append("fx_to_cad must be > 0")
    for f in fields(p):
        v = getattr(p, f.name)
        if f.name in RATE_FIELDS and not 0 <= v <= 1:
            errors.append(f"{f.name} must be between 0 and 1 (got {v})")
        elif isinstance(v, (int, float)) and v < 0:
            errors.append(f"{f.name} must be >= 0 (got {v})")
    return errors


@dataclass(frozen=True)
class CostResult:
    product_cost_cad: float
    freight_per_unit: float
    duty_per_unit: float
    landed_cost_per_unit: float
    platform_fees_per_sale: float
    return_cost_per_sale: float
    net_per_sale: float  # what each sale brings in after selling costs
    contribution_per_unit: float
    margin: float  # contribution / price
    cash_needed: float
    units_to_recover: int | None  # None = never recovers (net_per_sale <= 0)
    recoverable_within_batch: bool
    units_sold: int
    profit: float
    roi: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def compute(p: CostInputs) -> CostResult:
    product_cad = p.unit_cost * p.fx_to_cad
    freight = p.inbound_freight_total / p.order_qty
    duty = product_cad * p.duty_rate
    landed = product_cad + freight + duty + p.packaging_per_unit + p.labeling_per_unit

    platform = p.price * (p.referral_rate + p.payment_rate) + p.fulfillment_fee_per_unit
    returns = p.return_rate * p.loss_per_return
    net_per_sale = p.price - platform - returns - p.cac_per_order

    contribution = net_per_sale - landed
    cash_needed = landed * p.order_qty + p.fixed_costs

    units_to_recover = math.ceil(cash_needed / net_per_sale) if net_per_sale > 0 else None
    recoverable = units_to_recover is not None and units_to_recover <= p.order_qty

    units_sold = math.floor(p.order_qty * p.sell_through + 1e-9)
    profit = units_sold * net_per_sale - cash_needed
    roi = profit / cash_needed if cash_needed > 0 else 0.0

    warnings: list[str] = []
    if net_per_sale <= 0:
        warnings.append("每卖出一件都在亏钱：售价扣除平台费、退货和获客成本后 <= 0")
    elif contribution <= 0:
        warnings.append("单件贡献利润 <= 0：即使全部卖完也无法覆盖到岸成本")
    if units_to_recover is not None and not recoverable:
        warnings.append(
            f"需要卖出 {units_to_recover} 件才能回本，超过本批采购量 {p.order_qty} 件"
        )
    if net_per_sale > 0 and p.cac_per_order > 0.5 * net_per_sale:
        warnings.append("获客成本超过单笔净收入的一半，对广告效果非常敏感")

    return CostResult(
        product_cost_cad=product_cad,
        freight_per_unit=freight,
        duty_per_unit=duty,
        landed_cost_per_unit=landed,
        platform_fees_per_sale=platform,
        return_cost_per_sale=returns,
        net_per_sale=net_per_sale,
        contribution_per_unit=contribution,
        margin=contribution / p.price,
        cash_needed=cash_needed,
        units_to_recover=units_to_recover,
        recoverable_within_batch=recoverable,
        units_sold=units_sold,
        profit=profit,
        roi=roi,
        warnings=warnings,
    )


# ----------------------------------------------------------------------------- sensitivity


@dataclass(frozen=True)
class Sensitivity:
    param: str
    low_profit: float
    high_profit: float

    @property
    def swing(self) -> float:
        return abs(self.high_profit - self.low_profit)


SENSITIVITY_PARAMS = (
    "price",
    "unit_cost",
    "fx_to_cad",
    "inbound_freight_total",
    "referral_rate",
    "fulfillment_fee_per_unit",
    "return_rate",
    "cac_per_order",
    "sell_through",
)


def sensitivity(p: CostInputs, pct: float = 0.2) -> list[Sensitivity]:
    """Move each parameter down/up by ``pct`` and report the batch profit at each end.

    Sorted by swing, largest first: "which assumption matters most?".
    Parameters that are 0 are skipped (moving them by a percentage does nothing).
    """
    out: list[Sensitivity] = []
    for name in SENSITIVITY_PARAMS:
        base = getattr(p, name)
        if base == 0:
            continue

        def at(factor: float) -> float:
            v = base * factor
            if name in RATE_FIELDS:
                v = min(max(v, 0.0), 1.0)
            return compute(replace(p, **{name: v})).profit

        out.append(Sensitivity(name, at(1 - pct), at(1 + pct)))
    return sorted(out, key=lambda s: s.swing, reverse=True)
