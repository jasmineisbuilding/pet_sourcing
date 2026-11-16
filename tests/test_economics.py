import pytest

from petsourcing.economics import CostInputs, build_inputs, compute, load_scenarios, sensitivity
from petsourcing.profiles import load_profile


def base(**kw):
    values = dict(
        price=30.0, unit_cost=50.0, fx_to_cad=0.2, order_qty=100,
        inbound_freight_total=200.0, duty_rate=0.1, packaging_per_unit=1.0,
        labeling_per_unit=0.5, referral_rate=0.15, fulfillment_fee_per_unit=5.0,
        return_rate=0.1, loss_per_return=10.0, cac_per_order=4.0,
        fixed_costs=500.0, sell_through=0.8,
    )
    values.update(kw)
    return CostInputs(**values)


def test_hand_calculated_case():
    r = compute(base())
    # product 50*0.2=10, freight 2, duty 1, pack 1, label .5 -> landed 14.5
    assert r.landed_cost_per_unit == pytest.approx(14.5)
    # platform 30*.15+5=9.5, returns .1*10=1, cac 4 -> net 30-14.5=15.5
    assert r.net_per_sale == pytest.approx(15.5)
    assert r.contribution_per_unit == pytest.approx(1.0)
    assert r.cash_needed == pytest.approx(1950.0)
    assert r.units_to_recover == 126  # ceil(1950/15.5)
    assert r.recoverable_within_batch is False
    assert r.units_sold == 80
    assert r.profit == pytest.approx(80 * 15.5 - 1950)
    assert any("回本" in w for w in r.warnings)


def test_losing_money_per_sale_never_recovers():
    r = compute(base(price=10.0))
    assert r.net_per_sale <= 0
    assert r.units_to_recover is None
    assert r.recoverable_within_batch is False
    assert any("亏钱" in w for w in r.warnings)


@pytest.mark.parametrize(
    "kw", [dict(price=0), dict(order_qty=0), dict(return_rate=1.5), dict(cac_per_order=-1)]
)
def test_invalid_inputs_rejected(kw):
    with pytest.raises(ValueError):
        base(**kw)


def test_sensitivity_sorted_and_skips_zero_params():
    out = sensitivity(base(cac_per_order=0.0))
    swings = [s.swing for s in out]
    assert swings == sorted(swings, reverse=True)
    assert "cac_per_order" not in {s.param for s in out}
    assert out[0].param == "price"  # price dominates in this setup


def test_missing_values_are_marked_as_profile_defaults():
    profile = load_profile("pet_supplies")
    inputs, src = build_inputs({"price": 25, "unit_cost": 10, "order_qty": 50}, profile=profile)
    assert inputs.return_rate == profile.cost_defaults["return_rate"].value
    assert src["return_rate"].kind == "profile_default"
    assert src["price"].kind == "assumption"  # given, but no source declared
    assert src["referral_rate"].kind == "assumption"  # engine default, flagged


def test_build_inputs_rejects_unknown_and_missing():
    with pytest.raises(ValueError, match="Unknown"):
        build_inputs({"price": 1, "unit_cost": 1, "order_qty": 1, "magic": 3})
    with pytest.raises(ValueError, match="Missing"):
        build_inputs({"price": 1})


def test_demo_scenario_file_loads(tmp_path):
    from petsourcing import config

    ss = load_scenarios(config.REPO_ROOT / "examples" / "economics_demo.toml")
    names = [s.name for s in ss.scenarios]
    assert names == ["base", "optimistic", "pessimistic"]
    base_s = ss.scenarios[0]
    assert base_s.sources["unit_cost"].kind == "quote"
    pess = ss.scenarios[2]
    assert pess.sources["price"].kind == "assumption"  # override replaces declared source
    assert pess.result.profit < base_s.result.profit
