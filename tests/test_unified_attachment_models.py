"""tests/test_unified_attachment_models.py — Model-layer tests for unified attachment
schema.

Covers:
  - 3 new attachment models instantiate and persist (CompanyAttachment, SiteContactAttachment,
    MaterialCardAttachment)
  - back-ref `attachments` works on Company, SiteContact, MaterialCard
  - library_drive_id is nullable (discriminator: NULL = OneDrive fallback, set = company library)
  - Renamed columns library_item_id / library_web_url exist on the 3 existing models
    (OfferAttachment, RequisitionAttachment, RequirementAttachment)

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_company, test_requisition fixtures)
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Company,
    CompanyAttachment,
    MaterialCard,
    MaterialCardAttachment,
    OfferAttachment,
    RequirementAttachment,
    Requisition,
    RequisitionAttachment,
    SiteContact,
    SiteContactAttachment,
    User,
)
from app.models.crm import CustomerSite

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_customer_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name="Main Site",
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()
    return site


def _make_site_contact(db: Session, site: CustomerSite) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Alice Buyer",
        email="alice@acme.com",
        created_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    db.flush()
    return contact


def _make_material_card(db: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


# ── New model: CompanyAttachment ──────────────────────────────────────────────


def test_company_attachment_creates_and_back_ref(db_session: Session, test_company: Company, test_user: User):
    """CompanyAttachment persists and the company.attachments back-ref returns it."""
    att = CompanyAttachment(
        company_id=test_company.id,
        file_name="spec_sheet.pdf",
        library_item_id="item-abc-123",
        library_drive_id=None,  # OneDrive fallback
        library_web_url="https://onedrive.example.com/spec_sheet.pdf",
        content_type="application/pdf",
        size_bytes=204800,
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(test_company)
    assert len(test_company.attachments) == 1
    result = test_company.attachments[0]
    assert result.file_name == "spec_sheet.pdf"
    assert result.library_drive_id is None
    assert result.library_item_id == "item-abc-123"


def test_company_attachment_library_drive_id_nullable(db_session: Session, test_company: Company):
    """library_drive_id can be NULL (OneDrive fallback) or non-NULL (company
    library)."""
    onedrive_att = CompanyAttachment(
        company_id=test_company.id,
        file_name="od_file.pdf",
        library_drive_id=None,
        created_at=datetime.now(timezone.utc),
    )
    library_att = CompanyAttachment(
        company_id=test_company.id,
        file_name="lib_file.pdf",
        library_drive_id="b!driveId-ABC-123",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([onedrive_att, library_att])
    db_session.commit()

    db_session.refresh(test_company)
    assert len(test_company.attachments) == 2
    drive_ids = {a.library_drive_id for a in test_company.attachments}
    assert None in drive_ids
    assert "b!driveId-ABC-123" in drive_ids


# ── New model: SiteContactAttachment ─────────────────────────────────────────


def test_site_contact_attachment_creates_and_back_ref(db_session: Session, test_company: Company, test_user: User):
    """SiteContactAttachment persists and site_contact.attachments back-ref works."""
    site = _make_customer_site(db_session, test_company)
    contact = _make_site_contact(db_session, site)

    att = SiteContactAttachment(
        site_contact_id=contact.id,
        file_name="business_card.jpg",
        library_item_id="item-xyz-456",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/business_card.jpg",
        content_type="image/jpeg",
        size_bytes=51200,
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(contact)
    assert len(contact.attachments) == 1
    result = contact.attachments[0]
    assert result.file_name == "business_card.jpg"
    assert result.library_drive_id is None


def test_site_contact_attachment_library_drive_set(db_session: Session, test_company: Company):
    """SiteContactAttachment stores a non-NULL library_drive_id (SharePoint path)."""
    site = _make_customer_site(db_session, test_company)
    contact = _make_site_contact(db_session, site)

    att = SiteContactAttachment(
        site_contact_id=contact.id,
        file_name="contract.docx",
        library_drive_id="b!SPDriveId-789",
        library_item_id="sp-item-789",
        library_web_url="https://company.sharepoint.com/contract.docx",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(contact)
    assert contact.attachments[0].library_drive_id == "b!SPDriveId-789"


# ── New model: MaterialCardAttachment ─────────────────────────────────────────


def test_material_card_attachment_creates_and_back_ref(db_session: Session, test_user: User):
    """MaterialCardAttachment persists and material_card.attachments back-ref works."""
    card = _make_material_card(db_session)

    att = MaterialCardAttachment(
        material_card_id=card.id,
        file_name="test_report.pdf",
        library_item_id="item-mc-111",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/test_report.pdf",
        content_type="application/pdf",
        size_bytes=102400,
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(card)
    assert len(card.attachments) == 1
    result = card.attachments[0]
    assert result.file_name == "test_report.pdf"
    assert result.library_drive_id is None


def test_material_card_datasheets_relationship_unchanged(db_session: Session):
    """The existing datasheets relationship on MaterialCard is NOT disturbed."""
    card = _make_material_card(db_session)
    # datasheets is a separate relationship — should still be accessible and empty
    db_session.refresh(card)
    assert hasattr(card, "datasheets")
    assert card.datasheets == []
    # attachments is new and separate
    assert hasattr(card, "attachments")
    assert card.attachments == []


def test_material_card_attachment_library_drive_id_nullable(db_session: Session):
    """MaterialCardAttachment.library_drive_id can be NULL."""
    card = _make_material_card(db_session)
    att = MaterialCardAttachment(
        material_card_id=card.id,
        file_name="drawing.png",
        library_drive_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(att)
    assert att.library_drive_id is None


# ── Existing models: renamed columns present ──────────────────────────────────


def test_offer_attachment_has_renamed_columns(db_session: Session, test_requisition: Requisition, test_user: User):
    """OfferAttachment exposes library_item_id and library_web_url (not onedrive_*)."""
    from app.models import Offer

    req = test_requisition
    requirement = req.requirements[0]

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_name="TestVendor",
        mpn="LM317T",
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    att = OfferAttachment(
        offer_id=offer.id,
        file_name="offer_doc.pdf",
        library_item_id="oa-item-001",
        library_web_url="https://onedrive.example.com/offer_doc.pdf",
        library_drive_id=None,
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(att)
    assert att.library_item_id == "oa-item-001"
    assert att.library_web_url == "https://onedrive.example.com/offer_doc.pdf"
    assert att.library_drive_id is None
    # Ensure old names are gone
    assert not hasattr(att, "onedrive_item_id")
    assert not hasattr(att, "onedrive_url")


def test_requisition_attachment_has_renamed_columns(
    db_session: Session, test_requisition: Requisition, test_user: User
):
    """RequisitionAttachment exposes library_item_id and library_web_url (not
    onedrive_*)."""
    att = RequisitionAttachment(
        requisition_id=test_requisition.id,
        file_name="req_doc.pdf",
        library_item_id="ra-item-002",
        library_web_url="https://onedrive.example.com/req_doc.pdf",
        library_drive_id=None,
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(att)
    assert att.library_item_id == "ra-item-002"
    assert att.library_web_url == "https://onedrive.example.com/req_doc.pdf"
    assert att.library_drive_id is None
    assert not hasattr(att, "onedrive_item_id")
    assert not hasattr(att, "onedrive_url")


def test_requirement_attachment_has_renamed_columns(
    db_session: Session, test_requisition: Requisition, test_user: User
):
    """RequirementAttachment exposes library_item_id and library_web_url (not
    onedrive_*)."""
    requirement = test_requisition.requirements[0]

    att = RequirementAttachment(
        requirement_id=requirement.id,
        file_name="part_spec.pdf",
        library_item_id="rqa-item-003",
        library_web_url="https://onedrive.example.com/part_spec.pdf",
        library_drive_id="b!CompanyDrive",
        uploaded_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()

    db_session.refresh(att)
    assert att.library_item_id == "rqa-item-003"
    assert att.library_web_url == "https://onedrive.example.com/part_spec.pdf"
    assert att.library_drive_id == "b!CompanyDrive"
    assert not hasattr(att, "onedrive_item_id")
    assert not hasattr(att, "onedrive_url")


# ── Cascade delete: parent delete removes child attachments ───────────────────


def test_company_attachment_cascade_delete(db_session: Session, test_company: Company):
    """Deleting a Company also deletes its attachments (CASCADE)."""
    att = CompanyAttachment(
        company_id=test_company.id,
        file_name="cascade_test.pdf",
        library_drive_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()
    att_id = att.id

    db_session.delete(test_company)
    db_session.commit()

    assert db_session.get(CompanyAttachment, att_id) is None
