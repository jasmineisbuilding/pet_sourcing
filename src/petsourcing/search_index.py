"""Full-text search over evidence (SQLite FTS5, trigram tokenizer).

The index is *derived data*, not schema: ``ensure_index`` (called by
``init_db`` after migrations) creates it and its sync triggers if missing and
rebuilds it when it was just (re)created. That keeps it correct even if a
future migration rebuilds the ``evidence`` table (SQLite batch mode drops
triggers when it does).

Trigram tokenisation gives case-insensitive substring matching that works the
same for English, French and Chinese text. Terms shorter than 3 characters
cannot use the index and fall back to LIKE.
"""

from __future__ import annotations

import re

from sqlalchemy import Connection, text

FTS_TABLE = "evidence_fts"
_TRIGGERS = {
    "evidence_fts_ai": f"""CREATE TRIGGER evidence_fts_ai AFTER INSERT ON evidence BEGIN
        INSERT INTO {FTS_TABLE}(rowid, raw_text) VALUES (new.id, new.raw_text); END""",
    "evidence_fts_ad": f"""CREATE TRIGGER evidence_fts_ad AFTER DELETE ON evidence BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, raw_text) VALUES ('delete', old.id, old.raw_text); END""",
    "evidence_fts_au": f"""CREATE TRIGGER evidence_fts_au AFTER UPDATE OF raw_text ON evidence BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, raw_text) VALUES ('delete', old.id, old.raw_text);
        INSERT INTO {FTS_TABLE}(rowid, raw_text) VALUES (new.id, new.raw_text); END""",
}


def is_index_object(name: str | None) -> bool:
    """True for the FTS table and its shadow tables (used to hide them from Alembic)."""
    return bool(name) and name.startswith(FTS_TABLE)


def ensure_index(conn: Connection) -> bool:
    """Create the index and triggers if missing. Returns True if a rebuild happened."""
    existing = {
        r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"))
    }
    rebuilt = False
    if FTS_TABLE not in existing:
        conn.execute(text(
            f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(raw_text, content='evidence', "
            "content_rowid='id', tokenize='trigram remove_diacritics 1')"
        ))
        rebuilt = True
    for name, ddl in _TRIGGERS.items():
        if name not in existing:
            conn.execute(text(ddl))
            rebuilt = True
    if rebuilt:
        conn.execute(text(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')"))
    return rebuilt


def query_terms(query: str) -> tuple[list[str], list[str]]:
    """Split a query into (indexable terms >= 3 chars, short terms)."""
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    terms = [t.replace('"', "") for t in terms]
    return [t for t in terms if len(t) >= 3], [t for t in terms if 0 < len(t) < 3]


def fts_match_expression(terms: list[str]) -> str:
    """OR of quoted terms: recall first, bm25 ranks documents matching more terms higher."""
    return " OR ".join(f'"{t}"' for t in terms)
