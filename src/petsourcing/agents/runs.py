"""Research runs: one run = one investigation, with every tool call traced."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentStep, Opportunity, ResearchRun, utcnow

RUN_STATUSES = ("running", "succeeded", "failed", "paused")


def start_run(
    session: Session,
    opp: Opportunity,
    *,
    model: str | None = None,
    prompt_version: str | None = None,
    round: int = 1,
) -> ResearchRun:
    run = ResearchRun(opportunity_id=opp.id, model=model, prompt_version=prompt_version,
                      round=round, status="running")
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run: ResearchRun, status: str, error: str | None = None) -> None:
    if status not in RUN_STATUSES or status == "running":
        raise ValueError(f"finish status must be one of {RUN_STATUSES[1:]}")
    run.status = status
    run.error = error
    run.finished_at = utcnow()
    session.flush()


def run_steps(session: Session, run_id: int) -> list[AgentStep]:
    return list(session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.id)))
