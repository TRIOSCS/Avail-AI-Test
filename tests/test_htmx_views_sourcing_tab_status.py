"""Tests for derived vendor status in the sightings tab.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from app.models.auth import User
from app.models.offers import Contact, Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard


def _make_user(db_session) -> User:
    u = User(email="test@example.com", name="Test User", role="buyer")
    db_session.add(u)
    db_session.flush()
    return u


def _make_requisition(db_session) -> Requisition:
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    return req


def _make_requirement(db_session, req: Requisition) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN-001",
        manufacturer="TestMfr",
    )
    db_session.add(r)
    db_session.flush()
    return r


def _make_summary(db_session, req_id: int, vendor: str, qty: int = 100) -> VendorSightingSummary:
    s = VendorSightingSummary(
        requirement_id=req_id,
        vendor_name=vendor,
        estimated_qty=qty,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.flush()
    return s


class TestDeriveVendorStatus:
    """Test the compute_vendor_statuses helper function."""

    def test_default_status_is_sighting(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_contacted_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        user = _make_user(db_session)
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        contact = Contact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name="Acme Corp",
            parts_included=["TEST-MPN-001"],
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "contacted"

    def test_offer_in_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"

    def test_unavailable_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        sighting = Sighting(
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn_matched="TEST-MPN-001",
            is_unavailable=True,
        )
        db_session.add(sighting)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_blacklisted_overrides_all(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Bad Vendor")
        vc = VendorCard(normalized_name="bad vendor", display_name="Bad Vendor", is_blacklisted=True)
        db_session.add(vc)
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Bad Vendor",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Bad Vendor"] == "blacklisted"

    def test_offer_in_overrides_contacted(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        user = _make_user(db_session)
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(
            Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name="Acme Corp",
                parts_included=["TEST-MPN-001"],
                status="sent",
            )
        )
        db_session.add(
            Offer(
                requisition_id=req.id,
                requirement_id=r.id,
                vendor_name="Acme Corp",
                mpn="TEST-MPN-001",
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"
