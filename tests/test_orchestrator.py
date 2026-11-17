from datetime import date

import pytest
from sqlalchemy import func, select

from petsourcing import config
from petsourcing.agents import ToolExecutor, start_run
from petsourcing.agents.demo_investigation import CAUTIOUS, SCRIPTS, demo_model_for
from petsourcing.agents.model import ModelError, ScriptedModel, say, tool_call, turn
from petsourcing.agents.orchestrator import InvestigationLimits, investigate, prompt_set_version
from petsourcing.claims import unmatched_numbers
from petsourcing.db import init_db, make_engine, session_scope
from petsourcing.ingest import import_evidence, import_products, import_quotes
from petsourcing.models import (
    ActionProposal, Challenge, Claim, ClaimStatus, Decision, Opportunity, ResearchRun,
)

S = config.SAMPLES_DIR
TODAY = date(2026, 11, 16)
NO_SLEEP = lambda _s: None  # noqa: E731


@pytest.fixture
def s(tmp_path):
    e = make_engine(tmp_path / "o.db")
    init_db(e)
    with session_scope(e) as session:
        o = Opportunity(slug="t", title="Easy-clean brush", category_profile="pet_supplies")
        session.add(o)
        session.flush()
        import_products(session, o, S / "products.csv")
        import_evidence(session, o, S / "evidence.csv")
        import_quotes(session, o, S / "quotes.csv")
        yield session


def opp(s):
    return s.scalar(select(Opportunity))


def run(s, model_for, **limits):
    events = []
    r = investigate(s, opp(s), model_for, limits=InvestigationLimits(**limits), today=TODAY,
                    sleep=NO_SLEEP, on_event=lambda k, d: events.append((k, d)))
    return r, events


def claim_by(s, fragment):
    return s.scalar(select(Claim).where(Claim.text.contains(fragment)))


def with_overrides(over: dict):
    """demo_model_for, but some (agent, round) pairs replaced by given scripts."""
    def factory(agent, round_no):
        if (agent, round_no) in over:
            return ScriptedModel(over[(agent, round_no)])
        return demo_model_for(agent, round_no)
    return factory


# ------------------------------------------------------------------ the demo story


def test_demo_investigation_reaches_hold_in_two_rounds(s):
    r, events = run(s, demo_model_for)
    assert (r.status, r.rounds, r.warnings) == ("decided", 2, [])
    decision = s.get(Decision, r.decision_id)
    assert decision.verdict.value == "hold" and decision.assumptions["price"] == 24.99

    runs = list(s.scalars(select(ResearchRun).order_by(ResearchRun.id)))
    assert [(x.round, x.status) for x in runs] == [(1, "succeeded"), (2, "succeeded")]
    assert {x.investigation_id for x in runs} == {r.investigation_id}
    assert runs[0].prompt_version == prompt_set_version() and runs[0].model == "scripted-demo"
    assert decision.run_id == runs[1].id

    assert claim_by(s, "难以清理").status == ClaimStatus.SUPPORTED      # challenge rebutted
    assert claim_by(s, "害怕").status == ClaimStatus.UNVERIFIED          # insufficient, accepted
    assert claim_by(s, "耐用性").status == ClaimStatus.CHALLENGED        # needs_research
    cost = claim_by(s, "保守情景")
    assert cost.status == ClaimStatus.SUPPORTED and cost.source_step_id is not None

    agents_round2 = [d["agent"] for k, d in events if k == "agent_start" and d["round"] == 2]
    assert agents_round2 == ["competitor", "coordinator"]                # only what was asked
    assert ("skip", {"agent": "challenger", "round": 2, "reason": "no new claims"}) in events
    assert s.scalar(select(ActionProposal)).payload["description"].startswith("收集至少 30 条")


def test_cost_claim_numbers_match_the_engine(s):
    run(s, demo_model_for)
    text = claim_by(s, "保守情景").text
    assert "0.66" in text and "1,870" in text and "543" in text


# ------------------------------------------------------------------ flow rules


def test_last_round_without_decision_pauses(s):
    r, _ = run(s, demo_model_for, max_rounds=1)
    assert r.status == "paused" and "round limit" in r.reason
    run1 = s.get(ResearchRun, r.run_ids[0])
    assert run1.status == "paused" and run1.error == r.reason
    assert s.scalar(select(func.count()).select_from(Decision)) == 0


def test_unanswered_challenges_pause(s):
    lazy = [turn(say("I will think about it later."))]
    r, _ = run(s, with_overrides({("coordinator", 1): lazy}))
    assert r.status == "paused" and "unanswered" in r.reason
    assert s.scalar(select(func.count()).select_from(Challenge)) == 3


def test_agent_failure_fails_investigation(s):
    r, _ = run(s, with_overrides({("competitor", 1): [ModelError("bad request")]}))
    assert r.status == "failed" and r.reason.startswith("competitor failed")
    assert s.get(ResearchRun, r.run_ids[0]).status == "failed"
    assert [a.agent for a in r.agent_results] == ["demand", "competitor"]  # cost never ran


def test_stopped_agent_is_a_warning_not_a_failure(s):
    # competitor may not write "pain" claims: the same rejected call, again and again
    stuck = [turn(tool_call("add_claim", {"type": "pain", "text": "x"}))] * 5
    r, _ = run(s, with_overrides({("competitor", 2): stuck}))
    assert r.status == "decided"
    assert any("competitor stopped early (stuck" in w for w in r.warnings)


def test_shared_token_budget(s):
    r, _ = run(s, demo_model_for, max_total_tokens=1000)
    assert r.status == "paused" and "token budget" in r.reason
    assert r.total_tokens >= 1000


def test_crash_is_recorded_on_run(s):
    def boom(agent, round_no):
        if agent == "cost":
            raise KeyError("bug in factory")
        return demo_model_for(agent, round_no)

    with pytest.raises(KeyError):
        investigate(s, opp(s), boom, today=TODAY, sleep=NO_SLEEP)
    assert "crash: KeyError" in s.scalar(select(ResearchRun)).error


def test_every_scripted_agent_has_a_prompt():
    for agent, _round in SCRIPTS:
        assert prompt_set_version((agent,)).startswith("set-")


# ------------------------------------------------------------------ computed claims


@pytest.mark.parametrize("text, missing", [
    ("利润 1,870 CAD，回本 543 件", []),
    ("利润率 -52.2%", []),                     # matches -0.522 as a percentage
    ("单件 0.66 CAD", []),
    ("单件 0.7 CAD", []),                      # same number at coarser precision
    ("单件 0.67 CAD", ["0.67"]),
    ("回本 600 件", ["600"]),
])
def test_unmatched_numbers(text, missing):
    out = {"result": {"profit": -1870.1, "units_to_recover": 543, "contribution_per_unit": 0.6601,
                      "roi": -0.522}}
    assert unmatched_numbers(text, [out]) == missing


@pytest.fixture
def cost_ex(s):
    r = start_run(s, opp(s))
    return ToolExecutor(s, opp(s), r, "cost", today=TODAY)


def test_computed_claim_requires_matching_numbers(s, cost_ex):
    step = cost_ex.call("compute_economics", CAUTIOUS)["step_id"]
    bad = cost_ex.call("add_claim", {"type": "cost", "text": "本批亏损 1,000 CAD", "source_step_id": step})
    assert bad["error_type"] == "rejected" and "1,000" in bad["error"]
    ok = cost_ex.call("add_claim", {"type": "cost", "text": "本批亏损 1,870 CAD", "source_step_id": step})
    assert ok["ok"] and ok["status"] == "supported" and ok["warnings"] == []


def test_computed_claim_rejects_wrong_step_kind_and_run(s, cost_ex):
    search = cost_ex.call("list_quotes", {})["step_id"]
    r = cost_ex.call("add_claim", {"type": "cost", "text": "x", "source_step_id": search})
    assert "only ['compute_economics']" in r["error"]

    step = cost_ex.call("compute_economics", CAUTIOUS)["step_id"]
    other = ToolExecutor(s, opp(s), start_run(s, opp(s)), "cost", today=TODAY)
    r = other.call("add_claim", {"type": "cost", "text": "亏损 1,870", "source_step_id": step})
    assert "another run" in r["error"]
    assert cost_ex.call("add_claim", {"type": "cost", "text": "x", "source_step_id": 99999})["error_type"] == "rejected"
