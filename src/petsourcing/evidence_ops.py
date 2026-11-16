"""Operations on existing evidence.

Superseding instead of editing: when newer evidence replaces older evidence
(an updated listing price, a newer policy page), the old row is kept for
history but no longer counts as current. Claims citing it are surfaced for review.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Evidence, utcnow


class SupersedeError(ValueError):
    pass


def supersede(session: Session, old_id: int, new_id: int, note: str | None = None) -> Evidence:
    if old_id == new_id:
        raise SupersedeError("evidence cannot supersede itself")
    old = session.get(Evidence, old_id)
    new = session.get(Evidence, new_id)
    if old is None or new is None:
        raise SupersedeError(f"evidence {old_id if old is None else new_id} does not exist")
    if old.opportunity_id != new.opportunity_id:
        raise SupersedeError("evidence belongs to different opportunities")
    if new.superseded_by_id is not None:
        raise SupersedeError(
            f"evidence {new_id} is itself superseded by {new.superseded_by_id}; use the latest one"
        )
    if old.superseded_by_id is not None:
        raise SupersedeError(f"evidence {old_id} is already superseded by {old.superseded_by_id}")

    old.superseded_by_id = new.id
    old.superseded_at = utcnow()
    old.supersede_note = note
    # Anything that pointed at `old` as its replacement now points at `new`,
    # so chains never need to be walked.
    for e in session.query(Evidence).filter(Evidence.superseded_by_id == old.id):
        e.superseded_by_id = new.id
    session.flush()
    return old
