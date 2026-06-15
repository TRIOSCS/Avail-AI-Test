"""Quick-source (scratch requisition) service.

What: gives one-off Search actions (Send RFQ / Add Offer) a home. Creates — only on an
      action, never on a bare search — a lightweight scratch Requisition + Requirement
      for an MPN (idempotent per user+mpn), and persists client-posted market rows as
      Sightings so the existing rfq_compose / add_offer flows work unchanged.
Calls: models.sourcing (Requisition, Requirement, Sighting),
       search_service.resolve_material_card (lazy, optional link),
       services.sighting_ingest.sighting_from_row,
       services.vendor_unavailability.apply_to_fresh_sightings.
Depends on: a request-scoped Session. Flushes so ids are set; the CALLER commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import RequisitionStatus, SourcingStatus
from ..models.sourcing import Requirement, Requisition, Sighting
from .sighting_ingest import sighting_from_row
from .vendor_unavailability import apply_to_fresh_sightings

if TYPE_CHECKING:
    from ..models.auth import User


def get_or_create_scratch_req(db: Session, user: User, mpn: str) -> tuple[Requisition, Requirement]:
    """Return (Requisition, Requirement) for a one-off action on ``mpn``.

    Idempotent per (user, normalized mpn) among the user's *active scratch* reqs: a second
    action on the same part reuses the first scratch req instead of spawning duplicates.
    Flushes so ids are populated; does NOT commit. ``normalized_mpn`` follows the existing
    Requirement convention (uppercased primary_mpn), matching ``add_to_requisition``.
    """
    display = (mpn or "").strip().upper()
    if not display:
        raise ValueError("mpn is required to create a scratch requisition")

    existing = (
        db.query(Requisition)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .filter(
            Requisition.created_by == user.id,
            Requisition.is_scratch.is_(True),
            Requisition.status == RequisitionStatus.ACTIVE,
            Requirement.normalized_mpn == display,
        )
        .order_by(Requisition.created_at.desc())
        .first()
    )
    if existing is not None:
        requirement = (
            db.query(Requirement)
            .filter(
                Requirement.requisition_id == existing.id,
                Requirement.normalized_mpn == display,
            )
            .order_by(Requirement.id.asc())
            .first()
        )
        if requirement is None:  # req row without its requirement — repair defensively
            requirement = _new_requirement(db, existing.id, display, mpn)
        return existing, requirement

    req = Requisition(
        name=f"Quick-source: {display}",
        customer_name=None,
        status=RequisitionStatus.ACTIVE,
        is_scratch=True,
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    requirement = _new_requirement(db, req.id, display, mpn)
    logger.info("quick-source: created scratch req {} for {} (user {})", req.id, display, user.id)
    return req, requirement


def _new_requirement(db: Session, requisition_id: int, display: str, mpn: str) -> Requirement:
    """Create + flush a Requirement on ``requisition_id``, linking the MaterialCard if
    found."""
    card = _resolve_card(mpn, db)
    requirement = Requirement(
        requisition_id=requisition_id,
        primary_mpn=display,
        normalized_mpn=display,
        material_card_id=card.id if card else None,
        sourcing_status=SourcingStatus.OPEN,
    )
    db.add(requirement)
    db.flush()
    return requirement


def persist_rows_as_sightings(db: Session, requirement: Requirement, rows: list[dict]) -> list[Sighting]:
    """Persist client-posted market rows as Sightings under ``requirement``.

    Skips rows with no vendor name. Re-applies durable vendor+part unavailability to the
    fresh rows (same as ``add_to_requisition``). Flushes; does NOT commit. Rows come from
    the client payload (already rendered in the DOM), not the Redis cache — no TTL race.
    """
    created: list[Sighting] = []
    for item in rows:
        if not str(item.get("vendor_name") or "").strip():
            continue
        sighting = sighting_from_row(requirement.id, item)
        db.add(sighting)
        created.append(sighting)

    if created:
        apply_to_fresh_sightings(db, requirement, created)
        db.flush()
    return created


def _resolve_card(mpn: str, db: Session):
    """Best-effort MaterialCard link; an action must never fail because card resolve
    did.

    Imported lazily — search_service pulls in the whole connector stack, which we do not
    want at module import time for a thin service.
    """
    try:
        from ..search_service import resolve_material_card

        return resolve_material_card(mpn, db)
    except Exception as exc:  # pragma: no cover - defensive; the card link is optional
        logger.warning("quick-source: card resolve failed for {}: {}", mpn, exc)
        return None
