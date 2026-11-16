"""Run one agent end to end: open a run, loop, close the run with an honest status."""

from __future__ import annotations

import time
from datetime import date
from typing import Callable

from sqlalchemy.orm import Session

from ..models import Opportunity, ResearchRun
from ..profiles import load_profile
from .executor import ToolExecutor
from .loop import AgentResult, LoopLimits, run_agent
from .model import ModelClient
from .prompts import load_prompt
from .runs import finish_run, start_run

RUN_STATUS = {"completed": "succeeded", "stopped": "paused", "failed": "failed"}


def default_task(opp: Opportunity) -> str:
    profile = load_profile(opp.category_profile)
    lines = [
        f"Opportunity: {opp.title}",
        f"Category: {profile.name}. Target market: {opp.target_market}.",
    ]
    if opp.description:
        lines.append(f"Description: {opp.description}")
    lines.append("Known pain point vocabulary (use if it fits, add new ones if needed): "
                 + ", ".join(profile.pain_point_taxonomy))
    lines.append("Research this opportunity according to your role, using your tools.")
    return "\n".join(lines)


def run_single_agent(
    session: Session,
    opp: Opportunity,
    agent: str,
    model: ModelClient,
    *,
    task: str | None = None,
    limits: LoopLimits | None = None,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[ResearchRun, AgentResult]:
    system, version = load_prompt(agent)
    run = start_run(session, opp, model=model.name, prompt_version=version)
    executor = ToolExecutor(session, opp, run, agent, today=today)
    try:
        result = run_agent(executor, model, system=system, task=task or default_task(opp),
                           limits=limits, sleep=sleep, on_event=on_event)
    except Exception as e:  # a bug in our code: record it on the run, then surface it
        finish_run(session, run, "failed", f"crash: {type(e).__name__}: {e}")
        raise
    finish_run(session, run, RUN_STATUS[result.outcome],
               None if result.outcome == "completed" else result.reason)
    return run, result
