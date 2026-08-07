"""Offers tab — offer form, create, edit-form, update.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from datetime import date, datetime
from decimal import Decimal

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ...constants import (
    OfferCondition,
)
from ...database import get_db
from ...dependencies import (
    require_buyer,
    require_requisition_access,
    require_user,
)
from ...models import User
from ...models.offers import Offer
from ...models.sourcing import Requirement
from ...services.offer_qualification import prefill_from_vendor
from ...template_env import template_response
from ...utils import safe_float, safe_int
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


def _parse_iso_date(v: str | None) -> date | None:
    """Parse an optional ISO-date form field ('' or unparseable → None)."""
    s = (v or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _qual_dict(
    usage: str,
    refurbished_by: str,
    refurb_process: str,
    cert_doc: str,
    part_condition: str,
    provenance_story: str,
    terms: str,
    lead_time_reason: str,
) -> "dict | None":
    """Build the qualification JSON blob from submitted form values.

    Returns None when all fields are blank (no qualification data to store).
    """
    q = {
        "usage": usage or None,
        "refurbished_by": refurbished_by or None,
        "refurb_process": refurb_process or None,
        "cert_doc": cert_doc or None,
        "part_condition": part_condition or None,
        "provenance_story": provenance_story or None,
        "terms": terms or None,
        "lead_time_reason": lead_time_reason or None,
        "requests": [],
        "schema": 1,  # forward-version the qualification blob (spec §3.1)
    }
    _qual_keys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    return q if any(q[k] for k in _qual_keys) else None


def _echo_prefill(
    vendor_name: str,
    mpn: str,
    manufacturer: str,
    qty_available: str,
    unit_price: str,
    lead_time: str,
    date_code: str,
    condition: str,
    packaging: str,
    firmware: str,
    hardware_code: str,
    moq: str,
    spq: str,
    warranty: str,
    country_of_origin: str,
    valid_until: str,
    notes: str,
    usage: str,
    refurbished_by: str,
    refurb_process: str,
    cert_doc: str,
    part_condition: str,
    provenance_story: str,
    terms: str,
    lead_time_reason: str,
) -> dict:
    """Re-build a prefill dict from submitted form values so the modal preserves what
    the buyer typed on a validation error re-render.

    Keys match the input name= attributes in _offer_form_fields.html.
    """
    return {
        "vendor_name": vendor_name,
        "mpn": mpn,
        "manufacturer": manufacturer,
        "qty_available": qty_available,
        "unit_price": unit_price,
        "lead_time": lead_time,
        "date_code": date_code,
        "condition": condition,
        "packaging": packaging,
        "firmware": firmware,
        "hardware_code": hardware_code,
        "moq": moq,
        "spq": spq,
        "warranty": warranty,
        "country_of_origin": country_of_origin,
        "valid_until": valid_until,
        "notes": notes,
        "usage": usage,
        "refurbished_by": refurbished_by,
        "refurb_process": refurb_process,
        "cert_doc": cert_doc,
        "part_condition": part_condition,
        "provenance_story": provenance_story,
        "terms": terms,
        "lead_time_reason": lead_time_reason,
    }


@router.get("/v2/partials/sightings/{requirement_id}/offer-form", response_class=HTMLResponse)
async def sightings_offer_form(
    request: Request,
    requirement_id: int,
    vendor_name: str = Query(""),
    unit_price: str = Query(""),
    qty: str = Query(""),
    moq: str = Query(""),
    lead_days: str = Query(""),
    manufacturer: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Modal offer form — pre-filled from a sighting (Convert) or blank (Enter)."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    prefill = None
    if vendor_name:
        prefill = {
            "vendor_name": vendor_name,
            "mpn": requirement.primary_mpn,
            "manufacturer": manufacturer or requirement.manufacturer or "",
            "unit_price": unit_price,
            "qty_available": qty,
            "moq": moq,
            "lead_time": f"{lead_days} days" if lead_days else "",
        }
        remembered = prefill_from_vendor(db, normalize_vendor_name(vendor_name))
        for k, v in remembered.items():
            prefill.setdefault(k, v)  # only fill empty keys; buyer overrides
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": None}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/offers", response_class=HTMLResponse)
async def sightings_create_offer(
    request: Request,
    requirement_id: int,
    vendor_name: str = Form(...),
    mpn: str = Form(...),
    manufacturer: str = Form(""),
    qty_available: str = Form(""),
    unit_price: str = Form(""),
    lead_time: str = Form(""),
    date_code: str = Form(""),
    condition: str = Form(OfferCondition.NEW),
    packaging: str = Form(""),
    firmware: str = Form(""),
    hardware_code: str = Form(""),
    moq: str = Form(""),
    spq: str = Form(""),
    warranty: str = Form(""),
    country_of_origin: str = Form(""),
    valid_until: str = Form(""),
    notes: str = Form(""),
    usage: str = Form(""),
    refurbished_by: str = Form(""),
    refurb_process: str = Form(""),
    cert_doc: str = Form(""),
    part_condition: str = Form(""),
    provenance_story: str = Form(""),
    terms: str = Form(""),
    lead_time_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Create an offer for this part via the canonical offer_service.create_offer, then
    re-render the offers panel.

    Reused for both Convert-to-offer and Enter-offer.
    """
    from ...schemas.crm import OfferCreate
    from ...services.offer_qualification import essentials_data, normalize_offer_condition, validate_essentials
    from ...services.offer_service import create_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)

    # Build (and structurally validate) the payload FIRST so a bad numeric/date is
    # reported as a 422 (not masked by the essentials gate below or crashed as a 500).
    try:
        payload = OfferCreate(
            mpn=mpn,
            vendor_name=vendor_name,
            requirement_id=requirement_id,
            manufacturer=manufacturer or None,
            qty_available=safe_int(qty_available),
            unit_price=safe_float(unit_price),
            lead_time=lead_time or None,
            date_code=date_code or None,
            condition=condition or OfferCondition.NEW,
            packaging=packaging or None,
            firmware=firmware or None,
            hardware_code=hardware_code or None,
            moq=safe_int(moq),
            spq=safe_int(spq),
            warranty=warranty or None,
            country_of_origin=country_of_origin or None,
            valid_until=_parse_iso_date(valid_until),
            notes=notes or None,
            source="manual",
            qualification=_qual_dict(
                usage,
                refurbished_by,
                refurb_process,
                cert_doc,
                part_condition,
                provenance_story,
                terms,
                lead_time_reason,
            ),
        )
    except ValidationError as e:
        # Surface as a 422 (not a 500) so a bad numeric/date is reported, not crashed.
        raise RequestValidationError(e.errors()) from e

    # Gate: validate the buyer's submitted essentials BEFORE delegating to the
    # canonical builder (which no longer blocks). On a missing essential, re-render the
    # modal with inline errors and do not persist. Uses the schema-normalized condition.
    gate_errors = validate_essentials(
        normalize_offer_condition(payload.condition) or OfferCondition.NEW,
        essentials_data(
            manufacturer=manufacturer,
            packaging=packaging,
            usage=usage,
            refurbished_by=refurbished_by,
            refurb_process=refurb_process,
            cert_doc=cert_doc,
            part_condition=part_condition,
        ),
    )
    if gate_errors:
        prefill = _echo_prefill(
            vendor_name,
            mpn,
            manufacturer,
            qty_available,
            unit_price,
            lead_time,
            date_code,
            condition,
            packaging,
            firmware,
            hardware_code,
            moq,
            spq,
            warranty,
            country_of_origin,
            valid_until,
            notes,
            usage,
            refurbished_by,
            refurb_process,
            cert_doc,
            part_condition,
            provenance_story,
            terms,
            lead_time_reason,
        )
        ctx = {
            "request": request,
            "requirement": requirement,
            "offer": None,
            "prefill": prefill,
            "errors": gate_errors,
        }
        return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)

    # The canonical create_offer fires the offer-hook release itself
    # (maybe_release_on_offer) — no route-level call needed here. Essentials were already
    # gated above, so create_offer no longer rejects on qualification grounds.
    await create_offer(db, requirement.requisition_id, payload, user)
    db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), "Offer saved")


@router.get("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def sightings_offer_edit_form(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Modal offer form pre-filled from an existing offer (edit mode)."""
    requirement = db.get(Requirement, requirement_id)
    offer = db.get(Offer, offer_id)
    if not requirement or not offer:
        raise HTTPException(404, "Not found")
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer belongs to another requirement.
    if offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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

    def _json_safe(v: object) -> object:
        """Coerce DB field values to JSON-serializable types for |tojson in Alpine
        x-data."""
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    prefill = {f: _json_safe(getattr(offer, f)) for f in fields}
    # Repopulate the qualification chips/inputs from the stored JSON so the Alpine panel
    # reflects current state on edit (otherwise the chips render empty and a re-save would
    # appear to clear them). Keys match the offerQualification x-data names.
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
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": offer}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_update_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    vendor_name: str = Form(""),
    mpn: str = Form(""),
    manufacturer: str = Form(""),
    qty_available: str = Form(""),
    unit_price: str = Form(""),
    lead_time: str = Form(""),
    date_code: str = Form(""),
    condition: str = Form(""),
    packaging: str = Form(""),
    firmware: str = Form(""),
    hardware_code: str = Form(""),
    moq: str = Form(""),
    spq: str = Form(""),
    warranty: str = Form(""),
    country_of_origin: str = Form(""),
    valid_until: str = Form(""),
    notes: str = Form(""),
    usage: str = Form(""),
    refurbished_by: str = Form(""),
    refurb_process: str = Form(""),
    cert_doc: str = Form(""),
    part_condition: str = Form(""),
    provenance_story: str = Form(""),
    terms: str = Form(""),
    lead_time_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Update an offer via the canonical offer_service.update_offer, then re-render the
    panel."""
    from ...schemas.crm import OfferUpdate
    from ...services.offer_qualification import essentials_data, normalize_offer_condition, validate_essentials
    from ...services.offer_service import update_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)

    # Load the offer FIRST and scope it to the path requirement (prevents cross-requirement
    # IDOR via a guessed offer_id; 404 if missing or owned by another requirement).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})

    # MERGE-not-overwrite: start from the stored qualification JSON and overlay only the
    # SUBMITTED non-empty qual fields. This preserves the logged #7 `requests` array and any
    # optional keys (provenance_story / terms / lead_time_reason) that aren't on this form
    # submission — the previous always-rebuild path wiped them on every edit.
    merged_qual = dict(offer.qualification or {})
    _submitted_qual = {
        "usage": usage or None,
        "refurbished_by": refurbished_by or None,
        "refurb_process": refurb_process or None,
        "cert_doc": cert_doc or None,
        "part_condition": part_condition or None,
        "provenance_story": provenance_story or None,
        "terms": terms or None,
        "lead_time_reason": lead_time_reason or None,
    }
    for _k, _v in _submitted_qual.items():
        if _v:
            merged_qual[_k] = _v
    # Always preserve the existing requests list (never reset it to []); copy it last so it
    # can't be clobbered by anything overlaid above.
    merged_qual["requests"] = list((offer.qualification or {}).get("requests") or [])
    # Forward-version the blob (spec §3.1).
    merged_qual["schema"] = 1
    # If nothing meaningful is stored (only the structural keys), persist None.
    _qual_value_keys = (*_submitted_qual.keys(),)
    qualification_to_store = (
        merged_qual if (any(merged_qual.get(k) for k in _qual_value_keys) or merged_qual["requests"]) else None
    )

    try:
        payload = OfferUpdate(
            vendor_name=vendor_name or None,
            mpn=mpn or None,
            manufacturer=manufacturer or None,
            qty_available=safe_int(qty_available),
            unit_price=safe_float(unit_price),
            lead_time=lead_time or None,
            date_code=date_code or None,
            condition=condition or None,
            packaging=packaging or None,
            firmware=firmware or None,
            hardware_code=hardware_code or None,
            moq=safe_int(moq),
            spq=safe_int(spq),
            warranty=warranty or None,
            country_of_origin=country_of_origin or None,
            valid_until=_parse_iso_date(valid_until),
            notes=notes or None,
            qualification=qualification_to_store,
        )
    except ValidationError as e:
        raise RequestValidationError(e.errors()) from e

    # Gate: validate the MERGED essentials BEFORE delegating to the canonical builder
    # (payload already structurally validated above, so a bad numeric is a 422 either way).
    # Using merged data means editing an unrelated field on a pulls/refurb offer whose stored
    # usage/process is intact is NOT falsely blocked. On a missing essential, re-render the
    # modal with inline errors and do not persist.
    norm_condition = normalize_offer_condition(payload.condition)
    if norm_condition:
        gate_errors = validate_essentials(
            norm_condition,
            essentials_data(
                manufacturer=manufacturer,
                packaging=packaging,
                usage=merged_qual.get("usage"),
                refurbished_by=merged_qual.get("refurbished_by"),
                refurb_process=merged_qual.get("refurb_process"),
                cert_doc=merged_qual.get("cert_doc"),
                part_condition=merged_qual.get("part_condition"),
            ),
        )
        if gate_errors:
            prefill = _echo_prefill(
                vendor_name,
                mpn,
                manufacturer,
                qty_available,
                unit_price,
                lead_time,
                date_code,
                condition,
                packaging,
                firmware,
                hardware_code,
                moq,
                spq,
                warranty,
                country_of_origin,
                valid_until,
                notes,
                usage,
                refurbished_by,
                refurb_process,
                cert_doc,
                part_condition,
                provenance_story,
                terms,
                lead_time_reason,
            )
            ctx = {
                "request": request,
                "requirement": requirement,
                "offer": offer,
                "prefill": prefill,
                "errors": gate_errors,
            }
            return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)

    update_offer(db, offer, payload, user)
    db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), "Offer updated")
