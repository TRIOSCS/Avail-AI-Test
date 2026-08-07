"""Vendor CRUD — create/dup-check/delete, detail shell + tabs, edit, blacklist, archive.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import html as html_mod
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_admin, require_user
from ....models import Offer, Sighting, SourcingLead, User, VendorCard
from ....models.vendors import VendorContact
from ....services.crm_service import cadence_state as _cadence_state
from ....services.crm_service import next_best_touch as _next_best_touch
from ....services.vendor_duplicates import check_vendor_duplicate
from ....template_env import template_response
from ....utils.column_limits import ensure_fits_column
from ..._lookup_helpers import get_vendor_card_or_404
from .._shared import _base_ctx, _sanitize_hx_params
from .._shared_tabs import vendor_tab as _vendor_tab_impl
from .common import router
from .listing import vendors_list_partial


@router.get("/v2/partials/vendors/create-form", response_class=HTMLResponse)
async def vendor_create_form_early(
    request: Request,
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create-vendor form partial (early route to precede /{vendor_id}).

    Threads the sanitized embed-context params (hx_target/push_url_base — same allowlist
    as the vendor list) into the form so its submit + Cancel targets work inside the CRM
    shell as well as standalone (Wave 3 item 8a).
    """
    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")
    return template_response(
        "htmx/partials/vendors/create_form.html",
        {"request": request, "hx_target": hx_target, "push_url_base": push_url_base},
    )


@router.get("/v2/partials/vendors/check-duplicate", response_class=HTMLResponse)
async def vendor_check_duplicate_partial(
    display_name: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Inline duplicate warning for the vendor create form (early route to precede
    /{vendor_id}).

    Mirrors the company create dup-check (GET /v2/partials/customers/check-duplicate):
    the create form's name input hx-gets this route with its own field name
    (``display_name``) and the returned warning HTML swaps into ``#dup-warning``.
    Returns an empty 200 when the name is blank or has no match. Exact + fuzzy
    semantics come from services.vendor_duplicates.check_vendor_duplicate (exact
    normalized-name match short-circuits at score 100; fuzzy suggestions >= 80).
    """
    if not display_name.strip():
        return HTMLResponse("")
    matches = check_vendor_duplicate(display_name.strip(), db)
    if not matches:
        return HTMLResponse("")
    top = matches[0]
    name_esc = html_mod.escape(top["name"] or "")
    if top["match"] == "exact":
        msg = f'A vendor named "{name_esc}" already exists (ID {top["id"]}).'
    else:
        msg = f'Possible duplicate: "{name_esc}" ({top["score"]}% name match).'
    return HTMLResponse(f'<p class="text-sm text-amber-600">{msg}</p>')


@router.post("/v2/partials/vendors/create", response_class=HTMLResponse)
async def create_vendor_partial_early(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new VendorCard from the HTMX form (early route to precede /{vendor_id}).

    The create form carries the sanitized embed-context params as hidden inputs
    (hx_target/push_url_base — Wave 3 item 8a); they're re-sanitized here and threaded
    into the returned detail so its controls keep targeting the embed container instead
    of falling back to #main-content one step after create.
    """
    from ....models import VendorCard
    from ....utils.vendor_helpers import find_vendor_card_by_name
    from ....vendor_utils import normalize_vendor_name

    form = await request.form()
    hx_target, push_url_base = _sanitize_hx_params(
        str(form.get("hx_target") or "#main-content"),
        str(form.get("push_url_base") or "/v2/vendors"),
        "/v2/vendors",
    )
    display_name = form.get("display_name", "").strip()
    if not display_name:
        raise HTTPException(400, "Vendor name is required")

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    ensure_fits_column(VendorCard, "display_name", display_name, "Vendor name")
    website_val = form.get("website", "").strip() or None
    ensure_fits_column(VendorCard, "website", website_val, "Website")

    norm = normalize_vendor_name(display_name)
    existing = find_vendor_card_by_name(display_name, db)
    if existing:
        raise HTTPException(409, f"Vendor '{existing.display_name}' already exists (ID {existing.id})")

    emails_raw = form.get("emails", "").strip()
    emails = [e.strip() for e in emails_raw.split(",") if e.strip() and "@" in e] if emails_raw else []
    phones_raw = form.get("phones", "").strip()
    phones = [p.strip() for p in phones_raw.split(",") if p.strip()] if phones_raw else []

    card = VendorCard(
        normalized_name=norm,
        display_name=display_name,
        website=website_val,
        emails=emails,
        phones=phones,
        industry=form.get("industry", "").strip() or None,
        hq_city=form.get("hq_city", "").strip() or None,
        hq_country=form.get("hq_country", "").strip() or None,
        employee_size=form.get("employee_size", "").strip() or None,
        source="manual",
        is_blacklisted=False,
        is_new_vendor=True,
        sighting_count=0,
    )
    # Remaining bounded columns in this write — same guard as name/website above.
    for _field, _label in (
        ("industry", "Industry"),
        ("hq_city", "HQ city"),
        ("hq_country", "HQ country"),
        ("employee_size", "Employee size"),
    ):
        ensure_fits_column(VendorCard, _field, getattr(card, _field), _label)
    db.add(card)
    db.commit()
    db.refresh(card)
    logger.info("VendorCard {} created by {}", card.id, user.email)
    return await vendor_detail_partial(
        request=request,
        vendor_id=card.id,
        user=user,
        db=db,
        hx_target=hx_target,
        push_url_base=push_url_base,
    )


@router.delete("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def delete_vendor_partial(
    request: Request,
    vendor_id: int,
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor (admin-only) and return the refreshed vendor list.

    Carries the sanitized embed-context params through to the re-rendered list so an
    embedded delete keeps every list control targeting the embed container (Wave 3 item
    8a) instead of retargeting the main shell.
    """
    from ....models import VendorCard

    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")
    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    active_offers = db.query(Offer).filter(Offer.vendor_card_id == card.id).count()
    if active_offers > 0:
        raise HTTPException(
            400,
            f"Cannot delete vendor with {active_offers} active offers. Archive instead.",
        )
    db.delete(card)
    db.commit()
    logger.info("VendorCard {} deleted by {}", vendor_id, user.email)
    # Return the vendor list using safe defaults (embed params threaded through)
    return await vendors_list_partial(
        request=request,
        q="",
        hide_blacklisted=True,
        include_archived=False,
        sort="sighting_count",
        dir="desc",
        my_only=False,
        limit=30,
        offset=0,
        hx_target=hx_target,
        push_url_base=push_url_base,
        user=user,
        db=db,
    )


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_detail_partial(
    request: Request,
    vendor_id: int,
    mpn: str = "",
    # Plain-string defaults (NOT Query objects): sibling handlers call this
    # function directly, and a Query sentinel default would leak through as the
    # value there. FastAPI still infers both as query params on the route.
    hx_target: str = "#main-content",
    push_url_base: str = "/v2/vendors",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor detail as HTML partial with safety data and tabs.

    hx_target/push_url_base (sanitized, Wave 3 item 8a) thread the embed context into
    the template's Delete button URL so an embedded delete re-renders the list inside
    the embed container; safe defaults apply standalone.
    """
    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")
    vendor = get_vendor_card_or_404(db, vendor_id)

    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor_id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(20)
        .all()
    )

    sightings_query = db.query(Sighting).filter(Sighting.vendor_name_normalized == vendor.normalized_name)
    if mpn.strip():
        from app.utils.normalization import normalize_mpn

        norm = normalize_mpn(mpn)
        if norm:
            sightings_query = sightings_query.filter(Sighting.normalized_mpn == norm)

    recent_sightings = sightings_query.order_by(Sighting.created_at.desc().nullslast()).limit(10).all()

    # Load safety data from most recent SourcingLead
    safety_band = None
    safety_summary = None
    safety_flags = None
    lead = (
        db.query(SourcingLead)
        .filter(SourcingLead.vendor_name_normalized == vendor.normalized_name)
        .order_by(SourcingLead.created_at.desc())
        .first()
    )
    if lead:
        safety_band = lead.vendor_safety_band
        safety_summary = lead.vendor_safety_summary
        safety_flags = lead.vendor_safety_flags

    now_utc = datetime.now(UTC)
    vendor_cadence = _cadence_state(None, vendor.last_outbound_at, now_utc)
    vendor_nbt = _next_best_touch(None, vendor.last_outbound_at, now_utc)

    # Score hover (idea C): deterministic driver breakdown behind the header score,
    # threaded from the SAME inputs the score uses. Only computed when a score shows.
    from ....services.vendor_score import compute_single_vendor_score_breakdown

    vendor_score_breakdown = compute_single_vendor_score_breakdown(db, vendor_id) if vendor.vendor_score else []

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendor": vendor,
            "vendor_score_breakdown": vendor_score_breakdown,
            "contacts": contacts,
            "recent_sightings": recent_sightings,
            "safety_band": safety_band,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_score": None,
            "safety_available": False,
            "mpn_filter": mpn.strip().upper() if mpn.strip() else None,
            "cadence_state": vendor_cadence,
            "next_best_touch": vendor_nbt,
            "now_utc": now_utc,
            "hx_target": hx_target,
            "push_url_base": push_url_base,
        }
    )
    return template_response("htmx/partials/vendors/detail.html", ctx)


# Implementation lives in ._shared_tabs (P4.1 — archive.py reused this tab render by
# importing it straight off this sibling router module; it's now a shared home both
# import from). Registered here, unchanged, so the route/URL/tag and the `vendor_tab`
# name importable off this module are exactly as before.
vendor_tab = router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)(_vendor_tab_impl)


# ── Sprint 3: Vendor CRUD + Contact Management ────────────────────────


@router.get("/v2/partials/vendors/{vendor_id}/edit-form", response_class=HTMLResponse)
async def vendor_edit_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for vendor header fields."""
    vendor = get_vendor_card_or_404(db, vendor_id)
    return template_response(
        "htmx/partials/vendors/edit_vendor_form.html",
        {"request": request, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/edit", response_class=HTMLResponse)
async def edit_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save vendor edits and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    form = await request.form()
    # VendorCard.display_name is NOT NULL. The edit form always submits it (required
    # input), so a submitted-but-blank value means the user cleared a required field —
    # reject it. A field that is ABSENT entirely is a partial edit (website/emails-only),
    # which must leave the existing name untouched.
    display_name_raw = form.get("display_name")
    if display_name_raw is not None:
        display_name = display_name_raw.strip()
        if not display_name:
            raise HTTPException(400, "Vendor name is required.")
        ensure_fits_column(VendorCard, "display_name", display_name, "Vendor name")
        vendor.display_name = display_name
        from ....vendor_utils import normalize_vendor_name

        vendor.normalized_name = normalize_vendor_name(display_name)

    # website/emails/phones follow the same present-vs-absent rule as display_name
    # (Wave 3 item 3): a field ABSENT from the form is a partial edit (preserved); a
    # SUBMITTED blank is an explicit clear — the edit form prefills all three, so blank
    # means the user cleared it.
    website_raw = form.get("website")
    if website_raw is not None:
        website = website_raw.strip() or None
        ensure_fits_column(VendorCard, "website", website, "Website")
        vendor.website = website

    emails_raw = form.get("emails")
    if emails_raw is not None:
        emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
        # Reject anything that isn't a plausible address — an entry without an
        # '@' is a data-entry mistake, not a contactable email.
        invalid = [e for e in emails if "@" not in e]
        if invalid:
            raise HTTPException(400, f"Invalid email address: {', '.join(invalid)}")
        vendor.emails = emails

    phones_raw = form.get("phones")
    if phones_raw is not None:
        vendor.phones = [p.strip() for p in phones_raw.split(",") if p.strip()]

    vendor.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Vendor {} edited by {}", vendor_id, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/toggle-blacklist", response_class=HTMLResponse)
async def toggle_vendor_blacklist(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle blacklist status and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    vendor.is_blacklisted = not vendor.is_blacklisted
    vendor.updated_at = datetime.now(UTC)
    db.commit()
    status = "blacklisted" if vendor.is_blacklisted else "un-blacklisted"
    logger.info("Vendor {} {} by {}", vendor_id, status, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/archive", response_class=HTMLResponse)
async def archive_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-archive a vendor — sets is_active=False; never deletes.

    Mirrors the customer/company archive (deactivate). Archived vendors drop out of the
    default vendor list/search; "Show archived" surfaces them again. require_user gate
    matches the vendor blacklist toggle (vendor data is not tenant-scoped).
    """
    vendor = get_vendor_card_or_404(db, vendor_id)
    vendor.is_active = False
    vendor.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Vendor {} archived by {}", vendor_id, user.email)
    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/unarchive", response_class=HTMLResponse)
async def unarchive_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Restore a soft-archived vendor — sets is_active=True.

    Mirrors company reactivate.
    """
    vendor = get_vendor_card_or_404(db, vendor_id)
    vendor.is_active = True
    vendor.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Vendor {} unarchived by {}", vendor_id, user.email)
    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)
