import os

os.environ["TESTING"] = "1"
"""test_ai_router_new_coverage.py — Additional coverage for app/routers/ai.py."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from tests.conftest import engine

_ = engine  # ensure engine import side-effects run

from app.models import ProspectContact, Requirement, Requisition, VendorResponse

# ── _ai_enabled helper ────────────────────────────────────────────────


class TestAiEnabledHelper:
    def test_flag_off_returns_false(self):
        from types import SimpleNamespace

        from app.routers.ai import _ai_enabled

        user = SimpleNamespace(email="user@test.com")
        s = SimpleNamespace(ai_features_enabled="off", admin_emails=[])
        with patch("app.routers.ai.settings", s):
            assert _ai_enabled(user) is False

    def test_flag_all_returns_true(self):
        from types import SimpleNamespace

        from app.routers.ai import _ai_enabled

        user = SimpleNamespace(email="user@test.com")
        s = SimpleNamespace(ai_features_enabled="all", admin_emails=[])
        with patch("app.routers.ai.settings", s):
            assert _ai_enabled(user) is True

    def test_mike_only_user_in_list_returns_true(self):
        from types import SimpleNamespace

        from app.routers.ai import _ai_enabled

        user = SimpleNamespace(email="mike@trioscs.com")
        s = SimpleNamespace(ai_features_enabled="mike_only", admin_emails=["mike@trioscs.com"])
        with patch("app.routers.ai.settings", s):
            assert _ai_enabled(user) is True

    def test_mike_only_user_not_in_list_returns_false(self):
        from types import SimpleNamespace

        from app.routers.ai import _ai_enabled

        user = SimpleNamespace(email="other@test.com")
        s = SimpleNamespace(ai_features_enabled="mike_only", admin_emails=["mike@trioscs.com"])
        with patch("app.routers.ai.settings", s):
            assert _ai_enabled(user) is False


# ── parse-response endpoint ──────────────────────────────────────────


class TestParseResponse:
    def _make_vendor_response(self, db: Session, req_id: int | None = None) -> VendorResponse:
        vr = VendorResponse(
            vendor_name="Arrow Electronics",
            vendor_email="sales@arrow.com",
            subject="Re: RFQ LM317T",
            body="We have LM317T at $0.50 each qty 1000",
            requisition_id=req_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(vr)
        db.commit()
        db.refresh(vr)
        return vr

    def test_not_found_returns_404(self, client):
        with patch("app.routers.ai.settings") as ms:
            ms.ai_features_enabled = "all"
            resp = client.post("/api/ai/parse-response/99999")
        assert resp.status_code == 404

    def test_ai_disabled_returns_403(self, client, db_session):
        vr = self._make_vendor_response(db_session)
        with patch("app.routers.ai.settings") as ms:
            ms.ai_features_enabled = "off"
            resp = client.post(f"/api/ai/parse-response/{vr.id}")
        assert resp.status_code == 403

    def test_parser_returns_none(self, client, db_session):
        vr = self._make_vendor_response(db_session)
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.response_parser.parse_vendor_response", new_callable=AsyncMock) as mock_parse,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = None
            resp = client.post(f"/api/ai/parse-response/{vr.id}")
        assert resp.status_code == 200
        assert resp.json()["parsed"] is False

    def test_parser_returns_result(self, client, db_session, test_requisition):
        vr = self._make_vendor_response(db_session, req_id=test_requisition.id)
        fake_result = {
            "overall_classification": "quote_provided",
            "confidence": 0.9,
            "parts": [{"mpn": "LM317T", "price": 0.50}],
            "vendor_notes": None,
        }
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.response_parser.parse_vendor_response", new_callable=AsyncMock) as mock_parse,
            patch("app.services.response_parser.extract_draft_offers") as mock_extract,
            patch("app.services.response_parser.should_auto_apply") as mock_auto,
            patch("app.services.response_parser.should_flag_review") as mock_review,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = fake_result
            mock_extract.return_value = [{"mpn": "LM317T", "price": 0.50}]
            mock_auto.return_value = True
            mock_review.return_value = False
            resp = client.post(f"/api/ai/parse-response/{vr.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["parsed"] is True
        assert data["classification"] == "quote_provided"

    def test_with_requisition_context_builds_rfq(self, client, db_session, test_requisition):
        """VendorResponse linked to a requisition → rfq_context is built."""
        vr = self._make_vendor_response(db_session, req_id=test_requisition.id)
        fake_result = {
            "overall_classification": "no_stock",
            "confidence": 0.5,
            "parts": [],
            "vendor_notes": "Out of stock",
        }
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.response_parser.parse_vendor_response", new_callable=AsyncMock) as mock_parse,
            patch("app.services.response_parser.extract_draft_offers") as mock_extract,
            patch("app.services.response_parser.should_auto_apply") as mock_auto,
            patch("app.services.response_parser.should_flag_review") as mock_review,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = fake_result
            mock_extract.return_value = []
            mock_auto.return_value = False
            mock_review.return_value = True
            resp = client.post(f"/api/ai/parse-response/{vr.id}")
        assert resp.status_code == 200


# ── generate-description (standalone) ────────────────────────────────


class TestGenerateDescriptionStandalone:
    def test_missing_mpn_returns_400(self, client):
        resp = client.post(
            "/api/ai/generate-description",
            json={"mpn": "   ", "manufacturer": "TI"},
        )
        assert resp.status_code == 400

    def test_generates_successfully(self, client):
        fake_result = {
            "description": "IC VOLTAGE REG ADJ 1.5A TO-220",
            "confidence": 0.95,
            "source_count": 3,
        }
        with patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value=fake_result,
        ):
            resp = client.post(
                "/api/ai/generate-description",
                json={"mpn": "LM317T", "manufacturer": "TI", "existing_description": ""},
            )
        assert resp.status_code == 200


# ── generate-description for requirement ─────────────────────────────


class TestGenerateDescriptionForRequirement:
    def test_missing_requirement_returns_404(self, client):
        resp = client.post("/api/ai/generate-description/99999")
        assert resp.status_code == 404

    def test_saves_description_when_confidence_high(self, client, db_session, test_user):
        req = Requisition(
            name="Desc Test Req",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        fake_result = {"description": "IC VOLTAGE REG ADJ", "confidence": 0.9, "source_count": 2}
        with patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value=fake_result,
        ):
            resp = client.post(f"/api/ai/generate-description/{item.id}")
        assert resp.status_code == 200
        assert resp.json()["description"] == "IC VOLTAGE REG ADJ"

    def test_does_not_save_when_confidence_low(self, client, db_session, test_user):
        req = Requisition(
            name="Low Conf Req",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="UNKNOWN123",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        fake_result = {"description": "unknown part", "confidence": 0.4, "source_count": 0}
        with patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value=fake_result,
        ):
            resp = client.post(f"/api/ai/generate-description/{item.id}")
        assert resp.status_code == 200
        # description not saved back (confidence < 0.75) but result returned
        assert resp.json()["confidence"] == 0.4


# ── save-parsed-offers with valid response_id ─────────────────────────


class TestSaveParsedOffersResponseId:
    def test_response_id_wrong_requisition_returns_404(self, client, db_session, test_requisition):
        """VendorResponse with no requisition_id → not linked to test_requisition → 404."""
        # Create a second requisition so we can link VendorResponse to it (not test_requisition)
        other_req = Requisition(
            name="Other Req",
            status="active",
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_req)
        db_session.flush()
        vr = VendorResponse(
            vendor_name="Test Vendor",
            body="test",
            requisition_id=other_req.id,  # different requisition
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        resp = client.post(
            "/api/ai/save-parsed-offers",
            json={
                "requisition_id": test_requisition.id,
                "response_id": vr.id,
                "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
            },
        )
        assert resp.status_code == 404


# ── ai_find_contacts edge cases ───────────────────────────────────────


class TestAiFindContactsEdgeCases:
    def test_site_entity_type_resolves_site(self, client, db_session, test_company):
        from app.models import CustomerSite

        site = CustomerSite(
            site_name="Test Site",
            company_id=test_company.id,
        )
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)

        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock) as mock_search,
        ):
            ms.ai_features_enabled = "all"
            mock_search.return_value = [
                {
                    "full_name": "Jane Smith",
                    "title": "CTO",
                    "email": "jane@test.com",
                    "source": "linkedin",
                    "confidence": "medium",
                }
            ]
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "site", "entity_id": site.id},
            )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_deduplicates_contacts_by_email(self, client, db_session, test_vendor_card):
        """Same email returned twice → deduplicated to one entry."""
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock) as mock_search,
        ):
            ms.ai_features_enabled = "all"
            mock_search.return_value = [
                {
                    "full_name": "Bob Jones",
                    "email": "bob@dup.com",
                    "source": "web",
                    "confidence": "high",
                },
                {
                    "full_name": "Bob Jones",
                    "email": "bob@dup.com",
                    "source": "web",
                    "confidence": "high",
                },
            ]
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "vendor", "entity_id": test_vendor_card.id},
            )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_truncates_long_fields(self, client, db_session, test_vendor_card):
        """Fields exceeding max lengths get truncated before saving."""
        long_name = "A" * 300
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock) as mock_search,
        ):
            ms.ai_features_enabled = "all"
            mock_search.return_value = [
                {
                    "full_name": long_name,
                    "email": "truncate@test.com",
                    "source": "web",
                    "confidence": "low",
                }
            ]
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "vendor", "entity_id": test_vendor_card.id},
            )
        assert resp.status_code == 200
        assert len(resp.json()["contacts"][0]["full_name"]) <= 255

    def test_vendor_entity_id_not_found_returns_400(self, client):
        """entity_type=vendor with non-existent id → 400 (company_name empty)."""
        with patch("app.routers.ai.settings") as ms:
            ms.ai_features_enabled = "all"
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "vendor", "entity_id": 99999},
            )
        assert resp.status_code == 400


# ── promote_prospect_contact success path ─────────────────────────────


class TestPromoteProspectContactSuccess:
    def test_promote_succeeds(self, client, db_session, test_user):
        pc = ProspectContact(
            full_name="Promoted Contact",
            source="web_search",
            confidence="high",
            email="promoted@test.com",
        )
        db_session.add(pc)
        db_session.commit()
        db_session.refresh(pc)

        with patch("app.services.ai_offer_service.promote_prospect_contact") as mock_promote:
            mock_promote.return_value = {"ok": True, "type": "vendor_contact", "id": 42}
            resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── intake-parse freeform offer with requisition ──────────────────────


class TestParseFreeformOfferWithRequisition:
    def test_with_valid_requisition_builds_context(self, client, db_session, test_requisition):
        with (
            patch("app.routers.ai.settings") as ms,
            patch(
                "app.services.freeform_parser_service.parse_freeform_offer",
                new_callable=AsyncMock,
            ) as mock_parse,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = {"rows": [{"mpn": "LM317T", "price": 0.55, "qty": 500}]}
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={
                    "raw_text": "LM317T $0.55 qty 500 in stock",
                    "requisition_id": test_requisition.id,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True

    def test_returns_template_on_success(self, client):
        with (
            patch("app.routers.ai.settings") as ms,
            patch(
                "app.services.freeform_parser_service.parse_freeform_offer",
                new_callable=AsyncMock,
            ) as mock_parse,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = {"rows": [{"mpn": "TL431", "price": 0.12, "qty": 2000}]}
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "TL431 $0.12 qty 2000 in stock ready to ship"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True
        assert resp.json()["template"] is not None


# ── ai_parse_email with full result ──────────────────────────────────


class TestParseEmailFullFlow:
    def test_returns_auto_apply_fields(self, client):
        with (
            patch("app.routers.ai.settings") as ms,
            patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock) as mock_parse,
            patch("app.services.ai_email_parser.should_auto_apply") as mock_auto,
            patch("app.services.ai_email_parser.should_flag_review") as mock_review,
        ):
            ms.ai_features_enabled = "all"
            mock_parse.return_value = {
                "quotes": [{"mpn": "LM317T", "price": 0.50, "qty": 1000}],
                "overall_confidence": 0.85,
                "email_type": "quote",
                "vendor_notes": "Net 30",
            }
            mock_auto.return_value = True
            mock_review.return_value = False
            resp = client.post(
                "/api/ai/parse-email",
                json={
                    "email_body": "LM317T $0.50 qty 1000 net 30",
                    "email_subject": "Re: RFQ",
                    "vendor_name": "Arrow Electronics",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["parsed"] is True
        assert data["auto_apply"] is True
        assert data["needs_review"] is False
        assert data["overall_confidence"] == 0.85


# ── intake-parse with mode=offer ──────────────────────────────────────


class TestIntakeParseOffer:
    def test_mode_offer_accepted(self, client):
        with patch("app.services.ai_intake_parser.parse_freeform_intake", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = {"rows": [{"mpn": "LM317T", "qty": 500}]}
            resp = client.post(
                "/api/ai/intake-parse",
                content=b'{"text": "Arrow - LM317T $0.50 qty 500 ex-stock", "mode": "offer"}',
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True

    def test_empty_text_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            content=b'{"text": "", "mode": "auto"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
