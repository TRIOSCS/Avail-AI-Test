"""offer_service.py — the ONE offer lifecycle service (W3 consolidation, spec §9).

Single implementation for offer create / update / reconfirm / approve / reject /
mark-sold / delete plus the reply-scan system-actor create, consolidated from the
three drifted router copies (routers/crm/offers.py JSON handlers, routers/htmx/
offers/crud.py HTMX handlers, routers/sightings.py delegating wrappers) and the
email pipeline's private auto-create.

Canonical semantics locked here (spec §9 / §10 Wave-3 acceptance):
- reconfirm is TTL-RESETTING everywhere: expires_at = now + 14d, is_stale cleared,
  attribution_status back to active (the old crm JSON copy only bumped
  reconfirmed_at/reconfirm_count, leaving reconfirmed offers stuck stale).
- approve lands on ONE status: OfferStatus.ACTIVE (the htmx review door used to
  land on APPROVED; APPROVED is retired as a landing state — existing rows keep
  it until the W3 status-remap migration).
- create composes the richest merged pipeline (vendor-card resolve/fuzzy/create +
  optional background enrichment, material card, requisition OPEN→offers
  transition, per-part requirement-status advance, unavailability release hook,
  activity log, task auto-gen, knowledge capture, strategic-vendor clock, SSE)
  PLUS the htmx copy's spq persist and qualification schema=1 + requests[] stamp,
  PLUS currency persist (previously dropped by the JSON builder).

Authorization stays at the routes (require_user / require_buyer /
require_requisition_access) — this module assumes the caller has already gated
access and only enforces business rules (status guards, transition table).

Called by: routers/htmx/offers/crud.py, routers/sightings.py,
    routers/crm/offers.py (surviving JSON routes), services/ai_offer_service.py
    (form-parse save loop), email_service.py (reply-scan auto-create).
Depends on: models (Offer, Requirement, Requisition, VendorCard, ChangeLog,
    ActivityLog, VendorResponse, User), schemas/crm (OfferCreate, OfferUpdate),
    services/offer_qualification, services/status_machine,
    services/vendor_unavailability, services/activity_service,
    services/credential_service, search_service (lazy), vendor_utils,
    utils/normalization, utils/sql_helpers, utils/async_helpers.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import (
    ActivityType,
    AttributionStatus,
    OfferStatus,
    RequisitionStatus,
)
from ..models import ActivityLog, ChangeLog, Offer, Requirement, Requisition, User, VendorCard, VendorResponse
from ..schemas.crm import OfferCreate, OfferUpdate
from ..utils.async_helpers import hold_bg_task, safe_background_task
from ..utils.normalization import normalize_mpn_key
from ..utils.sql_helpers import escape_like
from ..vendor_utils import normalize_vendor_name
from .activity_service import log_activity
from .credential_service import get_credential_cached
from .offer_qualification import apply_qualification
from .status_machine import require_valid_transition
from .vendor_unavailability import maybe_release_on_offer

# Fields whose old→new values are written to the change_log on update. Union of the
# two retired copies' lists (crm JSON: through `status`; htmx edit added spq/firmware/
# hardware_code/country_of_origin/valid_until).
TRACKABLE_FIELDS = [
    "vendor_name",
    "qty_available",
    "unit_price",
    "lead_time",
    "condition",
    "warranty",
    "manufacturer",
    "date_code",
    "packaging",
    "moq",
    "notes",
    "status",
    "spq",
    "firmware",
    "hardware_code",
    "country_of_origin",
    "valid_until",
]


def record_changes(
    db: Session, entity_type: str, entity_id: int, user_id: int, old_dict: dict, new_dict: dict, fields: list[str]
):
    """Record field-level changes to the change_log table.

    Moved here from routers/crm/_helpers.py (W3) — the offer lifecycle is its only
    caller; _helpers re-exports it for backward compatibility.
    """
    for f in fields:
        old_val = str(old_dict.get(f) or "")
        new_val = str(new_dict.get(f) or "")
        if old_val != new_val:
            db.add(
                ChangeLog(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    user_id=user_id,
                    field_name=f,
                    old_value=old_val,
                    new_value=new_val,
                )
            )


def _log_offer_status_change(db: Session, offer: Offer, old_status, user: User) -> None:
    """Emit the standard OFFER_STATUS_CHANGED activity log for an offer transition."""
    log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )


def _resolve_vendor_card(db: Session, payload: OfferCreate) -> tuple[VendorCard, tuple | None]:
    """Resolve the vendor card for a create: id → exact normalized name → fuzzy ≥88
    (appending the submitted name as an alternate) → create new.

    Returns (card, enrich_args) where enrich_args is the background-enrichment tuple
    for a freshly created card with a domain and available enrichment credentials,
    else None.
    """
    norm_name = normalize_vendor_name(payload.vendor_name)
    card = None

    # 1) If the caller passed a vendor_card_id, use it directly
    if payload.vendor_card_id:
        card = db.get(VendorCard, payload.vendor_card_id)

    # 2) Exact match on normalized name
    if not card:
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()

    # 3) Fuzzy match: ILIKE prefix search + fuzzy scoring
    if not card:
        from ..vendor_utils import fuzzy_match_vendor

        prefix = norm_name.split()[0] if norm_name else ""
        if prefix and len(prefix) >= 2:
            candidates = (
                db.query(VendorCard)
                .filter(VendorCard.normalized_name.ilike(f"{escape_like(prefix)}%", escape="\\"))
                .limit(20)
                .all()
            )
            if candidates:
                matches = fuzzy_match_vendor(
                    payload.vendor_name,
                    [c.display_name for c in candidates],
                    threshold=88,
                )
                if matches:
                    best_name = matches[0]["name"]
                    card = next(c for c in candidates if c.display_name == best_name)
                    # Append submitted name as alternate for future exact lookups
                    alts = list(card.alternate_names or [])
                    if payload.vendor_name not in alts and payload.vendor_name != card.display_name:
                        alts.append(payload.vendor_name)
                        card.alternate_names = alts

    # 4) No match — create a new card
    enrich_args = None
    if not card:
        domain = ""
        if payload.vendor_website:
            domain = (
                payload.vendor_website.replace("https://", "")
                .replace("http://", "")
                .replace("www.", "")
                .split("/")[0]
                .lower()
            )
        card = VendorCard(
            normalized_name=norm_name,
            display_name=payload.vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(card)
        db.flush()
        if domain and (
            get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
            or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        ):
            enrich_args = (card.id, domain, card.display_name)
    return card, enrich_args


async def create_offer(db: Session, req_id: int, payload: OfferCreate, actor: User) -> Offer:
    """Create an offer on a requisition — the ONE interactive create pipeline.

    Commits. Raises HTTPException(404) if the requisition does not exist. Access checks
    are the caller's responsibility.
    """
    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    card, enrich_args = _resolve_vendor_card(db, payload)

    from ..search_service import resolve_material_card

    mat_card = resolve_material_card(payload.mpn, db)

    offer = Offer(
        requisition_id=req_id,
        requirement_id=payload.requirement_id,
        material_card_id=mat_card.id if mat_card else None,
        normalized_mpn=normalize_mpn_key(payload.mpn) if payload.mpn else None,
        vendor_card_id=card.id,
        vendor_name=card.display_name,
        vendor_name_normalized=card.normalized_name,
        mpn=payload.mpn,
        manufacturer=payload.manufacturer,
        qty_available=payload.qty_available,
        unit_price=payload.unit_price,
        lead_time=payload.lead_time,
        date_code=payload.date_code,
        condition=payload.condition,
        packaging=payload.packaging,
        firmware=payload.firmware,
        hardware_code=payload.hardware_code,
        moq=payload.moq,
        spq=payload.spq,
        warranty=payload.warranty,
        country_of_origin=payload.country_of_origin,
        valid_until=payload.valid_until,
        source=payload.source,
        vendor_response_id=payload.vendor_response_id,
        entered_by_id=actor.id,
        notes=payload.notes,
        status=payload.status,
    )
    if payload.currency:
        # Only override the column default ("USD") when a currency was supplied.
        offer.currency = payload.currency
    # Qualification blob: forward-version with schema=1 + a requests[] list (spec §3.1)
    # whenever a blob is present — every door now stamps identically.
    qual = dict(payload.qualification) if payload.qualification else None
    if qual is not None:
        qual.setdefault("requests", [])
        qual["schema"] = 1
    offer.qualification = qual
    # Non-raising: composes the standardized note + sets qualification_status. The
    # essentials gate is enforced at the buyer-facing routes, not in this builder.
    apply_qualification(offer)
    db.add(offer)

    if req.status == RequisitionStatus.OPEN:
        from .requisition_state import transition as req_transition

        try:
            req_transition(req, "offers", actor, db)
        except ValueError:
            pass  # already in offers or later state

    # Auto-advance per-part sourcing status when an ACTIVE offer lands on a part
    if payload.requirement_id and offer.status == OfferStatus.ACTIVE:
        try:
            from .requirement_status import on_offer_created

            requirement = db.get(Requirement, payload.requirement_id)
            if requirement:
                on_offer_created(requirement, db, actor=actor)
        except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
            logger.warning("Requirement status update failed: {}", e)

    db.flush()  # offer.id populated; activity row + offer committed together below

    # Offer hook: a user-entered ACTIVE offer is proof of availability — release the
    # vendor's matching active unavailability records ('offer_received'). Same
    # session/commit as the offer itself.
    if offer.status == OfferStatus.ACTIVE:
        maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, actor, offer_condition=offer.condition)

    log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=actor.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
    db.commit()

    # Auto-generate review task for the new offer
    try:
        from .task_service import on_offer_received

        on_offer_received(db, offer.requisition_id, offer.vendor_name, offer.mpn, offer.id)
    except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
        logger.warning("Task auto-gen for offer failed", exc_info=True)

    # Auto-capture offer facts into Knowledge Ledger
    try:
        from .knowledge_service import capture_offer_fact

        capture_offer_fact(db, offer=offer, user_id=actor.id)
    except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
        logger.warning("Knowledge auto-capture (offer) failed: {}", e)

    # Reset strategic vendor 39-day clock if this vendor is claimed
    if offer.vendor_card_id:
        try:
            from .strategic_vendor_service import record_offer

            record_offer(db, offer.vendor_card_id)
        except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
            logger.warning("Strategic vendor clock reset failed: {}", e)

    # Background vendor enrichment — fire after commit so the card is persisted
    if enrich_args:
        from ..utils.vendor_helpers import _background_enrich_vendor

        await safe_background_task(_background_enrich_vendor(*enrich_args), task_name="enrich_vendor_from_offer")

    # Notify the requisition creator via SSE that a new offer/quote was added
    notify_user_id = req.created_by if req.created_by and req.created_by != actor.id else actor.id
    try:
        from .sse_broker import broker

        await broker.publish(
            f"user:{notify_user_id}",
            "quote_updated",
            '{"offer_id": ' + str(offer.id) + ', "requisition_id": ' + str(req_id) + "}",
        )
    except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
        logger.warning("SSE quote_updated notification failed", exc_info=True)

    return offer


def update_offer(db: Session, offer: Offer, payload: OfferUpdate, actor: User) -> None:
    """Apply an OfferUpdate — field writes, change_log rows, transition guard on status,
    qualification re-apply.

    Commits.
    """
    changes = payload.model_dump(exclude_unset=True)
    old_dict = {f: getattr(offer, f) for f in TRACKABLE_FIELDS}
    if "status" in changes and changes["status"] != offer.status:
        require_valid_transition("offer", offer.status, changes["status"])
    for field, value in changes.items():
        setattr(offer, field, value)
    new_dict = {f: getattr(offer, f) for f in TRACKABLE_FIELDS}
    record_changes(db, "offer", offer.id, actor.id, old_dict, new_dict, TRACKABLE_FIELDS)
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = actor.id

    if "qualification" in changes:
        offer.qualification = changes["qualification"] or None
    # Non-raising: composes the standardized note + sets qualification_status. The
    # essentials gate is enforced at the buyer-facing routes, not here.
    apply_qualification(offer)

    db.commit()


def reconfirm_offer(db: Session, offer: Offer, actor: User) -> Offer:
    """Reconfirm an offer — THE canonical TTL-RESETTING semantics (spec §9).

    Resets the 14-day freshness TTL, clears staleness, and reactivates attribution in
    addition to stamping reconfirmed_at / reconfirm_count. Commits.
    """
    now = datetime.now(UTC)
    offer.reconfirmed_at = now
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    offer.expires_at = now + timedelta(days=14)
    offer.attribution_status = AttributionStatus.ACTIVE
    offer.is_stale = False
    offer.updated_at = now
    offer.updated_by_id = actor.id
    db.commit()
    logger.info("Offer {} reconfirmed (count={}) by {}", offer.id, offer.reconfirm_count, actor.email)
    return offer


def approve_offer(db: Session, offer: Offer, actor: User) -> None:
    """Approve a pending_review offer — lands on OfferStatus.ACTIVE (the ONE approve
    landing status). Commits, then triggers the proactive re-match.

    Raises HTTPException(400) unless the offer is pending_review.
    """
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be approved")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    offer.status = OfferStatus.ACTIVE
    offer.approved_by_id = actor.id
    offer.approved_at = datetime.now(UTC)
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = actor.id
    record_changes(
        db, "offer", offer.id, actor.id, {"status": old_status}, {"status": str(OfferStatus.ACTIVE)}, ["status"]
    )
    # Offer hook: user approval of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, actor, offer_condition=offer.condition)
    _log_offer_status_change(db, offer, old_status, actor)
    db.commit()

    # INVARIANT (status_machine): a transition into a live status from a non-live one
    # must trigger the targeted proactive re-match AFTER commit.
    from .proactive_matching import trigger_rematch_on_offer_approval

    trigger_rematch_on_offer_approval(db, offer)


def reject_offer(db: Session, offer: Offer, actor: User, reason: str = "") -> None:
    """Reject a pending_review offer (audit trail kept). Commits.

    Raises HTTPException(400) unless the offer is pending_review.
    """
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be rejected")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    offer.status = OfferStatus.REJECTED
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = actor.id
    if reason:
        offer.notes = f"{offer.notes or ''}\n[Rejected: {reason}]".strip()
    record_changes(
        db, "offer", offer.id, actor.id, {"status": old_status}, {"status": str(OfferStatus.REJECTED)}, ["status"]
    )
    _log_offer_status_change(db, offer, old_status, actor)
    db.commit()


def mark_offer_sold(db: Session, offer: Offer, actor: User) -> bool:
    """Mark an offer as sold — stock is confirmed purchased/gone. Commits.

    Returns False (no-op) when the offer is already sold, True otherwise.
    """
    if offer.status == OfferStatus.SOLD:
        return False
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = actor.id
    record_changes(
        db, "offer", offer.id, actor.id, {"status": old_status}, {"status": str(OfferStatus.SOLD)}, ["status"]
    )
    _log_offer_status_change(db, offer, old_status, actor)
    db.commit()
    return True


def delete_offer(db: Session, offer: Offer, actor: User) -> None:
    """Delete an offer.

    Commits.
    """
    offer_id = offer.id
    db.delete(offer)
    db.commit()
    logger.info("Offer {} deleted by {}", offer_id, actor.email)


# ── Reply-scan system-actor create (kernel pipeline) ─────────────────────────
# Kept as its own pipeline, NOT routed through create_offer: the email path is
# system-initiated (no actor, no access context), dedups by vendor_response_id+mpn,
# stamps evidence tier + parse confidence, uses the email-specific task hook, and
# deliberately skips vendor-card creation and the unavailability release hook.
# Behavior-preserving move from email_service._auto_create_offers_from_parse (W3).


def create_offers_from_parsed_response(vr: VendorResponse, parsed: dict, db: Session) -> None:
    """Auto-create Offer records and side effects from AI-parsed email data.

    - Creates Offer records (confidence >= 0.8 active, 0.5-0.8 pending_review)
    - Generates tasks for email-parsed offers
    - Captures offer facts into Knowledge Ledger
    - Propagates tags from material cards to vendor cards
    - Resets strategic vendor 39-day clock
    - Creates/updates deduplicated ActivityLog notifications
    Does not commit — the email pipeline commits per batch.

    Called by: email_service._apply_parsed_result (when db provided).
    Depends on: extract_draft_offers, resolve_material_card, normalize_mpn_key,
                tier_for_parsed_offer, task_service, knowledge_service, tagging.
    """
    if not (vr.confidence and vr.confidence >= 0.5):
        return

    try:
        from .response_parser import extract_draft_offers

        draft_offers = extract_draft_offers(parsed, vr.vendor_name or "Unknown")
    except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
        logger.warning("Failed to extract draft offers: {}", e)
        return

    req = db.get(Requisition, vr.requisition_id) if vr.requisition_id else None
    owner_id = vr.scanned_by_user_id
    if req and req.created_by:
        owner_id = req.created_by

    # Build maps for linking: MPN -> requirement_id, MPN -> material_card_id
    from ..search_service import resolve_material_card

    mpn_to_req_id: dict[str, int] = {}
    mpn_to_card_id: dict[str, int] = {}
    if req:
        for r in db.query(Requirement).filter(Requirement.requisition_id == vr.requisition_id).all():
            if r.primary_mpn:
                key = normalize_mpn_key(r.primary_mpn) or r.primary_mpn.upper().strip()
                mpn_to_req_id[key] = r.id
                if r.material_card_id:
                    mpn_to_card_id[key] = r.material_card_id

    for draft in draft_offers:
        try:
            raw_mpn = draft.get("mpn") or ""
            mpn_key = normalize_mpn_key(raw_mpn) or raw_mpn.upper().strip()
            # Dedup: check if offer already exists from this vendor response
            existing = (
                db.query(Offer.id)
                .filter(
                    Offer.vendor_response_id == vr.id,
                    Offer.mpn == draft.get("mpn", ""),
                )
                .first()
            )
            if existing:
                continue

            # Resolve material card -- use requirement's card or find/create
            card_id = mpn_to_card_id.get(mpn_key)
            if not card_id and raw_mpn.strip():
                card = resolve_material_card(raw_mpn, db)
                if card:
                    card_id = card.id

            from ..evidence_tiers import tier_for_parsed_offer

            offer = Offer(
                requisition_id=vr.requisition_id,
                requirement_id=mpn_to_req_id.get(mpn_key),
                material_card_id=card_id,
                vendor_name=draft.get("vendor_name", ""),
                vendor_name_normalized=normalize_vendor_name(draft.get("vendor_name", "")),
                mpn=draft.get("mpn", ""),
                manufacturer=draft.get("manufacturer"),
                qty_available=draft.get("qty_available"),
                unit_price=draft.get("unit_price"),
                currency=draft.get("currency", "USD"),
                lead_time=draft.get("lead_time"),
                date_code=draft.get("date_code"),
                condition=draft.get("condition"),
                packaging=draft.get("packaging"),
                moq=draft.get("moq"),
                notes=draft.get("notes"),
                source="email_parse",
                status=OfferStatus.ACTIVE if vr.confidence >= 0.8 else OfferStatus.PENDING_REVIEW,
                vendor_response_id=vr.id,
                evidence_tier=tier_for_parsed_offer(vr.confidence),
                parse_confidence=vr.confidence,
            )
            db.add(offer)
            db.flush()

            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=offer.requisition_id,
                requirement_id=offer.requirement_id,
                user_id=None,
                vendor_card_id=offer.vendor_card_id,
                description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
                details={"offer_id": offer.id, "source": offer.source},
            )

            # Auto-generate task for email-parsed offer
            try:
                from .task_service import on_email_offer_parsed

                on_email_offer_parsed(
                    db,
                    offer.requisition_id,
                    offer.vendor_name or "Unknown",
                    offer.mpn or "?",
                    offer.id,
                )
            except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
                logger.warning("Task auto-gen for email offer failed", exc_info=True)

            # Auto-capture offer facts into Knowledge Ledger
            try:
                from .knowledge_service import capture_offer_fact

                capture_offer_fact(db, offer=offer)
            except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
                logger.warning("Knowledge auto-capture (email offer) failed: {}", e)

            # Tag propagation: propagate material card tags to vendor
            try:
                if offer.material_card_id and offer.vendor_name_normalized:
                    from .tagging import propagate_tags_to_entity

                    vc = db.query(VendorCard).filter_by(normalized_name=offer.vendor_name_normalized).first()
                    if vc:  # pragma: no cover
                        propagate_tags_to_entity("vendor_card", vc.id, offer.material_card_id, 1.0, db)
            except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
                logger.warning("Tag propagation failed for offer {}", offer.id, exc_info=True)

            # Reset strategic vendor 39-day clock
            if offer.vendor_card_id:
                try:
                    from .strategic_vendor_service import record_offer as sv_record

                    sv_record(db, offer.vendor_card_id)
                except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
                    logger.warning("Strategic vendor clock reset failed for offer {}", offer.id, exc_info=True)

            # Deduplicated notification -- update existing if unread, else create new
            if owner_id:
                notif_q = db.query(ActivityLog).filter(
                    ActivityLog.user_id == owner_id,
                    ActivityLog.activity_type == "offer_pending_review",
                    ActivityLog.requisition_id == vr.requisition_id,
                    ActivityLog.dismissed_at.is_(None),
                )
                # When requisition_id is None the filter above would match ALL
                # null-req notifications from any vendor — scope to this vendor.
                if vr.requisition_id is None:
                    notif_q = notif_q.filter(ActivityLog.contact_name == vr.vendor_name)
                existing_notif = notif_q.first()
                if existing_notif:
                    existing_notif.subject = (
                        f"New vendor offer needs review: {vr.vendor_name or 'Unknown'} — {draft.get('mpn', '?')}"
                    )
                    existing_notif.created_at = datetime.now(UTC)
                else:
                    db.add(
                        ActivityLog(
                            user_id=owner_id,
                            activity_type="offer_pending_review",
                            channel="system",
                            requisition_id=vr.requisition_id,
                            contact_name=vr.vendor_name,
                            subject=f"New vendor offer needs review: {vr.vendor_name or 'Unknown'} — {draft.get('mpn', '?')}",
                        )
                    )
        except Exception as e:  # noqa: BLE001 — optional side effect must never fail the offer write
            logger.error("Failed to create offer for {}: {}", draft.get("mpn", "?"), e, exc_info=True)

    # Publish SSE event so sightings page refreshes for affected requirements
    if owner_id:
        try:
            from .sse_broker import broker

            affected_req_ids = set(mpn_to_req_id.values())
            loop = asyncio.get_event_loop()
            for rid in affected_req_ids:
                task = loop.create_task(
                    broker.publish(
                        f"user:{owner_id}",
                        "sighting-updated",
                        json.dumps({"requirement_id": rid}),
                    )
                )
                hold_bg_task(task)
        except Exception:  # noqa: BLE001 — optional side effect must never fail the offer write
            logger.debug("SSE publish from auto-create offers skipped (no event loop)")
