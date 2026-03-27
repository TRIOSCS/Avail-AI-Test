"""Tests for vendor_merge_service and company_merge_service.

Integration tests using the in-memory SQLite DB to verify merge logic:
tags, notes, sites, FK reassignment, and deletion.

Called by: pytest
Depends on: conftest fixtures, app.models, app.services.vendor_merge_service,
            app.services.company_merge_service
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    Requirement,
    Requisition,
    User,
    VendorCard,
    VendorContact,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_user(db: Session, email: str = "merge@test.com") -> User:
    u = User(
        email=email,
        name="Merge Tester",
        role="buyer",
        azure_id=f"az-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_company(db: Session, name: str, **kw) -> Company:
    defaults = dict(
        name=name,
        website=f"https://{name.lower().replace(' ', '')}.com",
        industry="Electronics",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


def _make_vendor(db: Session, display_name: str, **kw) -> VendorCard:
    defaults = dict(
        normalized_name=display_name.lower(),
        display_name=display_name,
        sighting_count=10,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_site(db: Session, company: Company, name: str = "HQ", **kw) -> CustomerSite:
    defaults = dict(company_id=company.id, site_name=name)
    defaults.update(kw)
    site = CustomerSite(**defaults)
    db.add(site)
    db.flush()
    return site


# ══════════════════════════════════════════════════════════════════════
# Vendor Merge Service
# ══════════════════════════════════════════════════════════════════════


class TestVendorMergeService:
    def test_merge_basic(self, db_session):
        """Merge two vendors — keep should absorb remove's data."""
        from app.services.vendor_merge_service import merge_vendor_cards

        keep = _make_vendor(
            db_session, "Arrow Electronics", sighting_count=10, emails=["a@arrow.com"], phones=["+1-111"]
        )
        remove = _make_vendor(db_session, "Arrow Elec.", sighting_count=5, emails=["b@arrow.com"], phones=["+1-222"])
        db_session.commit()

        result = merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["ok"] is True
        assert result["kept"] == keep.id
        assert result["removed"] == remove.id

        refreshed = db_session.get(VendorCard, keep.id)
        assert refreshed.sighting_count == 15  # summed
        assert "b@arrow.com" in [str(e) for e in refreshed.emails]
        assert "+1-222" in [str(p) for p in refreshed.phones]

        # Remove should be deleted
        assert db_session.get(VendorCard, remove.id) is None

    def test_merge_alternate_names(self, db_session):
        """Remove's display_name becomes an alternate name on keep."""
        from app.services.vendor_merge_service import merge_vendor_cards

        keep = _make_vendor(db_session, "Arrow Electronics")
        remove = _make_vendor(db_session, "Arrow Elec Inc")
        db_session.commit()

        merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        refreshed = db_session.get(VendorCard, keep.id)
        assert "Arrow Elec Inc" in (refreshed.alternate_names or [])

    def test_merge_fills_missing_domain(self, db_session):
        """Keep's empty domain should be filled from remove."""
        from app.services.vendor_merge_service import merge_vendor_cards

        keep = _make_vendor(db_session, "Acme Vendor", domain=None)
        remove = _make_vendor(db_session, "Acme V", domain="acme.com")
        db_session.commit()

        merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(VendorCard, keep.id).domain == "acme.com"

    def test_merge_reassigns_contacts(self, db_session):
        """VendorContacts should move from remove to keep."""
        from app.services.vendor_merge_service import merge_vendor_cards

        keep = _make_vendor(db_session, "Keep Vendor")
        remove = _make_vendor(db_session, "Remove Vendor")
        contact = VendorContact(
            vendor_card_id=remove.id,
            full_name="John",
            email="john@test.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()

        result = merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["reassigned"] >= 1
        assert db_session.get(VendorContact, contact.id).vendor_card_id == keep.id

    def test_merge_reassigns_offers(self, db_session):
        """Offers should be reassigned from remove to keep."""
        from app.services.vendor_merge_service import merge_vendor_cards

        user = _make_user(db_session)
        keep = _make_vendor(db_session, "Keep V")
        remove = _make_vendor(db_session, "Remove V")

        req = Requisition(
            name="REQ-1",
            customer_name="Test",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id, primary_mpn="LM317T", target_qty=100, created_at=datetime.now(timezone.utc)
        )
        db_session.add(requirement)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_card_id=remove.id,
            vendor_name="Remove V",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.50,
            status="active",
            entered_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Offer, offer.id).vendor_card_id == keep.id

    def test_merge_same_id_raises(self, db_session):
        """Merging a vendor with itself should raise ValueError."""
        from app.services.vendor_merge_service import merge_vendor_cards

        v = _make_vendor(db_session, "Solo Vendor")
        db_session.commit()

        with pytest.raises(ValueError, match="Cannot merge a vendor with itself"):
            merge_vendor_cards(v.id, v.id, db_session)

    def test_merge_not_found_raises(self, db_session):
        """Merging with a non-existent vendor should raise ValueError."""
        from app.services.vendor_merge_service import merge_vendor_cards

        v = _make_vendor(db_session, "Exists")
        db_session.commit()

        with pytest.raises(ValueError, match="not found"):
            merge_vendor_cards(v.id, 99999, db_session)

    def test_merge_does_not_duplicate_alternate_names(self, db_session):
        """If display_name already in alternate_names, don't add again."""
        from app.services.vendor_merge_service import merge_vendor_cards

        keep = _make_vendor(db_session, "Arrow Electronics", alternate_names=["Arrow Elec"])
        remove = _make_vendor(db_session, "Arrow Elec")
        db_session.commit()

        merge_vendor_cards(keep.id, remove.id, db_session)
        db_session.commit()

        refreshed = db_session.get(VendorCard, keep.id)
        assert (refreshed.alternate_names or []).count("Arrow Elec") == 1


# ══════════════════════════════════════════════════════════════════════
# Company Merge Service
# ══════════════════════════════════════════════════════════════════════


class TestCompanyMergeService:
    def test_merge_basic(self, db_session):
        """Basic company merge — remove deleted, keep preserved."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Acme Corp")
        remove = _make_company(db_session, "ACME Corporation")
        db_session.commit()

        result = merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["ok"] is True
        assert result["kept"] == keep.id
        assert result["removed"] == remove.id
        assert db_session.get(Company, remove.id) is None

    def test_merge_tags_deduped(self, db_session):
        """Tags from remove should merge into keep without duplicates."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep Co", brand_tags=["BrandA"], commodity_tags=["ICs"])
        remove = _make_company(
            db_session, "Remove Co", brand_tags=["BrandA", "BrandB"], commodity_tags=["ICs", "Capacitors"]
        )
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        refreshed = db_session.get(Company, keep.id)
        assert "BrandB" in [str(t) for t in refreshed.brand_tags]
        assert len(refreshed.brand_tags) == 2  # no dup of BrandA

    def test_merge_notes(self, db_session):
        """Notes from remove should be appended to keep."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", notes="Keep notes")
        remove = _make_company(db_session, "Remove", notes="Remove notes")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        refreshed = db_session.get(Company, keep.id)
        assert "Keep notes" in refreshed.notes
        assert "Remove notes" in refreshed.notes
        assert "Merged from Remove" in refreshed.notes

    def test_merge_fills_enrichment_gaps(self, db_session):
        """Enrichment fields should fill from remove if keep is None."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", domain=None, hq_city=None)
        remove = _make_company(db_session, "Remove", domain="remove.com", hq_city="Austin")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        refreshed = db_session.get(Company, keep.id)
        assert refreshed.domain == "remove.com"
        assert refreshed.hq_city == "Austin"

    def test_merge_keeps_existing_enrichment(self, db_session):
        """Keep's enrichment fields should NOT be overwritten."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", domain="keep.com")
        remove = _make_company(db_session, "Remove", domain="remove.com")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Company, keep.id).domain == "keep.com"

    def test_merge_boolean_or(self, db_session):
        """is_strategic should be True if either company was strategic."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", is_strategic=False)
        remove = _make_company(db_session, "Remove", is_strategic=True)
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Company, keep.id).is_strategic is True

    def test_merge_owner_fill(self, db_session):
        """Owner should fill from remove if keep has none."""
        from app.services.company_merge_service import merge_companies

        user = _make_user(db_session)
        keep = _make_company(db_session, "Keep", account_owner_id=None)
        remove = _make_company(db_session, "Remove", account_owner_id=user.id)
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Company, keep.id).account_owner_id == user.id

    def test_merge_moves_sites(self, db_session):
        """Sites should be moved from remove to keep."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep")
        remove = _make_company(db_session, "Remove")
        site = _make_site(db_session, remove, "Branch Office", contact_name="Jane", contact_email="jane@test.com")
        db_session.commit()

        result = merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["sites_moved"] == 1
        assert db_session.get(CustomerSite, site.id).company_id == keep.id

    def test_merge_deletes_empty_hq(self, db_session):
        """Empty HQ sites on remove should be deleted, not moved."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep")
        remove = _make_company(db_session, "Remove")
        _make_site(db_session, remove, "HQ")  # empty HQ — no contacts/address
        db_session.commit()

        result = merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["sites_deleted"] == 1
        assert result["sites_moved"] == 0

    def test_merge_renames_duplicate_site(self, db_session):
        """If keep already has a site with the same name, rename the moved site."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep")
        remove = _make_company(db_session, "Remove")
        _make_site(db_session, keep, "Main Office", contact_name="K")
        site_r = _make_site(db_session, remove, "Main Office", contact_name="R")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        moved = db_session.get(CustomerSite, site_r.id)
        assert moved.company_id == keep.id
        assert "Remove" in moved.site_name  # renamed to include source

    def test_merge_reassigns_activity_logs(self, db_session):
        """ActivityLogs referencing remove should be reassigned."""
        from app.services.company_merge_service import merge_companies

        user = _make_user(db_session)
        keep = _make_company(db_session, "Keep")
        remove = _make_company(db_session, "Remove")
        act = ActivityLog(
            user_id=user.id,
            activity_type="email_sent",
            channel="email",
            company_id=remove.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        result = merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert result["reassigned"] >= 1
        assert db_session.get(ActivityLog, act.id).company_id == keep.id

    def test_merge_same_id_raises(self, db_session):
        from app.services.company_merge_service import merge_companies

        co = _make_company(db_session, "Solo")
        db_session.commit()

        with pytest.raises(ValueError, match="Cannot merge a company with itself"):
            merge_companies(co.id, co.id, db_session)

    def test_merge_not_found_raises(self, db_session):
        from app.services.company_merge_service import merge_companies

        co = _make_company(db_session, "Exists")
        db_session.commit()

        with pytest.raises(ValueError, match="not found"):
            merge_companies(co.id, 99999, db_session)

    def test_merge_timestamp_keeps_latest(self, db_session):
        """Merge should keep the most recent last_activity_at."""
        from app.services.company_merge_service import merge_companies

        early = datetime(2024, 1, 1, tzinfo=timezone.utc)
        late = datetime(2025, 6, 15, tzinfo=timezone.utc)

        keep = _make_company(db_session, "Keep", last_activity_at=early)
        remove = _make_company(db_session, "Remove", last_activity_at=late)
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Company, keep.id).last_activity_at == late

    def test_merge_notes_none_keep(self, db_session):
        """If keep has no notes, remove notes should still be added."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", notes=None)
        remove = _make_company(db_session, "Remove", notes="Some notes")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert "Some notes" in db_session.get(Company, keep.id).notes

    def test_merge_remove_no_notes(self, db_session):
        """If remove has no notes, keep notes should be unchanged."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep", notes="Original")
        remove = _make_company(db_session, "Remove", notes=None)
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()

        assert db_session.get(Company, keep.id).notes == "Original"
