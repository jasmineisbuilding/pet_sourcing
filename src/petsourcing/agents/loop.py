"""The agent loop: model proposes tool calls, the executor runs them, results go back.

All safety limits live here, in code, not in the prompt:

- ``max_turns``        — model calls per agent
- ``max_tokens``       — input + output tokens per agent (cost cap)
- ``max_tool_calls``   — enforced by the executor
- stuck detection      — the same failing call (tool + args + error) N times in a row
- transient model errors are retried with exponential backoff; others fail the agent
- ``refusal`` and truncated output (``max_tokens`` stop) end the agent explicitly

Outcomes:
    completed — the model finished on its own (end_turn)
    stopped   — a limit was hit; work so far is kept, a human should look
    failed    — the model errored, refused, or was cut off
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from .executor import ToolExecutor
from .model import ModelClient, ModelError, ModelTurn

Outcome = Literal["completed", "stopped", "failed"]


@dataclass
class LoopLimits:
    max_turns: int = 20
    max_tokens: int = 200_000
    max_tool_calls: int = 40
    stuck_after: int = 3  # identical failing calls in a row
    model_retries: int = 3
    backoff_seconds: float = 2.0


@dataclass
class AgentResult:
    agent: str
    outcome: Outcome
    reason: str
    final_text: str = ""
    turns: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    messages: list[dict] = field(default_factory=list)


def run_agent(
    executor: ToolExecutor,
    model: ModelClient,
    *,
    system: str,
    task: str,
    limits: LoopLimits | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Callable[[str, dict], None] | None = None,
) -> AgentResult:
    limits = limits or LoopLimits()
    executor.max_calls = limits.max_tool_calls
    run = executor.ctx.run
    tools = executor.definitions()
    messages: list[dict] = [{"role": "user", "content": task}]
    res = AgentResult(executor.ctx.agent, "completed", "", messages=messages)
    last_failure: tuple[str, int] | None = None  # (signature, repeat count)

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, {"agent": res.agent, **data})

    def finish(outcome: Outcome, reason: str) -> AgentResult:
        res.outcome, res.reason = outcome, reason
        emit("finish", outcome=outcome, reason=reason)
        return res

    while True:
        if res.turns >= limits.max_turns:
            return finish("stopped", f"turn limit reached ({limits.max_turns})")
        if res.input_tokens + res.output_tokens >= limits.max_tokens:
            return finish("stopped", f"token budget reached ({limits.max_tokens})")

        turn = _call_model(model, system, messages, tools, limits, sleep, emit)
        if isinstance(turn, str):
            return finish("failed", turn)
        res.turns += 1
        res.input_tokens += turn.input_tokens
        res.output_tokens += turn.output_tokens
        run.input_tokens += turn.input_tokens
        run.output_tokens += turn.output_tokens
        messages.append({"role": "assistant", "content": turn.content})
        if turn.text:
            res.final_text = turn.text
            emit("text", text=turn.text)

        if turn.stop_reason == "refusal":
            return finish("failed", "model refused")
        if turn.stop_reason == "max_tokens":
            return finish("failed", "model output was truncated (max_tokens)")
        if turn.stop_reason == "end_turn" or not turn.tool_uses:
            return finish("completed", "model finished")

        if executor.budget_exhausted:
            return finish("stopped", f"tool call budget reached ({limits.max_tool_calls})")

        results = []
        for block in turn.tool_uses:  # all results go back in ONE user message
            out = executor.call(block["name"], block.get("input") or {})
            res.tool_calls += 1
            emit("tool", name=block["name"], input=block.get("input"), ok=out["ok"],
                 error=out.get("error"))
            if not out["ok"]:
                res.failed_tool_calls += 1
            results.append({"type": "tool_result", "tool_use_id": block["id"],
                            "content": json.dumps(out, ensure_ascii=False), "is_error": not out["ok"]})
        messages.append({"role": "user", "content": results})

        # Stuck: the model keeps making the exact same failing call.
        failing = [(b, json.loads(r["content"])) for b, r in zip(turn.tool_uses, results) if r["is_error"]]
        if len(failing) == len(results):
            sig = json.dumps([(b["name"], b.get("input"), o.get("error")) for b, o in failing],
                             sort_keys=True, ensure_ascii=False)
            count = last_failure[1] + 1 if last_failure and last_failure[0] == sig else 1
            last_failure = (sig, count)
            if count >= limits.stuck_after:
                return finish("stopped", f"stuck: same failing call repeated {count} times "
                                         f"({failing[0][0]['name']}: {failing[0][1].get('error')})")
        else:
            last_failure = None


def _call_model(model, system, messages, tools, limits, sleep, emit) -> ModelTurn | str:
    """Returns a turn, or an error string if the model failed for good."""
    for attempt in range(limits.model_retries + 1):
        try:
            return model.step(system=system, messages=messages, tools=tools)
        except ModelError as e:
            if not e.retryable or attempt == limits.model_retries:
                return f"model error: {e}" + (" (after retries)" if e.retryable else "")
            delay = limits.backoff_seconds * (2 ** attempt)
            emit("retry", attempt=attempt + 1, delay=delay, error=str(e))
            sleep(delay)
    return "unreachable"
