"""Model interface used by the agent loop, plus a scripted fake model for tests and demos.

Messages and content blocks follow the Anthropic Messages API shape
(``text`` / ``tool_use`` / ``tool_result`` blocks, ``stop_reason``), so the real
client in a later step is a thin adapter. The loop appends assistant content
exactly as returned, which is also what the real API requires (e.g. thinking
blocks must be passed back unchanged).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


@dataclass
class ModelTurn:
    content: list[dict]  # content blocks
    stop_reason: StopReason
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()


class ModelError(Exception):
    """Raised by a model client. ``retryable`` marks transient failures (rate limits, 5xx, network)."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ModelClient(Protocol):
    name: str

    def step(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


# --------------------------------------------------------------------------- scripted fake


_ids = itertools.count(1)


def tool_call(name: str, input: dict | None = None) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": input or {}}


def say(text: str) -> dict:
    return {"type": "text", "text": text}


def turn(*blocks: dict, stop_reason: StopReason | None = None, tokens: tuple[int, int] = (100, 50)) -> ModelTurn:
    """Build a turn. stop_reason defaults to tool_use if any tool is called, else end_turn."""
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return ModelTurn(list(blocks), stop_reason, *tokens)


ScriptStep = ModelTurn | Exception | Callable[[list[dict]], "ModelTurn | Exception"]


def last_tool_results(messages: list[dict]) -> list[dict]:
    """The tool_result blocks the loop sent in the latest user message."""
    last = messages[-1]
    if last["role"] != "user" or isinstance(last["content"], str):
        return []
    return [b for b in last["content"] if b.get("type") == "tool_result"]


@dataclass
class ScriptedModel:
    """Plays back a fixed script. A step may be a turn, an exception to raise, or a
    function of the conversation so far (to react to tool results, e.g. fix a
    rejected citation). Running past the end of the script is a test failure."""

    script: list[ScriptStep]
    name: str = "scripted"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def step(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages],
                           "tools": [t["name"] for t in tools]})
        i = len(self.calls) - 1
        if i >= len(self.script):
            raise AssertionError(f"ScriptedModel ran out of script at call {i + 1}")
        step = self.script[i]
        if callable(step) and not isinstance(step, ModelTurn):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        return step
