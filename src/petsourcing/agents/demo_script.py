"""A scripted demand agent for demos and tests: no API key, fully deterministic.

It behaves like a model would: looks around, makes one citation mistake, reads
the rejection, fixes it, and summarises. Used by `petsrc agent run --model demo`.
"""

from __future__ import annotations

import json

from .model import ModelTurn, ScriptedModel, last_tool_results, say, tool_call, turn


def _find(messages: list[dict], fragment: str) -> int:
    """Evidence id of the first search result (anywhere in the conversation) containing fragment."""
    for m in messages:
        if m["role"] != "user" or isinstance(m["content"], str):
            continue
        for block in m["content"]:
            data = json.loads(block.get("content", "{}"))
            for r in data.get("results", []):
                if fragment.lower() in r["text"].lower():
                    return r["evidence_id"]
    raise LookupError(fragment)


def _first_claim(messages: list[dict]) -> ModelTurn:
    teeth = _find(messages, "stuck between the teeth")
    base = _find(messages, "packed at the base")
    return turn(
        say("清洗问题在多个产品上出现，我先记录这个痛点。"),
        tool_call("add_claim", {
            "type": "pain",
            "text": "梳毛工具难以清理：毛发会卡在梳齿之间或堆积在底部，需要手动清除。",
            "citations": [
                {"evidence_id": teeth, "quote_span": "hair always gets stuck in the teeth"},  # wrong
                {"evidence_id": base, "quote_span": "ends up with hair packed at the base"},
            ],
        }),
    )


def _fix_claim(messages: list[dict]) -> ModelTurn:
    (result,) = last_tool_results(messages)
    if not result["is_error"]:
        return turn(say("论断已记录。"))
    teeth = _find(messages, "stuck between the teeth")
    base = _find(messages, "packed at the base")
    return turn(
        say("第一条引用不是原文，我改成证据里的原话。"),
        tool_call("add_claim", {
            "type": "pain",
            "text": "梳毛工具难以清理：毛发会卡在梳齿之间或堆积在底部，需要手动清除。",
            "citations": [
                {"evidence_id": teeth, "quote_span": "hair gets stuck between the teeth"},
                {"evidence_id": base, "quote_span": "ends up with hair packed at the base"},
            ],
            "confidence": 0.6,
        }),
    )


def _second_claim(messages: list[dict]) -> ModelTurn:
    runs_away = _find(messages, "runs away")
    mist = _find(messages, "mist scares")
    return turn(
        say("另外，有些猫害怕梳毛工具，这限制了使用场景。"),
        tool_call("add_claim", {
            "type": "pain",
            "text": "部分猫害怕梳毛工具（针太尖、喷雾声音），主人难以坚持使用。",
            "citations": [
                {"evidence_id": runs_away, "quote_span": "my cat runs away as soon as she sees it"},
                {"evidence_id": mist, "quote_span": "the mist scares my cat"},
            ],
        }),
    )


def demo_demand_model() -> ScriptedModel:
    return ScriptedModel(name="scripted-demo", script=[
        turn(say("先看看有哪些竞品和已有的痛点统计。"),
             tool_call("list_products"), tool_call("pain_point_stats")),
        turn(tool_call("search_evidence", {"query": "clean hair stuck packed"}),
             tool_call("search_evidence", {"query": "scared runs away mist"})),
        _first_claim,
        _fix_claim,
        _second_claim,
        turn(say("已记录 2 条痛点论断：\n"
                 "1. 清理困难（2 个不同来源，涉及梳子和讨论帖）\n"
                 "2. 猫害怕工具（2 个不同产品）\n"
                 "仍未知：评论尚未打标签，无法给出各痛点在样本中的占比；"
                 "也不知道这些问题在已收集样本之外有多普遍。")),
    ])
