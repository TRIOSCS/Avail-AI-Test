"""
tests/test_coverage_gaps.py -- Tests targeting uncovered lines in admin, crm, enrichment, requisitions.

Called by: pytest
Depends on: conftest.py fixtures
"""

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Offer,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    SystemConfig,
    User,
    VendorCard,
    VendorContact,
)
from app.rate_limit import limiter


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_admin
    app.dependency_overrides[require_user] = _override_admin

    limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ====== admin.py lines 549, 583-626, 637-663, 671-673 ======


class TestAdminTeamsChannels:
    def test_list_teams_channels_no_token(self, admin_client, db_session, admin_user):
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None):
            resp = admin_client.get("/api/admin/teams/channels")
        assert resp.status_code == 400

    def test_list_teams_channels_graph_error(self, admin_client, db_session, admin_user):
        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(return_value={"error": {"message": "Unauthorized"}})
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = admin_client.get("/api/admin/teams/channels")
        assert resp.status_code == 502

    def test_list_teams_channels_success(self, admin_client, db_session, admin_user):
        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(
            side_effect=[
                {"value": [{"id": "team-1", "displayName": "Sales Team"}]},
                {
                    "value": [
                        {"id": "ch-1", "displayName": "General", "membershipType": "standard"},
                        {"id": "ch-2", "displayName": "Alerts", "membershipType": "standard"},
                    ]
                },
            ]
        )
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = admin_client.get("/api/admin/teams/channels")
        assert resp.status_code == 200
        data = resp.json()
        assert "channels" in data
        assert len(data["channels"]) == 2

    def test_list_teams_channels_exception_in_fetch(self, admin_client, db_session, admin_user):
        mock_gc = AsyncMock()

        async def _side_effect(url, **kwargs):
            if "joinedTeams" in url:
                return {
                    "value": [
                        {"id": "team-1", "displayName": "Team 1"},
                        {"id": "team-2", "displayName": "Team 2"},
                    ]
                }
            if "team-1" in url:
                raise RuntimeError("Network error")
            return {"value": [{"id": "ch-1", "displayName": "General", "membershipType": "standard"}]}

        mock_gc.get_json = AsyncMock(side_effect=_side_effect)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = admin_client.get("/api/admin/teams/channels")
        assert resp.status_code == 200
        assert len(resp.json()["channels"]) == 1


class TestAdminTeamsTestPost:
    def test_teams_test_post_no_channel(self, admin_client, db_session):
        with patch("app.services.teams._get_teams_config", return_value=(None, None, False)):
            resp = admin_client.post("/api/admin/teams/test")
        assert resp.status_code == 400

    def test_teams_test_post_no_token(self, admin_client, db_session):
        with (
            patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
        ):
            resp = admin_client.post("/api/admin/teams/test")
        assert resp.status_code == 400

    def test_teams_test_post_failure(self, admin_client, db_session, admin_user):
        with (
            patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.services.teams._make_card", return_value={"type": "AdaptiveCard"}),
            patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=False),
        ):
            resp = admin_client.post("/api/admin/teams/test")
        assert resp.status_code == 502

    def test_teams_test_post_success(self, admin_client, db_session, admin_user):
        with (
            patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.services.teams._make_card", return_value={"type": "AdaptiveCard"}),
            patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True),
        ):
            resp = admin_client.post("/api/admin/teams/test")
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"


class TestAdminUpsertConfig:
    def test_upsert_config_creates_new_row(self, admin_client, db_session, admin_user):
        resp = admin_client.post(
            "/api/admin/teams/config",
            json={
                "team_id": "upsert-new-team",
                "channel_id": "upsert-new-ch",
                "enabled": True,
                "channel_name": "TestChannel",
            },
        )
        assert resp.status_code == 200
        row = db_session.query(SystemConfig).filter(SystemConfig.key == "teams_channel_name").first()
        assert row is not None
        assert row.value == "TestChannel"

    def test_upsert_config_updates_existing_row(self, admin_client, db_session, admin_user):
        admin_client.post("/api/admin/teams/config", json={"team_id": "old", "channel_id": "old", "enabled": False})
        resp = admin_client.post(
            "/api/admin/teams/config", json={"team_id": "new", "channel_id": "new", "enabled": True}
        )
        assert resp.status_code == 200
        row = db_session.query(SystemConfig).filter(SystemConfig.key == "teams_team_id").first()
        assert row.value == "new"


class TestAdminSetTeamsConfigLine549:
    def test_teams_config_infer_enabled(self, admin_client, db_session):
        db_session.add(SystemConfig(key="teams_team_id", value="team-123"))
        db_session.add(SystemConfig(key="teams_channel_id", value="ch-456"))
        db_session.commit()
        resp = admin_client.get("/api/admin/teams/config")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True


# ====== crm.py lines 442-491, 1021-1022, 1329-1330, 1878 ======


class TestCrmCompanyAutoEnrich:
    def test_create_company_with_domain_triggers_enrichment(self, client, db_session):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Test Corp", "testcorp.com"),
            ),
            patch("app.routers.crm.companies.get_credential_cached", return_value="fake-key"),
            patch(
                "app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value={"industry": "Electronics"}
            ),
            patch("app.enrichment_service.apply_enrichment_to_company"),
            patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
            patch("app.database.SessionLocal", return_value=db_session),
        ):
            resp = client.post("/api/companies", json={"name": "Test Corp", "domain": "testcorp.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrich_triggered"] is True
        assert "id" in data

    def test_create_company_without_credential_no_enrichment(self, client, db_session):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("NoCred", "nocred.com"),
            ),
            patch("app.routers.crm.companies.get_credential_cached", return_value=None),
        ):
            resp = client.post("/api/companies", json={"name": "NoCred", "domain": "nocred.com"})
        assert resp.status_code == 200
        assert resp.json()["enrich_triggered"] is False

    def test_create_company_no_domain_no_enrichment(self, client, db_session):
        with patch(
            "app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("NoDomain", "")
        ):
            resp = client.post("/api/companies", json={"name": "NoDomain"})
        assert resp.status_code == 200
        assert resp.json()["enrich_triggered"] is False


class TestCrmCustomerImportError:
    def test_import_customers_row_error(self, admin_client, db_session, admin_user):
        """Customer import catches per-row exceptions and reports them."""
        # Pass a row with an extremely long site_name that would cause an error
        # when the DB tries to process it, or mock query to raise
        original_query = db_session.query
        call_count = [0]

        def _failing_query(*args, **kwargs):
            result = original_query(*args, **kwargs)
            # Make the Company query for the second row fail
            call_count[0] += 1
            if call_count[0] == 3:  # Third query is for second row's Company lookup
                raise Exception("Simulated lookup error")
            return result

        with patch.object(db_session, "query", side_effect=_failing_query):
            resp = admin_client.post(
                "/api/customers/import",
                json=[
                    {"company_name": "Good Co", "site_name": "HQ"},
                    {"company_name": "Bad Co", "site_name": "Office"},
                ],
            )
        assert resp.status_code == 200
        result = resp.json()
        assert len(result["errors"]) >= 1
        assert "Row" in result["errors"][0]


class TestCrmCompetitiveQuoteAlert:
    def test_create_offer_competitive_triggers_alert(self, client, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        existing_offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Expensive Vendor",
            mpn="LM317T",
            qty_available=1000,
            unit_price=10.00,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_offer)
        test_requisition.status = "active"
        db_session.commit()

        with (
            patch("app.routers.crm.offers.get_credential_cached", return_value=None),
            patch("app.services.teams.send_competitive_quote_alert", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/offers",
                json={
                    "vendor_name": "Cheap Vendor",
                    "mpn": "LM317T",
                    "requirement_id": req_item.id,
                    "qty_available": 500,
                    "unit_price": 2.00,
                },
            )
        assert resp.status_code == 200
        activities = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "competitive_quote").all()
        assert len(activities) >= 1

    def test_create_offer_competitive_alert_exception_handled(self, client, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        existing_offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Expensive Vendor2",
            mpn="LM317T",
            qty_available=1000,
            unit_price=10.00,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_offer)
        test_requisition.status = "active"
        db_session.commit()

        with (
            patch("app.routers.crm.offers.get_credential_cached", return_value=None),
            patch(
                "app.services.teams.send_competitive_quote_alert",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Teams down"),
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/offers",
                json={
                    "vendor_name": "Another Vendor",
                    "mpn": "LM317T",
                    "requirement_id": req_item.id,
                    "qty_available": 500,
                    "unit_price": 2.00,
                },
            )
        assert resp.status_code == 200
        assert "id" in resp.json()


class TestCrmQuoteEmailFmtPrice:
    def test_preview_quote_with_zero_sell_price(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-ZERO",
            status="draft",
            line_items=[
                {"mpn": "LM317T", "qty": 100, "sell_price": 0, "cost_price": 3.00},
                {"mpn": "NE555P", "qty": 50, "sell_price": None, "cost_price": 1.00},
            ],
            subtotal=0,
            total_cost=400.0,
            total_margin_pct=0,
            validity_days=30,
            payment_terms="Net 30",
            shipping_terms="FOB",
            notes="Test note",
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()
        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        html = resp.json()["html"]
        assert "\u2014" in html


# ====== enrichment.py lines 440, 461, 465, 496, 499, 502, 505, 512, 525-528, 596-637, 642-645 ======


class TestEnrichmentBackfillEmails:
    def test_backfill_activity_log_contacts(self, admin_client, db_session, test_vendor_card):
        al = ActivityLog(
            user_id=1,
            activity_type="email_sent",
            channel="email",
            vendor_card_id=test_vendor_card.id,
            contact_email="vendor@example.com",
            contact_name="John Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(al)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["activity_log_created"] >= 1

    def test_backfill_skips_existing_contacts(self, admin_client, db_session, test_vendor_card):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id, email="existing@example.com", source="manual", confidence=90
        )
        db_session.add(vc)
        al = ActivityLog(
            user_id=1,
            activity_type="email_sent",
            channel="email",
            vendor_card_id=test_vendor_card.id,
            contact_email="existing@example.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(al)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["activity_log_created"] == 0

    def test_backfill_skips_invalid_emails(self, admin_client, db_session, test_vendor_card):
        al = ActivityLog(
            user_id=1,
            activity_type="email_sent",
            channel="email",
            vendor_card_id=test_vendor_card.id,
            contact_email="not-an-email",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(al)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["activity_log_created"] == 0

    def test_backfill_vendor_card_emails(self, admin_client, db_session):
        card = VendorCard(
            normalized_name="test vendor backfill",
            display_name="Test Vendor Backfill",
            emails=["sales@testvendor.com", "info@testvendor.com"],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["vendor_card_created"] >= 2

    def test_backfill_vendor_card_skips_invalid_email(self, admin_client, db_session):
        card = VendorCard(
            normalized_name="bad email vendor",
            display_name="Bad Email Vendor",
            emails=["nope", ""],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["vendor_card_created"] == 0

    def test_backfill_vendor_card_non_list_emails(self, admin_client, db_session):
        card = VendorCard(
            normalized_name="string email vendor",
            display_name="String Email Vendor",
            emails="not-a-list",
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["vendor_card_created"] == 0

    def test_backfill_brokerbin_sightings(self, admin_client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        card = VendorCard(
            normalized_name="bb vendor",
            display_name="BB Vendor",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="BB Vendor",
            vendor_email="sales@bbvendor.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["brokerbin_created"] >= 1

    def test_backfill_brokerbin_skips_no_vendor_name(self, admin_client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="",
            vendor_email="orphan@example.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["brokerbin_created"] == 0

    def test_backfill_brokerbin_skips_no_matching_card(self, admin_client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Unknown Vendor XYZ",
            vendor_email="sales@unknown.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["brokerbin_created"] == 0

    def test_backfill_brokerbin_skips_invalid_email(self, admin_client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        card = VendorCard(
            normalized_name="invalid email bb",
            display_name="Invalid Email BB",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Invalid Email BB",
            vendor_email="not-email",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["brokerbin_created"] == 0

    def test_backfill_brokerbin_skips_existing_contact(self, admin_client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        card = VendorCard(
            normalized_name="existing bb",
            display_name="Existing BB",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        vc = VendorContact(vendor_card_id=card.id, email="already@bbvendor.com", source="manual", confidence=90)
        db_session.add(vc)
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Existing BB",
            vendor_email="already@bbvendor.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["brokerbin_created"] == 0

    def test_backfill_commit_failure(self, admin_client, db_session, test_vendor_card):
        with patch.object(db_session, "commit", side_effect=SQLAlchemyError("DB error")):
            resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 500


class TestEnrichmentDeepScan:
    def _setup_user(self, admin_user, db_session):
        admin_user.m365_connected = True
        admin_user.access_token = "fake-token"
        db_session.commit()

    def test_deep_scan_creates_contacts(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="scan vendor",
            display_name="Scan Vendor",
            domain="scanvendor.com",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        mock_results = {
            "messages_scanned": 100,
            "per_domain": {"scanvendor.com": {"emails": ["contact@scanvendor.com", "sales@scanvendor.com"]}},
        }
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.merge_emails_into_card"),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] >= 1

    def test_deep_scan_matches_by_normalized_name(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="fallback",
            display_name="Fallback Inc",
            domain=None,
            website=None,
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        mock_results = {"messages_scanned": 50, "per_domain": {"fallback.com": {"emails": ["info@fallback.com"]}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.merge_emails_into_card"),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] >= 1

    def test_deep_scan_no_matching_card(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        mock_results = {"messages_scanned": 20, "per_domain": {"nocard.com": {"emails": ["info@nocard.com"]}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] == 0

    def test_deep_scan_skips_empty_emails(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="empty emails",
            display_name="Empty Emails",
            domain="emptyemails.com",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        mock_results = {"messages_scanned": 10, "per_domain": {"emptyemails.com": {"emails": []}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] == 0

    def test_deep_scan_skips_existing_contacts(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="dup check vendor",
            display_name="Dup Check Vendor",
            domain="dupcheck.com",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        vc = VendorContact(vendor_card_id=card.id, email="existing@dupcheck.com", source="manual", confidence=90)
        db_session.add(vc)
        db_session.commit()
        mock_results = {"messages_scanned": 10, "per_domain": {"dupcheck.com": {"emails": ["existing@dupcheck.com"]}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.merge_emails_into_card"),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] == 0

    def test_deep_scan_skips_invalid_email(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="invalid deep",
            display_name="Invalid Deep",
            domain="invaliddeep.com",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        mock_results = {"messages_scanned": 10, "per_domain": {"invaliddeep.com": {"emails": ["not-an-email", ""]}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.merge_emails_into_card"),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] == 0

    def test_deep_scan_commit_failure(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        mock_results = {"messages_scanned": 5, "per_domain": {}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch.object(db_session, "commit", side_effect=SQLAlchemyError("Commit failed")),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 500

    def test_deep_scan_user_not_found(self, admin_client, db_session):
        resp = admin_client.post("/api/enrichment/deep-email-scan/99999")
        assert resp.status_code == 404

    def test_deep_scan_user_not_m365(self, admin_client, db_session, admin_user):
        admin_user.m365_connected = False
        db_session.commit()
        resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 400

    def test_deep_scan_no_token(self, admin_client, db_session, admin_user):
        admin_user.m365_connected = True
        admin_user.access_token = None
        db_session.commit()
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 400

    def test_deep_scan_matches_by_website(self, admin_client, db_session, admin_user):
        self._setup_user(admin_user, db_session)
        card = VendorCard(
            normalized_name="web match vendor",
            display_name="Web Match Vendor",
            domain=None,
            website="https://www.webmatch.com/contact",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        mock_results = {"messages_scanned": 30, "per_domain": {"webmatch.com": {"emails": ["info@webmatch.com"]}}}
        mock_miner = AsyncMock()
        mock_miner.deep_scan_inbox = AsyncMock(return_value=mock_results)
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.merge_emails_into_card"),
        ):
            resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] >= 1


# ====== requisitions.py lines 511-551, 685-688, 709-719, 736, 760, 772, 779, 936-937, 1013-1015, 1076, 1111, 1151-1152, 1167 ======


class TestRequisitionClone:
    def test_clone_requisition_basic(self, client, db_session, test_requisition):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "(clone)" in data["name"] or "(copy)" in data["name"]
        assert data["id"] != test_requisition.id

    def test_clone_requisition_copies_requirements(self, client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["NE555P", "LM7805"]
        req_item.firmware = "v2.0"
        req_item.date_codes = "2025+"
        req_item.packaging = "reel"
        req_item.condition = "new"
        req_item.notes = "Important"
        db_session.commit()
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        clone_id = resp.json()["id"]
        cloned_reqs = db_session.query(Requirement).filter(Requirement.requisition_id == clone_id).all()
        assert len(cloned_reqs) == 1
        assert cloned_reqs[0].notes == "Important"

    def test_clone_requisition_not_found(self, client):
        resp = client.post("/api/requisitions/99999/clone")
        assert resp.status_code == 404

    def test_clone_requisition_deduplicates_substitutes(self, client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["NE555P", "NE555P", "ne555p", "LM7805"]
        db_session.commit()
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        clone_id = resp.json()["id"]
        cloned_reqs = db_session.query(Requirement).filter(Requirement.requisition_id == clone_id).all()
        assert len(cloned_reqs) == 1
        subs = cloned_reqs[0].substitutes or []
        assert len(subs) <= 3


class TestRequisitionCreateRequirements:
    def test_add_requirements_with_substitutes_dedup(self, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[
                {"primary_mpn": "ABC123", "target_qty": 100, "substitutes": ["DEF456", "def456", "DEF-456", "GHI789"]}
            ],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) >= 1

    def test_add_requirements_teams_hot_alert(self, client, db_session, test_requisition, test_customer_site):
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        with (
            patch("app.config.settings") as mock_settings,
            patch("app.services.teams.send_hot_requirement_alert", new_callable=AsyncMock) as mock_alert,
        ):
            mock_settings.teams_hot_threshold = 100
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[{"primary_mpn": "EXPENSIVE-001", "target_qty": 1000, "target_price": 1.00}],
            )
        assert resp.status_code == 200

    def test_add_requirements_teams_alert_failure_handled(self, client, db_session, test_requisition):
        """Teams alert failure does not break requirement creation."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[{"primary_mpn": "SAFE-001", "target_qty": 10}],
        )
        assert resp.status_code == 200

    def test_add_requirements_duplicate_detection(self, client, db_session, test_customer_site):
        """Adding MPN that was recently quoted for same customer shows duplicate warning."""
        from app.models import MaterialCard, Requirement, Requisition

        # Create a material card so duplicate detection can match on material_card_id
        mc = MaterialCard(normalized_mpn="dupmpn001", display_mpn="DUP-MPN-001")
        db_session.add(mc)
        db_session.flush()
        # Create first requisition with an MPN linked to the card
        req1 = Requisition(name="First-Req", customer_site_id=test_customer_site.id, status="active", created_by=1)
        db_session.add(req1)
        db_session.flush()
        r1 = Requirement(
            requisition_id=req1.id,
            primary_mpn="DUP-MPN-001",
            normalized_mpn="dupmpn001",
            material_card_id=mc.id,
            target_qty=10,
        )
        db_session.add(r1)
        db_session.commit()
        # Create second requisition for same customer
        req2 = Requisition(name="Second-Req", customer_site_id=test_customer_site.id, status="draft", created_by=1)
        db_session.add(req2)
        db_session.commit()
        # Add same MPN — resolve_material_card finds existing card, triggering duplicate
        resp = client.post(
            f"/api/requisitions/{req2.id}/requirements",
            json=[{"primary_mpn": "DUP-MPN-001", "target_qty": 5}],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1
        assert len(data["duplicates"]) >= 1
        assert data["duplicates"][0]["mpn"] == "DUP-MPN-001"
        assert data["duplicates"][0]["req_id"] == req1.id


class TestRequisitionUploadExtended:
    def test_upload_oversized_file(self, client, test_requisition):
        large_content = b"mpn,qty\n" + b"X" * (10_000_001)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("big.csv", io.BytesIO(large_content), "text/csv")},
        )
        assert resp.status_code == 413

    @patch("app.database.SessionLocal")
    def test_upload_with_sub_columns(self, mock_sl, client, test_requisition):
        csv_bytes = "mpn,qty,sub_1,sub_2\nABC123,100,DEF456,GHI789".encode()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] >= 1

    @patch("app.database.SessionLocal")
    def test_upload_with_empty_mpn_rows(self, mock_sl, client, test_requisition):
        csv_bytes = "mpn,qty\n,100\nABC123,200\n,50".encode()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] >= 1

    @patch("app.database.SessionLocal")
    def test_upload_with_invalid_substitute(self, mock_sl, client, test_requisition):
        csv_bytes = 'mpn,qty,substitutes\nABC123,100,",,,  "'.encode()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] >= 1


class TestRequisitionSearch:
    def test_search_handles_exception_in_requirement(self, client, db_session, test_requisition):
        async def _failing_search(r, db):
            raise RuntimeError("Connector timeout")

        with (
            patch("app.search_service.search_requirement", side_effect=_failing_search),
            patch("app.routers.rfq._enrich_with_vendor_cards"),
        ):
            resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
        assert resp.status_code == 200
        assert "source_stats" in resp.json()


class TestRequisitionSightings:
    def test_get_sightings_with_material_history(self, client, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Test Vendor",
            mpn_matched="LM317T",
            qty_available=500,
            unit_price=0.45,
            source_type="api",
            score=75,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        with (
            patch("app.routers.rfq._enrich_with_vendor_cards"),
            patch("app.search_service._get_material_history", return_value=[]),
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_get_sightings_with_historical_offers(self, client, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        other_req = Requisition(
            name="OTHER-REQ",
            customer_name="Other Co",
            status="offers",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_req)
        db_session.flush()
        hist_offer = Offer(
            requisition_id=other_req.id,
            vendor_name="Historical Vendor",
            mpn="LM317T",
            qty_available=200,
            unit_price=0.55,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(hist_offer)
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Current Vendor",
            mpn_matched="LM317T",
            qty_available=300,
            unit_price=0.50,
            source_type="api",
            score=80,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        with (
            patch("app.routers.rfq._enrich_with_vendor_cards"),
            patch("app.search_service._get_material_history", return_value=[]),
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200


class TestMarkSightingUnavailable:
    def test_mark_sighting_unavailable(self, client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Unavail Vendor",
            mpn_matched="LM317T",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        resp = client.put(f"/api/sightings/{s.id}/unavailable", json={"unavailable": True})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["is_unavailable"] is True

    def test_mark_sighting_unavailable_not_found(self, client):
        resp = client.put("/api/sightings/99999/unavailable", json={"unavailable": True})
        assert resp.status_code == 404

    def test_mark_sighting_unavailable_no_req(self, client, db_session, test_requisition, test_user):
        """Sighting whose requirement is orphaned (req deleted) returns 403."""
        from sqlalchemy import text

        req_item = test_requisition.requirements[0]
        s = Sighting(
            requirement_id=req_item.id,
            vendor_name="Orphan Vendor",
            mpn_matched="LM317T",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        sighting_id = s.id
        req_item_id = req_item.id
        # Disable FK checks via session connection, orphan the requirement
        db_session.execute(text("PRAGMA foreign_keys=OFF"))
        db_session.execute(text(f"UPDATE requirements SET requisition_id = 99999 WHERE id = {req_item_id}"))
        db_session.commit()
        db_session.execute(text("PRAGMA foreign_keys=ON"))
        db_session.expire_all()
        resp = client.put(f"/api/sightings/{sighting_id}/unavailable", json={"unavailable": True})
        assert resp.status_code == 403


class TestImportStockMatching:
    def test_import_stock_matches_requirements(self, client, db_session, test_requisition):
        csv_data = "mpn,qty,price\nLM317T,5000,0.40\nUNMATCHED-XYZ,2000,0.20"
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Stock Vendor"},
            files={"file": ("stock.csv", io.BytesIO(csv_data.encode()), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_rows"] >= 2
        assert data["matched_sightings"] >= 1

    def test_import_stock_matches_substitutes(self, client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["NE555P"]
        db_session.commit()
        csv_data = "mpn,qty,price\nNE555P,3000,0.15"
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Sub Match Vendor"},
            files={"file": ("stock.csv", io.BytesIO(csv_data.encode()), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["matched_sightings"] >= 1

    def test_import_stock_with_extra_columns(self, client, db_session, test_requisition):
        csv_data = "mpn,qty,price,condition,packaging,date_code,lead_time,manufacturer\nLM317T,5000,0.40,new,reel,2025+,14,Texas Instruments"
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Detailed Vendor"},
            files={"file": ("stock.csv", io.BytesIO(csv_data.encode()), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["matched_sightings"] >= 1

    def test_import_stock_no_match(self, client, db_session, test_requisition):
        csv_data = "mpn,qty,price\nXYZ999,100,1.00"
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "No Match Vendor"},
            files={"file": ("stock.csv", io.BytesIO(csv_data.encode()), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["matched_sightings"] == 0
