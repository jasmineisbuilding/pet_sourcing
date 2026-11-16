"""Import evidence by pasting text (e.g. reviews copied from a product page).

Entries are separated by a blank line or a line containing only ``---``.
All entries in one paste share the same provenance (source, date, product),
which must be given explicitly. Same de-duplication as the CSV importer.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Evidence, EvidenceKind, Opportunity, Product
from .csv_import import EvidenceRow, ImportReport, _fmt, content_hash

_SEPARATOR = re.compile(r"\n\s*(?:---+)?\s*\n")


def split_entries(text: str) -> list[str]:
    text = text.replace("\r\n", "\n")
    return [" ".join(e.split()) for e in _SEPARATOR.split(text) if e.strip()]


def import_pasted(
    session: Session,
    opp: Opportunity,
    text: str,
    *,
    kind: EvidenceKind | str,
    captured_at: date,
    source_url: str | None = None,
    source_note: str | None = None,
    product_key: str | None = None,
    language: str | None = None,
) -> ImportReport:
    report = ImportReport("paste", hashlib.sha256(text.encode()).hexdigest()[:16])

    product = None
    if product_key:
        product = session.scalar(
            select(Product).where(Product.opportunity_id == opp.id, Product.key == product_key)
        )
        if product is None:
            report.errors.append((0, f"unknown product_key '{product_key}'"))
            return report

    for i, entry in enumerate(split_entries(text), start=1):
        try:
            row = EvidenceRow(kind=kind, raw_text=entry, product_key=product_key,
                              source_url=source_url, source_note=source_note,
                              captured_at=captured_at, language=language)
        except ValidationError as e:
            report.errors.append((i, _fmt(e)))
            continue
        h = content_hash(row.raw_text, product_key)
        if session.scalar(select(Evidence.id).where(Evidence.opportunity_id == opp.id,
                                                    Evidence.content_hash == h)):
            report.skipped_duplicates += 1
            continue
        session.add(Evidence(
            opportunity_id=opp.id, product_id=product.id if product else None,
            kind=row.kind, raw_text=row.raw_text, source_url=row.source_url,
            source_note=row.source_note, captured_at=row.captured_at,
            language=row.language, content_hash=h, import_batch=report.batch,
        ))
        session.flush()
        report.inserted += 1
    return report
