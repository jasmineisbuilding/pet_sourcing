import json
from datetime import date

import pytest
from sqlalchemy import func, select

from petsourcing import config
from petsourcing.agents import ToolExecutor, run_steps, start_run
from petsourcing.agents.demo_script import demo_demand_model
from petsourcing.agents.loop import LoopLimits, run_agent
from petsourcing.agents.model import ModelError, ScriptedModel, last_tool_results, say, tool_call, turn
from petsourcing.agents.prompts import load_prompt
from petsourcing.agents.runner import run_single_agent
from petsourcing.db import init_db, make_engine, session_scope
from petsourcing.ingest import import_evidence, import_products, import_quotes
from petsourcing.models import Claim, Evidence, Opportunity, ResearchRun

S = config.SAMPLES_DIR
TODAY = date(2026, 11, 16)
NO_SLEEP = lambda _s: None  # noqa: E731


@pytest.fixture
def s(tmp_path):
    e = make_engine(tmp_path / "l.db")
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


def run_loop(s, script, agent="demand", **limits):
    model = ScriptedModel(script)
    run = start_run(s, opp(s), model=model.name)
    ex = ToolExecutor(s, opp(s), run, agent, today=TODAY)
    events = []
    res = run_agent(ex, model, system="sys", task="task", limits=LoopLimits(**limits),
                    sleep=NO_SLEEP, on_event=lambda k, d: events.append((k, d)))
    return res, model, run, events


def ev_id(s, fragment):
    return s.scalar(select(Evidence.id).where(Evidence.raw_text.contains(fragment)))


GOOD = "hair gets stuck between the teeth"


def claim_call(s, quote=GOOD):
    return tool_call("add_claim", {"type": "pain", "text": "梳子难清理",
                                   "citations": [{"evidence_id": ev_id(s, "stuck between"), "quote_span": quote}]})


# ------------------------------------------------------------------ happy path & protocol


def test_completes_and_tracks_tokens(s):
    res, model, run, _ = run_loop(s, [
        turn(tool_call("search_evidence", {"query": "clean"}), tokens=(1000, 200)),
        turn(claim_call(s), tokens=(1500, 300)),
        turn(say("done"), tokens=(1600, 50)),
    ])
    assert (res.outcome, res.turns, res.tool_calls, res.final_text) == ("completed", 3, 2, "done")
    assert (res.input_tokens, res.output_tokens) == (4100, 550)
    assert (run.input_tokens, run.output_tokens) == (4100, 550)
    assert s.scalar(select(func.count()).select_from(Claim)) == 1
    assert model.calls[0]["tools"] == sorted(model.calls[0]["tools"])  # executor's tool list
    assert "add_challenge" not in model.calls[0]["tools"]


def test_parallel_tool_results_return_in_one_message_in_order(s):
    a, b = tool_call("list_products"), tool_call("add_challenge", {})
    res, model, _, _ = run_loop(s, [turn(a, b), turn(say("ok"))])
    sent = model.calls[1]["messages"][-1]
    assert sent["role"] == "user"
    assert [r["tool_use_id"] for r in sent["content"]] == [a["id"], b["id"]]
    assert [r["is_error"] for r in sent["content"]] == [False, True]
    assert json.loads(sent["content"][1]["content"])["error_type"] == "forbidden"
    assert res.outcome == "completed" and res.failed_tool_calls == 1


def test_assistant_content_is_passed_back_unchanged(s):
    first = turn(say("thinking out loud"), tool_call("list_products"))
    _, model, _, _ = run_loop(s, [first, turn(say("ok"))])
    assert model.calls[1]["messages"][1] == {"role": "assistant", "content": first.content}


def test_self_correction_after_rejected_citation(s):
    def fix(messages):
        (r,) = last_tool_results(messages)
        assert r["is_error"] and "quote not found" in r["content"]
        return turn(claim_call(s))

    res, _, run, _ = run_loop(s, [turn(claim_call(s, "hair is impossible to remove")), fix, turn(say("ok"))])
    assert res.outcome == "completed" and res.failed_tool_calls == 1
    assert s.scalar(select(func.count()).select_from(Claim)) == 1
    assert [st.ok for st in run_steps(s, run.id)] == [False, True]


# ------------------------------------------------------------------ limits


def test_stuck_on_identical_failing_call(s):
    bad = lambda _m: turn(claim_call(s, "invented quote here"))  # noqa: E731
    res, _, _, _ = run_loop(s, [bad, bad, bad, bad], stuck_after=3)
    assert res.outcome == "stopped" and "stuck" in res.reason and res.turns == 3
    assert s.scalar(select(func.count()).select_from(Claim)) == 0


def test_different_failures_are_not_stuck(s):
    script = [turn(claim_call(s, f"invented quote number {i}")) for i in range(4)] + [turn(say("give up"))]
    res, *_ = run_loop(s, script, stuck_after=3)
    assert res.outcome == "completed"


def test_turn_limit(s):
    res, *_ = run_loop(s, [turn(tool_call("list_products"))] * 5, max_turns=3)
    assert res.outcome == "stopped" and "turn limit" in res.reason and res.turns == 3


def test_token_budget(s):
    res, *_ = run_loop(s, [turn(tool_call("list_products"), tokens=(600, 100))] * 5, max_tokens=1000)
    assert res.outcome == "stopped" and "token budget" in res.reason and res.turns == 2


def test_tool_call_budget(s):
    script = [turn(tool_call("list_products"), tool_call("list_products"))] * 3
    res, *_ = run_loop(s, script, max_tool_calls=3)
    assert res.outcome == "stopped" and "tool call budget" in res.reason
    assert res.tool_calls == 4  # 4th call returned budget_exhausted to the model
    assert res.failed_tool_calls == 1


# ------------------------------------------------------------------ model failures


def test_transient_errors_are_retried_with_backoff(s):
    sleeps = []
    model = ScriptedModel([ModelError("overloaded", retryable=True),
                           ModelError("overloaded", retryable=True), turn(say("ok"))])
    run = start_run(s, opp(s))
    res = run_agent(ToolExecutor(s, opp(s), run, "demand"), model, system="", task="t",
                    limits=LoopLimits(backoff_seconds=1), sleep=sleeps.append)
    assert res.outcome == "completed" and sleeps == [1, 2]


def test_persistent_or_fatal_errors_fail(s):
    res, *_ = run_loop(s, [ModelError("overloaded", retryable=True)] * 3, model_retries=2)
    assert res.outcome == "failed" and "after retries" in res.reason
    res, model, *_ = run_loop(s, [ModelError("invalid request")])
    assert res.outcome == "failed" and len(model.calls) == 1  # not retried


@pytest.mark.parametrize("stop, reason", [("refusal", "refused"), ("max_tokens", "truncated")])
def test_refusal_and_truncation_fail(s, stop, reason):
    res, *_ = run_loop(s, [turn(tool_call("list_products"), stop_reason=stop)])
    assert res.outcome == "failed" and reason in res.reason
    assert res.tool_calls == 0  # a truncated/refused turn's tool calls are not executed


# ------------------------------------------------------------------ runner & prompts


def test_prompt_version_tracks_prompt_text():
    text, version = load_prompt("demand")
    assert version.startswith("demand-") and "verbatim" in text and "需求" in text
    assert load_prompt("demand")[1] == version
    with pytest.raises(FileNotFoundError):
        load_prompt("nobody")


@pytest.mark.parametrize("script, status", [
    ([turn(say("done"))], "succeeded"),
    ([turn(tool_call("list_products"))] * 3, "paused"),
    ([ModelError("bad request")], "failed"),
])
def test_runner_maps_outcome_to_run_status(s, script, status):
    run, res = run_single_agent(s, opp(s), "demand", ScriptedModel(script),
                                limits=LoopLimits(max_turns=2), sleep=NO_SLEEP)
    assert run.status == status and run.finished_at is not None
    assert run.prompt_version == load_prompt("demand")[1] and run.model == "scripted"
    assert (run.error is None) == (status == "succeeded")


def test_runner_records_crash_on_run(s):
    def crash(_m):
        raise KeyError("bug")

    with pytest.raises(KeyError):
        run_single_agent(s, opp(s), "demand", ScriptedModel([crash]), sleep=NO_SLEEP)
    run = s.scalar(select(ResearchRun))
    assert run.status == "failed" and "crash: KeyError" in run.error


def test_default_task_includes_context(s):
    _, res = run_single_agent(s, opp(s), "demand", ScriptedModel([turn(say("ok"))]))
    task = res.messages[0]["content"]
    assert "Easy-clean brush" in task and "难清洗" in task and "CA" in task


def test_demo_script_end_to_end(s):
    run, res = run_single_agent(s, opp(s), "demand", demo_demand_model(), today=TODAY)
    assert res.outcome == "completed" and res.failed_tool_calls == 1
    claims = list(s.scalars(select(Claim)))
    assert len(claims) == 2 and all(c.status.value == "supported" for c in claims)
    assert all(c.run_id == run.id and c.agent == "demand" for c in claims)
