"""Quick-source (scratch requisition) service.

What: gives one-off Search actions (Send RFQ / Add Offer) a home. Creates — only on an
      action, never on a bare search — a lightweight scratch Requisition + Requirement
      for an MPN (idempotent per user+mpn), and persists client-posted market rows as
      Sightings so the existing RFQ (vendor-modal composer) / add_offer flows work
      unchanged.
Calls: models.sourcing (Requisition, Requirement, Sighting),
       services.requirement_service.create_requirements (THE creation pipeline, spec §9),
       services.sighting_ingest.sighting_from_row,
       services.vendor_unavailability.apply_to_fresh_sightings.
Depends on: a request-scoped Session. Flushes so ids are set; the CALLER commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import RequisitionStatus
from ..models.sourcing import Requirement, Requisition, Sighting
from ..utils.normalization import normalize_mpn_key
from .sighting_ingest import sighting_from_row
from .vendor_unavailability import apply_to_fresh_sightings

if TYPE_CHECKING:
    from ..models.auth import User


def get_or_create_scratch_req(db: Session, user: User, mpn: str) -> tuple[Requisition, Requirement]:
    """Return (Requisition, Requirement) for a one-off action on ``mpn``.

    Best-effort idempotent per (user, normalized mpn) among the user's NON-TERMINAL scratch
    reqs: a second action on the same part reuses the most-recent matching scratch req
    instead of spawning duplicates. There is deliberately NO DB unique constraint (a partial
    unique index would be Postgres-only and break SQLite-test parity), so two truly
    concurrent same-(user, mpn) POSTs could each create one — benign (duplicate scratch reqs,
    never an error or data loss) and effectively unreachable on single-user staging. Flushes
    so ids are populated; does NOT commit. ``normalized_mpn`` is the canonical key form
    (lowercase, non-alphanumeric stripped — requirement_service pipeline convention;
    migration 206 remapped the legacy uppercase-display rows to it).
    """
    display = (mpn or "").strip().upper()
    if not display:
        raise ValueError("mpn is required to create a scratch requisition")
    mpn_key = normalize_mpn_key(display)

    existing = (
        db.query(Requisition)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .filter(
            Requisition.created_by == user.id,
            Requisition.is_scratch.is_(True),
            Requisition.status.notin_(RequisitionStatus.TERMINAL),
            Requirement.normalized_mpn == mpn_key,
        )
        .order_by(Requisition.created_at.desc())
        .first()
    )
    if existing is not None:
        requirement = (
            db.query(Requirement)
            .filter(
                Requirement.requisition_id == existing.id,
                Requirement.normalized_mpn == mpn_key,
            )
            .order_by(Requirement.id.asc())
            .first()
        )
        if requirement is None:  # req row without its requirement — repair defensively
            requirement = _new_requirement(db, existing, mpn)
        return existing, requirement

    req = Requisition(
        name=f"Quick-source: {display}",
        customer_name=None,
        status=RequisitionStatus.OPEN,
        is_scratch=True,
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    requirement = _new_requirement(db, req, mpn)
    logger.info("quick-source: created scratch req {} for {} (user {})", req.id, display, user.id)
    return req, requirement


def _new_requirement(db: Session, req: Requisition, mpn: str) -> Requirement:
    """Create + flush a Requirement on ``req`` through THE creation pipeline
    (services/requirement_service.py, spec §9).

    Fixes the old display-as-normalized_mpn bug: the pipeline stores the canonical
    key form so part-history / material-card joins line up. Scratch reqs skip task
    auto-gen inside the pipeline; site-scoped dup detection / tag propagation no-op
    (scratch reqs have no customer site). Flushes; does NOT commit.
    """
    from .requirement_service import create_requirements

    result = create_requirements(db, req, [{"primary_mpn": mpn}])
    return result.created[0]


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
