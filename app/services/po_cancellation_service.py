"""PO-cancellation service — record vendor fall-down + update vendor performance.

When a buyer cancels a vendor's cut PO (vendor fall-down) and re-sources the buy-plan
line, this service writes the immutable ``POCancellation`` fact, transitions the dead
offer to SOLD, marks the vendor unavailable for the part, and recomputes the vendor's
cancellation metrics on the ``VendorCard`` (how OFTEN their POs get cancelled and how
QUICKLY — ``days_to_cancel`` over ``SLOW_CANCEL_THRESHOLD_DAYS`` is "slow/severe" and
weighs the vendor score down harder via ``vendor_score._cancel_dampener``).

Every function is NO-COMMIT: the caller (the re-source workflow router) owns the
transaction. Functions ``db.add(...)`` / mutate ORM objects and ``db.flush()`` only when
an id is needed.

Called by: app/routers (the re-source / cancel-PO workflow slice), nightly vendor-metric
           refresh.
Depends on: app/models/po_cancellation.POCancellation, app/models VendorCard/Offer,
            app/services/status_machine.require_valid_transition,
            app/services/vendor_unavailability.record_unavailability,
            app/services/activity_service.log_activity, app/models/intelligence.ChangeLog,
            app/constants (OfferStatus, ActivityType, RESOURCE_TO_UNAVAILABILITY_REASON,
            UnavailabilityReason).
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import (
    RESOURCE_TO_UNAVAILABILITY_REASON,
    ActivityType,
    OfferStatus,
    UnavailabilityReason,
)
from ..models.po_cancellation import POCancellation

SLOW_CANCEL_THRESHOLD_DAYS = 7


def record_po_cancellation(
    db: Session,
    *,
    line,
    offer,
    requirement,
    reason_code,
    reason_text,
    user,
) -> POCancellation:
    """Build + ``db.add`` an immutable ``POCancellation`` row from a cancelled buy-plan
    line.

    ``days_to_cancel = (now - line.po_confirmed_at).days`` if ``line.po_confirmed_at`` else
    None. Vendor identity / normalized MPN are derived from the offer (MPN falls back to
    ``requirement.primary_mpn``); the model's ``@validates`` re-normalizes the keys and
    validates ``reason_code`` (a ``LineResourceReason``/``POCancellationReason`` value —
    they share the same string values). Flushes so the row carries an id. Returns the row.
    """
    now = datetime.now(timezone.utc)

    po_cut_at = line.po_confirmed_at
    days_to_cancel = None
    if po_cut_at is not None:
        cut = po_cut_at if po_cut_at.tzinfo is not None else po_cut_at.replace(tzinfo=timezone.utc)
        days_to_cancel = (now - cut).days

    mpn_source = offer.normalized_mpn or offer.mpn or (requirement.primary_mpn if requirement else None)

    row = POCancellation(
        buy_plan_id=line.buy_plan_id,
        buy_plan_line_id=line.id,
        requirement_id=requirement.id if requirement else None,
        offer_id=offer.id,
        vendor_card_id=offer.vendor_card_id,
        # Model @validates re-normalizes both keys through the canonical helpers.
        vendor_name_normalized=offer.vendor_name_normalized or offer.vendor_name,
        normalized_mpn=mpn_source,
        po_number=line.po_number or "",
        po_cut_at=po_cut_at,
        cancelled_at=now,
        days_to_cancel=days_to_cancel,
        reason_code=reason_code,
        reason_text=reason_text,
        cancelled_by_id=user.id if user else None,
    )
    db.add(row)
    db.flush()
    logger.info(
        "Recorded PO cancellation: vendor_card={} line={} reason={} days_to_cancel={}",
        offer.vendor_card_id,
        line.id,
        reason_code,
        days_to_cancel,
    )
    return row


def mark_offer_sold(db: Session, offer, user) -> None:
    """Transition the offer to ``OfferStatus.SOLD`` (idempotent — no-op if already
    sold).

    Mirrors the ChangeLog + ``OFFER_STATUS_CHANGED`` ActivityLog writes that
    ``app/routers/htmx_views.py:mark_offer_sold_htmx`` performs.
    """
    if offer.status == OfferStatus.SOLD:
        return

    from ..models.intelligence import ChangeLog
    from ..services.activity_service import log_activity
    from ..services.status_machine import require_valid_transition

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id if user else None

    db.add(
        ChangeLog(
            entity_type="offer",
            entity_id=offer.id,
            user_id=user.id if user else None,
            field_name="status",
            old_value=old_status,
            new_value="sold",
        )
    )

    log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id if user else None,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )
    logger.info("Offer {} marked sold (PO cancellation re-source)", offer.id)


def mark_vendor_unavailable(db: Session, *, requirement, offer, reason_code, note, user) -> int:
    """Mark the cancelled vendor unavailable for this part on the sightings tab.

    Maps ``reason_code`` → an ``UnavailabilityReason`` via
    ``RESOURCE_TO_UNAVAILABILITY_REASON`` (most fall-downs read as "sold elsewhere") and
    delegates to ``vendor_unavailability.record_unavailability`` (which writes its own
    ``VENDOR_UNAVAILABLE`` ActivityLog — not duplicated here). Returns its count. When
    ``requirement`` is None there is nothing to key on, so returns 0.
    """
    if requirement is None:
        return 0

    from ..services.vendor_unavailability import record_unavailability

    unavailability_reason = RESOURCE_TO_UNAVAILABILITY_REASON.get(
        reason_code, UnavailabilityReason.SOLD_ELSEWHERE.value
    )
    return record_unavailability(db, requirement, offer.vendor_name, unavailability_reason, note, user)


def refresh_vendor_cancellation_metrics(db: Session, vendor_card_id) -> None:
    """Recompute the vendor's cancellation metrics from ALL its ``POCancellation`` rows.

    - ``cancellation_rate`` = ``min(1.0, cancels / total_pos)`` if ``total_pos`` else
      (``1.0`` if any cancels else None).
    - ``avg_days_to_cancel`` = ``round(mean(days_to_cancel over non-null), 1)`` or None.
    - ``slow_cancel_count`` = count of ``days_to_cancel > SLOW_CANCEL_THRESHOLD_DAYS``.

    ``total_pos`` comes from ``VendorCard.total_pos`` (None treated as 0). No-op if the
    card is missing.
    """
    from ..models import VendorCard

    card = db.get(VendorCard, vendor_card_id)
    if card is None:
        return

    rows = db.query(POCancellation.days_to_cancel).filter(POCancellation.vendor_card_id == vendor_card_id).all()
    cancels = len(rows)
    days = [d for (d,) in rows if d is not None]

    total_pos = card.total_pos or 0
    if total_pos:
        card.cancellation_rate = min(1.0, cancels / total_pos)
    else:
        card.cancellation_rate = 1.0 if cancels else None

    card.avg_days_to_cancel = round(sum(days) / len(days), 1) if days else None
    card.slow_cancel_count = sum(1 for d in days if d > SLOW_CANCEL_THRESHOLD_DAYS)
