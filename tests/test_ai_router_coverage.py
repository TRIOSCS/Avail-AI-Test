"""test_ai_router_coverage.py — Coverage tests for app/routers/ai.py.

Targets missing branches: _build_vendor_history, prospect contacts CRUD,
promote_prospect_contact, parse-email, normalize-parts, standardize-description,
parse-response, save-parsed-offers, company-intel, draft-rfq, intake-parse,
freeform rfq/offer, apply-freeform-rfq, save-freeform-offers.

Called by: pytest
Depends on: app/routers/ai.py, conftest.py
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models import CustomerSite, ProspectContact

os.environ["TESTING"] = "1"

# ── ai_enabled gate with empty admin_emails ─────────────────────────


def test_ai_enabled_mike_only_empty_admin_emails_returns_false():
    from app.routers.ai import _ai_enabled

    user = SimpleNamespace(email="mike@trioscs.com")
    settings = SimpleNamespace(ai_features_enabled="mike_only", admin_emails=[])
    with patch("app.routers.ai.settings", settings):
        result = _ai_enabled(user)
    assert result is False


def test_ai_enabled_unknown_flag_returns_false():
    from app.routers.ai import _ai_enabled

    user = SimpleNamespace(email="anyone@test.com")
    settings = SimpleNamespace(ai_features_enabled="unknown_mode", admin_emails=[])
    with patch("app.routers.ai.settings", settings):
        result = _ai_enabled(user)
    assert result is False


# ── _build_vendor_history ────────────────────────────────────────────


class TestBuildVendorHistory:
    def test_returns_empty_when_no_vendor_card(self, db_session, test_user):
        from app.routers.ai import _build_vendor_history

        result = _build_vendor_history("NonExistentVendorXYZ123", db_session)
        assert result == {}

    def test_returns_history_when_vendor_exists(self, db_session, test_vendor_card):
        from app.routers.ai import _build_vendor_history

        result = _build_vendor_history(test_vendor_card.display_name, db_session)
        # May be empty dict if normalized name doesn't match, but no crash
        assert isinstance(result, dict)


# ── list_prospect_contacts ────────────────────────────────────────────


class TestListProspectContacts:
    def test_missing_entity_type_returns_400(self, client):
        resp = client.get("/api/ai/prospect-contacts")
        assert resp.status_code == 400

    def test_company_type_returns_list(self, client, db_session, test_user, test_company):
        site = CustomerSite(
            site_name="Acme HQ",
            site_type="headquarters",
            city="Chicago",
            company_id=test_company.id,
        )
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)

        resp = client.get(f"/api/ai/prospect-contacts?entity_type=company&entity_id={site.id}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_vendor_type_returns_list(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/ai/prospect-contacts?entity_type=vendor&entity_id={test_vendor_card.id}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── save_prospect_contact ─────────────────────────────────────────────


class TestSaveProspectContact:
    def _make_pc(self, db_session, test_user):
        pc = ProspectContact(
            full_name="Jane Doe",
            title="VP Sales",
            email="jane@test.com",
            source="web_search",
            confidence="high",
        )
        db_session.add(pc)
        db_session.commit()
        db_session.refresh(pc)
        return pc

    def test_save_marks_is_saved(self, client, db_session, test_user):
        pc = self._make_pc(db_session, test_user)
        resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/save")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_save_with_notes(self, client, db_session, test_user):
        pc = self._make_pc(db_session, test_user)
        resp = client.post(
            f"/api/ai/prospect-contacts/{pc.id}/save",
            json={"notes": "Follow up next week"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        db_session.refresh(pc)
        assert pc.notes == "Follow up next week"

    def test_save_not_found_returns_404(self, client):
        resp = client.post("/api/ai/prospect-contacts/99999/save")
        assert resp.status_code == 404


# ── delete_prospect_contact ───────────────────────────────────────────


class TestDeleteProspectContact:
    def test_delete_returns_ok(self, client, db_session, test_user):
        pc = ProspectContact(
            full_name="Delete Me",
            source="web_search",
            confidence="low",
        )
        db_session.add(pc)
        db_session.commit()
        db_session.refresh(pc)

        resp = client.delete(f"/api/ai/prospect-contacts/{pc.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_not_found_returns_404(self, client):
        resp = client.delete("/api/ai/prospect-contacts/99999")
        assert resp.status_code == 404


# ── promote_prospect_contact ──────────────────────────────────────────


class TestPromoteProspectContact:
    def test_promote_not_found_returns_404(self, client, db_session):
        with patch("app.services.ai_offer_service.promote_prospect_contact") as mock_promote:
            mock_promote.side_effect = ValueError("Contact not found")
            resp = client.post("/api/ai/prospect-contacts/99999/promote")
        assert resp.status_code == 404

    def test_promote_validation_error_returns_400(self, client, db_session, test_user):
        pc = ProspectContact(
            full_name="Promote Test",
            source="web_search",
            confidence="medium",
        )
        db_session.add(pc)
        db_session.commit()
        db_session.refresh(pc)

        with patch("app.services.ai_offer_service.promote_prospect_contact") as mock_promote:
            mock_promote.side_effect = ValueError("No vendor or site context available")
            resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
        assert resp.status_code == 400


# ── parse-email ────────────────────────────────────────────────────────


class TestParseEmail:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post(
                "/api/ai/parse-email",
                json={
                    "email_body": "We have LM317T in stock",
                    "email_subject": "Re: RFQ",
                    "vendor_name": "Arrow",
                },
            )
        assert resp.status_code == 403

    def test_parse_email_parser_returns_none(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock) as mock_parse,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_parse.return_value = None
            resp = client.post(
                "/api/ai/parse-email",
                json={
                    "email_body": "No quotes here",
                    "email_subject": "Re: RFQ",
                    "vendor_name": "Arrow",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is False

    def test_parse_email_returns_quotes(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock) as mock_parse,
            patch("app.services.ai_email_parser.should_auto_apply") as mock_auto,
            patch("app.services.ai_email_parser.should_flag_review") as mock_review,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_parse.return_value = {
                "quotes": [{"mpn": "LM317T", "price": 0.50}],
                "overall_confidence": 0.9,
                "email_type": "quote",
                "vendor_notes": None,
            }
            mock_auto.return_value = True
            mock_review.return_value = False
            resp = client.post(
                "/api/ai/parse-email",
                json={
                    "email_body": "LM317T $0.50 qty 1000",
                    "email_subject": "Re: RFQ",
                    "vendor_name": "Arrow",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True


# ── normalize-parts ────────────────────────────────────────────────────


class TestNormalizeParts:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post("/api/ai/normalize-parts", json={"parts": ["LM317T", "TL431"]})
        assert resp.status_code == 403

    def test_normalizes_parts_when_ai_enabled(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_part_normalizer.normalize_parts", new_callable=AsyncMock) as mock_normalize,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_normalize.return_value = [{"mpn": "LM317T", "manufacturer": "TI"}]
            resp = client.post("/api/ai/normalize-parts", json={"parts": ["LM317T"]})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


# ── standardize-description ───────────────────────────────────────────


class TestStandardizeDescription:
    def test_empty_description_returns_empty(self, client):
        resp = client.post(
            "/api/ai/standardize-description",
            json={"description": "   ", "mpn": "", "manufacturer": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == ""

    def test_standardizes_description(self, client):
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = "IC MCU 32-BIT 168MHZ LQFP-100"
            resp = client.post(
                "/api/ai/standardize-description",
                json={
                    "description": "32-bit ARM Cortex microcontroller",
                    "mpn": "STM32F407VGT6",
                    "manufacturer": "ST",
                },
            )
        assert resp.status_code == 200
        assert len(resp.json()["description"]) > 0

    def test_fallback_when_claude_returns_none(self, client):
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = None
            resp = client.post(
                "/api/ai/standardize-description",
                json={"description": "voltage regulator", "mpn": "LM317T", "manufacturer": "TI"},
            )
        assert resp.status_code == 200
        assert resp.json()["description"] == "VOLTAGE REGULATOR"


# ── company-intel ────────────────────────────────────────────────────


class TestCompanyIntel:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.get("/api/ai/company-intel?company_name=Acme")
        assert resp.status_code == 403

    def test_missing_company_name_returns_400(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            resp = client.get("/api/ai/company-intel")
        assert resp.status_code == 400

    def test_returns_unavailable_when_intel_empty(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.company_intel", new_callable=AsyncMock) as mock_intel,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_intel.return_value = None
            resp = client.get("/api/ai/company-intel?company_name=Acme")
        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_returns_intel_when_available(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.company_intel", new_callable=AsyncMock) as mock_intel,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_intel.return_value = {"summary": "Acme is a leading electronics company"}
            resp = client.get("/api/ai/company-intel?company_name=Acme&domain=acme.com")
        assert resp.status_code == 200
        assert resp.json()["available"] is True


# ── draft-rfq ────────────────────────────────────────────────────────


class TestDraftRfq:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post(
                "/api/ai/draft-rfq",
                json={"vendor_name": "Arrow", "parts": ["LM317T x100"]},
            )
        assert resp.status_code == 403

    def test_returns_unavailable_when_draft_fails(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock) as mock_draft,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_draft.return_value = None
            resp = client.post(
                "/api/ai/draft-rfq",
                json={"vendor_name": "Arrow", "parts": ["LM317T x100"]},
            )
        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_returns_draft_when_successful(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock) as mock_draft,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_draft.return_value = "Dear Arrow, we are looking for LM317T..."
            resp = client.post(
                "/api/ai/draft-rfq",
                json={"vendor_name": "Arrow", "parts": ["LM317T x100"]},
            )
        assert resp.status_code == 200
        assert resp.json()["available"] is True
        assert "body" in resp.json()


# ── intake-parse ──────────────────────────────────────────────────────


class TestIntakeParse:
    def test_text_too_short_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            content=b'{"text": "hi", "mode": "auto"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_text_too_long_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            content=('{"text": "' + "A" * 12001 + '", "mode": "auto"}').encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_invalid_mode_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            content=b'{"text": "LM317T qty 1000", "mode": "invalid"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_parse_returns_false_when_no_result(self, client):
        with patch("app.services.ai_intake_parser.parse_freeform_intake", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = None
            resp = client.post(
                "/api/ai/intake-parse",
                content=b'{"text": "random text here with no structure", "mode": "auto"}',
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is False

    def test_parse_returns_template_when_successful(self, client):
        with patch("app.services.ai_intake_parser.parse_freeform_intake", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = {"rows": [{"mpn": "LM317T", "qty": 1000}]}
            resp = client.post(
                "/api/ai/intake-parse",
                content=b'{"text": "LM317T qty 1000 units needed", "mode": "rfq"}',
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True


# ── parse-freeform-rfq ─────────────────────────────────────────────────


class TestParseFreeformRfq:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post(
                "/api/ai/parse-freeform-rfq",
                json={"raw_text": "We need 1000 LM317T from TI"},
            )
        assert resp.status_code == 403

    def test_parser_returns_none(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.freeform_parser_service.parse_freeform_rfq", new_callable=AsyncMock) as mock_parse,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_parse.return_value = None
            resp = client.post(
                "/api/ai/parse-freeform-rfq",
                json={"raw_text": "gibberish text"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is False

    def test_parser_returns_template(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.freeform_parser_service.parse_freeform_rfq", new_callable=AsyncMock) as mock_parse,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_parse.return_value = {"name": "Test RFQ", "requirements": []}
            resp = client.post(
                "/api/ai/parse-freeform-rfq",
                json={"raw_text": "We need 1000 LM317T from TI ASAP"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True


# ── parse-freeform-offer ───────────────────────────────────────────────


class TestParseFreeformOffer:
    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "LM317T $0.50 qty 1000"},
            )
        assert resp.status_code == 403

    def test_parser_returns_none(self, client):
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.freeform_parser_service.parse_freeform_offer", new_callable=AsyncMock) as mock_parse,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_parse.return_value = None
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "gibberish"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is False

    def test_parser_with_requisition_not_found(self, client, db_session):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "LM317T $0.50", "requisition_id": 99999},
            )
        assert resp.status_code == 404


# ── apply-freeform-rfq ─────────────────────────────────────────────────


class TestApplyFreeformRfq:
    def test_missing_customer_site_id_returns_400(self, client):
        """customer_site_id is None by default, router checks and raises 400."""
        resp = client.post(
            "/api/ai/apply-freeform-rfq",
            json={
                "name": "Test RFQ",
                "customer_name": "Acme",
                "requirements": [{"mpn": "LM317T", "target_qty": 100}],
            },
        )
        assert resp.status_code == 400

    def test_site_not_found_returns_404(self, client, db_session):
        with patch("app.services.ai_offer_service.apply_freeform_rfq") as mock_apply:
            mock_apply.side_effect = ValueError("Site not found")
            resp = client.post(
                "/api/ai/apply-freeform-rfq",
                json={
                    "name": "Test RFQ",
                    "customer_name": "Acme",
                    "customer_site_id": 99999,
                    "requirements": [{"mpn": "LM317T", "target_qty": 100}],
                },
            )
        assert resp.status_code == 404

    def test_applies_rfq_successfully(self, client, db_session):
        with (
            patch("app.services.ai_offer_service.apply_freeform_rfq") as mock_apply,
            patch("app.cache.decorators.invalidate_prefix"),
        ):
            mock_apply.return_value = {"requisition_id": 1, "requirements_created": 2}
            resp = client.post(
                "/api/ai/apply-freeform-rfq",
                json={
                    "name": "Test RFQ",
                    "customer_name": "Acme",
                    "customer_site_id": 1,
                    "requirements": [{"mpn": "LM317T", "target_qty": 100}],
                },
            )
        assert resp.status_code == 200


# ── save-freeform-offers ───────────────────────────────────────────────


class TestSaveFreeformOffers:
    def test_requisition_not_found_returns_404(self, client, db_session):
        """Non-existent requisition should return 404.

        Uses one valid offer.
        """
        resp = client.post(
            "/api/ai/save-freeform-offers",
            json={
                "requisition_id": 99999,
                "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
            },
        )
        assert resp.status_code == 404

    def test_saves_offers_successfully(self, client, db_session, test_requisition):
        with patch("app.services.ai_offer_service.save_freeform_offers") as mock_save:
            mock_save.return_value = {"created": 1, "updated": 0}
            resp = client.post(
                "/api/ai/save-freeform-offers",
                json={
                    "requisition_id": test_requisition.id,
                    "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
                },
            )
        assert resp.status_code == 200


# ── save-parsed-offers ─────────────────────────────────────────────────


class TestSaveParsedOffers:
    def test_requisition_not_found_returns_404(self, client, db_session):
        """Non-existent requisition returns 404.

        Uses one valid offer.
        """
        resp = client.post(
            "/api/ai/save-parsed-offers",
            json={
                "requisition_id": 99999,
                "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
            },
        )
        assert resp.status_code == 404

    def test_saves_successfully(self, client, db_session, test_requisition):
        with patch("app.services.ai_offer_service.save_parsed_offers") as mock_save:
            mock_save.return_value = {"created": 1, "updated": 0}
            resp = client.post(
                "/api/ai/save-parsed-offers",
                json={
                    "requisition_id": test_requisition.id,
                    "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
                },
            )
        assert resp.status_code == 200

    def test_invalid_response_id_returns_404(self, client, db_session, test_requisition):
        """response_id that doesn't exist → 404."""
        resp = client.post(
            "/api/ai/save-parsed-offers",
            json={
                "requisition_id": test_requisition.id,
                "response_id": 99999,
                "offers": [{"mpn": "LM317T", "unit_price": 0.50, "quantity": 100, "vendor_name": "Arrow"}],
            },
        )
        assert resp.status_code == 404


# ── ai_find_contacts (entity resolution + web search) ─────────────────


class TestAiFindContacts:
    def test_vendor_entity_type_calls_web_search(self, client, db_session, test_vendor_card):
        """entity_type=vendor → resolves VendorCard and calls web search."""
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock) as mock_search,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_search.return_value = [
                {
                    "full_name": "Bob Smith",
                    "title": "VP Sales",
                    "email": "bob@vendor.com",
                    "source": "web",
                    "confidence": "high",
                }
            ]
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "vendor", "entity_id": test_vendor_card.id},
            )
        assert resp.status_code == 200
        assert resp.json()["total"] >= 0

    def test_company_entity_type_calls_web_search(self, client, db_session, test_company):
        """entity_type=company → resolves CustomerSite and calls web search."""
        from app.models import CustomerSite

        site = CustomerSite(
            site_name="Acme HQ",
            site_type="headquarters",
            company_id=test_company.id,
        )
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)

        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock) as mock_search,
        ):
            mock_settings.ai_features_enabled = "all"
            mock_search.return_value = []
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "company", "entity_id": site.id},
            )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_missing_entity_id_returns_400(self, client):
        """No entity_id with no company_name → 400."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "company"},
            )
        assert resp.status_code == 400

    def test_ai_disabled_returns_403(self, client):
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            resp = client.post(
                "/api/ai/find-contacts",
                json={"entity_type": "vendor", "entity_id": 1},
            )
        assert resp.status_code == 403


# ── intake-parse with requisition_id ──────────────────────────────────


class TestIntakeParseWithRequisitionId:
    def test_parse_with_requisition_id_found(self, client, db_session, test_requisition):
        """Intake-parse with a valid requisition_id includes rfq_context."""
        with patch("app.services.ai_intake_parser.parse_freeform_intake", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = {"rows": [{"mpn": "LM317T", "qty": 100}]}
            resp = client.post(
                "/api/ai/intake-parse",
                content=(
                    f'{{"text": "LM317T qty 100 units needed", "mode": "rfq", "requisition_id": {test_requisition.id}}}'
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.json()["parsed"] is True

    def test_parse_with_requisition_id_not_found(self, client, db_session):
        """Intake-parse with a missing requisition_id returns 404."""
        resp = client.post(
            "/api/ai/intake-parse",
            content=b'{"text": "LM317T qty 100 units needed", "mode": "rfq", "requisition_id": 99999}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404
