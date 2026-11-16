from datetime import date

import pytest
from sqlalchemy import func, select, text

from petsourcing import config
from petsourcing.agents import AGENT_TOOLS, TOOLS, ToolExecutor, finish_run, run_steps, start_run
from petsourcing.agents import tools as tools_mod
from petsourcing.db import init_db, make_engine, session_scope
from petsourcing.evidence_ops import supersede
from petsourcing.ingest import import_evidence, import_products, import_quotes
from petsourcing.ingest.paste import import_pasted
from petsourcing.models import (
    ActionProposal, AgentStep, Claim, Decision, Evidence, EvidenceTag, Opportunity,
)
from petsourcing.search_index import ensure_index

S = config.SAMPLES_DIR
TODAY = date(2026, 11, 16)


@pytest.fixture
def engine(tmp_path):
    e = make_engine(tmp_path / "a.db")
    init_db(e)
    with session_scope(e) as s:
        o = Opportunity(slug="t", title="t", category_profile="pet_supplies")
        s.add(o)
        s.flush()
        import_products(s, o, S / "products.csv")
        import_evidence(s, o, S / "evidence.csv")
        import_quotes(s, o, S / "quotes.csv")
    return e


@pytest.fixture
def s(engine):
    with session_scope(engine) as session:
        yield session


@pytest.fixture
def ex(s):
    """Factory: ex('demand') -> executor sharing one run."""
    opp = s.scalar(select(Opportunity))
    run = start_run(s, opp, model="test", prompt_version="v0")
    return lambda agent, **kw: ToolExecutor(s, opp, run, agent, today=TODAY, **kw)


def ev_id(s, fragment):
    return s.scalar(select(Evidence.id).where(Evidence.raw_text.contains(fragment)))


def cite(s, fragment, quote=None):
    return {"evidence_id": ev_id(s, fragment), "quote_span": quote or fragment}


# ------------------------------------------------------------------ executor guarantees


def test_forbidden_unknown_and_invalid_calls_are_structured_errors(ex):
    d = ex("demand")
    assert d.call("add_challenge", {})["error_type"] == "forbidden"
    assert d.call("drop_tables", {})["error_type"] == "unknown_tool"
    r = d.call("search_evidence", {"query": "x", "bogus": 1})
    assert r["error_type"] == "invalid_input" and "bogus" in r["error"]


def test_every_call_is_traced_including_failures(s, ex):
    d = ex("demand")
    d.call("search_evidence", {"query": "clean"})
    d.call("add_challenge", {})
    steps = run_steps(s, d.ctx.run.id)
    assert [(x.tool_name, x.ok) for x in steps] == [("search_evidence", True), ("add_challenge", False)]
    assert steps[1].error and steps[0].latency_ms is not None


def test_budget_is_enforced(ex):
    d = ex("demand", max_calls=2)
    d.call("list_products", {})
    d.call("list_products", {})
    assert d.call("list_products", {})["error_type"] == "budget_exhausted"


def test_failing_tool_rolls_back_its_writes(s, ex, monkeypatch):
    def half_write_then_crash(ctx, inp):
        ctx.session.add(Claim(opportunity_id=ctx.opp.id, agent="x", type="pain", text="partial"))
        ctx.session.flush()
        raise RuntimeError("boom")

    monkeypatch.setitem(TOOLS, "list_products", tools_mod.Tool(
        "list_products", "", tools_mod.NoIn, half_write_then_crash))
    r = ex("demand").call("list_products", {})
    assert r["error_type"] == "internal_error" and "boom" in r["error"]
    assert s.scalar(select(func.count()).select_from(Claim)) == 0
    assert s.scalar(select(func.count()).select_from(AgentStep)) == 1  # but the failure is logged


def test_definitions_only_expose_allowed_tools_with_closed_schemas(ex):
    for agent in AGENT_TOOLS:
        defs = ex(agent).definitions()
        assert {d["name"] for d in defs} == AGENT_TOOLS[agent]
        for d in defs:
            assert d["input_schema"].get("additionalProperties") is False, d["name"]


def test_run_lifecycle(s, ex):
    run = ex("demand").ctx.run
    finish_run(s, run, "failed", "model refused")
    assert run.status == "failed" and run.finished_at and run.error == "model refused"
    with pytest.raises(ValueError):
        finish_run(s, run, "running")


# ------------------------------------------------------------------ search


def test_search_ranks_and_filters(s, ex):
    d = ex("demand")
    r = d.call("search_evidence", {"query": "wash glove"})
    assert r["results"][0]["text"].startswith("Hair sticks to the glove and washing")
    only_p2 = d.call("search_evidence", {"query": "hair", "product_key": "P2"})
    assert {x["product_key"] for x in only_p2["results"]} == {"P2"}
    notes = d.call("search_evidence", {"query": "store", "kind": "note"})
    assert notes["count"] == 1
    assert d.call("search_evidence", {"query": "x", "product_key": "P9"})["error_type"] == "rejected"


def test_search_excludes_superseded_and_sees_new_rows(s, ex):
    import_pasted(s, s.scalar(select(Opportunity)), "Zipper broke instantly.", kind="review",
                  captured_at=TODAY, source_note="test")
    d = ex("demand")
    assert d.call("search_evidence", {"query": "zipper"})["count"] == 1  # trigger kept index in sync
    old, new = ev_id(s, "stopped working"), ev_id(s, "Zipper")
    supersede(s, old, new)
    texts = [x["text"] for x in d.call("search_evidence", {"query": "button"})["results"]]
    assert not any("stopped working" in t for t in texts)


def test_search_chinese_and_short_terms(s, ex):
    import_pasted(s, s.scalar(select(Opportunity)), "梳子很难清洗，毛都卡在底部。", kind="review",
                  captured_at=TODAY, source_note="test", language="zh")
    d = ex("demand")
    assert d.call("search_evidence", {"query": "难清洗"})["count"] == 1   # trigram index
    assert d.call("search_evidence", {"query": "清洗"})["count"] == 1     # 2 chars -> LIKE fallback
    mixed = d.call("search_evidence", {"query": "clean 毛"})
    assert mixed["ignored_terms"] == ["毛"]


def test_index_is_rebuilt_if_missing(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE evidence_fts"))
        conn.execute(text("DROP TRIGGER evidence_fts_ai"))
        assert ensure_index(conn) is True
        assert conn.execute(text("SELECT count(*) FROM evidence_fts WHERE evidence_fts MATCH '\"glove\"'")).scalar() >= 1
        assert ensure_index(conn) is False


# ------------------------------------------------------------------ read tools


def test_products_and_quotes(ex):
    c = ex("cost")
    prods = c.call("list_products", {})
    assert "NOT units sold" in prods["note"]
    p4 = next(p for p in prods["products"] if p["product_key"] == "P4")
    assert p4["price"] is None
    quotes = c.call("list_quotes", {})["quotes"]
    assert all(q["is_latest_for_tier"] for q in quotes)  # 2 MOQ tiers of A are not history


def test_newer_quote_replaces_older_in_same_tier(s, ex):
    from petsourcing.models import SupplierQuote

    old = s.scalar(select(SupplierQuote).where(SupplierQuote.supplier == "Demo Supplier B"))
    s.add(SupplierQuote(opportunity_id=old.opportunity_id, supplier=old.supplier,
                        product_desc=old.product_desc, unit_price=19.0, currency="CNY",
                        moq=old.moq, quoted_at=date(2026, 11, 15)))
    s.flush()
    quotes = ex("cost").call("list_quotes", {})["quotes"]
    b = [q for q in quotes if q["supplier"] == "Demo Supplier B"]
    assert [q["is_latest_for_tier"] for q in b] == [False, True]


def test_pain_point_stats_reports_denominator(s, ex):
    d = ex("demand")
    assert d.call("pain_point_stats", {})["tagged_evidence"] == 0
    for frag in ("stuck between", "washing it is annoying"):
        s.add(EvidenceTag(evidence_id=ev_id(s, frag), pain_point="难清洗", tagged_by="manual"))
    s.flush()
    r = d.call("pain_point_stats", {})
    assert r["tagged_evidence"] == 2 and r["total_evidence"] == 11
    assert r["pain_points"][0] == {"pain_point": "难清洗", "evidence_count": 2,
                                   "example_evidence_ids": sorted([ev_id(s, "stuck between"),
                                                                   ev_id(s, "washing it is annoying")])}


def test_compute_economics_labels_assumptions(ex):
    c = ex("cost")
    r = c.call("compute_economics", {"price": 30, "unit_cost": 10, "order_qty": 100})
    assert r["ok"] and r["assumed_inputs"]["return_rate"]["source"] == "profile_default"
    assert len(r["most_sensitive"]) == 3
    assert c.call("compute_economics", {"price": 30})["error_type"] == "invalid_input"
    assert c.call("compute_economics", {"price": 30, "unit_cost": 1, "order_qty": 1,
                                        "return_rate": 2})["error_type"] == "rejected"


# ------------------------------------------------------------------ claims & challenges


def test_claim_types_are_restricted_per_agent(s, ex):
    body = {"text": "Hair gets stuck in combs.", "citations": [cite(s, "hair gets stuck between the teeth")]}
    assert ex("demand").call("add_claim", {"type": "gap", **body})["error_type"] == "rejected"
    r = ex("demand").call("add_claim", {"type": "pain", **body})
    assert r["ok"] and r["status"] == "supported"
    assert s.get(Claim, r["claim_id"]).run_id == ex("demand").ctx.run.id


def _claim(s, ex):
    return ex("demand").call("add_claim", {
        "type": "pain", "text": "Hair gets stuck in combs.",
        "citations": [cite(s, "hair gets stuck between the teeth")]})["claim_id"]


def test_challenge_blocks_decision_until_answered(s, ex):
    cid = _claim(s, ex)
    ch = ex("challenger").call("add_challenge", {"claim_id": cid, "type": "counterexample",
                                                 "text": "P2 also has a 5-star review."})
    assert ch["claim_status"] == "challenged"
    again = ex("challenger").call("add_challenge", {"claim_id": cid, "type": "counterexample",
                                                    "text": "P2 also has a  5-star review. "})
    assert again["duplicate"] and again["challenge_id"] == ch["challenge_id"]

    coord = ex("coordinator")
    r = coord.call("record_decision", {"verdict": "hold", "rationale": "x", "claim_ids": [cid]})
    assert r["error_type"] == "rejected" and str(ch["challenge_id"]) in r["error"]

    no_cite = coord.call("resolve_challenge", {"challenge_id": ch["challenge_id"],
                                               "resolution": "rebutted", "note": "no"})
    assert no_cite["error_type"] == "rejected"
    fake = coord.call("resolve_challenge", {"challenge_id": ch["challenge_id"], "resolution": "rebutted",
                                            "note": "n", "citations": [cite(s, "Too big", "everyone loves it")]})
    assert fake["error_type"] == "rejected"
    ok = coord.call("resolve_challenge", {"challenge_id": ch["challenge_id"], "resolution": "rebutted",
                                          "note": "Cleaning complaints recur across products.",
                                          "citations": [cite(s, "washing it is annoying")]})
    assert ok["claim_status"] == "supported"
    d = coord.call("record_decision", {"verdict": "hold", "rationale": "Need more data.",
                                       "claim_ids": [cid]})
    assert d["ok"] and s.get(Decision, d["decision_id"]).run_id is not None


@pytest.mark.parametrize("ctype, expected", [("counterexample", "refuted"),
                                             ("insufficient", "unverified")])
def test_accepted_challenges_downgrade_claims(s, ex, ctype, expected):
    cid = _claim(s, ex)
    ch = ex("challenger").call("add_challenge", {"claim_id": cid, "type": ctype, "text": "t"})
    r = ex("coordinator").call("resolve_challenge", {"challenge_id": ch["challenge_id"],
                                                     "resolution": "accepted", "note": "fair"})
    assert r["claim_status"] == expected
    again = ex("coordinator").call("resolve_challenge", {"challenge_id": ch["challenge_id"],
                                                         "resolution": "rebutted", "note": "x"})
    assert "already accepted" in again["error"]


def test_enter_verdict_needs_plan_and_supported_claims(s, ex):
    cid = _claim(s, ex)
    unverified = ex("manual").call("add_claim", {"type": "gap", "text": "Nobody sells this."})["claim_id"]
    coord = ex("coordinator")
    assert "validation_plan" in coord.call("record_decision", {
        "verdict": "enter", "rationale": "r", "claim_ids": [cid]})["error"]
    assert str(unverified) in coord.call("record_decision", {
        "verdict": "enter", "rationale": "r", "claim_ids": [cid, unverified],
        "validation_plan": "order 3 samples"})["error"]
    assert coord.call("record_decision", {"verdict": "enter", "rationale": "r", "claim_ids": [cid],
                                          "validation_plan": "order 3 samples"})["ok"]


# ------------------------------------------------------------------ human approval


def test_propose_action_is_idempotent_and_never_executes(s, ex):
    coord = ex("coordinator")
    a = coord.call("propose_action", {"action_type": "change_stage", "reason": "r",
                                      "payload": {"stage": "awaiting_sample", "note": "x"}})
    b = coord.call("propose_action", {"action_type": "change_stage", "reason": "retry",
                                      "payload": {"note": "x", "stage": "awaiting_sample"}})
    assert a["status"] == "proposed" and b["duplicate"] and b["proposal_id"] == a["proposal_id"]
    assert s.scalar(select(func.count()).select_from(ActionProposal)) == 1
    assert s.scalar(select(Opportunity)).stage.value == "to_research"  # nothing executed
    assert coord.call("propose_action", {"action_type": "change_stage", "reason": "r",
                                         "payload": {"stage": "launched"}})["error_type"] == "rejected"
    assert coord.call("propose_action", {"action_type": "order_sample", "reason": "r",
                                         "payload": {}})["error_type"] == "rejected"
