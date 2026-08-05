"""ai_offer_service.py — AI offer and RFQ business logic extracted from routers/ai.py.

Handles: prospect contact promotion, saving the HTMX form-array of user-edited
AI-parsed offers (each row through offer_service.create_offer — W3 "one
offer_service"), and applying freeform RFQ templates. The retired JSON-API pair
(save_parsed_offers / save_freeform_offers) died with their /api/ai routes in the
W2 sweep. promote_prospect_contact and apply_freeform_rfq take a db Session and
do NOT commit; save_form_parsed_offers commits per offer (via the service).

Called by: routers/htmx/offers/crud.py (save_parsed_offers route →
    parse_offer_form_rows + save_form_parsed_offers, P4.2)
Depends on: models (Requirement, Requisition, VendorContact, SiteContact,
            ProspectContact, CustomerSite, User), schemas/crm (OfferCreate),
            services/offer_service (canonical create),
            services/requirement_service (THE requirement-creation pipeline, spec §9)
"""

from loguru import logger
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..constants import OfferCondition
from ..models import (
    CustomerSite,
    ProspectContact,
    Requirement,
    Requisition,
    SiteContact,
    User,
    VendorContact,
)
from ..utils import safe_float, safe_int

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


def _promote_prospect_to_contact[ContactT: (VendorContact, SiteContact)](
    db: Session,
    pc: ProspectContact,
    model_class: type[ContactT],
    fk_field: str,
    fk_value: int,
    extra_fields: dict | None = None,
) -> ContactT:
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

    Requirements go through THE creation pipeline
    (services/requirement_service.create_requirements, spec §9): MPN display+key
    normalization, condition/packaging vocabulary, substitute dedup, MaterialCard
    resolve, tag propagation, task auto-gen, dup detection. Caller owns the final commit
    (task auto-gen commits mid-pipeline — see requirement_service docstring). Returns
    dict with id, name, requirements_added. Raises ValueError if customer_site not
    found.
    """
    from ..schemas.requisitions import RequirementCreate
    from .requirement_service import create_requirements

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

    items: list[dict] = []
    for item in requirements[:50]:
        try:
            parsed = RequirementCreate.model_validate(item)
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping invalid requirement item: {} — {}", item, exc)
            continue
        items.append(
            {
                "primary_mpn": parsed.primary_mpn,
                "manufacturer": parsed.manufacturer,
                "target_qty": parsed.target_qty,
                "target_price": parsed.target_price,
                "brand": parsed.brand,
                "substitutes": parsed.substitutes,
                "condition": parsed.condition,
                "date_codes": parsed.date_codes,
                "firmware": parsed.firmware,
                "hardware_codes": parsed.hardware_codes,
                "packaging": parsed.packaging,
                "description": parsed.description,
                "package_type": parsed.package_type,
                "revision": parsed.revision,
                "customer_pn": parsed.customer_pn,
                "need_by_date": parsed.need_by_date,
                "notes": parsed.notes,
            }
        )

    result = create_requirements(db, req, items)
    added = len(result.created)
    logger.info("Created freeform requisition id={} name='{}' with {} requirements", req.id, req.name, added)
    return {"id": req.id, "name": req.name, "requirements_added": added}


# -- Save HTMX Form-Parsed Offers ---------------------------------------------
# The HTMX parse-results partial lets a buyer edit the AI-parsed offers in a form
# before saving, so these go straight to ACTIVE with qualification scoring applied
# instead of sitting in PENDING_REVIEW. Split in two so the router can short-circuit
# on "no rows at all" (parse_offer_form_rows) before doing any DB work.


# parse_offer_form_rows below used to have its own private _safe_int/_safe_float
# (falsy pre-check: `if not val: return None`) instead of importing app.utils'
# safe_int/safe_float (None-pre-check: `if v is None: return None`). Those two only
# actually disagree on a literal falsy-but-convertible input — int 0, float 0.0,
# or "" — and every call site below feeds these functions Starlette FormData values,
# which are ALWAYS `str | None` (never a real int/float): the string "0" is
# TRUTHY (non-empty), so both versions take the try/int(val) branch and return 0
# either way; "" is falsy in both AND fails int()/float() regardless, landing on
# None either way. Confirmed behavior-identical for these form paths, so the
# duplicate was deleted in favor of the shared app.utils helpers (imported above)
# rather than keeping a redundant private copy.


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
                "qty_available": safe_int(form.get(f"offers[{idx}].qty_available")),
                "unit_price": safe_float(form.get(f"offers[{idx}].unit_price")),
                "lead_time": form.get(f"offers[{idx}].lead_time"),
                "date_code": form.get(f"offers[{idx}].date_code"),
                "condition": form.get(f"offers[{idx}].condition", OfferCondition.NEW),
                "moq": safe_int(form.get(f"offers[{idx}].moq")),
                "notes": form.get(f"offers[{idx}].notes"),
            }
        )
        idx += 1
    return offers_data


async def save_form_parsed_offers(
    db: Session, requisition_id: int, vendor_name: str, offers_data: list[dict], user: User
) -> int:
    """Save user-edited, HTMX-form-parsed offers (from ``parse_offer_form_rows``) as
    ACTIVE — each row through the canonical ``offer_service.create_offer`` (W3).

    Vendor resolution/creation, normalized_mpn, qualification scoring, activity
    logging, and the vendor-unavailability release hook all live in the service.
    This loop keeps only the form-specific adaptation: vendor-name fallback to the
    form's top-level *vendor_name*, then "Unknown"; requirement matching by an
    EXACT (case-insensitive, whitespace-trimmed) ``primary_mpn`` match — fuzzy
    matching is deliberately NOT used because the user already reviewed/corrected
    the MPN in the edit form. Rows with no ``mpn`` are silently skipped; a row that
    fails payload validation (e.g. negative qty) is skipped with a warning instead
    of aborting the batch. Commits per offer (via the service). Returns the count
    of offers saved.
    """
    from ..schemas.crm import OfferCreate
    from .offer_service import create_offer

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
        try:
            payload = OfferCreate(
                mpn=o["mpn"],
                vendor_name=vn,
                requirement_id=req_match_id,
                manufacturer=o.get("manufacturer"),
                qty_available=o.get("qty_available"),
                unit_price=o.get("unit_price"),
                lead_time=o.get("lead_time"),
                date_code=o.get("date_code"),
                condition=o.get("condition") or OfferCondition.NEW,
                moq=o.get("moq"),
                notes=o.get("notes"),
                source="ai_parsed",
            )
        except ValidationError as exc:
            logger.warning("Skipping invalid parsed offer row (mpn={}): {}", o.get("mpn"), exc)
            continue
        await create_offer(db, requisition_id, payload, user)
        saved_count += 1

    return saved_count
