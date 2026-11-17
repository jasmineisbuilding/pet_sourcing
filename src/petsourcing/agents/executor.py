"""Tool executor: the single gate between an agent and the database.

For every call, in order:
1. the tool must exist and the agent must be allowed to use it;
2. the call budget for this run must not be exhausted;
3. arguments are validated against the tool's schema;
4. the tool runs inside a SAVEPOINT — any failure rolls back its writes;
5. the call is recorded in ``agent_step`` (input, output, ok/error, latency).

Failures never raise to the caller. They come back as
``{"ok": false, "error": ..., "error_type": ...}`` so the agent loop can hand
them to the model, which can correct itself and retry.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..models import AgentStep, Opportunity, ResearchRun
from .tools import AGENT_TOOLS, TOOLS, ToolContext, ToolError


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if hasattr(v, "value"):  # enums
        return v.value
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


class ToolExecutor:
    def __init__(self, session: Session, opp: Opportunity, run: ResearchRun, agent: str,
                 *, max_calls: int | None = None, today: date | None = None):
        if agent not in AGENT_TOOLS:
            raise ValueError(f"unknown agent '{agent}'. Known: {sorted(AGENT_TOOLS)}")
        if run.opportunity_id != opp.id:
            raise ValueError("run belongs to a different opportunity")
        self.ctx = ToolContext(session, opp, run, agent, today)
        self.max_calls = max_calls
        self.calls = 0

    @property
    def allowed(self) -> list[str]:
        return sorted(AGENT_TOOLS[self.ctx.agent])

    def definitions(self) -> list[dict]:
        """Tool definitions for this agent, in the Messages API tool format."""
        return [{
            "name": name,
            "description": TOOLS[name].description,
            "input_schema": TOOLS[name].input_model.model_json_schema(),
        } for name in self.allowed]

    @property
    def budget_exhausted(self) -> bool:
        return self.max_calls is not None and self.calls >= self.max_calls

    def call(self, name: str, args: dict | None) -> dict:
        args = args or {}
        start = time.perf_counter()
        out: dict | None = None
        error_type: str | None = None
        error: str | None = None

        tool = TOOLS.get(name)
        if tool is None:
            error_type, error = "unknown_tool", f"no tool named '{name}'. Available: {self.allowed}"
        elif name not in AGENT_TOOLS[self.ctx.agent]:
            error_type, error = "forbidden", f"agent '{self.ctx.agent}' is not allowed to use '{name}'"
        elif self.budget_exhausted:
            error_type, error = "budget_exhausted", (
                f"tool call budget of {self.max_calls} for this run is used up; stop and summarise")
        else:
            try:
                inp = tool.input_model.model_validate(args)
            except ValidationError as e:
                error_type = "invalid_input"
                error = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'input'}: {x['msg']}"
                                  for x in e.errors())
            else:
                savepoint = self.ctx.session.begin_nested()
                try:
                    out = tool.fn(self.ctx, inp)
                    savepoint.commit()
                except ToolError as e:
                    savepoint.rollback()
                    error_type, error = "rejected", str(e)
                except Exception as e:  # a bug, not the agent's fault — still don't crash the run
                    savepoint.rollback()
                    error_type, error = "internal_error", f"{type(e).__name__}: {e}"

        self.calls += 1
        ok = error is None
        result = {"ok": True, **_jsonable(out)} if ok else {
            "ok": False, "error_type": error_type, "error": error}
        step = AgentStep(
            run_id=self.ctx.run.id, agent=self.ctx.agent, tool_name=name,
            tool_input=_jsonable(args), tool_output=result, ok=ok, error=error,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        self.ctx.session.add(step)
        self.ctx.session.flush()
        # The step id lets a later claim cite this exact call (e.g. a cost computation).
        return {**result, "step_id": step.id}
