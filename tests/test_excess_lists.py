"""Tests for Phase 2B: Excess List Differentiation.

Covers:
- match_email_to_entity correctly classifies company vs vendor senders
- Sighting model has source_company_id column
- MaterialVendorHistory source_type tagging for excess_list vs email_auto_import
- ActivityLog model has dismissed_at column

Called by: pytest
Depends on: conftest.py fixtures, app.services.activity_service, app.models
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from app.models import (
    ActivityLog,
    CustomerSite,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Sighting,
)

# ── Model tests ────────────────────────────────────────────────────────


class TestSightingSourceCompanyId:
    """Sighting model should have source_company_id column."""

    def test_sighting_has_source_company_id(self, db_session, test_requisition, test_company):
        req = test_requisition
        requirement = db_session.query(Requirement).filter_by(requisition_id=req.id).first()

        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="Customer Corp",
            vendor_email="excess@customer.com",
            mpn_matched="LM317T",
            source_type="excess_list",
            source_company_id=test_company.id,
            qty_available=500,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()
        db_session.refresh(sighting)

        assert sighting.source_company_id == test_company.id
        assert sighting.source_type == "excess_list"

    def test_sighting_source_company_nullable(self, db_session, test_requisition):
        """Regular sightings should have null source_company_id."""
        requirement = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()

        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="Arrow Electronics",
            mpn_matched="LM317T",
            source_type="api",
            qty_available=1000,
        )
        db_session.add(sighting)
        db_session.commit()

        assert sighting.source_company_id is None

    def test_sighting_source_company_relationship(self, db_session, test_requisition, test_company):
        """source_company relationship should resolve to Company."""
        requirement = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()

        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="Customer Co",
            mpn_matched="LM317T",
            source_type="excess_list",
            source_company_id=test_company.id,
        )
        db_session.add(sighting)
        db_session.commit()
        db_session.refresh(sighting)

        assert sighting.source_company is not None
        assert sighting.source_company.name == "Acme Electronics"


class TestActivityLogDismissedAt:
    """ActivityLog model should have dismissed_at column."""

    def test_dismissed_at_default_null(self, db_session, test_user):
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="test@example.com",
        )
        db_session.add(a)
        db_session.commit()
        db_session.refresh(a)

        assert a.dismissed_at is None

    def test_dismissed_at_can_be_set(self, db_session, test_user):
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="test@example.com",
        )
        db_session.add(a)
        db_session.flush()

        now = datetime.now(timezone.utc)
        a.dismissed_at = now
        db_session.commit()
        db_session.refresh(a)

        assert a.dismissed_at is not None


# ── Sender classification tests ────────────────────────────────────────


class TestSenderClassification:
    """match_email_to_entity correctly classifies senders."""

    def test_customer_site_email_matches_company(self, db_session, test_company):
        """Email matching a customer_site returns company type."""
        from app.services.activity_service import match_email_to_entity

        site = CustomerSite(
            company_id=test_company.id,
            site_name="Acme Main",
            contact_email="procurement@acme-electronics.com",
            is_active=True,
        )
        db_session.add(site)
        db_session.commit()

        result = match_email_to_entity("procurement@acme-electronics.com", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == test_company.id

    def test_company_domain_matches_company(self, db_session, test_company):
        """Email with company domain returns company type."""
        from app.services.activity_service import match_email_to_entity

        test_company.domain = "acme-electronics.com"
        db_session.commit()

        result = match_email_to_entity("someone@acme-electronics.com", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == test_company.id

    def test_vendor_card_domain_matches_vendor(self, db_session, test_vendor_card):
        """Email with vendor card domain returns vendor type."""
        from app.services.activity_service import match_email_to_entity

        test_vendor_card.domain = "arrow.com"
        db_session.commit()

        result = match_email_to_entity("sales@arrow.com", db_session)
        assert result is not None
        assert result["type"] == "vendor"
        assert result["id"] == test_vendor_card.id

    def test_unknown_email_returns_none(self, db_session):
        """Completely unknown email returns None."""
        from app.services.activity_service import match_email_to_entity

        result = match_email_to_entity("nobody@totallyrandom.xyz", db_session)
        assert result is None

    def test_generic_domain_returns_none(self, db_session):
        """Gmail/Yahoo/etc should not match anything."""
        from app.services.activity_service import match_email_to_entity

        result = match_email_to_entity("someone@gmail.com", db_session)
        assert result is None


class TestMaterialVendorHistorySourceType:
    """MaterialVendorHistory.source_type should reflect stock vs excess list."""

    def test_stock_list_source_type(self, db_session):
        """Regular stock list import should use email_auto_import."""
        card = MaterialCard(
            normalized_mpn="LM317T",
            display_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="arrow electronics",
            source_type="email_auto_import",
            last_qty=1000,
        )
        db_session.add(mvh)
        db_session.commit()

        assert mvh.source_type == "email_auto_import"

    def test_excess_list_source_type(self, db_session):
        """Excess list import should use excess_list source type."""
        card = MaterialCard(
            normalized_mpn="SN74HC595N",
            display_mpn="SN74HC595N",
        )
        db_session.add(card)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="customer corp",
            source_type="excess_list",
            last_qty=5000,
        )
        db_session.add(mvh)
        db_session.commit()

        assert mvh.source_type == "excess_list"
