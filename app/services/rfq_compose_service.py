"""rfq_compose_service.py — RFQ compose/send business logic extracted from routers/htmx_views.py.

Handles: building vendor lists for RFQ compose, creating RFQ contact records.
All functions take a db Session and return data — they do NOT commit.

Called by: routers/htmx_views.py
Depends on: models (Requisition, Requirement, Sighting, VendorCard, VendorContact, Contact)
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models import Requirement, Sighting, VendorCard, VendorContact
from ..models.offers import Contact as RfqContact


def build_rfq_vendor_list(db: Session, req_id: int) -> list[dict]:
    """Build the vendor list for RFQ compose from sighting data.

    Returns list of vendor dicts with contacts and already-asked status.
    Does NOT commit.
    """
    parts = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    part_ids = [p.id for p in parts]
    if not part_ids:
        return []

    # Get distinct vendor names from sightings
    vendor_names = (
        db.query(Sighting.vendor_name_normalized)
        .filter(Sighting.requirement_id.in_(part_ids), Sighting.vendor_name_normalized.isnot(None))
        .distinct()
        .all()
    )
    norm_names = [n[0] for n in vendor_names if n[0]]
    vendor_rows = (
        db.query(VendorCard).filter(VendorCard.normalized_name.in_(norm_names)).limit(50).all()
        if norm_names
        else []
    )

    # Check which vendors already have RFQs sent
    sent_vendor_names = set()
    existing_contacts = db.query(RfqContact).filter(RfqContact.requisition_id == req_id).all()
    for c in existing_contacts:
        if c.vendor_name_normalized:
            sent_vendor_names.add(c.vendor_name_normalized)

    # Batch-load all vendor contacts in one query (avoids N+1)
    vendor_ids = [v.id for v in vendor_rows]
    all_contacts = (
        db.query(VendorContact).filter(VendorContact.vendor_card_id.in_(vendor_ids)).all()
        if vendor_ids
        else []
    )
    contacts_by_vendor: dict[int, list] = {}
    for c in all_contacts:
        contacts_by_vendor.setdefault(c.vendor_card_id, []).append(c)

    vendors = []
    for v in vendor_rows:
        v_contacts = contacts_by_vendor.get(v.id, [])[:5]
        vendors.append(
            {
                "id": v.id,
                "display_name": v.display_name,
                "normalized_name": v.normalized_name,
                "domain": v.domain,
                "contacts": v_contacts,
                "already_asked": v.normalized_name in sent_vendor_names,
                "emails": [c.email for c in v_contacts if c.email],
            }
        )

    return vendors


def create_rfq_contacts(
    db: Session, req_id: int, user_id: int,
    vendor_names: list[str], vendor_emails: list[str],
    subject: str, parts_text: str,
) -> list[dict]:
    """Create RFQ Contact records for each selected vendor.

    Does NOT commit — caller must commit.
    Returns list of sent result dicts.
    """
    sent = []
    for name, email in zip(vendor_names, vendor_emails):
        if not email:
            continue
        contact = RfqContact(
            requisition_id=req_id,
            user_id=user_id,
            contact_type="email",
            vendor_name=name,
            vendor_name_normalized=name.lower().strip(),
            vendor_contact=email,
            parts_included=parts_text,
            subject=subject,
            status="sent",
            status_updated_at=datetime.now(timezone.utc),
        )
        db.add(contact)
        sent.append({"vendor": name, "email": email, "status": "sent"})

    logger.info("Created {} RFQ contacts for requisition_id={}", len(sent), req_id)
    return sent
