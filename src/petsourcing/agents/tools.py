"""Agent tools: the only way agents read or write data.

Each tool is a plain function ``(ctx, input) -> dict`` with a pydantic input
model (which doubles as the JSON schema sent to the model). Tools raise
``ToolError`` for expected failures; the executor turns that into a structured
error the agent can read and act on.

Everything here is deterministic code. The LLM decides *which* tool to call
and with what arguments; the tools decide what is allowed and what is true.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..claims import Citation, add_claim, check_citation
from ..economics import CostInputs, build_inputs, compute, sensitivity
from ..models import (
    ActionProposal,
    Challenge,
    ChallengeResolution,
    ChallengeType,
    Claim,
    ClaimStatus,
    ClaimType,
    Decision,
    Evidence,
    EvidenceKind,
    EvidenceTag,
    Opportunity,
    Product,
    ResearchRun,
    Stage,
    SupplierQuote,
    Verdict,
)
from ..profiles import CategoryProfile, load_profile
from ..search_index import FTS_TABLE, fts_match_expression, query_terms


class ToolError(Exception):
    """An expected failure. The message is shown to the agent so it can correct itself."""


@dataclass
class ToolContext:
    session: Session
    opp: Opportunity
    run: ResearchRun
    agent: str
    today: date | None = None

    @property
    def profile(self) -> CategoryProfile:
        return load_profile(self.opp.category_profile)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- helpers


def _ev_dict(ctx: ToolContext, e: Evidence) -> dict:
    return {
        "evidence_id": e.id,
        "kind": e.kind.value,
        "product_key": e.product.key if e.product else None,
        "text": e.raw_text,
        "rating": e.rating,
        "source": e.source_url or e.source_note,
        "captured_at": e.captured_at.isoformat(),
        "stale": ctx.profile.is_stale(e.kind.value, e.captured_at, ctx.today),
        "superseded_by": e.superseded_by_id,
    }


def _current_evidence(ctx: ToolContext):
    return select(Evidence).where(Evidence.opportunity_id == ctx.opp.id,
                                  Evidence.superseded_by_id.is_(None))


def _product_id(ctx: ToolContext, key: str | None) -> int | None:
    if key is None:
        return None
    pid = ctx.session.scalar(select(Product.id).where(Product.opportunity_id == ctx.opp.id,
                                                      Product.key == key))
    if pid is None:
        raise ToolError(f"unknown product_key '{key}'. Call list_products to see valid keys.")
    return pid


def _get_claim(ctx: ToolContext, claim_id: int) -> Claim:
    c = ctx.session.get(Claim, claim_id)
    if c is None or c.opportunity_id != ctx.opp.id:
        raise ToolError(f"claim {claim_id} does not exist in this opportunity")
    return c


def _claim_dict(c: Claim) -> dict:
    return {
        "claim_id": c.id, "type": c.type.value, "status": c.status.value, "agent": c.agent,
        "text": c.text, "confidence": c.confidence,
        "citations": [{"evidence_id": x.evidence_id, "quote_span": x.quote_span} for x in c.citations],
        "challenges": [
            {"challenge_id": ch.id, "type": ch.type.value, "text": ch.text,
             "resolution": ch.resolution.value, "resolution_note": ch.resolution_note}
            for ch in c.challenges
        ],
    }


def recompute_claim_status(c: Claim) -> ClaimStatus:
    """Derive a claim's status from its citations and challenges."""
    res = [ch for ch in c.challenges]
    if any(ch.resolution == ChallengeResolution.ACCEPTED and ch.type != ChallengeType.INSUFFICIENT
           for ch in res):
        c.status = ClaimStatus.REFUTED
    elif any(ch.resolution in (ChallengeResolution.OPEN, ChallengeResolution.NEEDS_RESEARCH)
             for ch in res):
        c.status = ClaimStatus.CHALLENGED
    elif any(ch.resolution == ChallengeResolution.ACCEPTED for ch in res) or not c.citations:
        c.status = ClaimStatus.UNVERIFIED  # accepted "insufficient evidence", or never cited
    else:
        c.status = ClaimStatus.SUPPORTED
    return c.status


# --------------------------------------------------------------------------- read tools


class SearchEvidenceIn(_In):
    query: str = Field(description="Words to search for. Matching is case-insensitive substring "
                                   "matching; any word may match, more matches rank higher.")
    kind: EvidenceKind | None = Field(None, description="Only this kind of evidence")
    product_key: str | None = Field(None, description="Only evidence about this product")
    limit: int = Field(10, ge=1, le=30)


def search_evidence(ctx: ToolContext, inp: SearchEvidenceIn) -> dict:
    long_terms, short_terms = query_terms(inp.query)
    if not long_terms and not short_terms:
        raise ToolError("query is empty")
    stmt = _current_evidence(ctx)
    if inp.kind:
        stmt = stmt.where(Evidence.kind == inp.kind)
    if inp.product_key:
        stmt = stmt.where(Evidence.product_id == _product_id(ctx, inp.product_key))

    if long_terms:
        ranked = ctx.session.execute(
            text(f"SELECT rowid, bm25({FTS_TABLE}) AS r FROM {FTS_TABLE} "
                 f"WHERE {FTS_TABLE} MATCH :m ORDER BY r"),
            {"m": fts_match_expression(long_terms)},
        ).all()
        order = {rowid: i for i, (rowid, _) in enumerate(ranked)}
        rows = list(ctx.session.scalars(stmt.where(Evidence.id.in_(order))))
        rows.sort(key=lambda e: order[e.id])
    else:
        rows = list(ctx.session.scalars(stmt.where(
            or_(*[Evidence.raw_text.ilike(f"%{t}%") for t in short_terms])).order_by(Evidence.id)))

    rows = rows[: inp.limit]
    out: dict[str, Any] = {
        "count": len(rows),
        "results": [_ev_dict(ctx, e) for e in rows],
        "note": "When citing, copy quote_span verbatim from `text`.",
    }
    if long_terms and short_terms:
        out["ignored_terms"] = short_terms
        out["note"] += " Terms shorter than 3 characters were ignored."
    return out


class GetEvidenceIn(_In):
    ids: list[int] = Field(min_length=1, max_length=20)


def get_evidence(ctx: ToolContext, inp: GetEvidenceIn) -> dict:
    found, missing = [], []
    for i in inp.ids:
        e = ctx.session.get(Evidence, i)
        if e is None or e.opportunity_id != ctx.opp.id:
            missing.append(i)
        else:
            found.append(_ev_dict(ctx, e))
    return {"evidence": found, "not_found": missing}


class NoIn(_In):
    pass


def list_products(ctx: ToolContext, inp: NoIn) -> dict:
    prods = ctx.session.scalars(select(Product).where(Product.opportunity_id == ctx.opp.id)
                                .order_by(Product.key))
    return {
        "products": [{
            "product_key": p.key, "name": p.name, "brand": p.brand, "platform": p.platform,
            "price": p.price, "currency": p.currency, "rating": p.rating,
            "review_count": p.review_count, "captured_at": p.captured_at.isoformat(),
            "stale": ctx.profile.is_stale("listing", p.captured_at, ctx.today), "notes": p.notes,
        } for p in prods],
        "note": "review_count is the number of reviews shown on the listing, NOT units sold. "
                "null means unknown — do not estimate it.",
    }


def list_quotes(ctx: ToolContext, inp: NoIn) -> dict:
    quotes = list(ctx.session.scalars(select(SupplierQuote).where(
        SupplierQuote.opportunity_id == ctx.opp.id).order_by(SupplierQuote.quoted_at)))
    # Same supplier + item + MOQ tier = one price series; different MOQs are price
    # tiers, not history, so each tier has its own "latest".
    latest: dict[tuple, int] = {}
    for q in quotes:
        latest[(q.supplier, q.product_desc, q.moq)] = q.id  # sorted by date, so last wins
    return {"quotes": [{
        "quote_id": q.id, "supplier": q.supplier, "product_desc": q.product_desc,
        "unit_price": q.unit_price, "currency": q.currency, "moq": q.moq,
        "unit_weight_g": q.unit_weight_g, "dims_cm": q.dims_cm, "incoterm": q.incoterm,
        "lead_time_days": q.lead_time_days, "quoted_at": q.quoted_at.isoformat(),
        "stale": ctx.profile.is_stale("quote", q.quoted_at, ctx.today),
        "is_latest_for_tier": latest[(q.supplier, q.product_desc, q.moq)] == q.id,
    } for q in quotes]}


class PainStatsIn(_In):
    product_key: str | None = None


def pain_point_stats(ctx: ToolContext, inp: PainStatsIn) -> dict:
    base = _current_evidence(ctx).where(Evidence.kind.in_([EvidenceKind.REVIEW,
                                                            EvidenceKind.DISCUSSION,
                                                            EvidenceKind.INTERVIEW]))
    if inp.product_key:
        base = base.where(Evidence.product_id == _product_id(ctx, inp.product_key))
    evidence = list(ctx.session.scalars(base))
    ids = [e.id for e in evidence]
    tags = list(ctx.session.scalars(select(EvidenceTag).where(EvidenceTag.evidence_id.in_(ids))))
    tagged = {t.evidence_id for t in tags}
    per_point: dict[str, set[int]] = defaultdict(set)
    for t in tags:
        if t.pain_point:
            per_point[t.pain_point].add(t.evidence_id)
    counts = Counter({k: len(v) for k, v in per_point.items()})
    return {
        "total_evidence": len(ids),
        "tagged_evidence": len(tagged),
        "pain_points": [{"pain_point": k, "evidence_count": n,
                         "example_evidence_ids": sorted(per_point[k])[:5]}
                        for k, n in counts.most_common()],
        "note": ("Counts are over the collected sample only, not the market. Always state the "
                 "denominator (tagged_evidence) when quoting a share."
                 + ("" if tags else " No evidence has been tagged yet.")),
    }


_COST_FIELDS = [f.name for f in fields(CostInputs)]
_REQUIRED_COST = {"price": (float, ...), "unit_cost": (float, ...), "order_qty": (int, ...)}
EconomicsIn = create_model(
    "EconomicsIn", __base__=_In,
    **{n: _REQUIRED_COST.get(n, (float | None, None)) for n in _COST_FIELDS},
)
EconomicsIn.__doc__ = "Cost inputs. Omitted fields use the category profile's labelled assumptions."


def compute_economics(ctx: ToolContext, inp: BaseModel) -> dict:
    values = {k: v for k, v in inp.model_dump().items() if v is not None}
    try:
        inputs, sources = build_inputs(values, profile=ctx.profile)
    except ValueError as e:
        raise ToolError(str(e)) from e
    r = compute(inputs)
    return {
        "result": r.as_dict(),
        "assumed_inputs": {k: {"value": getattr(inputs, k), "source": s.kind, "note": s.note}
                           for k, s in sources.items() if s.kind in ("profile_default", "assumption")
                           and k not in values},
        "most_sensitive": [{"param": s.param, "profit_low": round(s.low_profit, 2),
                            "profit_high": round(s.high_profit, 2)}
                           for s in sensitivity(inputs)[:3]],
        "note": "All money in CAD excluding sales tax. Quote these numbers as computed, do not recompute.",
    }


# --------------------------------------------------------------------------- claim tools


class CitationIn(_In):
    evidence_id: int
    quote_span: str = Field(description="Text copied verbatim from the evidence. '...' may join fragments.")


class AddClaimIn(_In):
    type: ClaimType
    text: str = Field(description="One specific, checkable statement.")
    citations: list[CitationIn] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0, le=1)


AGENT_CLAIM_TYPES: dict[str, set[ClaimType]] = {
    "demand": {ClaimType.PAIN, ClaimType.OTHER},
    "competitor": {ClaimType.GAP, ClaimType.RISK, ClaimType.OTHER},
    "cost": {ClaimType.COST, ClaimType.RISK},
    "manual": set(ClaimType),
}


def add_claim_tool(ctx: ToolContext, inp: AddClaimIn) -> dict:
    allowed = AGENT_CLAIM_TYPES.get(ctx.agent, set())
    if inp.type not in allowed:
        raise ToolError(f"agent '{ctx.agent}' may only add claims of type "
                        f"{sorted(t.value for t in allowed)}")
    r = add_claim(ctx.session, ctx.opp, agent=ctx.agent, type=inp.type, text=inp.text,
                  citations=[Citation(c.evidence_id, c.quote_span) for c in inp.citations],
                  run_id=ctx.run.id, confidence=inp.confidence, today=ctx.today)
    if not r.ok:
        raise ToolError("claim rejected, nothing was saved: " + " | ".join(r.errors))
    return {"claim_id": r.claim_id, "status": r.status.value, "warnings": r.warnings}


class ListClaimsIn(_In):
    status: ClaimStatus | None = None
    type: ClaimType | None = None


def list_claims(ctx: ToolContext, inp: ListClaimsIn) -> dict:
    stmt = select(Claim).where(Claim.opportunity_id == ctx.opp.id).order_by(Claim.id)
    if inp.status:
        stmt = stmt.where(Claim.status == inp.status)
    if inp.type:
        stmt = stmt.where(Claim.type == inp.type)
    return {"claims": [_claim_dict(c) for c in ctx.session.scalars(stmt)]}


class AddChallengeIn(_In):
    claim_id: int
    type: ChallengeType
    text: str = Field(description="Why the claim may not hold, and what would settle it.")


def add_challenge(ctx: ToolContext, inp: AddChallengeIn) -> dict:
    c = _get_claim(ctx, inp.claim_id)
    if c.status == ClaimStatus.REFUTED:
        raise ToolError(f"claim {c.id} is already refuted")
    for ch in c.challenges:  # retry-safe: same objection twice is one challenge
        if ch.type == inp.type and " ".join(ch.text.split()).lower() == " ".join(inp.text.split()).lower():
            return {"challenge_id": ch.id, "duplicate": True, "claim_status": c.status.value}
    ch = Challenge(run_id=ctx.run.id, type=inp.type, text=inp.text.strip())
    c.challenges.append(ch)
    ctx.session.flush()
    recompute_claim_status(c)
    return {"challenge_id": ch.id, "duplicate": False, "claim_status": c.status.value}


class ResolveChallengeIn(_In):
    challenge_id: int
    resolution: Literal["accepted", "rebutted", "needs_research"]
    note: str
    citations: list[CitationIn] = Field(default_factory=list,
                                        description="Required when rebutting: evidence against the challenge.")


def resolve_challenge(ctx: ToolContext, inp: ResolveChallengeIn) -> dict:
    ch = ctx.session.get(Challenge, inp.challenge_id)
    if ch is None or ch.claim.opportunity_id != ctx.opp.id:
        raise ToolError(f"challenge {inp.challenge_id} does not exist in this opportunity")
    if ch.resolution in (ChallengeResolution.ACCEPTED, ChallengeResolution.REBUTTED):
        raise ToolError(f"challenge {ch.id} is already {ch.resolution.value}")
    note = inp.note.strip()
    if inp.resolution == "rebutted":
        if not inp.citations:
            raise ToolError("a rebuttal must cite evidence")
        errors = [chk.error for chk in (check_citation(ctx.session, ctx.opp,
                  Citation(c.evidence_id, c.quote_span), ctx.today) for c in inp.citations) if not chk.ok]
        if errors:
            raise ToolError("rebuttal rejected: " + " | ".join(errors))
        note += "\nEvidence: " + "; ".join(f'#{c.evidence_id} "{c.quote_span}"' for c in inp.citations)
    ch.resolution = ChallengeResolution(inp.resolution)
    ch.resolution_note = note
    ctx.session.flush()
    status = recompute_claim_status(ch.claim)
    return {"challenge_id": ch.id, "resolution": ch.resolution.value, "claim_status": status.value}


# --------------------------------------------------------------------------- decision & actions


class RecordDecisionIn(_In):
    verdict: Verdict
    rationale: str
    claim_ids: list[int] = Field(min_length=1, description="Claims the decision rests on")
    assumptions: dict[str, Any] | None = Field(None, description="Key assumptions, e.g. cost inputs used")
    validation_plan: str | None = Field(None, description="Required when verdict is 'enter'")


def record_decision(ctx: ToolContext, inp: RecordDecisionIn) -> dict:
    claims = [_get_claim(ctx, i) for i in inp.claim_ids]
    open_ch = list(ctx.session.scalars(select(Challenge.id).join(Claim).where(
        Claim.opportunity_id == ctx.opp.id, Challenge.resolution == ChallengeResolution.OPEN)))
    if open_ch:
        raise ToolError(f"cannot decide while challenges are unanswered: {open_ch}. "
                        "Resolve each one first (accepted / rebutted / needs_research).")
    if inp.verdict == Verdict.ENTER:
        if not (inp.validation_plan or "").strip():
            raise ToolError("an 'enter' verdict requires a concrete validation_plan")
        weak = [c.id for c in claims if c.status != ClaimStatus.SUPPORTED]
        if weak:
            raise ToolError(f"an 'enter' verdict can only rest on supported claims; not supported: {weak}")
    d = Decision(opportunity_id=ctx.opp.id, run_id=ctx.run.id, verdict=inp.verdict,
                 rationale=inp.rationale.strip(), claim_ids=inp.claim_ids,
                 assumptions=inp.assumptions, validation_plan=inp.validation_plan)
    ctx.session.add(d)
    ctx.session.flush()
    return {"decision_id": d.id, "verdict": d.verdict.value,
            "claim_statuses": {c.id: c.status.value for c in claims}}


ACTION_TYPES = ("change_stage", "request_data", "order_sample", "contact_supplier")


class ProposeActionIn(_In):
    action_type: Literal["change_stage", "request_data", "order_sample", "contact_supplier"]
    payload: dict[str, Any] = Field(description='e.g. {"stage": "awaiting_sample"} or '
                                                '{"description": "import reviews for P3"}')
    reason: str


def idempotency_key(opp_id: int, action_type: str, payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{opp_id}|{action_type}|{canon}".encode()).hexdigest()


def propose_action(ctx: ToolContext, inp: ProposeActionIn) -> dict:
    if inp.action_type == "change_stage":
        try:
            Stage(inp.payload.get("stage"))
        except ValueError:
            raise ToolError(f"payload.stage must be one of {[s.value for s in Stage]}") from None
    elif not str(inp.payload.get("description", "")).strip():
        raise ToolError("payload.description is required for this action type")
    key = idempotency_key(ctx.opp.id, inp.action_type, inp.payload)
    existing = ctx.session.scalar(select(ActionProposal).where(ActionProposal.idempotency_key == key))
    if existing:
        return {"proposal_id": existing.id, "duplicate": True, "status": existing.status.value}
    p = ActionProposal(opportunity_id=ctx.opp.id, run_id=ctx.run.id, action_type=inp.action_type,
                       payload=inp.payload, reason=inp.reason.strip(), idempotency_key=key)
    ctx.session.add(p)
    try:
        ctx.session.flush()
    except IntegrityError:  # lost a race with a concurrent identical proposal
        raise ToolError("duplicate proposal; call again to get its id") from None
    return {"proposal_id": p.id, "duplicate": False, "status": p.status.value,
            "note": "Proposed only. A human must approve before anything happens."}


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], dict]
    writes: bool = False


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_evidence", "Full-text search over current (non-superseded) evidence for this "
         "opportunity. Returns full text to quote from.", SearchEvidenceIn, search_evidence),
    Tool("get_evidence", "Fetch evidence by id.", GetEvidenceIn, get_evidence),
    Tool("list_products", "List competing products with price, rating and review count "
         "(review count is not sales).", NoIn, list_products),
    Tool("list_quotes", "List supplier quotes, oldest first, marking the latest quote per supplier, "
         "item and MOQ tier (different MOQs are price tiers, not history).",
         NoIn, list_quotes),
    Tool("pain_point_stats", "Counts of tagged pain points over the collected sample, with the "
         "denominator.", PainStatsIn, pain_point_stats),
    Tool("compute_economics", "Deterministic unit economics: landed cost, margin, cash needed, "
         "break-even, profit, sensitivity.", EconomicsIn, compute_economics),
    Tool("add_claim", "Record one claim with verbatim citations. If any citation does not match "
         "its evidence, the whole claim is rejected with the reason.", AddClaimIn, add_claim_tool,
         writes=True),
    Tool("list_claims", "List claims with their citations and challenges.", ListClaimsIn, list_claims),
    Tool("add_challenge", "Challenge a claim: counterexample, insufficient evidence, "
         "contradiction or logic gap.", AddChallengeIn, add_challenge, writes=True),
    Tool("resolve_challenge", "Answer a challenge. Rebuttals must cite evidence.",
         ResolveChallengeIn, resolve_challenge, writes=True),
    Tool("record_decision", "Record the verdict (enter / hold / drop). Refused while any "
         "challenge is unanswered.", RecordDecisionIn, record_decision, writes=True),
    Tool("propose_action", "Propose a real-world action for human approval. Never executes "
         "anything itself. Repeating the same proposal returns the existing one.",
         ProposeActionIn, propose_action, writes=True),
]}

_READ = {"search_evidence", "get_evidence", "list_products", "pain_point_stats"}
AGENT_TOOLS: dict[str, set[str]] = {
    "demand": _READ | {"add_claim"},
    "competitor": _READ | {"add_claim"},
    "cost": _READ | {"list_quotes", "compute_economics", "add_claim"},
    "challenger": _READ | {"list_quotes", "compute_economics", "list_claims", "add_challenge"},
    "coordinator": _READ | {"list_claims", "resolve_challenge", "record_decision", "propose_action"},
    "manual": set(TOOLS),
}
