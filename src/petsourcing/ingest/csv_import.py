"""CSV import for products, evidence and supplier quotes.

Rules enforced here (not left to the user's discipline or to an LLM):
- Every row must carry provenance: evidence needs captured_at plus a source_url
  or source_note; quotes need quoted_at; products need captured_at.
- Import is idempotent: evidence is de-duplicated by a hash of its normalised
  text, products by their key, quotes by (supplier, desc, price, date). Running
  the same file twice adds nothing the second time.
- Bad rows are reported with their line number. ``strict=True`` aborts the
  whole file on any error; otherwise valid rows are imported.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Evidence, EvidenceKind, Opportunity, Product, SupplierQuote


# --------------------------------------------------------------------------- helpers


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def content_hash(text: str, product_key: str | None = None) -> str:
    return hashlib.sha256(f"{product_key or ''}|{normalize_text(text)}".encode()).hexdigest()


def file_batch_id(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _blank_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and v.strip() == "" else v


class _Row(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _blanks(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: _blank_to_none(v) for k, v in data.items() if k is not None}
        return data


# --------------------------------------------------------------------------- row schemas


class ProductRow(_Row):
    key: str
    name: str
    brand: str | None = None
    platform: str | None = None
    url: str | None = None
    price: float | None = None
    currency: str | None = None
    review_count: int | None = None
    rating: float | None = None
    captured_at: date
    notes: str | None = None

    @model_validator(mode="after")
    def _currency_with_price(self) -> ProductRow:
        if self.price is not None and not self.currency:
            raise ValueError("currency is required when price is given")
        return self


class EvidenceRow(_Row):
    kind: EvidenceKind
    raw_text: str
    product_key: str | None = None
    rating: float | None = None
    source_url: str | None = None
    source_note: str | None = None
    captured_at: date
    language: str | None = None

    @field_validator("raw_text")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("raw_text is empty")
        return v

    @model_validator(mode="after")
    def _has_source(self) -> EvidenceRow:
        if not (self.source_url or self.source_note):
            raise ValueError("provenance missing: provide source_url or source_note")
        return self


class QuoteRow(_Row):
    supplier: str
    product_desc: str
    unit_price: float
    currency: str
    moq: int | None = None
    unit_weight_g: float | None = None
    dims_cm: str | None = None
    incoterm: str | None = None
    lead_time_days: int | None = None
    quoted_at: date
    source_note: str | None = None
    notes: str | None = None


# --------------------------------------------------------------------------- report


@dataclass
class ImportReport:
    kind: str
    batch: str
    inserted: int = 0
    skipped_duplicates: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (line number, message)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


def _rows(path: Path):
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        # line 1 is the header, so data starts on line 2
        yield from enumerate(csv.DictReader(fh), start=2)


def _fmt(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(x) for x in err['loc']) or 'row'}: {err['msg']}" for err in e.errors()
    )


def _finish(session: Session, report: ImportReport, strict: bool) -> ImportReport:
    if strict and report.errors:
        session.rollback()
        report.aborted = True
        report.inserted = 0
    else:
        session.flush()
    return report


# --------------------------------------------------------------------------- importers


def import_products(
    session: Session, opp: Opportunity, path: Path, strict: bool = False
) -> ImportReport:
    report = ImportReport("products", file_batch_id(path))
    for line, raw in _rows(path):
        try:
            row = ProductRow.model_validate(raw)
        except ValidationError as e:
            report.errors.append((line, _fmt(e)))
            continue
        exists = session.scalar(
            select(Product).where(Product.opportunity_id == opp.id, Product.key == row.key)
        )
        if exists:
            report.skipped_duplicates += 1
            continue
        session.add(Product(opportunity_id=opp.id, **row.model_dump()))
        session.flush()
        report.inserted += 1
    return _finish(session, report, strict)


def import_evidence(
    session: Session, opp: Opportunity, path: Path, strict: bool = False
) -> ImportReport:
    report = ImportReport("evidence", file_batch_id(path))
    products = {
        p.key: p for p in session.scalars(select(Product).where(Product.opportunity_id == opp.id))
    }
    for line, raw in _rows(path):
        try:
            row = EvidenceRow.model_validate(raw)
        except ValidationError as e:
            report.errors.append((line, _fmt(e)))
            continue
        product = None
        if row.product_key:
            product = products.get(row.product_key)
            if product is None:
                report.errors.append(
                    (line, f"unknown product_key '{row.product_key}' (import products first)")
                )
                continue
        h = content_hash(row.raw_text, row.product_key)
        exists = session.scalar(
            select(Evidence.id).where(Evidence.opportunity_id == opp.id, Evidence.content_hash == h)
        )
        if exists:
            report.skipped_duplicates += 1
            continue
        session.add(
            Evidence(
                opportunity_id=opp.id,
                product_id=product.id if product else None,
                kind=row.kind,
                raw_text=row.raw_text.strip(),
                rating=row.rating,
                source_url=row.source_url,
                source_note=row.source_note,
                captured_at=row.captured_at,
                language=row.language,
                content_hash=h,
                import_batch=report.batch,
            )
        )
        session.flush()
        report.inserted += 1
    return _finish(session, report, strict)


def import_quotes(
    session: Session, opp: Opportunity, path: Path, strict: bool = False
) -> ImportReport:
    report = ImportReport("quotes", file_batch_id(path))
    for line, raw in _rows(path):
        try:
            row = QuoteRow.model_validate(raw)
        except ValidationError as e:
            report.errors.append((line, _fmt(e)))
            continue
        exists = session.scalar(
            select(SupplierQuote.id).where(
                SupplierQuote.opportunity_id == opp.id,
                SupplierQuote.supplier == row.supplier,
                SupplierQuote.product_desc == row.product_desc,
                SupplierQuote.unit_price == row.unit_price,
                SupplierQuote.quoted_at == row.quoted_at,
            )
        )
        if exists:
            report.skipped_duplicates += 1
            continue
        session.add(SupplierQuote(opportunity_id=opp.id, **row.model_dump()))
        session.flush()
        report.inserted += 1
    return _finish(session, report, strict)


IMPORTERS = {"products": import_products, "evidence": import_evidence, "quotes": import_quotes}
