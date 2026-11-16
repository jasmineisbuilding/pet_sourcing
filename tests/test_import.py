from datetime import date

import pytest
from sqlalchemy import func, select

from petsourcing import config
from petsourcing.db import init_db, make_engine, session_scope
from petsourcing.ingest import import_evidence, import_products, import_quotes
from petsourcing.ingest.csv_import import content_hash
from petsourcing.models import Evidence, Opportunity
from petsourcing.profiles import load_profile

S = config.SAMPLES_DIR


@pytest.fixture
def engine(tmp_path):
    e = make_engine(tmp_path / "t.db")
    init_db(e)
    with session_scope(e) as s:
        s.add(Opportunity(slug="t", title="t", category_profile="pet_supplies"))
    return e


def _opp(s):
    return s.scalar(select(Opportunity).where(Opportunity.slug == "t"))


def test_sample_files_import_cleanly(engine):
    with session_scope(engine) as s:
        o = _opp(s)
        assert import_products(s, o, S / "products.csv").inserted == 4
        r = import_evidence(s, o, S / "evidence.csv")
        assert r.ok and r.inserted == 12
        assert import_quotes(s, o, S / "quotes.csv").inserted == 3


def test_import_is_idempotent(engine):
    for _ in range(2):
        with session_scope(engine) as s:
            o = _opp(s)
            import_products(s, o, S / "products.csv")
            last = import_evidence(s, o, S / "evidence.csv")
            q = import_quotes(s, o, S / "quotes.csv")
    assert last.inserted == 0 and last.skipped_duplicates == 12
    assert q.inserted == 0
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Evidence)) == 12


def test_unknown_price_stays_null(engine):
    with session_scope(engine) as s:
        o = _opp(s)
        import_products(s, o, S / "products.csv")
        p4 = next(p for p in o.products if p.key == "P4")
        assert p4.price is None and p4.rating is None


def test_bad_rows_reported_and_valid_rows_kept(engine):
    with session_scope(engine) as s:
        o = _opp(s)
        import_products(s, o, S / "products.csv")
        r = import_evidence(s, o, S / "evidence_bad_rows.csv")
    assert r.inserted == 1
    lines = {line: msg for line, msg in r.errors}
    assert set(lines) == {2, 3, 4, 5}
    assert "provenance" in lines[2]
    assert "P9" in lines[3]


def test_strict_mode_imports_nothing_on_error(engine):
    with session_scope(engine) as s:
        o = _opp(s)
        import_products(s, o, S / "products.csv")
    with session_scope(engine) as s:
        r = import_evidence(s, _opp(s), S / "evidence_bad_rows.csv", strict=True)
    assert r.aborted and r.inserted == 0
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Evidence)) == 0


def test_hash_ignores_whitespace_and_case():
    assert content_hash("Hard  to CLEAN ", "P1") == content_hash("hard to clean", "P1")
    assert content_hash("hard to clean", "P1") != content_hash("hard to clean", "P2")


def test_freshness_policy():
    prof = load_profile("pet_supplies")
    today = date(2026, 11, 16)
    assert prof.is_stale("listing", date(2026, 9, 1), today)
    assert not prof.is_stale("review", date(2026, 9, 1), today)
    assert not prof.is_stale("unknown_kind", date(2000, 1, 1), today)
