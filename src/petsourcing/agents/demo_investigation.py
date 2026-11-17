"""Scripted two-round investigation that ends in "hold" (暂不进入). No API key needed.

The story the agents act out, on the fictional demo data:
- demand:     cleaning grooming tools is a real, repeated pain; some cats fear the tools.
- competitor: product P1 already solves cleaning with a push-button design (positive review);
              its durability may be weak — a possible gap, but one review only.
- cost:       an improved, more durable version priced against P1 loses money in a cautious scenario.
- challenger: "cats fear tools" is thin; the durability gap is one review; P1 counter-examples cleaning.
- coordinator (round 1): accepts / rebuts / marks needs_research; asks competitor to look for
              durability complaints on other products.
- competitor (round 2): finds none, records nothing new.
- coordinator (round 2): decides HOLD and asks a human to collect durability reviews.

Every script step reads ids and numbers from real tool results, like a model would.
"""

from __future__ import annotations

import json

from .demo_script import demo_demand_model
from .model import ModelClient, ScriptedModel, say, tool_call, turn


def _payloads(messages: list[dict]):
    for m in messages:
        if m["role"] == "user" and not isinstance(m["content"], str):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    yield json.loads(b["content"])


def _evidence(messages, fragment: str) -> int:
    for p in _payloads(messages):
        for r in p.get("results", []):
            if fragment.lower() in r["text"].lower():
                return r["evidence_id"]
    raise LookupError(fragment)


def _claim(messages, fragment: str) -> dict:
    for p in reversed(list(_payloads(messages))):
        for c in p.get("claims", []):
            if fragment in c["text"]:
                return c
    raise LookupError(fragment)


def _computed(messages) -> dict:
    for p in _payloads(messages):
        if "result" in p and "most_sensitive" in p:
            return p
    raise LookupError("compute_economics result")


# --------------------------------------------------------------------------- competitor


def _competitor_round1() -> ScriptedModel:
    def risk(m):
        return turn(say("P1 的好评说明按钮推毛设计已经解决了清理问题。"), tool_call("add_claim", {
            "type": "risk",
            "text": "竞品 P1 的按钮推毛设计已解决清理问题，有用户明确好评；单纯主打“易清洁”可能没有空缺。",
            "citations": [{"evidence_id": _evidence(m, "one sheet"),
                           "quote_span": "hair comes off in one sheet when I press the button"}]}))

    def gap(m):
        return turn(say("不过 P1 的按钮有耐用性差评，可能是改进方向，但只有一条。"), tool_call("add_claim", {
            "type": "gap",
            "text": "现有自清洁机构的耐用性可能不足，耐用的自清洁梳可能是改进方向（目前仅 1 条证据）。",
            "citations": [{"evidence_id": _evidence(m, "stopped working"),
                           "quote_span": "The button that pushes the hair off stopped working after two months"}],
            "confidence": 0.3}))

    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("list_products"), tool_call("search_evidence", {"query": "button press clean sheet"})),
        risk, gap,
        turn(say("记录了 1 条风险（P1 已解决清理）和 1 条低置信度空缺（耐用性）。")),
    ])


def _competitor_round2() -> ScriptedModel:
    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("search_evidence", {"query": "broke broken durable stopped working"})),
        turn(say("除 P1 那一条以外，没有找到其他产品的耐用性或损坏相关评论。证据仍然不足，不新增论断。")),
    ])


# --------------------------------------------------------------------------- cost


CAUTIOUS = {"price": 24.99, "unit_cost": 18.9, "fx_to_cad": 0.19, "order_qty": 500,
            "inbound_freight_total": 650, "packaging_per_unit": 0.6, "labeling_per_unit": 0.25,
            "referral_rate": 0.15, "fulfillment_fee_per_unit": 6.2, "cac_per_order": 8,
            "fixed_costs": 600, "sell_through": 0.5}


def _cost() -> ScriptedModel:
    def claim(m):
        out = _computed(m)
        r = out["result"]
        text = (f"保守情景（售价 24.99 CAD 对标 P1、改进版单价 18.9 CNY、获客成本 8、售罄 50%）下，"
                f"单件贡献利润仅 {r['contribution_per_unit']:.2f} CAD，本批亏损 {abs(r['profit']):,.0f} CAD，"
                f"需卖出 {r['units_to_recover']} 件才能回本，超过 MOQ 500 件。")
        return turn(say("用计算结果记录成本结论。"), tool_call("add_claim", {
            "type": "cost", "text": text, "source_step_id": out["step_id"]}))

    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("list_quotes"), tool_call("list_products")),
        turn(say("改进的耐用机构假设比现有报价贵约 30%，按 P1 的价格做保守测算。"),
             tool_call("compute_economics", CAUTIOUS)),
        claim,
        turn(say("保守情景下亏损；结果对售价和获客成本最敏感。")),
    ])


# --------------------------------------------------------------------------- challenger & coordinator


def _challenger() -> ScriptedModel:
    def challenges(m):
        return turn(
            tool_call("add_challenge", {"claim_id": _claim(m, "害怕")["claim_id"], "type": "insufficient",
                                        "text": "只有 2 条评论，其中一条是 P4 喷雾产品特有的问题，不足以说明普遍现象。"}),
            tool_call("add_challenge", {"claim_id": _claim(m, "耐用性")["claim_id"], "type": "insufficient",
                                        "text": "只有 1 条 P1 的耐用性差评，无法判断是普遍问题还是个别情况。需要更多产品的耐用性评论。"}),
            tool_call("add_challenge", {"claim_id": _claim(m, "难以清理")["claim_id"], "type": "counterexample",
                                        "text": "P1 的好评显示按钮推毛设计已解决清理问题，这个痛点可能已被满足。"}),
        )

    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("list_claims")), challenges,
        turn(say("提出 3 条质疑；成本论断由计算结果支撑，暂无质疑。")),
    ])


def _coordinator_round1() -> ScriptedModel:
    def resolve(m):
        fear, gap, clean = _claim(m, "害怕"), _claim(m, "耐用性"), _claim(m, "难以清理")
        cite = clean["citations"][0]
        return turn(
            tool_call("resolve_challenge", {"challenge_id": fear["challenges"][0]["challenge_id"],
                                            "resolution": "accepted", "note": "证据确实不足，降为未验证。"}),
            tool_call("resolve_challenge", {"challenge_id": gap["challenges"][0]["challenge_id"],
                                            "resolution": "needs_research", "note": "需要更多产品的耐用性证据。"}),
            tool_call("resolve_challenge", {
                "challenge_id": clean["challenges"][0]["challenge_id"], "resolution": "rebutted",
                "note": "痛点本身在多个来源中出现，是真实的；P1 已解决它是另一个事实，由竞品的风险论断记录。",
                "citations": [cite]}),
            tool_call("request_followup", {"agent": "competitor",
                                           "instruction": "搜索 P1 以外产品关于按钮、机构损坏或耐用性的评论，判断自清洁机构不耐用是否普遍。"}),
        )

    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("list_claims")), resolve,
        turn(say("质疑已全部回应；耐用性空缺需要补充调查，进入第 2 轮。")),
    ])


def _coordinator_round2() -> ScriptedModel:
    def decide(m):
        clean, p1, cost = _claim(m, "难以清理"), _claim(m, "按钮推毛"), _claim(m, "保守情景")
        return turn(
            tool_call("record_decision", {
                "verdict": "hold",
                "rationale": "清理痛点真实，但竞品 P1 已基本解决；可能的改进方向（耐用性）只有 1 条证据；"
                             "按 P1 价格做保守测算时本批亏损。现在进入风险大于收益。",
                "claim_ids": [clean["claim_id"], p1["claim_id"], cost["claim_id"]],
                "assumptions": CAUTIOUS,
            }),
            tool_call("propose_action", {
                "action_type": "request_data", "reason": "耐用性空缺是唯一可能的切入点，但证据不足。",
                "payload": {"description": "收集至少 30 条自清洁梳毛刷（不限于 P1）关于按钮或机构耐用性的评论"}}),
        )

    return ScriptedModel(name="scripted-demo", script=[
        turn(tool_call("list_claims")), decide,
        turn(say("结论：暂不进入。已请求人工补充耐用性评论；如果耐用性问题普遍，再重新评估。")),
    ])


SCRIPTS = {
    ("demand", 1): demo_demand_model,
    ("competitor", 1): _competitor_round1,
    ("cost", 1): _cost,
    ("challenger", 1): _challenger,
    ("coordinator", 1): _coordinator_round1,
    ("competitor", 2): _competitor_round2,
    ("coordinator", 2): _coordinator_round2,
}


def demo_model_for(agent: str, round_no: int) -> ModelClient:
    try:
        return SCRIPTS[(agent, round_no)]()
    except KeyError:
        raise LookupError(f"no demo script for {agent} in round {round_no}") from None
