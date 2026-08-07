"""RFQ preview + send paths.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    RfqAttachmentStatus,
    SightingsSkipReason,
    SourcingStatus,
)
from ...database import get_db
from ...dependencies import (
    require_access,
    require_fresh_token,
    require_requisition_access_bulk,
    require_user,
)
from ...models import User
from ...models.intelligence import MaterialCardDatasheet
from ...models.sourcing import Requirement
from ...models.vendors import VendorCard
from ...services.activity_service import log_rfq_activity
from ...services.rfq_attachments import trim_datasheet_names_to_cap
from ...services.vendor_reachability import dnc_emails_for_cards as _dnc_emails_for_cards
from ...services.vendor_unavailability import (
    excluded_vendor_norms,
)
from ...template_env import template_response
from ...vendor_utils import normalize_vendor_name
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

# Result headers on POST /v2/partials/sightings/send-inquiry: the route returns HTTP 200
# even on a partial/total send failure, so the browser modal reads the true delivered
# count from these rather than inferring success from the status code.
RFQ_SENT_HEADER = "X-RFQ-Sent"
RFQ_TOTAL_HEADER = "X-RFQ-Total"
RFQ_SKIPPED_HEADER = "X-RFQ-Skipped"  # vendors with no contact email (not a delivery failure)
RFQ_UNAVAILABLE_HEADER = "X-RFQ-Unavailable"  # vendors dropped by the active-only unavailability re-check
RFQ_DATASHEETS_DROPPED_HEADER = "X-RFQ-Datasheets-Dropped"  # oversized datasheets dropped before send


def _partition_by_unavailability(vendor_names: list[str], excluded_norms: set[str]) -> tuple[list[str], list[str]]:
    """Split vendor display names into (unavailable, sendable) against the active-only
    excluded-norm set, preserving order.

    The RFQ preview/send re-check shares this partition: vendors with an ACTIVE
    unavailability record on the selected parts are dropped from the send and reported
    visibly, never silently.
    """
    unavailable = [vn for vn in vendor_names if normalize_vendor_name(vn) in excluded_norms]
    sendable = [vn for vn in vendor_names if normalize_vendor_name(vn) not in excluded_norms]
    return unavailable, sendable


@router.post("/v2/partials/sightings/preview-inquiry", response_class=HTMLResponse)
async def sightings_preview_inquiry(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Preview rendered RFQ emails per vendor without sending.

    Called by: vendor_modal.html Preview button
    Depends on: email_service._build_html_body, VendorCard, VendorContact
    """
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.isdigit()]
    vendor_names = form.getlist("vendor_names")
    email_body = form.get("email_body", "")

    if not requirement_ids or not vendor_names:
        raise HTTPException(status_code=400, detail="requirement_ids and vendor_names required")

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    require_requisition_access_bulk(db, (r.requisition_id for r in requirements), user, label="Requirement")

    # Request-time re-validation against ACTIVE unavailability records (the modal
    # filter alone leaves a TOCTOU hole): excluded vendors are dropped from the
    # preview and reported visibly — never a silent drop.
    excluded = excluded_vendor_norms(db, requirements)
    unavailable_vendors, vendor_names = _partition_by_unavailability(vendor_names, excluded)

    # LOCKSTEP with send-inquiry: one [ref:{id}] token per involved requisition,
    # ascending requisition id — exactly what send_batch_rfq will append.
    requisition_ids = sorted({r.requisition_id for r in requirements})

    # Batch-fetch vendor cards + contacts (same logic as send-inquiry)
    normalized_names = [normalize_vendor_name(vn) for vn in vendor_names]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    # Order worst-first so the dict's last-wins keeps the BEST contact per vendor: a
    # vendor with multiple contacts (e.g. an rfq_manual row added via the composer plus
    # an enriched row) must not pick a NULL-email contact over one that has the
    # real email — that would silently skip the vendor as "had no email".
    contacts = _best_contacts_by_card(db, card_ids)
    contact_map = {c.vendor_card_id: c for c in contacts}

    from ...email_service import _build_html_body

    avail_token = " ".join(f"[ref:{rid}]" for rid in requisition_ids)
    parts_list = [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements]

    # Per-requisition grouped parts for the preview body — when the basket spans more
    # than one requisition the preview shows a REQ-{id} subhead per group so the buyer
    # can see which parts belong to which requisition. requisition_ids is already sorted
    # ascending (lockstep with the subject tokens). The flat parts_list stays for the
    # single-requisition case (is_cross_req False).
    requisition_parts_grouped: list[dict] = []
    for rid in requisition_ids:
        req_parts = [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements if r.requisition_id == rid]
        if req_parts:
            requisition_parts_grouped.append({"req_id": rid, "parts": req_parts})
    is_cross_req = len(requisition_ids) > 1

    # Advisory DNC set — look up which resolved emails are flagged do_not_contact.
    # This mirrors the send-time check in email_service.py so preview ≈ what sends.
    preview_dnc_emails: set[str] = _dnc_emails_for_cards(db, card_ids) if card_ids else set()

    previews = []
    for vn in vendor_names:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
            if contact and contact.email:
                vendor_email = contact.email

        # Compute advisory skip reason for the badge in the preview template.
        if not vendor_email:
            skip_reason = SightingsSkipReason.NO_EMAIL
        elif vendor_email.lower() in preview_dnc_emails:
            skip_reason = SightingsSkipReason.DO_NOT_CONTACT
        else:
            skip_reason = SightingsSkipReason.READY

        raw_subject = f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}"
        tagged_subject = f"{raw_subject} {avail_token}" if avail_token else raw_subject
        html_body = _build_html_body(email_body)

        previews.append(
            {
                "vendor_name": vn,
                "vendor_email": vendor_email,
                "subject": tagged_subject,
                "html_body": html_body,
                "parts": parts_list,
                "skip_reason": skip_reason,
                "normalized_name": normalize_vendor_name(vn),
            }
        )

    # Resolve selected datasheet names for the preview attachment list — apply the same
    # ~3 MB combined cap + largest-first drop used at send time so preview == what sends.
    datasheet_ids_raw = form.getlist("datasheet_ids")
    selected_ds_ids = [int(x) for x in datasheet_ids_raw if x.isdigit()]
    preview_attachments: list[dict] = []
    if selected_ds_ids:
        mc_ids = [r.material_card_id for r in requirements if r.material_card_id]
        if mc_ids:
            ds_rows = (
                db.query(MaterialCardDatasheet)
                .filter(
                    MaterialCardDatasheet.id.in_(selected_ds_ids),
                    MaterialCardDatasheet.material_card_id.in_(mc_ids),
                )
                .all()
            )
            # Build (id, file_name, size_bytes) tuples — use size_bytes when available,
            # fall back to 0 so un-sized datasheets are always kept (conservative).
            names_with_sizes = [(ds.id, ds.file_name, ds.size_bytes or 0) for ds in ds_rows]
            kept, _dropped = trim_datasheet_names_to_cap(names_with_sizes)
            preview_attachments = [{"id": ds_id, "file_name": fname} for ds_id, fname in kept]

    ctx = {
        "request": request,
        "previews": previews,
        "requirement_ids": requirement_ids,
        "vendor_names": vendor_names,
        "email_body": email_body,
        "unavailable_vendors": unavailable_vendors,
        "preview_attachments": preview_attachments,
        "requisition_parts_grouped": requisition_parts_grouped,
        "is_cross_req": is_cross_req,
    }
    return template_response("htmx/partials/sightings/preview_inquiry.html", ctx)


@router.post("/v2/partials/sightings/send-inquiry", response_class=HTMLResponse)
async def sightings_send_inquiry(
    request: Request,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_access(AccessKey.SEND_RFQ)),
    token: str = Depends(require_fresh_token),
):
    """Send batch RFQ to selected vendors for selected requirements.

    Uses require_fresh_token to get a valid Graph API token for email sending.
    """
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.isdigit()]
    vendor_names = form.getlist("vendor_names")
    email_body = form.get("email_body", "")

    if not requirement_ids or not vendor_names or not email_body:
        raise HTTPException(
            status_code=400,
            detail="requirement_ids, vendor_names, and email_body required",
        )

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    if not requirements:
        # Every posted requirement id is stale (rows deleted under an open modal).
        # Without this guard the send would proceed with NO requisition at all —
        # emails out, zero Contact tracking — instead of telling the user.
        raise HTTPException(status_code=400, detail="selected requirements no longer exist — refresh and retry")

    # IDOR guard: a restricted (SALES/TRADER) user may only send RFQs for parts on
    # requisitions they own. Enforce over the whole basket in one query — any
    # non-owned requisition 404s the whole send rather than emailing on its behalf.
    require_requisition_access_bulk(db, {r.requisition_id for r in requirements}, user)

    # Send-time re-validation (closes the TOCTOU the modal filter alone leaves open):
    # vendors with an ACTIVE unavailability record on the selected parts are dropped
    # from the send and reported visibly below — never a silent drop.
    excluded = excluded_vendor_norms(db, requirements)
    unavailable_vendors, sendable_vendors = _partition_by_unavailability(vendor_names, excluded)

    # Per-requisition parts map: NO collapse to one arbitrary requisition. Each
    # involved requisition gets its own Contact rows (scoped to its parts) and
    # its own [ref:{id}] subject token inside send_batch_rfq.
    requisition_parts_map: dict[int, list] = {}
    for r in requirements:
        requisition_parts_map.setdefault(r.requisition_id, []).append({"mpn": r.primary_mpn, "qty": r.target_qty})

    # Batch-fetch vendor cards + contacts in two queries instead of N+1
    normalized_names = [normalize_vendor_name(vn) for vn in sendable_vendors]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    # Best-contact-per-vendor (see _best_contacts_by_card): last-wins dict over a
    # worst-first ordering so a non-NULL email always beats a NULL-email row.
    contacts = _best_contacts_by_card(db, card_ids)
    contact_map = {c.vendor_card_id: c for c in contacts}

    vendor_groups = []
    for vn in sendable_vendors:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
            if contact and contact.email:
                vendor_email = contact.email

        vendor_groups.append(
            {
                "vendor_name": vn,
                "vendor_email": vendor_email,
                "parts": [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements],
                "subject": f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}",
                "body": email_body,
            }
        )

    # Collect datasheet attachments in their own guard: a fetch error DEGRADES to
    # send-without-attachments (never a 500). The buyer opts in per send via
    # datasheet_ids posted from the compose form.
    attachments = None
    dropped_datasheet_count = 0
    datasheet_ids_raw = form.getlist("datasheet_ids")
    selected_ds_ids = [int(x) for x in datasheet_ids_raw if x.isdigit()]
    if selected_ds_ids:
        material_card_ids = [r.material_card_id for r in requirements if r.material_card_id]
        try:
            from ...services.rfq_attachments import collect_rfq_attachments

            attachments, ds_statuses = await collect_rfq_attachments(
                db=db,
                material_card_ids=material_card_ids,
                selected_ids=selected_ds_ids,
            )
            dropped_datasheet_count = sum(1 for s in ds_statuses if s["status"] != RfqAttachmentStatus.ATTACHED)
            if dropped_datasheet_count:
                logger.warning(
                    "RFQ datasheet attachment: {} datasheet(s) dropped (oversized)",
                    dropped_datasheet_count,
                )
        except Exception:
            logger.warning("RFQ datasheet attachment collection failed — sending without attachments", exc_info=True)
            attachments = None
            dropped_datasheet_count = len(selected_ds_ids)

    sent_count = 0
    progressed_count = 0
    failed_vendors: list[str] = []
    no_email_vendors: list[str] = []
    try:
        if vendor_groups:
            from ...email_service import send_batch_rfq

            results = await send_batch_rfq(
                token=token,
                db=db,
                user_id=user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=requisition_parts_map,
                attachments=attachments,
            )
        else:
            results = []  # every requested vendor was dropped by the unavailability re-check
        # send_batch_rfq returns one record per requested vendor tagged "sent" / "failed"
        # / "skipped" (no contact email). A vendor is delivered only when status=="sent"
        # — len(results) would over-count, and a "skipped" vendor is not a delivery
        # failure (the user just needs to add an email), so surface the three distinctly.
        sent_count = sum(1 for r in results if r.get("status") == "sent")
        sent_vendors = {r.get("vendor_name") for r in results if r.get("status") == "sent"}
        failed_vendors = [r.get("vendor_name", "") for r in results if r.get("status") == "failed"]
        no_email_vendors = [r.get("vendor_name", "") for r in results if r.get("status") == "skipped"]

        # Log "RFQ sent" activity only for vendors actually reached.
        for r in requirements:
            for vn in sendable_vendors:
                if vn in sent_vendors:
                    log_rfq_activity(
                        db=db,
                        rfq_id=r.requisition_id,
                        activity_type="rfq_sent",
                        description=f"RFQ sent to {vn}",
                        user_id=user.id,
                        requirement_id=r.id,
                    )

        # Auto-progress sourcing status OPEN → SOURCING only once at least one RFQ went out.
        if sent_vendors:
            from ...services.sourcing_auto_progress import auto_progress_status

            for r in requirements:
                if auto_progress_status(r, SourcingStatus.SOURCING, db, user.id):
                    progressed_count += 1
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        # A mid-batch crash can leave partial Contact/ActivityLog rows pending on
        # the session — roll them back BEFORE the commit below, or the commit
        # would persist tracking for sends in an unknown state.
        db.rollback()
        failed_vendors = list(sendable_vendors)
        sent_count = 0

    db.commit()

    # Notify SSE listeners for each affected requirement
    for r in requirements:
        await _publish_if_user_source(source, user.id, r.id)

    total = len(vendor_names)
    if sent_count >= total:
        msg = f"RFQ sent to {sent_count} vendor{'s' if sent_count != 1 else ''}."
        if progressed_count:
            msg += f" {progressed_count} requirement{'s' if progressed_count != 1 else ''} advanced to sourcing."
        level = "success"
    else:
        bits = [f"Sent to {sent_count}/{total} vendors."]
        if failed_vendors:
            bits.append(f"Failed: {', '.join(v for v in failed_vendors if v)}.")
        if no_email_vendors:
            bits.append(f"No email on file: {', '.join(v for v in no_email_vendors if v)}.")
        if unavailable_vendors:
            bits.append(f"Skipped (marked unavailable): {', '.join(unavailable_vendors)}.")
        msg = " ".join(bits)
        level = "warning"

    # Machine-readable result so the browser caller (rfqVendorModal.confirmSend) can
    # report the TRUE outcome: this route intentionally returns 200 even on a partial /
    # total failure (failures are captured above, not raised), so the client must not
    # infer success from the HTTP status. X-RFQ-Skipped counts no-email vendors so the
    # client can distinguish "had no email" from "send failed".
    resp = _oob_toast(msg, level)
    resp.headers[RFQ_SENT_HEADER] = str(sent_count)
    resp.headers[RFQ_TOTAL_HEADER] = str(total)
    resp.headers[RFQ_SKIPPED_HEADER] = str(len(no_email_vendors))
    resp.headers[RFQ_UNAVAILABLE_HEADER] = str(len(unavailable_vendors))
    resp.headers[RFQ_DATASHEETS_DROPPED_HEADER] = str(dropped_datasheet_count)
    return resp
