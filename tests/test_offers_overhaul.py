"""Tests for Offers Tab Overhaul — changelog, approval, inline edit, auto-parse, quoted
badge, notifications, buy plan lead_time, PO validation."""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone

import pytest

from app.models import (
    ActivityLog,
    ChangeLog,
    Offer,
    Quote,
    Requisition,
    VendorResponse,
)

# ── Changelog / Audit Trail ──────────────────────────────────────────


class TestChangelog:
    """Change tracking on offer and requirement updates."""

    def test_offer_update_creates_changelog(self, client, test_offer, db_session):
        """PUT /api/offers/{id} records field-level changes."""
        resp = client.put(
            f"/api/offers/{test_offer.id}",
            json={"vendor_name": "Mouser Electronics", "lead_time": "5-7 days"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        logs = (
            db_session.query(ChangeLog)
            .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == test_offer.id)
            .all()
        )
        fields_changed = {log.field_name for log in logs}
        assert "vendor_name" in fields_changed
        assert "lead_time" in fields_changed

        vn_log = next(lg for lg in logs if lg.field_name == "vendor_name")
        assert vn_log.old_value == "Arrow Electronics"
        assert vn_log.new_value == "Mouser Electronics"

    def test_offer_update_sets_updated_at(self, client, test_offer, db_session):
        """Offer updated_at and updated_by_id are set on update."""
        client.put(f"/api/offers/{test_offer.id}", json={"notes": "test note"})
        db_session.refresh(test_offer)
        assert test_offer.updated_at is not None
        assert test_offer.updated_by_id is not None

    def test_no_changelog_when_same_value(self, client, test_offer, db_session):
        """No changelog entry when value doesn't change."""
        client.put(
            f"/api/offers/{test_offer.id}",
            json={"vendor_name": "Arrow Electronics"},
        )
        logs = (
            db_session.query(ChangeLog)
            .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == test_offer.id)
            .all()
        )
        assert len(logs) == 0

    def test_requirement_update_creates_changelog(self, client, test_requisition, db_session):
        """PUT /api/requirements/{id} records field-level changes."""
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"target_qty": 2000, "notes": "updated notes"},
        )
        assert resp.status_code == 200

        logs = (
            db_session.query(ChangeLog)
            .filter(
                ChangeLog.entity_type == "requirement",
                ChangeLog.entity_id == req_item.id,
            )
            .all()
        )
        fields_changed = {log.field_name for log in logs}
        assert "target_qty" in fields_changed
        assert "notes" in fields_changed

    def test_get_changelog_api(self, client, test_offer, db_session):
        """GET /api/changelog/offer/{id} returns change history."""
        # Make a change first
        client.put(f"/api/offers/{test_offer.id}", json={"lead_time": "3 weeks"})
        resp = client.get(f"/api/changelog/offer/{test_offer.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["field_name"] == "lead_time"
        assert data[0]["new_value"] == "3 weeks"
        assert "user_name" in data[0]
        assert "created_at" in data[0]

    def test_changelog_invalid_entity_type(self, client):
        """GET /api/changelog with invalid entity_type returns 400."""
        resp = client.get("/api/changelog/invalid/1")
        assert resp.status_code == 400


# ── Offer Approval Workflow ──────────────────────────────────────────


class TestOfferApproval:
    """Approve and reject pending_review offers."""

    @pytest.fixture()
    def pending_offer(self, db_session, test_requisition, test_user):
        """Create a pending_review offer."""
        o = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Draft Vendor",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.60,
            status="pending_review",
            source="email_parse",
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        return o

    def test_approve_offer(self, client, pending_offer, db_session):
        """PUT /api/offers/{id}/approve changes status to active."""
        resp = client.put(f"/api/offers/{pending_offer.id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        db_session.refresh(pending_offer)
        assert pending_offer.status == "active"
        assert pending_offer.approved_by_id is not None
        assert pending_offer.approved_at is not None

    def test_approve_creates_changelog(self, client, pending_offer, db_session):
        """Approving creates a changelog entry."""
        client.put(f"/api/offers/{pending_offer.id}/approve")
        logs = (
            db_session.query(ChangeLog)
            .filter(
                ChangeLog.entity_type == "offer",
                ChangeLog.entity_id == pending_offer.id,
                ChangeLog.field_name == "status",
            )
            .all()
        )
        assert len(logs) == 1
        assert logs[0].old_value == "pending_review"
        assert logs[0].new_value == "active"

    def test_reject_offer(self, client, pending_offer, db_session):
        """PUT /api/offers/{id}/reject changes status to rejected."""
        resp = client.put(f"/api/offers/{pending_offer.id}/reject?reason=Too+expensive")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        db_session.refresh(pending_offer)
        assert pending_offer.status == "rejected"
        assert "Too expensive" in (pending_offer.notes or "")

    def test_approve_non_pending_fails(self, client, test_offer):
        """Cannot approve an already active offer."""
        resp = client.put(f"/api/offers/{test_offer.id}/approve")
        assert resp.status_code == 400

    def test_reject_non_pending_fails(self, client, test_offer):
        """Cannot reject an already active offer."""
        resp = client.put(f"/api/offers/{test_offer.id}/reject")
        assert resp.status_code == 400

    def test_approve_not_found(self, client):
        """404 for nonexistent offer."""
        resp = client.put("/api/offers/99999/approve")
        assert resp.status_code == 404

    def test_reject_not_found(self, client):
        """404 for nonexistent offer."""
        resp = client.put("/api/offers/99999/reject")
        assert resp.status_code == 404


# ── Quoted Offer Cross-Reference ─────────────────────────────────────


class TestQuotedOfferBadge:
    """Offers show quoted_on badge when used in a quote."""

    def test_offers_include_quoted_on(self, client, test_requisition, db_session, test_user):
        """Offers used in quotes have quoted_on set."""
        from app.models import Company, CustomerSite

        req_item = test_requisition.requirements[0]
        # Create offer linked to requirement
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Quote Badge Vendor",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.50,
            status="active",
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        co = Company(name="Badge Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", contact_name="X", contact_email="x@test.com")
        db_session.add(site)
        db_session.flush()

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-2026-0099",
            status="sent",
            line_items=[{"offer_id": offer.id, "mpn": "LM317T", "qty": 100}],
            subtotal=100,
            total_cost=50,
            total_margin_pct=50,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        groups = data["groups"]
        # Find the offer in the groups
        found = None
        for g in groups:
            for o in g.get("offers", []):
                if o["id"] == offer.id:
                    found = o
                    break
        assert found is not None
        assert found["quoted_on"] == "Q-2026-0099"

    def test_offers_no_quoted_on_when_not_used(self, client, test_requisition, db_session, test_user):
        """Offers not in any quote have no quoted_on."""
        req_item = test_requisition.requirements[0]
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="No Quote Vendor",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.50,
            status="active",
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        groups = data["groups"]
        found = None
        for g in groups:
            for o in g.get("offers", []):
                if o["id"] == offer.id:
                    found = o
                    break
        assert found is not None
        assert found.get("quoted_on") is None


# ── Buy Plan Uses Quote Line Items ───────────────────────────────────


class TestBuyPlanQuoteLineItems:
    """V1 buy plan submission is deprecated and always returns 404."""

    def test_buy_plan_submit_returns_404(self, client, test_requisition, test_offer, db_session, test_user):
        """V1 submit_buy_plan always returns 404."""
        from app.models import Company, CustomerSite

        co = Company(name="BP Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", contact_name="X", contact_email="x@bp.com")
        db_session.add(site)
        db_session.flush()

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-2026-0100",
            status="sent",
            line_items=[
                {
                    "offer_id": test_offer.id,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow Electronics",
                    "qty": 1000,
                    "cost_price": 0.50,
                    "lead_time": "2-3 weeks (includes transit)",
                    "condition": "New Surplus",
                }
            ],
            subtotal=1000.00,
            total_cost=500.00,
            total_margin_pct=50.00,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/buy-plan",
            json={
                "offer_ids": [test_offer.id],
                "salesperson_notes": "Rush order",
            },
        )
        assert resp.status_code == 404

    def test_buy_plan_fallback_returns_404(self, client, test_requisition, test_offer, db_session, test_user):
        """V1 submit_buy_plan always returns 404 regardless of quote line_items."""
        from app.models import Company, CustomerSite

        co = Company(name="BP Fallback Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", contact_name="X", contact_email="x@fb.com")
        db_session.add(site)
        db_session.flush()

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-2026-0101",
            status="sent",
            line_items=[],
            subtotal=1000.00,
            total_cost=500.00,
            total_margin_pct=50.00,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/buy-plan",
            json={"offer_ids": [test_offer.id], "salesperson_notes": ""},
        )
        assert resp.status_code == 404


# ── Auto-Parse Email Offers ──────────────────────────────────────────


class TestAutoParseOffers:
    """Auto-creation of draft offers from parsed vendor emails."""

    def test_apply_parsed_result_creates_offers(self, db_session, test_requisition, test_user):
        """_apply_parsed_result creates Offer records from parsed email."""
        req_item = test_requisition.requirements[0]

        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor XYZ",
            vendor_email="sales@xyz.com",
            subject="RE: RFQ LM317T",
            body="We can offer LM317T at $0.55",
            scanned_by_user_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {
            "confidence": 0.85,
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "parts": [
                {
                    "mpn": "LM317T",
                    "status": "quoted",
                    "unit_price": 0.55,
                    "qty_available": 5000,
                    "lead_time": "3 weeks",
                    "condition": "New",
                }
            ],
        }

        from app.email_service import _apply_parsed_result

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        # Check that offers were created
        offers = db_session.query(Offer).filter(Offer.vendor_response_id == vr.id).all()
        assert len(offers) == 1
        assert offers[0].mpn == "LM317T"
        assert offers[0].status == "active"  # confidence 0.85 >= 0.8 threshold
        assert offers[0].source == "email_parse"
        assert offers[0].vendor_name == "Vendor XYZ"
        assert float(offers[0].unit_price) == 0.55
        assert offers[0].requirement_id == req_item.id

    def test_dedup_prevents_duplicate_offers(self, db_session, test_requisition, test_user):
        """Re-parsing same email doesn't create duplicate offers."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor ABC",
            vendor_email="sales@abc.com",
            subject="RE: RFQ",
            body="Offer: LM317T $0.50",
            scanned_by_user_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {
            "confidence": 0.9,
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.50, "qty_available": 1000}],
        }

        from app.email_service import _apply_parsed_result

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()
        count_1 = db_session.query(Offer).filter(Offer.vendor_response_id == vr.id).count()

        # Parse again
        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()
        count_2 = db_session.query(Offer).filter(Offer.vendor_response_id == vr.id).count()

        assert count_1 == count_2 == 1

    def test_low_confidence_skips_offer_creation(self, db_session, test_requisition, test_user):
        """Confidence < 0.5 doesn't create offers."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Low Conf",
            vendor_email="sales@low.com",
            subject="RE: RFQ",
            body="Maybe?",
            scanned_by_user_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {
            "confidence": 0.3,
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 1.00, "qty_available": 100}],
        }

        from app.email_service import _apply_parsed_result

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        offers = db_session.query(Offer).filter(Offer.vendor_response_id == vr.id).all()
        assert len(offers) == 0

    def test_creates_notification_for_pending_offer(self, db_session, test_requisition, test_user):
        """Auto-parse creates an offer_pending_review ActivityLog entry."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Notif Vendor",
            vendor_email="sales@notif.com",
            subject="RE: RFQ",
            body="Offer for LM317T",
            scanned_by_user_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {
            "confidence": 0.8,
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.45, "qty_available": 2000}],
        }

        from app.email_service import _apply_parsed_result

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        notif = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.activity_type == "offer_pending_review",
                ActivityLog.requisition_id == test_requisition.id,
            )
            .first()
        )
        assert notif is not None
        assert "LM317T" in notif.subject
        assert "Notif Vendor" in notif.subject


class TestOffersListFields:
    """GET /api/requisitions/{id}/offers returns all new fields."""

    def test_offers_include_updated_fields(self, client, test_requisition, db_session, test_user):
        """updated_at, updated_by, entered_by_id are in response."""
        req_item = test_requisition.requirements[0]
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Field Test Vendor",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.50,
            status="active",
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        # Make an edit to set updated_at
        client.put(f"/api/offers/{offer.id}", json={"notes": "test"})

        resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        found = None
        for g in data["groups"]:
            for o in g.get("offers", []):
                if o["id"] == offer.id:
                    found = o
                    break
        assert found is not None
        assert "updated_at" in found
        assert "updated_by" in found
        assert "entered_by_id" in found
        assert found["updated_at"] is not None
        assert found["updated_by"] == "Test Buyer"


# ── ChangeLog Model ─────────────────────────────────────────────────


class TestChangeLogModel:
    """ChangeLog model creation and querying."""

    def test_changelog_model_fields(self, db_session, test_user):
        """ChangeLog can be created with all fields."""
        cl = ChangeLog(
            entity_type="offer",
            entity_id=1,
            user_id=test_user.id,
            field_name="vendor_name",
            old_value="Old",
            new_value="New",
        )
        db_session.add(cl)
        db_session.commit()
        db_session.refresh(cl)
        assert cl.id is not None
        assert cl.entity_type == "offer"
        assert cl.created_at is not None

    def test_changelog_user_relationship(self, db_session, test_user):
        """ChangeLog user relationship loads correctly."""
        cl = ChangeLog(
            entity_type="requirement",
            entity_id=1,
            user_id=test_user.id,
            field_name="target_qty",
            old_value="100",
            new_value="200",
        )
        db_session.add(cl)
        db_session.commit()
        db_session.refresh(cl)
        assert cl.user is not None
        assert cl.user.name == "Test Buyer"


# ── Offer Model New Fields ───────────────────────────────────────────


class TestOfferModelFields:
    """New fields on Offer model."""

    def test_offer_approval_fields(self, db_session, test_requisition, test_user):
        """Offer has approved_by_id and approved_at fields."""
        o = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test",
            mpn="TEST123",
            status="pending_review",
            approved_by_id=test_user.id,
            approved_at=datetime.now(timezone.utc),
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        assert o.approved_by_id == test_user.id
        assert o.approved_at is not None

    def test_offer_updated_fields(self, db_session, test_requisition, test_user):
        """Offer has updated_at and updated_by_id fields."""
        o = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test",
            mpn="TEST456",
            status="active",
            updated_at=datetime.now(timezone.utc),
            updated_by_id=test_user.id,
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        assert o.updated_at is not None
        assert o.updated_by_id == test_user.id
        assert o.updated_by is not None


# ── Requisition Updated Fields ───────────────────────────────────────


class TestRequisitionUpdatedFields:
    """New updated_at/updated_by_id fields on Requisition."""

    def test_requisition_updated_fields(self, db_session, test_user):
        """Requisition has updated_at and updated_by_id."""
        req = Requisition(
            name="REQ-UPD",
            status="active",
            created_by=test_user.id,
            updated_at=datetime.now(timezone.utc),
            updated_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)
        assert req.updated_at is not None
        assert req.updated_by is not None
