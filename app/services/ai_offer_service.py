"""ai_offer_service.py — AI offer and RFQ business logic extracted from routers/ai.py.

Handles: prospect contact promotion, saving AI-parsed offers (JSON API + the HTMX
form-array sibling), applying freeform RFQ templates, and saving freeform offers. All
functions take a db Session and return data — they do NOT commit.

Called by: routers/ai.py (save_parsed_offers, apply_freeform_rfq, save_freeform_offers),
    routers/htmx/offers.py (save_parsed_offers → parse_offer_form_rows +
    save_form_parsed_offers, P4.2)
Depends on: models (Offer, Requirement, Requisition, VendorCard, VendorContact,
            SiteContact, ProspectContact, CustomerSite, User),
            vendor_utils, search_service, utils/normalization,
            vendor_unavailability (offer-hook release on user-saved ACTIVE offers),
            offer_qualification (apply_qualification — form-parsed path only)
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, OfferCondition, OfferStatus
from ..models import (
    CustomerSite,
    Offer,
    ProspectContact,
    Requirement,
    Requisition,
    SiteContact,
    User,
    VendorCard,
    VendorContact,
)
from ..utils.normalization import fuzzy_mpn_match, normalize_mpn_key
from ..vendor_utils import normalize_vendor_name
from .activity_service import log_activity
from .vendor_unavailability import maybe_release_on_offer

# -- Prospect Contact Promotion -----------------------------------------------


def promote_prospect_contact(db: Session, contact_id: int, user_id: int) -> dict:
    """Promote a prospect contact to a VendorContact or SiteContact.

    Does NOT commit — caller must commit. Returns dict with promoted_to_type and
    promoted_to_id. Raises ValueError if contact not found or has no linked entity.
    """
    pc = db.query(ProspectContact).filter(ProspectContact.id == contact_id).first()
    if not pc:
        raise ValueError("Prospect contact not found")

    if pc.vendor_card_id:
        vc = _promote_to_vendor_contact(db, pc)
        pc.promoted_to_type = "vendor_contact"
        pc.promoted_to_id = vc.id
    elif pc.customer_site_id:
        sc = _promote_to_site_contact(db, pc)
        pc.promoted_to_type = "site_contact"
        pc.promoted_to_id = sc.id
    else:
        raise ValueError("Contact has no vendor_card_id or customer_site_id")

    pc.is_saved = True
    pc.saved_by_id = user_id

    logger.info(
        "Prospect contact promoted: id={} type={} target_id={}",
        contact_id,
        pc.promoted_to_type,
        pc.promoted_to_id,
    )

    return {
        "ok": True,
        "promoted_to_type": pc.promoted_to_type,
        "promoted_to_id": pc.promoted_to_id,
    }


def _promote_prospect_to_contact(
    db: Session,
    pc: ProspectContact,
    model_class: type,
    fk_field: str,
    fk_value: int,
    extra_fields: dict | None = None,
) -> VendorContact | SiteContact:
    """Generic helper: promote a prospect to VendorContact or SiteContact.

    Deduplicates by email within the FK scope. Backfills empty name/title/phone
    on existing records. Creates a new record if no duplicate found.

    Args:
        model_class: VendorContact or SiteContact.
        fk_field: Foreign key column name (e.g. "vendor_card_id").
        fk_value: Foreign key value to filter/set.
        extra_fields: Additional fields for creation (e.g. linkedin_url, source).
    """
    extra = extra_fields or {}

    # Dedupe by email within the FK scope
    existing = None
    if pc.email:
        existing = db.query(model_class).filter_by(**{fk_field: fk_value}, email=pc.email).first()
    if existing:
        if pc.full_name and not existing.full_name:
            existing.full_name = pc.full_name
        if pc.title and not existing.title:
            existing.title = pc.title
        if pc.phone and not existing.phone:
            existing.phone = pc.phone
        # Backfill any extra fields (e.g. linkedin_url) if present
        for attr, val in extra.items():
            if val and not getattr(existing, attr, None):
                setattr(existing, attr, val)
        return existing

    contact = model_class(
        **{fk_field: fk_value},
        full_name=pc.full_name,
        title=pc.title,
        email=pc.email,
        phone=pc.phone,
        **extra,
    )
    db.add(contact)
    db.flush()
    return contact


def _promote_to_vendor_contact(db: Session, pc: ProspectContact) -> VendorContact:
    """Promote prospect to VendorContact, deduping by email."""
    return _promote_prospect_to_contact(
        db,
        pc,
        VendorContact,
        "vendor_card_id",
        pc.vendor_card_id,
        extra_fields={"linkedin_url": pc.linkedin_url, "source": "prospect_promote"},
    )


def _promote_to_site_contact(db: Session, pc: ProspectContact) -> SiteContact:
    """Promote prospect to SiteContact, deduping by email."""
    return _promote_prospect_to_contact(
        db,
        pc,
        SiteContact,
        "customer_site_id",
        pc.customer_site_id,
    )


# -- Save AI-Parsed Offers ---------------------------------------------------


def save_parsed_offers(db: Session, requisition_id: int, response_id: int | None, offers: list, user_id: int) -> dict:
    """Save AI-parsed draft offers to the Offers table.

    Does NOT commit — caller must commit. Returns dict with created count and offer_ids.
    """
    from ..search_service import resolve_material_card

    reqs = db.query(Requirement).filter(Requirement.requisition_id == requisition_id).all()

    created = []
    for o in offers:
        req_id = _match_requirement(o.mpn, reqs) if o.mpn else None
        mat_card = resolve_material_card(o.mpn, db) if o.mpn else None

        offer = Offer(
            requisition_id=requisition_id,
            requirement_id=req_id,
            material_card_id=mat_card.id if mat_card else None,
            normalized_mpn=normalize_mpn_key(o.mpn) if o.mpn else None,
            vendor_name=o.vendor_name,
            vendor_name_normalized=normalize_vendor_name(o.vendor_name or ""),
            mpn=o.mpn,
            manufacturer=o.manufacturer,
            qty_available=o.qty_available,
            unit_price=o.unit_price,
            currency=o.currency,
            lead_time=o.lead_time,
            date_code=o.date_code,
            condition=o.condition,
            packaging=o.packaging,
            moq=o.moq,
            source="ai_parsed",
            vendor_response_id=response_id,
            entered_by_id=user_id,
            notes=o.notes,
            status=OfferStatus.PENDING_REVIEW,
        )
        db.add(offer)
        db.flush()
        _log_offer_created(db, offer, user_id)
        created.append(offer.id)

    logger.info(
        "Saved {} AI-parsed offers for requisition_id={} response_id={}",
        len(created),
        requisition_id,
        response_id,
    )
    return {"created": len(created), "offer_ids": created}


# -- Apply Freeform RFQ Template ----------------------------------------------


def apply_freeform_rfq(
    db: Session,
    name: str,
    customer_site_id: int,
    customer_name: str | None,
    deadline: str | None,
    requirements: list,
    user_id: int,
) -> dict:
    """Create requisition + requirements from edited freeform RFQ template.

    Does NOT commit — caller must commit. Returns dict with id, name,
    requirements_added. Raises ValueError if customer_site not found.
    """
    from ..schemas.requisitions import RequirementCreate
    from ..search_service import resolve_material_card

    site = db.query(CustomerSite).filter(CustomerSite.id == customer_site_id).first()
    if not site:
        raise ValueError("Customer site not found")

    resolved_name = customer_name or site.site_name or (site.company.name if site.company else None)

    req = Requisition(
        name=name.strip() or "Untitled",
        customer_site_id=customer_site_id,
        customer_name=resolved_name,
        deadline=deadline,
        created_by=user_id,
        status="draft",
    )
    db.add(req)
    db.flush()

    added = 0
    for item in requirements[:50]:
        try:
            parsed = RequirementCreate.model_validate(item)
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping invalid requirement item: {} — {}", item, exc)
            continue
        mat_card = resolve_material_card(parsed.primary_mpn, db) if parsed.primary_mpn else None
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=parsed.primary_mpn,
            normalized_mpn=normalize_mpn_key(parsed.primary_mpn),
            material_card_id=mat_card.id if mat_card else None,
            target_qty=parsed.target_qty,
            target_price=parsed.target_price,
            substitutes=parsed.substitutes[:20],
            condition=parsed.condition or "",
            date_codes=parsed.date_codes or "",
            firmware=parsed.firmware or "",
            hardware_codes=parsed.hardware_codes or "",
            packaging=parsed.packaging or "",
            notes=parsed.notes or "",
        )
        db.add(r)
        added += 1

    logger.info("Created freeform requisition id={} name='{}' with {} requirements", req.id, req.name, added)
    return {"id": req.id, "name": req.name, "requirements_added": added}


# -- Save Freeform Offers ----------------------------------------------------


def save_freeform_offers(
    db: Session,
    requisition_id: int,
    offers: list,
    user_id: int,
) -> dict:
    """Save freeform-parsed offers to a requisition.

    Does NOT commit — caller must commit. Returns dict with created count and offer_ids.
    """
    from ..search_service import resolve_material_card

    reqs = db.query(Requirement).filter(Requirement.requisition_id == requisition_id).all()
    user = db.get(User, user_id)

    created = []
    for o in offers:
        req_id = _match_requirement(o.mpn, reqs) if o.mpn else None
        mat_card = resolve_material_card(o.mpn, db) if o.mpn else None

        norm_name = normalize_vendor_name(o.vendor_name or "")
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
        if not card:
            card = VendorCard(
                normalized_name=norm_name,
                display_name=o.vendor_name or "Unknown",
                emails=[],
                phones=[],
            )
            db.add(card)
            db.flush()

        offer = Offer(
            requisition_id=requisition_id,
            requirement_id=req_id,
            material_card_id=mat_card.id if mat_card else None,
            normalized_mpn=normalize_mpn_key(o.mpn) if o.mpn else None,
            vendor_card_id=card.id,
            vendor_name=card.display_name,
            vendor_name_normalized=card.normalized_name,
            mpn=o.mpn,
            manufacturer=o.manufacturer,
            qty_available=o.qty_available,
            unit_price=o.unit_price,
            currency=o.currency or "USD",
            lead_time=o.lead_time,
            date_code=o.date_code,
            condition=o.condition or OfferCondition.NEW,
            packaging=o.packaging,
            moq=o.moq,
            source="freeform_parsed",
            entered_by_id=user_id,
            notes=o.notes,
            status=OfferStatus.ACTIVE,
        )
        db.add(offer)
        db.flush()
        # Offer hook: freeform offers are saved ACTIVE after user review — user-
        # initiated proof of availability, release matching active records.
        maybe_release_on_offer(db, req_id, offer.vendor_name, user, offer_condition=offer.condition)
        _log_offer_created(db, offer, user_id)
        created.append(offer.id)

    logger.info(
        "Saved {} freeform offers for requisition_id={}",
        len(created),
        requisition_id,
    )
    return {"created": len(created), "offer_ids": created}


# -- Save HTMX Form-Parsed Offers ---------------------------------------------
# The user-facing sibling of save_parsed_offers (JSON API, PENDING_REVIEW) above:
# the HTMX parse-results partial lets a buyer edit the AI-parsed offers in a form
# before saving, so these go straight to ACTIVE with qualification scoring applied
# instead of sitting in PENDING_REVIEW. Split in two so the router can short-circuit
# on "no rows at all" (parse_offer_form_rows) before doing any DB work.


def _safe_int(val) -> int | None:
    """Safely convert a form value to int, or None."""
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely convert a form value to float, or None."""
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_offer_form_rows(form, vendor_name: str) -> list[dict]:
    """Collect ``offers[i].*`` fields off an HTMX form into a list of offer dicts.

    Reads sequential ``offers[0].mpn``, ``offers[1].mpn``, ... (or ``.vendor_name`` for
    freeform rows with no mpn) until a gap is hit. *vendor_name* is the form's top-level
    fallback vendor name for rows that don't specify their own. Returns ``[]`` when the
    form has no offer rows at all — the router treats that as "nothing to save" without
    ever calling ``save_form_parsed_offers``.
    """
    offers_data: list[dict] = []
    idx = 0
    while True:
        mpn = form.get(f"offers[{idx}].mpn")
        if mpn is None:
            # Also check vendor_name field for freeform offers
            vn = form.get(f"offers[{idx}].vendor_name")
            if vn is None:
                break
        offers_data.append(
            {
                "vendor_name": form.get(f"offers[{idx}].vendor_name", vendor_name),
                "mpn": form.get(f"offers[{idx}].mpn", ""),
                "manufacturer": form.get(f"offers[{idx}].manufacturer"),
                "qty_available": _safe_int(form.get(f"offers[{idx}].qty_available")),
                "unit_price": _safe_float(form.get(f"offers[{idx}].unit_price")),
                "lead_time": form.get(f"offers[{idx}].lead_time"),
                "date_code": form.get(f"offers[{idx}].date_code"),
                "condition": form.get(f"offers[{idx}].condition", OfferCondition.NEW),
                "moq": _safe_int(form.get(f"offers[{idx}].moq")),
                "notes": form.get(f"offers[{idx}].notes"),
            }
        )
        idx += 1
    return offers_data


def save_form_parsed_offers(
    db: Session, requisition_id: int, vendor_name: str, offers_data: list[dict], user: User
) -> int:
    """Save user-edited, HTMX-form-parsed offers (from ``parse_offer_form_rows``) to the
    Offers table as ACTIVE.

    Resolves/creates one VendorCard per distinct vendor name (falling back to the
    form's top-level *vendor_name*, then "Unknown", exactly as ``parse_offer_form_rows``'
    own default does for a row with no vendor_name field at all — this second fallback
    additionally covers a row whose vendor_name field is present but blank), matches
    each offer's mpn to a Requirement by an EXACT (case-insensitive, whitespace-trimmed)
    ``primary_mpn`` match — fuzzy matching is deliberately NOT used here, unlike
    ``_match_requirement``, because the user has already reviewed/corrected the MPN in
    the edit form — applies qualification scoring, and triggers the
    vendor-unavailability release hook (a user-saved ACTIVE offer is proof of
    availability). Rows with no ``mpn`` are silently skipped. Does NOT commit — caller
    must commit. Returns the count of offers saved.
    """
    from .offer_qualification import apply_qualification

    reqs = db.query(Requirement).filter(Requirement.requisition_id == requisition_id).all()

    saved_count = 0
    for o in offers_data:
        if not o["mpn"]:
            continue

        req_match_id = None
        mpn_lower = (o["mpn"] or "").strip().lower()
        for r in reqs:
            if r.primary_mpn and r.primary_mpn.strip().lower() == mpn_lower:
                req_match_id = r.id
                break

        vn = o.get("vendor_name") or vendor_name or "Unknown"
        norm_name = normalize_vendor_name(vn)
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
        if not card:
            card = VendorCard(normalized_name=norm_name, display_name=vn, emails=[], phones=[])
            db.add(card)
            db.flush()

        offer = Offer(
            requisition_id=requisition_id,
            requirement_id=req_match_id,
            vendor_card_id=card.id,
            vendor_name=card.display_name,
            vendor_name_normalized=card.normalized_name,
            mpn=o["mpn"],
            # Canonical dedup key (dash-stripped) so the part-centric offers query
            # matches these AI-parsed offers, mirroring add_offer / create_offer.
            normalized_mpn=normalize_mpn_key(o["mpn"]),
            manufacturer=o.get("manufacturer"),
            qty_available=o.get("qty_available"),
            unit_price=o.get("unit_price"),
            lead_time=o.get("lead_time"),
            date_code=o.get("date_code"),
            condition=o.get("condition") or OfferCondition.NEW,
            moq=o.get("moq"),
            notes=o.get("notes"),
            source="ai_parsed",
            entered_by_id=user.id,
            status=OfferStatus.ACTIVE,
        )
        apply_qualification(offer)  # non-raising: composes note + sets qualification_status
        db.add(offer)
        # Offer hook: the user reviewed and saved this parse ACTIVE — user-initiated
        # proof of availability, release the vendor's matching active records.
        maybe_release_on_offer(db, req_match_id, offer.vendor_name, user, offer_condition=offer.condition)
        saved_count += 1

    return saved_count


# -- Helpers ------------------------------------------------------------------


def _match_requirement(mpn: str, requirements: list[Requirement]) -> int | None:
    """Find a matching requirement by fuzzy MPN match."""
    for r in requirements:
        if fuzzy_mpn_match(mpn, r.primary_mpn):
            return r.id
    return None


def _log_offer_created(db: Session, offer: Offer, user_id: int) -> None:
    """Log an OFFER_CREATED activity for a freshly saved offer (must be flushed)."""
    log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=user_id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
