"""Multi-agent investigation: research → challenge → coordinate, in up to N rounds.

Round 1:  demand, competitor (independent) → cost (can read their claims)
          → challenger reviews every new claim
          → coordinator answers each challenge, then decides or requests follow-ups
Round 2+: only the agents the coordinator asked for, with its instructions
          → challenger (only if there are new claims) → coordinator

The flow is plain code (a state machine), not an agent: which agent runs when,
how many rounds, the shared token budget, and what counts as "done" are fixed
here. Each round is one ResearchRun; rounds share an ``investigation_id``.

Outcomes:
    decided — the coordinator recorded a decision
    paused  — no decision yet (follow-ups but no rounds left, a limit hit, the
              coordinator stopped without deciding); a human should look
    failed  — an agent failed (model error, refusal, truncation)
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentStep, Challenge, ChallengeResolution, Claim, Decision, Opportunity, ResearchRun
from .executor import ToolExecutor
from .loop import AgentResult, LoopLimits, run_agent
from .model import ModelClient
from .prompts import load_prompt
from .runner import default_task
from .runs import finish_run, start_run

RESEARCH_ORDER = ("demand", "competitor", "cost")
ALL_AGENTS = (*RESEARCH_ORDER, "challenger", "coordinator")
ModelFactory = Callable[[str, int], ModelClient]  # (agent, round) -> model


@dataclass
class InvestigationLimits:
    max_rounds: int = 2
    max_total_tokens: int = 600_000
    agent: LoopLimits = field(default_factory=LoopLimits)


@dataclass
class InvestigationResult:
    investigation_id: str
    status: Literal["decided", "paused", "failed"]
    reason: str
    rounds: int = 0
    run_ids: list[int] = field(default_factory=list)
    decision_id: int | None = None
    agent_results: list[AgentResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.agent_results)


def prompt_set_version(agents=ALL_AGENTS) -> str:
    versions = "|".join(load_prompt(a)[1] for a in agents)
    return "set-" + hashlib.sha256(versions.encode()).hexdigest()[:8]


class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason


def investigate(
    session: Session,
    opp: Opportunity,
    model_for: ModelFactory,
    *,
    limits: InvestigationLimits | None = None,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[str, dict], None] | None = None,
) -> InvestigationResult:
    limits = limits or InvestigationLimits()
    res = InvestigationResult(uuid.uuid4().hex[:12], "paused", "")
    emit = (lambda kind, **d: on_event(kind, d)) if on_event else (lambda kind, **d: None)
    followups: dict[str, list[str]] = {}
    run: ResearchRun | None = None

    def run_one(agent: str, task: str, round_no: int) -> AgentResult:
        remaining = limits.max_total_tokens - res.total_tokens
        if remaining <= 0:
            raise _Stop("paused", f"investigation token budget reached ({limits.max_total_tokens})")
        system, version = load_prompt(agent)
        agent_limits = LoopLimits(**{**limits.agent.__dict__,
                                     "max_tokens": min(limits.agent.max_tokens, remaining)})
        emit("agent_start", agent=agent, round=round_no, prompt_version=version)
        ex = ToolExecutor(session, opp, run, agent, today=today)
        model = model_for(agent, round_no)
        used = set(filter(None, (run.model or "").split(","))) | {model.name}
        run.model = ",".join(sorted(used))[:80]
        r = run_agent(ex, model, system=system, task=task, limits=agent_limits,
                      sleep=sleep, on_event=lambda k, d: emit(k, round=round_no, **d))
        res.agent_results.append(r)
        if r.outcome == "failed":
            raise _Stop("failed", f"{agent} failed: {r.reason}")
        if r.outcome == "stopped":
            res.warnings.append(f"round {round_no}: {agent} stopped early ({r.reason})")
        return r

    try:
        for round_no in range(1, limits.max_rounds + 1):
            res.rounds = round_no
            agents = list(RESEARCH_ORDER) if round_no == 1 else [a for a in RESEARCH_ORDER if a in followups]
            run = start_run(session, opp, prompt_version=prompt_set_version(), round=round_no)
            run.investigation_id = res.investigation_id
            res.run_ids.append(run.id)
            emit("round_start", round=round_no, agents=agents, run_id=run.id)

            for agent in agents:
                task = default_task(opp) if round_no == 1 else _followup_task(opp, round_no, followups[agent])
                run_one(agent, task, round_no)

            new_claims = list(session.scalars(select(Claim).where(Claim.run_id == run.id).order_by(Claim.id)))
            if new_claims:
                run_one("challenger", _challenger_task(new_claims), round_no)
            else:
                emit("skip", agent="challenger", round=round_no, reason="no new claims")

            run_one("coordinator", _coordinator_task(session, opp, round_no, limits.max_rounds), round_no)

            decision = session.scalar(select(Decision).where(Decision.run_id == run.id))
            if decision is not None:
                res.decision_id = decision.id
                finish_run(session, run, "succeeded")
                res.status, res.reason = "decided", f"verdict: {decision.verdict.value}"
                emit("decided", verdict=decision.verdict.value, decision_id=decision.id)
                return res

            followups = _requested_followups(session, run.id)
            open_ids = _open_challenges(session, opp)
            if open_ids:
                reason = f"coordinator left challenges unanswered: {open_ids}"
            elif followups and round_no < limits.max_rounds:
                finish_run(session, run, "succeeded")
                emit("followups", round=round_no, followups=followups)
                continue
            elif followups:
                reason = f"follow-ups requested but the round limit ({limits.max_rounds}) is reached"
            else:
                reason = "coordinator finished without a decision or follow-ups"
            raise _Stop("paused", reason)
        raise _Stop("paused", "round limit reached")  # unreachable with max_rounds >= 1
    except _Stop as stop:
        res.status, res.reason = stop.status, stop.reason
        if run is not None and run.status == "running":
            finish_run(session, run, "failed" if stop.status == "failed" else "paused", stop.reason)
        emit("stopped", status=stop.status, reason=stop.reason)
        return res
    except Exception as e:
        if run is not None and run.status == "running":
            finish_run(session, run, "failed", f"crash: {type(e).__name__}: {e}")
        raise


# --------------------------------------------------------------------------- tasks & state


def _followup_task(opp: Opportunity, round_no: int, instructions: list[str]) -> str:
    lines = [f"Opportunity: {opp.title}", f"Round {round_no}: the coordinator asked you to follow up on:"]
    lines += [f"- {i}" for i in instructions]
    lines.append("Earlier claims already exist; check them with your tools if needed. Record new "
                 "claims only where the evidence supports them, and say plainly if it does not.")
    return "\n".join(lines)


def _challenger_task(claims: list[Claim]) -> str:
    lines = ["Review these claims recorded in this round:"]
    lines += [f"- claim {c.id} ({c.type.value}, by {c.agent}): {c.text}" for c in claims]
    lines.append("Use list_claims / get_evidence / search_evidence to check them.")
    return "\n".join(lines)


def _coordinator_task(session: Session, opp: Opportunity, round_no: int, max_rounds: int) -> str:
    claims = list(session.scalars(select(Claim).where(Claim.opportunity_id == opp.id).order_by(Claim.id)))
    lines = [f"Opportunity: {opp.title}", f"Round {round_no} of {max_rounds}.", "Claims so far:"]
    for c in claims:
        open_n = sum(ch.resolution == ChallengeResolution.OPEN for ch in c.challenges)
        lines.append(f"- claim {c.id} [{c.type.value}, {c.status.value}"
                     + (f", {open_n} open challenge(s)" if open_n else "") + f"]: {c.text}")
    if round_no < max_rounds:
        lines.append("You may request follow-ups for the next round instead of deciding.")
    else:
        lines.append("This is the last round: record a decision. If data is missing, decide `hold` "
                     "and propose a request_data action describing what to collect.")
    return "\n".join(lines)


def _requested_followups(session: Session, run_id: int) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id,
                                                    AgentStep.tool_name == "request_followup",
                                                    AgentStep.ok.is_(True)).order_by(AgentStep.id))
    for st in steps:
        agent, text = st.tool_output["agent"], st.tool_output["instruction"]
        if text not in out.setdefault(agent, []):
            out[agent].append(text)
    return out


def _open_challenges(session: Session, opp: Opportunity) -> list[int]:
    return list(session.scalars(select(Challenge.id).join(Claim).where(
        Claim.opportunity_id == opp.id, Challenge.resolution == ChallengeResolution.OPEN)))
