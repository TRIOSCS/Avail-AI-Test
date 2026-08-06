"""requisition_state.py — Requisition stored-status machine + derived pipeline stage.

The stored status is user intent only (draft/open/hotlist/won/lost/cancelled).
The mid-pipeline stages the Sales Hub displays (rfqs_sent / offers / quoted) are
DERIVED here from data — RFQ Contact rows, Offer rows, QuoteRequisition rows —
never written to the requisitions table (spec §5.1: "derived, not a stored
9-state ladder"; migration 209 collapsed the stored rows).

Called by: routers (requisitions, offers, quotes, buy_plans), forecast/knowledge
services for display.
Depends on: constants.py, models (ActivityLog, Contact, Offer, QuoteRequisition)
"""

from collections.abc import Iterable

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, RequisitionStatus
from ..models import ActivityLog

# Closing a requisition requires recording why. Single source of truth for the
# states that demand a non-empty outcome_reason.
TERMINAL_REASON_REQUIRED: frozenset[str] = frozenset({RequisitionStatus.WON, RequisitionStatus.LOST})

# Derived pipeline stage vocabulary, most-advanced first. These strings are the
# display contract (badges, dots, forecast buckets) and intentionally match the
# pre-collapse stored values so templates and macros render unchanged.
STAGE_QUOTED = "quoted"
STAGE_OFFERS = "offers"
STAGE_RFQS_SENT = "rfqs_sent"
PIPELINE_STAGES: tuple[str, ...] = (STAGE_QUOTED, STAGE_OFFERS, STAGE_RFQS_SENT)


class OutcomeReasonRequired(ValueError):
    """Raised when a transition to WON/LOST is attempted without a reason.

    Routers translate this into a 400 with the user-facing message; it is a distinct
    subclass of ValueError so callers can tell it apart from an illegal-transition
    ValueError when they need to (status code differs).
    """

    MESSAGE = "A reason is required to mark a requisition Won or Lost"

    def __init__(self, message: str = MESSAGE) -> None:
        super().__init__(message)


# Stored-status transitions only. Mid-pipeline movement no longer exists as a
# transition — it is derived. Legacy origin rows (active/sourcing/quoting/
# reopened/archived) cannot exist on a migrated database: migration 158 remapped
# them and its ck_requisitions_status CHECK has forbidden them since.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"open", "hotlist"},
    "open": {"won", "lost", "hotlist"},
    "hotlist": {"open", "won", "lost"},
    "won": {"open"},
    "lost": {"open", "hotlist"},
    "cancelled": {"open"},
}


def transition(
    req,
    new_status: str | RequisitionStatus,
    actor,
    db: Session,
    *,
    reason: str | None = None,
) -> None:
    """Validate and apply a stored-status transition.

    Closing to WON or LOST requires a non-empty ``reason`` (persisted on
    ``req.outcome_reason``); a blank reason raises ``OutcomeReasonRequired``.
    Non-terminal transitions clear any stale outcome_reason. Raises ValueError
    for illegal transitions. Logs to ActivityLog.
    """
    old_status = req.status or "open"
    new_val = new_status.value if isinstance(new_status, RequisitionStatus) else new_status

    if old_status == new_val:
        return  # no-op

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_val not in allowed:
        raise ValueError(f"Invalid transition: {old_status} → {new_val} (allowed: {sorted(allowed)})")

    if new_val in TERMINAL_REASON_REQUIRED:
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise OutcomeReasonRequired()
        req.outcome_reason = clean_reason
    else:
        # Reopening / moving off a terminal state drops the stale close reason.
        req.outcome_reason = None

    req.status = new_val

    try:
        actor_id = actor.id if actor else None
        log_entry = ActivityLog(
            user_id=actor_id,
            activity_type=ActivityType.STATUS_CHANGED,
            channel=Channel.SYSTEM,
            requisition_id=req.id,
            subject=f"Status: {old_status} → {new_val}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.error("Failed to log status transition: {}", e, exc_info=True)


def derived_status_map(db: Session, req_ids: Iterable[int]) -> dict[int, str]:
    """Derived pipeline stage for many requisitions in three set queries (no N+1).

    Returns {req_id: stage} where stage is quoted > offers > rfqs_sent for ids
    with matching data, and 'open' otherwise. Only meaningful for rows whose
    STORED status is 'open' — callers overlay stored draft/hotlist/terminal
    values themselves (or use display_status / derived_status, which do it).

    Signals (each backed by an existing index):
    - quoted:    QuoteRequisition membership (combined quotes included; every
                 quote has >=1 join row via the after_insert listener).
    - offers:    Offer.requisition_id — create paths always stamp it (W3.1).
    - rfqs_sent: Contact rows of contact_type='email' whose send did not fail
                 (the multiplier/vendor-scorecard RFQ ground truth).
    """
    from ..models import Contact, Offer, QuoteRequisition

    ids = list({int(i) for i in req_ids})
    if not ids:
        return {}

    quoted = {
        row[0]
        for row in db.query(QuoteRequisition.requisition_id)
        .filter(QuoteRequisition.requisition_id.in_(ids))
        .distinct()
        .all()
    }
    offered = {row[0] for row in db.query(Offer.requisition_id).filter(Offer.requisition_id.in_(ids)).distinct().all()}
    rfqd = {
        row[0]
        for row in db.query(Contact.requisition_id)
        .filter(
            Contact.requisition_id.in_(ids),
            Contact.contact_type == "email",
            Contact.status != "failed",
        )
        .distinct()
        .all()
    }

    result: dict[int, str] = {}
    for rid in ids:
        if rid in quoted:
            result[rid] = STAGE_QUOTED
        elif rid in offered:
            result[rid] = STAGE_OFFERS
        elif rid in rfqd:
            result[rid] = STAGE_RFQS_SENT
        else:
            result[rid] = RequisitionStatus.OPEN.value
    return result


def derived_status(db: Session, req) -> str:
    """Display status for one requisition: stored intent, else derived stage.

    Stored draft/hotlist/won/lost/cancelled always win (they carry meaning data
    cannot express — outcome_reason, monitor intent). A stored-open requisition
    reports its derived pipeline stage.
    """
    stored = req.status or RequisitionStatus.OPEN.value
    if stored != RequisitionStatus.OPEN.value:
        return stored
    return derived_status_map(db, [req.id])[req.id]


def attach_display_status(db: Session, reqs: list) -> None:
    """Set ``display_status`` (a plain attribute, never the ORM column) on each row.

    Batch helper for list views: one derived_status_map call for the stored-open
    rows, stored value passed through for the rest. Assigning req.status itself
    would dirty the session and flush a derived value into the table — never do
    that.
    """
    open_ids = [r.id for r in reqs if (r.status or "open") == RequisitionStatus.OPEN.value]
    stage_map = derived_status_map(db, open_ids) if open_ids else {}
    for r in reqs:
        stored = r.status or RequisitionStatus.OPEN.value
        r.display_status = stage_map.get(r.id, stored)
