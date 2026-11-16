"""Database schema.

The evidence chain is the core asset of the project:

    Evidence (raw text + source + capture date)
      -> Claim (an agent's statement, must cite evidence with a verbatim quote)
      -> Challenge (the sceptic agent's objection to a claim)
      -> Decision (verdict + the claims and assumptions it rested on)
      -> Validation (what later happened in the real world)

Phase 0 only fills Opportunity / Product / Evidence / SupplierQuote via import.
The agent-side tables are defined now so the data model is settled before any
agent is written.

Conventions:
- Unknown values are NULL, never a guess. Where useful, a ``*_note`` column
  explains why something is unknown.
- ``review_count`` is the number of reviews shown on a listing. It is NOT a
  sales figure and must never be used as one.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum]) -> Enum:
    # Store the enum *value* (e.g. "to_research") rather than its name.
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False)


# --------------------------------------------------------------------------- enums


class Stage(str, enum.Enum):
    TO_RESEARCH = "to_research"  # 待研究
    RESEARCHING = "researching"  # 研究中
    AWAITING_SAMPLE = "awaiting_sample"  # 待拿样
    TESTING = "testing"  # 测试中
    DROPPED = "dropped"  # 放弃


class EvidenceKind(str, enum.Enum):
    REVIEW = "review"  # 用户评论
    LISTING = "listing"  # 商品页面描述
    DISCUSSION = "discussion"  # 论坛 / 社区讨论
    INTERVIEW = "interview"  # 用户访谈
    QUOTE = "quote"  # 供应商报价原文
    POLICY = "policy"  # 平台费用、关税、法规等政策资料
    NOTE = "note"  # 你自己的观察记录


class ClaimType(str, enum.Enum):
    PAIN = "pain"  # 用户痛点
    GAP = "gap"  # 市场空缺 / 竞品不足
    COST = "cost"  # 成本结论
    RISK = "risk"  # 风险
    OTHER = "other"


class ClaimStatus(str, enum.Enum):
    UNVERIFIED = "unverified"  # 无有效引用
    SUPPORTED = "supported"  # 引用校验通过
    CHALLENGED = "challenged"  # 有未解决的质疑
    REFUTED = "refuted"  # 被推翻


class ChallengeType(str, enum.Enum):
    COUNTEREXAMPLE = "counterexample"  # 反例
    INSUFFICIENT = "insufficient"  # 证据不足
    CONTRADICTION = "contradiction"  # 与其他证据矛盾
    LOGIC_GAP = "logic_gap"  # 推理跳跃


class ChallengeResolution(str, enum.Enum):
    OPEN = "open"
    ACCEPTED = "accepted"  # 质疑成立
    REBUTTED = "rebutted"  # 被反驳（需引用证据）
    NEEDS_RESEARCH = "needs_research"  # 需要补充调查


class Verdict(str, enum.Enum):
    ENTER = "enter"  # 进入验证
    HOLD = "hold"  # 暂不进入
    DROP = "drop"  # 放弃


class ValidationKind(str, enum.Enum):
    SAMPLE = "sample"  # 拿样体验
    INTERVIEW = "interview"  # 访谈
    TEST_SALE = "test_sale"  # 小规模试卖
    OTHER = "other"


class ValidationResult(str, enum.Enum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


# --------------------------------------------------------------------------- research targets


class Opportunity(Base):
    """A product opportunity being researched (one card on the board)."""

    __tablename__ = "opportunity"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    category_profile: Mapped[str] = mapped_column(String(80))  # key of config/categories/*.toml
    target_market: Mapped[str] = mapped_column(String(10), default="CA")
    stage: Mapped[Stage] = mapped_column(_enum(Stage), default=Stage.TO_RESEARCH)
    description: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    products: Mapped[list[Product]] = relationship(back_populates="opportunity")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="opportunity")
    quotes: Mapped[list[SupplierQuote]] = relationship(back_populates="opportunity")


class Product(Base):
    """A competing product observed in the market."""

    __tablename__ = "product"
    __table_args__ = (UniqueConstraint("opportunity_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    key: Mapped[str] = mapped_column(String(40))  # short id used in CSVs, e.g. "P1"
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(120))
    platform: Mapped[str | None] = mapped_column(String(60))
    url: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    review_count: Mapped[int | None] = mapped_column(Integer)  # NOT sales
    rating: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship(back_populates="products")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="product")


class Evidence(Base):
    """A piece of raw source material. Every claim must trace back to these."""

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("opportunity_id", "content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"))
    kind: Mapped[EvidenceKind] = mapped_column(_enum(EvidenceKind))
    raw_text: Mapped[str] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(Float)  # e.g. review stars, if any
    source_url: Mapped[str | None] = mapped_column(Text)
    source_note: Mapped[str | None] = mapped_column(Text)  # when there is no URL
    captured_at: Mapped[date] = mapped_column(Date)
    language: Mapped[str | None] = mapped_column(String(10))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch: Mapped[str | None] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Memory hygiene: newer evidence (e.g. an updated price page) supersedes older
    # evidence instead of overwriting it, so history is kept but not used as current.
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"))

    opportunity: Mapped[Opportunity] = relationship(back_populates="evidence")
    product: Mapped[Product | None] = relationship(back_populates="evidence")
    tags: Mapped[list[EvidenceTag]] = relationship(back_populates="evidence")


class EvidenceTag(Base):
    """Structured labels extracted from a piece of evidence (phase 1: batch LLM tagging)."""

    __tablename__ = "evidence_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id"))
    pain_point: Mapped[str | None] = mapped_column(String(120))
    scenario: Mapped[str | None] = mapped_column(String(120))
    sentiment: Mapped[str | None] = mapped_column(String(20))
    tagged_by: Mapped[str] = mapped_column(String(60))  # "manual" or a model id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    evidence: Mapped[Evidence] = relationship(back_populates="tags")


class SupplierQuote(Base):
    """A supplier quote. Multiple quotes per supplier over time = price history."""

    __tablename__ = "supplier_quote"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"))
    supplier: Mapped[str] = mapped_column(String(200))
    product_desc: Mapped[str] = mapped_column(Text)
    unit_price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    moq: Mapped[int | None] = mapped_column(Integer)
    unit_weight_g: Mapped[float | None] = mapped_column(Float)
    dims_cm: Mapped[str | None] = mapped_column(String(60))  # "L x W x H"
    incoterm: Mapped[str | None] = mapped_column(String(10))  # EXW / FOB / DDP ...
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    quoted_at: Mapped[date] = mapped_column(Date)
    source_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship(back_populates="quotes")


# --------------------------------------------------------------------------- agent side (phase 1+)


class ResearchRun(Base):
    __tablename__ = "research_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    round: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="running")  # running/succeeded/failed/paused
    # Reproducibility: which model and prompt set produced this run, so an eval
    # regression can be traced to the exact change that caused it.
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class AgentStep(Base):
    """One tool call made by an agent — the raw trace behind the 'research process' view."""

    __tablename__ = "agent_step"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id"))
    agent: Mapped[str] = mapped_column(String(40))
    tool_name: Mapped[str] = mapped_column(String(80))
    tool_input: Mapped[dict | None] = mapped_column(JSON)
    tool_output: Mapped[dict | None] = mapped_column(JSON)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)  # e.g. "citation rejected: quote not found"
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Claim(Base):
    __tablename__ = "claim"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id"))
    agent: Mapped[str] = mapped_column(String(40))  # "demand", "competitor", "manual", ...
    type: Mapped[ClaimType] = mapped_column(_enum(ClaimType))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[ClaimStatus] = mapped_column(_enum(ClaimStatus), default=ClaimStatus.UNVERIFIED)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    citations: Mapped[list[ClaimEvidence]] = relationship(back_populates="claim")
    challenges: Mapped[list[Challenge]] = relationship(back_populates="claim")


class ClaimEvidence(Base):
    """A citation. ``quote_span`` must be found verbatim (after normalisation) in the evidence."""

    __tablename__ = "claim_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"))
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id"))
    quote_span: Mapped[str] = mapped_column(Text)

    claim: Mapped[Claim] = relationship(back_populates="citations")


class Challenge(Base):
    __tablename__ = "challenge"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id"))
    type: Mapped[ChallengeType] = mapped_column(_enum(ChallengeType))
    text: Mapped[str] = mapped_column(Text)
    resolution: Mapped[ChallengeResolution] = mapped_column(
        _enum(ChallengeResolution), default=ChallengeResolution.OPEN
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="challenges")


class Decision(Base):
    """A verdict, with a snapshot of the claims and assumptions it was based on."""

    __tablename__ = "decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id"))
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict))
    rationale: Mapped[str] = mapped_column(Text)
    claim_ids: Mapped[list | None] = mapped_column(JSON)
    assumptions: Mapped[dict | None] = mapped_column(JSON)  # e.g. economics inputs used
    validation_plan: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionStatus(str, enum.Enum):
    PROPOSED = "proposed"  # an agent suggested it
    APPROVED = "approved"  # a human approved it
    REJECTED = "rejected"
    EXECUTED = "executed"


class ActionProposal(Base):
    """Anything with real-world side effects goes through here.

    Agents may only *propose* (change stage, contact a supplier, order samples,
    spend money). A human approves. ``idempotency_key`` is unique, so a retried
    agent run that proposes the same action again is a no-op instead of a
    duplicate, and execution checks ``status`` before acting.
    """

    __tablename__ = "action_proposal"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id"))
    action_type: Mapped[str] = mapped_column(String(60))  # "change_stage", "order_sample", ...
    payload: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[ActionStatus] = mapped_column(_enum(ActionStatus), default=ActionStatus.PROPOSED)
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Validation(Base):
    """Real-world feedback: samples, interviews, test sales."""

    __tablename__ = "validation"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"))
    kind: Mapped[ValidationKind] = mapped_column(_enum(ValidationKind))
    happened_on: Mapped[date] = mapped_column(Date)
    summary: Mapped[str] = mapped_column(Text)
    result: Mapped[ValidationResult | None] = mapped_column(_enum(ValidationResult))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
