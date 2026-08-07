"""Offers tab — lifecycle actions, qualify-AI, vendor requests.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
)
from ...database import get_db
from ...dependencies import (
    require_access,
    require_buyer,
    require_fresh_token,
    require_requisition_access,
    require_user,
)
from ...models import User
from ...models.offers import Offer
from ...models.sourcing import Requirement
from ...services.activity_service import log_rfq_activity
from ...template_env import template_response
from .common import (  # noqa: F401
    _EXCLUDED_REQ_STATUSES,
    _EXCLUDED_SOURCING_STATUSES,
    _SEARCH_FANOUT_LIMIT,
    MAX_BATCH_SIZE,
    _active_sourcing_status_clause,
    _append_oob_toast,
    _best_contacts_by_card,
    _get_cached,
    _invalidate_cache,
    _mpn_link_map,
    _oob_toast,
    _oob_toast_html,
    _publish_if_user_source,
    _refresh_offers_panel,
    _render_offers_panel,
    _toast_suppressed_for_sse,
    _with_toast,
    router,
)
from .offers_forms import _echo_prefill, _parse_iso_date, _qual_dict  # noqa: F401
from .rfq_send import (  # noqa: F401
    RFQ_DATASHEETS_DROPPED_HEADER,
    RFQ_SENT_HEADER,
    RFQ_SKIPPED_HEADER,
    RFQ_TOTAL_HEADER,
    RFQ_UNAVAILABLE_HEADER,
)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def sightings_review_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    action: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
):
    """Approve or reject a pending_review offer, then re-render the offers panel."""
    from ...services.offer_service import approve_offer, reject_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)
    # Scope the offer to the path requirement (IDOR guard — prevents a guessed offer_id
    # from a different requirement from being approved/rejected).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    if action == "approve":
        approve_offer(db, offer, user)
    else:
        reject_offer(db, offer, user)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/reconfirm", response_class=HTMLResponse)
async def sightings_reconfirm_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
):
    """Reconfirm an offer (canonical TTL-resetting semantics), then re-render the offers
    panel."""
    from ...services.offer_service import reconfirm_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    reconfirm_offer(db, offer, user)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/mark-sold", response_class=HTMLResponse)
async def sightings_mark_offer_sold(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Mark an offer sold, then re-render the offers panel."""
    from ...services.offer_service import mark_offer_sold

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    mark_offer_sold(db, offer, user)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.delete("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_delete_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Delete an offer, then re-render the offers panel."""
    from ...services.offer_service import delete_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    delete_offer(db, offer, user)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


def _offer_for_requirement_or_404(db: Session, requirement_id: int, offer_id: int):
    requirement = db.get(Requirement, requirement_id)
    offer = db.get(Offer, offer_id)
    if not requirement or not offer or offer.requirement_id != requirement_id:
        raise HTTPException(404, "Not found")
    return requirement, offer


@router.get(
    "/v2/partials/sightings/{requirement_id}/offers/{offer_id}/qualify-ai",
    response_class=HTMLResponse,
)
async def sightings_qualify_ai(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Qualify-with-AI modal: parse the linked vendor email, pre-fill the offer form,
    and compute the ask-the-vendor gap checklist. Read-only; nothing is sent or saved."""
    from ...models.offers import VendorResponse
    from ...services.offer_qualification import compute_qual_gaps, normalize_offer_condition
    from ...services.response_parser import extract_draft_offers, parse_vendor_response

    requirement, offer = _offer_for_requirement_or_404(db, requirement_id, offer_id)
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")

    vr = db.get(VendorResponse, offer.vendor_response_id) if offer.vendor_response_id else None
    if not vr:
        raise HTTPException(404, "No linked vendor email for this offer")

    def _json_safe(v: object) -> object:
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    fields = [
        "vendor_name",
        "mpn",
        "manufacturer",
        "qty_available",
        "unit_price",
        "lead_time",
        "date_code",
        "condition",
        "packaging",
        "firmware",
        "hardware_code",
        "moq",
        "spq",
        "warranty",
        "country_of_origin",
        "notes",
    ]
    prefill = {f: _json_safe(getattr(offer, f, None)) for f in fields}
    _q = offer.qualification or {}
    for _qk in (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    ):
        prefill[_qk] = _json_safe(_q.get(_qk))

    rfq_context = {"mpn": requirement.primary_mpn, "qty": requirement.target_qty}
    try:
        parsed = await parse_vendor_response(vr.body or "", vr.subject or "", vr.vendor_name or "", rfq_context)
    except Exception as exc:  # parse must never break the modal
        logger.warning("qualify-ai parse failed for offer {}: {}", offer_id, exc)
        parsed = None

    confidence = None
    ai_extract: dict = {}
    if parsed:
        confidence = parsed.get("confidence")
        drafts = extract_draft_offers(parsed, vr.vendor_name or "")
        if drafts:
            ai_extract = next(
                (d for d in drafts if (d.get("mpn") or "").lower() == (offer.mpn or "").lower()),
                drafts[0],
            )
    # Overlay AI values onto EMPTY offer fields only — never clobber saved values.
    for f in ("manufacturer", "qty_available", "unit_price", "lead_time", "date_code", "condition", "packaging", "moq"):
        if prefill.get(f) in (None, "") and ai_extract.get(f) not in (None, ""):
            prefill[f] = _json_safe(ai_extract.get(f))

    condition = normalize_offer_condition(prefill.get("condition")) or prefill.get("condition")
    gap_items = compute_qual_gaps(prefill, condition)

    ctx = {
        "request": request,
        "requirement": requirement,
        "offer": offer,
        "prefill": prefill,
        "gap_items": gap_items,
        "confidence": confidence,
        "vr": vr,
    }
    return template_response("htmx/partials/sightings/qual_request_modal.html", ctx)


@router.post(
    "/v2/partials/sightings/{requirement_id}/offers/{offer_id}/qualify-ai/draft-request",
    response_class=HTMLResponse,
)
async def sightings_qualify_ai_draft(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Draft a vendor reply asking for the chosen qualification items (AI-suggested gaps
    the user kept, plus any custom items the user added).

    Renders the editable compose box; sending goes through the existing send-reply path.
    """
    from ...models.offers import VendorResponse
    from ...services.email_drafting import draft_email

    requirement, offer = _offer_for_requirement_or_404(db, requirement_id, offer_id)
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")

    vr = db.get(VendorResponse, offer.vendor_response_id) if offer.vendor_response_id else None
    if not vr:
        raise HTTPException(404, "No linked vendor email for this offer")

    form = await request.form()
    checked = [c.strip() for c in form.getlist("checked_items") if c and c.strip()]
    custom = [c.strip() for c in form.getlist("custom_items") if c and c.strip()]
    items_requested = checked + custom

    result = await draft_email(
        "qual_request",
        {"vendor_name": vr.vendor_name, "subject": vr.subject, "mpn": offer.mpn, "items_requested": items_requested},
    )

    default_subject = vr.subject or "RFQ"
    if not default_subject.lower().startswith("re:"):
        default_subject = f"Re: {default_subject}"
    ctx = {
        "request": request,
        "req_id": offer.requisition_id,
        "r": vr,
        "reply_subject": (result or {}).get("subject") or default_subject,
        "reply_body": (result or {}).get("body") or "",
        "ai_failed": result is None,
    }
    return template_response("htmx/partials/requisitions/tabs/reply_compose.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/request", response_class=HTMLResponse)
async def sightings_offer_request(
    request: Request,
    requirement_id: int,
    offer_id: int,
    kind: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Log a pending vendor request (images / FPQ / cert / pkg qty) on an offer.

    Logs the request to offer.qualification['requests'] (status="pending") and returns
    the drafted RFQ-back line as a toast. This route is require_buyer-only (NO Graph
    token) so logging never 401s on an expired token. Sending a logged PENDING request
    as a real email is now available on demand via the separate token-bearing route
    `.../offers/{offer_id}/request/{index}/send` (sightings_offer_request_send) — the
    buyer no longer has to copy the draft into the solicit modal by hand.
    """
    from datetime import datetime

    from ...services.offer_qualification import REQUEST_KINDS, request_template

    if kind not in REQUEST_KINDS:
        raise HTTPException(status_code=400, detail={"error": "invalid request kind"})
    offer = db.get(Offer, offer_id)
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer is missing or belongs to another requirement.
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    draft = request_template(kind, offer.mpn)
    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    reqs.append(
        {
            "kind": kind,
            "status": "pending",
            "requested_at": datetime.now(UTC).isoformat(),
            "contact_id": None,
        }
    )
    q["requests"] = reqs
    offer.qualification = q
    db.commit()
    db.expire_all()
    return _append_oob_toast(_refresh_offers_panel(request, requirement_id, db), f"Logged request: {draft}")


@router.post(
    "/v2/partials/sightings/{requirement_id}/offers/{offer_id}/request/{index}/send",
    response_class=HTMLResponse,
)
async def sightings_offer_request_send(
    request: Request,
    requirement_id: int,
    offer_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
    token: str = Depends(require_fresh_token),
):
    """Send a previously-logged PENDING #7 vendor request as a real RFQ-back email.

    Distinct from the logging route (sightings_offer_request) on purpose: this route
    also requires a fresh Graph token (require_fresh_token) so the actual send fails
    loudly on an expired token, while LOGGING a request never 401s. `index` addresses
    the entry in offer.qualification['requests'] (append-only, so the index is stable).

    Flow: resolve the vendor's best contact email (_best_contacts_by_card, mirroring the
    batch send-inquiry path), draft the request body via request_template, and hand a
    single vendor group to send_batch_rfq with the SCALAR requisition_id (single-req
    mode; passing both the scalar and a parts-map raises ValueError). send_batch_rfq
    commits internally and can expire the session, so the entry-status update is applied
    AFTER it returns against a freshly re-fetched offer. Idempotent on an already-"sent"
    entry; a single request is logged as an outreach activity but does NOT auto-progress
    the sourcing status (one clarification is not a full RFQ round).
    """
    from datetime import datetime

    from ...email_service import send_batch_rfq
    from ...services.offer_qualification import request_template

    offer = db.get(Offer, offer_id)
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer is missing or belongs to another requirement.
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")

    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    if index < 0 or index >= len(reqs):
        raise HTTPException(status_code=404, detail={"error": "request not found"})
    # Copy the entry into a fresh nested dict (the dict()/list() above are SHALLOW, so
    # reqs[index] is still the committed-JSON baseline object — see the post-send block).
    entry = dict(reqs[index])
    reqs[index] = entry

    # Idempotency: a request already sent is never re-sent (the entry is the durable
    # record of the outreach). Surface an info toast, leave state untouched.
    if entry.get("status") == "sent":
        return _append_oob_toast(
            _refresh_offers_panel(request, requirement_id, db),
            "Request already sent",
            "info",
        )

    # Requisition guard: Contact.requisition_id is NOT NULL, so send_batch_rfq can write
    # no tracking row without a requisition. An offer with no requisition (unsolicited
    # inbound) is marked "skipped" rather than firing an untracked email.
    if offer.requisition_id is None:
        entry["status"] = "skipped"
        q["requests"] = reqs
        offer.qualification = q
        db.commit()
        return _append_oob_toast(
            _refresh_offers_panel(request, requirement_id, db),
            "No requisition on this offer — not sent",
            "warning",
        )

    # Resolve the vendor's BEST contact email exactly as the batch send path does:
    # worst-first ordering + last-wins dict so a real email always beats a NULL/'' row.
    contact_map = (
        {c.vendor_card_id: c for c in _best_contacts_by_card(db, [offer.vendor_card_id])}
        if offer.vendor_card_id
        else {}
    )
    contact = contact_map.get(offer.vendor_card_id)
    vendor_email = contact.email if contact and contact.email else ""

    draft = request_template(entry["kind"], offer.mpn)
    requirement = db.get(Requirement, requirement_id)
    parts = [{"mpn": offer.mpn, "qty": requirement.target_qty if requirement else None}]

    # Single-requisition mode: pass the SCALAR requisition_id (one Contact row, parts
    # from the vendor group). Passing requisition_parts_map AS WELL would raise ValueError.
    results = await send_batch_rfq(
        token=token,
        db=db,
        user_id=user.id,
        requisition_id=offer.requisition_id,
        vendor_groups=[
            {
                "vendor_name": offer.vendor_name,
                "vendor_email": vendor_email,
                "parts": parts,
                "subject": f"Request: {entry['kind']} — {offer.mpn}",
                "body": draft,
            }
        ],
    )

    # CRITICAL: send_batch_rfq does its own db.commit() and can expire the session, so
    # re-fetch the offer and re-read the requests list before mutating the entry status.
    offer = db.get(Offer, offer_id)
    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    # COPY the target entry into a fresh nested dict before mutating: dict(q)/list(reqs)
    # are SHALLOW, so reqs[index] is still the SAME object SQLAlchemy holds as the
    # committed JSON baseline. Mutating it in place would make the new value equal the
    # (already-mutated) old value and the JSON flush would write nothing. Re-slotting a
    # fresh dict keeps the change detectable.
    entry = dict(reqs[index])
    reqs[index] = entry

    r = results[0]
    if r["status"] == "sent":
        entry["status"] = "sent"
        entry["contact_id"] = r.get("id")
        entry["sent_at"] = datetime.now(UTC).isoformat()
        toast_msg, toast_level = (f"Request sent to {offer.vendor_name}", "success")
        # Log the outreach (mirrors sightings_send_inquiry's rfq_sent), but deliberately
        # NO auto_progress: one clarification request is not a full RFQ round.
        log_rfq_activity(
            db=db,
            rfq_id=offer.requisition_id,
            activity_type="rfq_sent",
            description=f"Requested {entry['kind']} from {offer.vendor_name}",
            user_id=user.id,
            requirement_id=requirement_id,
        )
    elif r["status"] == "skipped":
        entry["status"] = "skipped"
        toast_msg, toast_level = ("Not sent — no contact email on file", "warning")
    else:
        entry["status"] = "failed"
        entry["error"] = r.get("error")
        toast_msg, toast_level = (f"Send failed for {offer.vendor_name}", "error")

    # Reassign a NEW dict so SQLAlchemy's JSON change detection persists the mutation.
    q["requests"] = reqs
    offer.qualification = q
    db.commit()

    return _append_oob_toast(
        _refresh_offers_panel(request, requirement_id, db),
        toast_msg,
        toast_level,
    )
