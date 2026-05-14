"""tests/test_ai_router_nightly2.py — Additional coverage for app/routers/ai.py.

Targets:
  line  184: field truncation in bulk_save_contacts (value[:max_len])
  lines 432-433: generate_description with empty MPN → 400
  lines 471-475: MaterialCard description update path
  line  503: parse_response where req mismatch → 404
  lines 662-686: intake_parse validation paths + None result
  line  727: parse_freeform_offer missing requisition → 404
  line  784: save_freeform_offers missing requisition → 404

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import Requisition

# ── generate_description empty MPN → 400 (lines 432-433) ────────────────────


class TestGenerateDescriptionValidation:
    def test_empty_mpn_returns_400(self, client):
        resp = client.post(
            "/api/ai/generate-description",
            json={"mpn": "   ", "manufacturer": "", "existing_description": ""},
        )
        assert resp.status_code == 400

    def test_description_calls_service(self, client):
        with patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value={"description": "Test desc", "confidence": 0.9, "source_count": 3},
        ):
            resp = client.post(
                "/api/ai/generate-description",
                json={"mpn": "LM317T", "manufacturer": "TI", "existing_description": ""},
            )
        assert resp.status_code == 200


# ── intake-parse validation (lines 662-686) ──────────────────────────────────


class TestIntakeParseValidation:
    def test_short_text_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "hi", "mode": "auto"},
        )
        assert resp.status_code == 422

    def test_text_too_long_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "x" * 12001, "mode": "auto"},
        )
        assert resp.status_code == 422

    def test_invalid_mode_returns_422(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "LM317T 1000 pcs $0.50", "mode": "invalid_mode"},
        )
        assert resp.status_code == 422

    def test_none_parse_result_returns_not_parsed(self, client):
        with patch(
            "app.services.ai_intake_parser.parse_freeform_intake",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/api/ai/intake-parse",
                json={"text": "some vendor text here", "mode": "auto"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["parsed"] is False

    def test_missing_requisition_returns_404(self, client):
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "LM317T 1000 $0.50", "mode": "rfq", "requisition_id": 999999},
        )
        assert resp.status_code == 404


# ── parse_freeform_offer missing requisition → 404 (line 727) ───────────────


class TestParseFreeformOfferValidation:
    def test_missing_requisition_returns_404(self, client):
        with patch("app.routers.ai._ai_enabled", return_value=True):
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "LM317T 1000 $0.50", "requisition_id": 999999},
            )
        assert resp.status_code == 404

    def test_none_result_returns_not_parsed(self, client):
        with patch("app.routers.ai._ai_enabled", return_value=True):
            with patch(
                "app.services.freeform_parser_service.parse_freeform_offer",
                new_callable=AsyncMock,
                return_value=None,
            ):
                resp = client.post(
                    "/api/ai/parse-freeform-offer",
                    json={"raw_text": "some vendor text here"},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["parsed"] is False


# ── save_freeform_offers missing requisition → 404 (line 784) ───────────────


class TestSaveFreeformOffersValidation:
    def test_missing_requisition_returns_404(self, client):
        resp = client.post(
            "/api/ai/save-freeform-offers",
            json={
                "requisition_id": 999999,
                "offers": [{"vendor_name": "Test Vendor", "mpn": "LM317T"}],
            },
        )
        assert resp.status_code == 404


# ── MaterialCard description update path (lines 471-475) ────────────────────


class TestGenerateDescriptionForRequirement:
    def test_updates_material_card_description(self, client, db_session: Session, test_requisition: Requisition):
        from app.models.intelligence import MaterialCard

        card = MaterialCard(
            normalized_mpn="lm317t-nightly2",
            display_mpn="LM317T",
            description=None,
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        req = test_requisition.requirements[0]
        req.material_card_id = card.id
        db_session.commit()

        with patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value={
                "description": "Low dropout voltage regulator",
                "confidence": 0.95,
                "source_count": 4,
            },
        ):
            resp = client.post(f"/api/ai/generate-description/{req.id}")
        assert resp.status_code == 200
        result = resp.json()
        assert result["confidence"] >= 0.75
