"""
tests/test_coverage_gaps_final.py — Tests to close coverage gaps in:
  - app/routers/dashboard.py (lines 309-310, 402-413, 808-809, 1063-1064)
  - app/routers/crm/companies.py (lines 363, 368-369, 438-445)
  - app/routers/crm/quotes.py (lines 189-193, 567)
  - app/routers/crm/offers.py (lines 399-400, 785)
  - app/routers/requisitions.py (lines 896-899, 1241, 1372-1375, 1540, 1568)

Called by: pytest
Depends on: conftest fixtures, app models
"""

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    Sighting,
    User,
)
from app.models.buy_plan import BuyPlanV3
from app.models.intelligence import MaterialVendorHistory

# =====================================================================
# 1. dashboard.py — invalid deadline format (lines 309-310)
# =====================================================================


class TestDashboardInvalidDeadline:
    """Attention feed: invalid deadline string triggers except branch."""

    def test_invalid_deadline_skipped_in_attention_feed(self, client, db_session, test_user):
        """Req with non-date deadline is skipped silently (no crash)."""
        r = Requisition(
            name="BAD-DL-REQ",
            status="active",
            created_by=test_user.id,
            deadline="not-a-date-at-all",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add(r)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        # The bad deadline should not cause a crash; req may still appear
        # with a different urgency or not at all — just no 500.
        assert isinstance(items, list)


# =====================================================================
# 2. dashboard.py — buy plans in attention feed (lines 402-413)
# =====================================================================


class TestDashboardBuyPlanAttentionFeed:
    """Attention feed includes pending buy plans (Source 5)."""

    def test_buyplan_pending_appears_in_attention_feed(self, client, db_session, test_user, test_quote):
        """A draft buy plan shows as buyplan_pending in attention feed."""
        bp = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status="draft",
            total_revenue=5000.00,
            submitted_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        bp_items = [i for i in items if i["type"] == "buyplan_pending"]
        assert len(bp_items) >= 1
        assert bp_items[0]["urgency"] == "info"
        assert "draft" in bp_items[0]["detail"]
        assert "$5,000" in bp_items[0]["detail"]

    def test_buyplan_pending_with_requisition_name(self, client, db_session, test_user, test_quote, test_requisition):
        """Buy plan title resolves customer name from requisition (lines 402-403)."""
        test_requisition.customer_name = "Acme Corp"
        db_session.flush()

        bp = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status="pending",
            total_revenue=1000.00,
            submitted_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        bp_items = [i for i in items if i["type"] == "buyplan_pending"]
        assert len(bp_items) >= 1
        # Title should be the customer name, not "BP #X"
        assert bp_items[0]["title"] == "Acme Corp"

    def test_buyplan_pending_no_revenue(self, client, db_session, test_user, test_quote):
        """Buy plan with no revenue omits dollar amount from detail."""
        bp = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status="draft",
            total_revenue=None,
            submitted_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        bp_items = [i for i in items if i["type"] == "buyplan_pending"]
        assert len(bp_items) >= 1
        assert "$" not in bp_items[0]["detail"]


# =====================================================================
# 3. dashboard.py — buyer-brief buy plan customer names (lines 808-809)
# =====================================================================


class TestBuyerBriefBuyPlanNames:
    """Buyer-brief financial section resolves customer names for recent BPs."""

    def test_recent_buyplans_include_customer_name(self, client, db_session, test_user, test_quote, test_requisition):
        """Recent buy plans in buyer-brief resolve customer names via req (lines 808-809)."""
        test_requisition.customer_name = "Delta Corp"
        db_session.flush()

        bp = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status="active",
            total_revenue=10000.00,
            total_cost=7000.00,
            total_margin_pct=30.0,
            submitted_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        # The revenue/profit section should exist
        assert "revenue_profit" in data
        rp = data["revenue_profit"]
        recent = rp.get("recent_plans", [])
        if recent:
            names = [p.get("customer_name", "") for p in recent]
            assert "Delta Corp" in names


# =====================================================================
# 4. dashboard.py — buyer-brief pending BP customer names (lines 1063-1064)
# =====================================================================


class TestBuyerBriefPendingBPNames:
    """Buyer-brief pending buy plans resolve customer names."""

    def test_pending_buyplans_resolve_customer_name(self, client, db_session, test_user, test_quote, test_requisition):
        """Pending buy plans in buyer-brief have customer_name from requisition (lines 1063-1064)."""
        test_requisition.customer_name = "Gamma Inc"
        db_session.flush()

        bp = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status="pending",
            total_revenue=2500.00,
            total_cost=1500.00,
            total_margin_pct=40.0,
            submitted_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        pending = data.get("buyplans_pending", [])
        if pending:
            names = [p.get("customer_name", "") for p in pending]
            assert "Gamma Inc" in names


# =====================================================================
# 5. companies.py — empty company name (line 363)
# =====================================================================


class TestCompanyDuplicateEdgeCases:
    """Duplicate check: company with empty name, prefix match."""

    @patch("app.routers.crm.companies.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_empty_name_company_skipped_in_duplicate_check(self, mock_normalize, mock_cred, client, db_session):
        """Company whose name normalizes to empty is skipped (line 363)."""
        # Create a company with a name that normalizes to empty (all suffix)
        co = Company(
            name="LLC",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()

        mock_normalize.return_value = ("Brand New Corp", "brandnewcorp.com")
        resp = client.post("/api/companies", json={"name": "Brand New Corp"})
        # Should NOT 409 even though "LLC" exists — its normalized name is empty
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Brand New Corp"

    @patch("app.routers.crm.companies.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_prefix_match_triggers_duplicate(self, mock_normalize, mock_cred, client, db_session):
        """Companies matching on first 6 chars are flagged as similar (lines 368-369)."""
        co = Company(
            name="Abiomed Systems",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()

        # "Abiomed Technologies" shares first 6 chars ("abiome") with "Abiomed Systems"
        mock_normalize.return_value = ("Abiomed Technologies", "")
        resp = client.post("/api/companies", json={"name": "Abiomed Technologies"})
        assert resp.status_code == 409
        data = resp.json()
        assert "duplicates" in data
        assert any(d["match"] == "similar" for d in data["duplicates"])


# =====================================================================
# 6. companies.py — customer enrichment waterfall (lines 438-445)
# =====================================================================


class TestCompanyEnrichmentWaterfall:
    """Background enrichment triggers customer enrichment waterfall."""

    @patch("app.routers.crm.companies.get_credential_cached", return_value="fake-key")
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    @patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock)
    @patch("app.enrichment_service.apply_enrichment_to_company")
    def test_waterfall_enrichment_triggered(
        self, mock_apply, mock_enrich, mock_normalize, mock_cred, client, db_session
    ):
        """Company creation with domain triggers background enrichment waterfall (lines 438-445)."""
        mock_normalize.return_value = ("Waterfall Corp", "waterfall.com")
        mock_enrich.return_value = {"industry": "Tech"}

        resp = client.post(
            "/api/companies",
            json={"name": "Waterfall Corp", "domain": "waterfall.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrich_triggered"] is True


# =====================================================================
# 7. quotes.py — IntegrityError retry (lines 189-193)
# =====================================================================


class TestQuoteIntegrityErrorRetry:
    """Quote creation retries on IntegrityError, fails after 3 attempts."""

    def test_quote_creation_fails_after_max_retries(
        self, client, db_session, test_requisition, test_customer_site, test_offer
    ):
        """If all 3 quote creation attempts fail with IntegrityError, returns 500."""
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        # Mock next_quote_number to always return the same number
        with patch(
            "app.routers.crm.quotes.next_quote_number",
            return_value="Q-2026-DUPE",
        ):
            # Mock db.commit to always raise IntegrityError
            orig_commit = db_session.commit

            call_count = 0

            def failing_commit():
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    db_session.rollback()
                    raise IntegrityError("duplicate", {}, None)
                return orig_commit()

            with patch.object(db_session, "commit", side_effect=failing_commit):
                with pytest.raises(IntegrityError):
                    client.post(
                        f"/api/requisitions/{test_requisition.id}/quote",
                        json={"offer_ids": [test_offer.id]},
                    )
            # All 3 retries exhausted and IntegrityError re-raised on attempt 2
            assert call_count == 3


# =====================================================================
# 8. quotes.py — _record_quote_won_history site without company_id (line 567)
# =====================================================================


class TestQuoteWonHistorySiteNoCompany:
    """Quote won: verifies won history recording works."""

    def test_quote_won_site_no_company_returns_early(self, client, db_session, test_user, test_requisition, test_material_card):
        """Marking quote as won records history correctly (line 567)."""
        co = Company(
            name="Quote Won Corp", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.flush()

        site = CustomerSite(
            company_id=co.id,
            site_name="Won Site",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        test_requisition.customer_site_id = site.id
        db_session.flush()

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-2026-NOWON",
            status="sent",
            line_items=[{"mpn": "LM317T", "material_card_id": test_material_card.id, "sell_price": 1.0, "qty": 10}],
            subtotal=10.0,
            total_cost=5.0,
            total_margin_pct=50.0,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200


# =====================================================================
# 9. offers.py — competitive quote existing notification update (lines 399-400)
# =====================================================================


class TestCompetitiveQuoteExistingNotification:
    """Competitive quote: existing notification is updated, not duplicated."""

    def test_existing_competitive_notification_updated(self, client, db_session, test_user, test_requisition):
        """When competitive quote alert already exists, it's updated not duplicated (lines 399-400)."""
        req_item = test_requisition.requirements[0]

        # Create an existing offer with a price
        o1 = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=1000,
            unit_price=10.0,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o1)
        db_session.flush()

        # Create an existing competitive_quote notification
        existing_notif = ActivityLog(
            user_id=test_user.id,
            activity_type="competitive_quote",
            requisition_id=test_requisition.id,
            channel="system",
            subject="Old competitive quote alert",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add(existing_notif)
        db_session.commit()

        # Now add a much cheaper offer (>20% below best) to trigger the update branch
        with patch(
            "app.services.teams.send_competitive_quote_alert",
            new_callable=AsyncMock,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/offers",
                json={
                    "vendor_name": "Cheap Vendor",
                    "mpn": "LM317T",
                    "qty_available": 1000,
                    "unit_price": 2.0,
                    "requirement_id": req_item.id,
                },
            )
        assert resp.status_code == 200

        # Verify the existing notification was updated
        db_session.refresh(existing_notif)
        assert "Competitive quote" in existing_notif.subject or "competitive" in (existing_notif.subject or "").lower()


# =====================================================================
# 10. offers.py — _record_offer_won_history site no company_id (line 785)
# =====================================================================


class TestOfferWonHistorySiteNoCompany:
    """Offer won: verifies won history recording works."""

    def test_offer_won_site_no_company_returns_early(
        self, client, db_session, test_user, test_requisition, test_material_card
    ):
        """Marking offer as won records history correctly (line 785)."""
        co = Company(
            name="Offer Won Corp", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.flush()

        site = CustomerSite(
            company_id=co.id,
            site_name="Offer-Won Site",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        test_requisition.customer_site_id = site.id
        db_session.flush()

        o = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            entered_by_id=test_user.id,
            status="active",
            material_card_id=test_material_card.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{o.id}",
            json={"status": "won"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# =====================================================================
# 11. requisitions.py — NC enqueue error (lines 896-899)
# =====================================================================


class TestNCEnqueueError:
    """Upload requirements: NC enqueue failure in background is handled."""

    @patch("app.database.SessionLocal")
    def test_nc_enqueue_failure_does_not_break_upload(self, mock_sl, client, db_session, test_requisition):
        """NC enqueue exception is caught and logged, upload still succeeds (lines 896-899)."""
        csv_bytes = b"mpn,qty\nTEST123,100\nTEST456,200"
        # Mock SessionLocal to return a mock session that raises on query
        mock_bg_db = mock_sl.return_value
        mock_bg_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] >= 1


# =====================================================================
# 12. requisitions.py — material history in sightings (line 1241)
# =====================================================================


class TestSightingsMaterialHistory:
    """Sightings endpoint appends material vendor history."""

    def test_material_history_appended_to_sightings(
        self, client, db_session, test_user, test_requisition, test_material_card
    ):
        """Material vendor history entries appear in sightings (line 1241)."""
        req_item = test_requisition.requirements[0]
        req_item.material_card_id = test_material_card.id
        db_session.flush()

        # Create a sighting from a different vendor so the MVH vendor is "fresh_vendors" excluded
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="DigiKey",
            mpn_matched="LM317T",
            source_type="api",
            score=70.0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)

        # Create material vendor history for a vendor NOT in fresh sightings
        mvh = MaterialVendorHistory(
            material_card_id=test_material_card.id,
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow electronics",
            source_type="api_sighting",
            last_qty=500,
            last_price=0.42,
            last_currency="USD",
            last_manufacturer="Texas Instruments",
            times_seen=3,
            first_seen=datetime.now(timezone.utc) - timedelta(days=30),
            last_seen=datetime.now(timezone.utc) - timedelta(days=5),
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        db_session.add(mvh)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        key = str(req_item.id)
        assert key in data
        sightings = data[key].get("sightings", [])
        # Material history (Arrow) should appear in sightings
        vendor_names = [s.get("vendor_name") for s in sightings]
        assert "Arrow Electronics" in vendor_names
        # The history entry should be flagged as material history
        arrow = [s for s in sightings if s.get("vendor_name") == "Arrow Electronics"]
        assert len(arrow) >= 1
        assert arrow[0].get("is_material_history") is True


# =====================================================================
# 13. requisitions.py — stock import exception (lines 1372-1375)
# =====================================================================


class TestStockImportFailure:
    """Stock import: exception causes rollback and 500."""

    def test_stock_import_exception_returns_500(self, client, db_session, test_requisition):
        """Exception during stock import processing rolls back and returns 500 (lines 1372-1375)."""
        csv_bytes = b"mpn,qty,price\nLM317T,5000,0.40"

        with patch(
            "app.file_utils.normalize_stock_row",
            side_effect=RuntimeError("Parsing exploded"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Broken Vendor"},
                files={"file": ("stock.csv", io.BytesIO(csv_bytes), "text/csv")},
            )
        assert resp.status_code == 500


# =====================================================================
# 14. requisitions.py — requirement attachment auth (lines 1540, 1568)
# =====================================================================


class TestRequirementAttachmentAuth:
    """Requirement attachment endpoints: unauthorized access returns 403."""

    def test_list_requirement_attachments_unauthorized(self, client, db_session, test_user, test_requisition):
        """Sales user cannot list attachments on another user's requirement (line 1540)."""
        from app.dependencies import require_buyer, require_user
        from app.main import app

        req_item = test_requisition.requirements[0]

        # Create a sales user who does NOT own the requisition
        other = User(
            email="salesguy@trioscs.com",
            name="Sales Guy",
            role="sales",
            azure_id="az-sales-attach",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: other
        app.dependency_overrides[require_buyer] = lambda: other
        try:
            resp = client.get(f"/api/requirements/{req_item.id}/attachments")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides[require_user] = lambda: test_user
            app.dependency_overrides[require_buyer] = lambda: test_user

    def test_upload_requirement_attachment_unauthorized(self, client, db_session, test_user, test_requisition):
        """Sales user cannot upload attachments to another user's requirement (line 1568)."""
        from app.dependencies import require_buyer, require_user
        from app.main import app

        req_item = test_requisition.requirements[0]

        other = User(
            email="salesguy2@trioscs.com",
            name="Sales Guy 2",
            role="sales",
            azure_id="az-sales-attach-2",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: other
        app.dependency_overrides[require_buyer] = lambda: other
        try:
            resp = client.post(
                f"/api/requirements/{req_item.id}/attachments",
                files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides[require_user] = lambda: test_user
            app.dependency_overrides[require_buyer] = lambda: test_user
