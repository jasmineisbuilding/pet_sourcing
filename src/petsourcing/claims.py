"""Claims and citation verification — the anti-fabrication guardrail.

Every claim (from a human now, from an agent in phase 1) goes through
``add_claim``. The rules are enforced in code, not in a prompt:

1. A citation must point to evidence that exists and belongs to the same opportunity.
2. Its ``quote_span`` must be found in that evidence's text (after normalising
   case, whitespace, quotes and dashes). ``...`` may join fragments, which must
   appear in order. Very short quotes are rejected — "the" proves nothing.
3. Superseded evidence cannot be cited: the error names the evidence that replaced it.
4. If *any* citation fails, the whole claim is rejected and nothing is written.
   The caller (an agent) gets the reasons back and can retry.
5. A claim with no citations is allowed but stored as ``unverified``.

Warnings (claim is stored, but flagged):
- cited evidence is past its freshness window;
- a number in the claim text appears in none of the cited quotes (it may be a
  computed figure — phase 1 will require those to come from a tool result).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from .models import Claim, ClaimEvidence, ClaimStatus, ClaimType, Evidence, Opportunity
from .profiles import load_profile

MIN_QUOTE_CHARS = 12
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
                         "–": "-", "—": "-", "‐": "-"})
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \"'")


def quote_in_text(quote: str, text: str) -> bool:
    """True if quote (possibly with ... gaps) occurs in text, fragments in order."""
    haystack = normalize_for_match(text)
    pos = 0
    for frag in (normalize_for_match(f) for f in _ELLIPSIS.split(quote)):
        if not frag:
            continue
        i = haystack.find(frag, pos)
        if i < 0:
            return False
        pos = i + len(frag)
    return True


@dataclass(frozen=True)
class Citation:
    evidence_id: int
    quote_span: str


@dataclass
class CitationCheck:
    citation: Citation
    ok: bool
    error: str | None = None
    warning: str | None = None


def check_citation(
    session: Session, opp: Opportunity, c: Citation, today: date | None = None
) -> CitationCheck:
    ev = session.get(Evidence, c.evidence_id)
    if ev is None:
        return CitationCheck(c, False, f"evidence {c.evidence_id} does not exist")
    if ev.opportunity_id != opp.id:
        return CitationCheck(c, False, f"evidence {c.evidence_id} belongs to another opportunity")
    if ev.superseded_by_id is not None:
        return CitationCheck(
            c, False,
            f"evidence {c.evidence_id} is superseded by evidence {ev.superseded_by_id}; cite that instead",
        )
    quote = _ELLIPSIS.sub(" ", c.quote_span)
    if len(normalize_for_match(quote)) < MIN_QUOTE_CHARS:
        return CitationCheck(
            c, False, f"quote is too short (min {MIN_QUOTE_CHARS} characters): {c.quote_span!r}"
        )
    if not quote_in_text(c.quote_span, ev.raw_text):
        return CitationCheck(
            c, False, f"quote not found in evidence {c.evidence_id}: {c.quote_span!r}"
        )

    warning = None
    profile = load_profile(opp.category_profile)
    if profile.is_stale(ev.kind.value, ev.captured_at, today):
        warning = f"evidence {ev.id} ({ev.kind.value}, captured {ev.captured_at}) may be stale"
    return CitationCheck(c, True, warning=warning)


@dataclass
class AddClaimResult:
    ok: bool
    claim_id: int | None = None
    status: ClaimStatus | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def add_claim(
    session: Session,
    opp: Opportunity,
    *,
    agent: str,
    type: ClaimType,
    text: str,
    citations: list[Citation] | None = None,
    run_id: int | None = None,
    confidence: float | None = None,
    today: date | None = None,
) -> AddClaimResult:
    if not text.strip():
        return AddClaimResult(False, errors=["claim text is empty"])
    if confidence is not None and not 0 <= confidence <= 1:
        return AddClaimResult(False, errors=["confidence must be between 0 and 1"])

    citations = citations or []
    checks = [check_citation(session, opp, c, today) for c in citations]
    errors = [ch.error for ch in checks if not ch.ok]
    if errors:
        return AddClaimResult(False, errors=errors)

    warnings = [ch.warning for ch in checks if ch.warning]
    if citations:
        quoted = " ".join(normalize_for_match(c.quote_span) for c in citations)
        missing = [n for n in _NUMBER.findall(text) if n not in quoted]
        if missing:
            warnings.append(
                f"number(s) {missing} not found in any cited quote — make sure they come from a source"
            )
    else:
        warnings.append("no citations: stored as unverified")

    status = ClaimStatus.SUPPORTED if citations else ClaimStatus.UNVERIFIED
    claim = Claim(opportunity_id=opp.id, run_id=run_id, agent=agent, type=type,
                  text=text.strip(), status=status, confidence=confidence)
    session.add(claim)
    session.flush()
    for c in citations:
        session.add(ClaimEvidence(claim_id=claim.id, evidence_id=c.evidence_id,
                                  quote_span=c.quote_span))
    session.flush()
    return AddClaimResult(True, claim.id, status, warnings=warnings)


def claims_citing_superseded(session: Session, opp: Opportunity) -> dict[int, list[int]]:
    """claim_id -> ids of superseded evidence it cites (these claims need review)."""
    out: dict[int, list[int]] = {}
    for claim in session.query(Claim).filter(Claim.opportunity_id == opp.id):
        for cit in claim.citations:
            ev = session.get(Evidence, cit.evidence_id)
            if ev is not None and ev.superseded_by_id is not None:
                out.setdefault(claim.id, []).append(ev.id)
    return out
