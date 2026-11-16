from datetime import date

import pytest
from sqlalchemy import func, select

from petsourcing import config
from petsourcing.claims import Citation, add_claim, claims_citing_superseded, quote_in_text
from petsourcing.db import init_db, make_engine, session_scope
from petsourcing.evidence_ops import SupersedeError, supersede
from petsourcing.ingest import import_evidence, import_products
from petsourcing.ingest.paste import import_pasted, split_entries
from petsourcing.models import Claim, ClaimStatus, ClaimType, Evidence, EvidenceKind, Opportunity

S = config.SAMPLES_DIR
TODAY = date(2026, 11, 16)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(tmp_path / "c.db")
    init_db(engine)
    with session_scope(engine) as s:
        for slug in ("a", "b"):
            s.add(Opportunity(slug=slug, title=slug, category_profile="pet_supplies"))
        s.flush()
        a = s.scalar(select(Opportunity).where(Opportunity.slug == "a"))
        import_products(s, a, S / "products.csv")
        import_evidence(s, a, S / "evidence.csv")
    return engine


def opp(s, slug="a"):
    return s.scalar(select(Opportunity).where(Opportunity.slug == slug))


def ev_id(s, fragment):
    return s.scalar(select(Evidence.id).where(Evidence.raw_text.contains(fragment)))


def claim(s, *cites, slug="a", text="Cleaning is a recurring complaint."):
    return add_claim(s, opp(s, slug), agent="test", type=ClaimType.PAIN, text=text,
                     citations=list(cites), today=TODAY)


# ---------------------------------------------------------------- quote matching


@pytest.mark.parametrize("quote", [
    "hair gets stuck between the teeth",
    "HAIR   gets stuck\nbetween the teeth",
    "“Removes a ton of undercoat”",
    "Removes a ton of undercoat ... pick it out by hand",
    "removes a ton of undercoat…by hand",
])
def test_quote_matching_tolerates_formatting(quote):
    text = ("Removes a ton of undercoat but hair gets stuck between the teeth "
            "and I have to pick it out by hand.")
    assert quote_in_text(quote, text)


@pytest.mark.parametrize("quote", [
    "hair never gets stuck",
    "pick it out by hand ... removes a ton",  # fragments out of order
])
def test_quote_matching_rejects_non_quotes(quote):
    text = "Removes a ton of undercoat but hair gets stuck. I pick it out by hand."
    assert not quote_in_text(quote, text)


# ---------------------------------------------------------------- add_claim


def test_valid_citation_is_supported(env):
    with session_scope(env) as s:
        r = claim(s, Citation(ev_id(s, "stuck between"), "hair gets stuck between the teeth"))
        assert r.ok and r.status == ClaimStatus.SUPPORTED
        assert len(s.get(Claim, r.claim_id).citations) == 1


def test_fabricated_quote_rejects_whole_claim(env):
    with session_scope(env) as s:
        good = Citation(ev_id(s, "stuck between"), "hair gets stuck between the teeth")
        bad = Citation(ev_id(s, "stuck between"), "everyone hates cleaning this comb")
        r = claim(s, good, bad)
        assert not r.ok and "not found" in r.errors[0]
        assert s.scalar(select(func.count()).select_from(Claim)) == 0


@pytest.mark.parametrize("make, msg", [
    (lambda s: Citation(99999, "hair gets stuck between the teeth"), "does not exist"),
    (lambda s: Citation(ev_id(s, "stuck between"), "the teeth"), "too short"),
])
def test_bad_citations(env, make, msg):
    with session_scope(env) as s:
        r = claim(s, make(s))
        assert not r.ok and msg in r.errors[0]


def test_cannot_cite_other_opportunity(env):
    with session_scope(env) as s:
        r = claim(s, Citation(ev_id(s, "stuck between"), "hair gets stuck between the teeth"),
                  slug="b")
        assert not r.ok and "another opportunity" in r.errors[0]


def test_no_citation_is_unverified(env):
    with session_scope(env) as s:
        r = claim(s)
        assert r.ok and r.status == ClaimStatus.UNVERIFIED
        assert any("unverified" in w for w in r.warnings)


def test_numbers_must_appear_in_quotes(env):
    with session_scope(env) as s:
        c = Citation(ev_id(s, "stopped working"), "stopped working after two months")
        assert any("not found" in w for w in claim(s, c, text="Breaks after 2 months").warnings)
        assert not claim(s, c, text="Breaks after two months").warnings


def test_stale_evidence_warns(env):
    with session_scope(env) as s:
        e = s.get(Evidence, ev_id(s, "stuck between"))
        e.kind = EvidenceKind.LISTING
        e.captured_at = date(2026, 1, 1)
        r = claim(s, Citation(e.id, "hair gets stuck between the teeth"))
        assert r.ok and any("stale" in w for w in r.warnings)


# ---------------------------------------------------------------- supersede


def test_supersede_blocks_citing_and_flags_claims(env):
    with session_scope(env) as s:
        old, new = ev_id(s, "stuck between"), ev_id(s, "Too big")
        r = claim(s, Citation(old, "hair gets stuck between the teeth"))
        supersede(s, old, new, "updated")
        assert claims_citing_superseded(s, opp(s)) == {r.claim_id: [old]}
        again = claim(s, Citation(old, "hair gets stuck between the teeth"))
        assert not again.ok and f"superseded by evidence {new}" in again.errors[0]


def test_supersede_keeps_chains_flat(env):
    with session_scope(env) as s:
        a, b, c = ev_id(s, "stuck between"), ev_id(s, "Too big"), ev_id(s, "Best deshedding")
        supersede(s, a, b)
        supersede(s, b, c)
        assert s.get(Evidence, a).superseded_by_id == c


@pytest.mark.parametrize("pair, msg", [
    (("same", "same"), "itself"),
    (("stuck between", 99999), "does not exist"),
])
def test_supersede_validation(env, pair, msg):
    with session_scope(env) as s:
        ids = [ev_id(s, p) if isinstance(p, str) else p for p in pair]
        if pair[0] == "same":
            ids = [ev_id(s, "stuck between")] * 2
        with pytest.raises(SupersedeError, match=msg):
            supersede(s, *ids)


def test_cannot_supersede_with_superseded_evidence(env):
    with session_scope(env) as s:
        a, b, c = ev_id(s, "stuck between"), ev_id(s, "Too big"), ev_id(s, "Best deshedding")
        supersede(s, a, b)
        with pytest.raises(SupersedeError, match="itself superseded"):
            supersede(s, c, a)


# ---------------------------------------------------------------- paste


def test_split_entries():
    text = "first review\nspans lines\n\nsecond\n---\nthird\r\n\r\n\n"
    assert split_entries(text) == ["first review spans lines", "second", "third"]


def test_paste_import_and_dedupe(env):
    text = "Great brush.\n\nHandle broke after a week."
    with session_scope(env) as s:
        kw = dict(kind="review", captured_at=TODAY, source_url="https://example.com/x",
                  product_key="P1")
        assert import_pasted(s, opp(s), text, **kw).inserted == 2
        assert import_pasted(s, opp(s), text, **kw).skipped_duplicates == 2


def test_paste_requires_provenance_and_known_product(env):
    with session_scope(env) as s:
        r = import_pasted(s, opp(s), "text", kind="review", captured_at=TODAY)
        assert r.inserted == 0 and "provenance" in r.errors[0][1]
        r = import_pasted(s, opp(s), "text", kind="review", captured_at=TODAY,
                          source_note="x", product_key="P9")
        assert "P9" in r.errors[0][1]
