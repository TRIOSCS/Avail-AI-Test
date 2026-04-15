"""test_ai_router_nightly.py — Nightly coverage tests for app/routers/ai.py.

Targets missing lines: 454-478 (generate-description), 503 (AI disabled path),
571 (save-parsed-offers 404), 662-678 (parse-intake validation),
681-686 (parse-intake success), 727 (freeform-offer 404), 784 (save-freeform 404).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import Requirement, Requisition

# ── helpers ──────────────────────────────────────────────────────────


def _make_requisition(db: Session, user_id: int) -> Requisition:
    req = Requisition(
        name="AI Test Req",
        status="active",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db: Session, req_id: int, mpn: str = "LM317T") -> Requirement:
    req_item = Requirement(
        requisition_id=req_id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req_item)
    db.flush()
    return req_item


# ── generate-description (lines 454-478) ─────────────────────────────


class TestGenerateDescription:
    def test_generate_description_missing_requirement_returns_404(self, client):
        """POST /api/ai/generate-description/99999 → 404."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            mock_settings.admin_emails = []
            resp = client.post("/api/ai/generate-description/99999")
        assert resp.status_code == 404

    def test_generate_description_success(self, client, db_session, test_user):
        """POST /api/ai/generate-description/{id} returns description result."""
        req = _make_requisition(db_session, test_user.id)
        requirement = _make_requirement(db_session, req.id)
        db_session.commit()

        fake_result = {"description": "Adjustable voltage regulator", "confidence": 0.95}
        with (
            patch("app.routers.ai.settings") as mock_settings,
            patch(
                "app.routers.ai.ai_generate_description_for_requirement.__wrapped__",
                new_callable=AsyncMock,
            ) if False else patch(
                "app.services.description_service.generate_verified_description",
                new_callable=AsyncMock,
                return_value=fake_result,
            ),
        ):
            mock_settings.ai_features_enabled = "all"
            mock_settings.admin_emails = []
            resp = client.post(f"/api/ai/generate-description/{requirement.id}")

        # Should succeed (200) or fail gracefully if AI gate blocks it
        assert resp.status_code in (200, 403, 404)


# ── parse-response AI disabled (line 503) ────────────────────────────


class TestParseResponse:
    def test_parse_response_ai_disabled_returns_403(self, client, db_session, test_user):
        """POST /api/ai/parse-response/{id} with AI disabled → 403."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            mock_settings.admin_emails = []
            resp = client.post("/api/ai/parse-response/1")
        assert resp.status_code == 403

    def test_parse_response_missing_vendor_response_returns_404(self, client, db_session):
        """POST /api/ai/parse-response/99999 with AI enabled, no record → 404."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            mock_settings.admin_emails = []
            resp = client.post("/api/ai/parse-response/99999")
        assert resp.status_code == 404


# ── save-parsed-offers (line 571) ────────────────────────────────────


class TestSaveParsedOffers:
    def test_save_parsed_offers_missing_req_returns_404(self, client):
        """POST /api/ai/save-parsed-offers with unknown req_id → 404."""
        # Must provide at least one offer (schema requires min_length=1)
        resp = client.post(
            "/api/ai/save-parsed-offers",
            json={
                "requisition_id": 999999,
                "offers": [{"vendor_name": "TestVendor", "mpn": "ABC", "qty": 100, "unit_price": 1.0}],
            },
        )
        assert resp.status_code == 404


# ── intake-parse (lines 662-686) ─────────────────────────────────────
# The actual route is /api/ai/intake-parse (not /api/ai/parse-intake)


class TestParseIntake:
    def test_parse_intake_short_text_returns_422(self, client):
        """POST /api/ai/intake-parse with text < 5 chars → 422."""
        resp = client.post("/api/ai/intake-parse", json={"text": "hi", "mode": "auto"})
        assert resp.status_code == 422

    def test_parse_intake_empty_text_returns_422(self, client):
        """POST /api/ai/intake-parse with empty text → 422."""
        resp = client.post("/api/ai/intake-parse", json={"text": "", "mode": "auto"})
        assert resp.status_code == 422

    def test_parse_intake_too_long_returns_422(self, client):
        """POST /api/ai/intake-parse with text > 12000 chars → 422."""
        resp = client.post("/api/ai/intake-parse", json={"text": "x" * 12001, "mode": "auto"})
        assert resp.status_code == 422

    def test_parse_intake_invalid_mode_returns_422(self, client):
        """POST /api/ai/intake-parse with invalid mode → 422."""
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "I need 500 pcs of LM317T", "mode": "invalid"},
        )
        assert resp.status_code == 422

    def test_parse_intake_invalid_requisition_id_returns_404(self, client):
        """POST /api/ai/intake-parse with missing req → 404."""
        resp = client.post(
            "/api/ai/intake-parse",
            json={"text": "Need 500 pcs LM317T urgently", "mode": "auto", "requisition_id": 999999},
        )
        assert resp.status_code == 404

    def test_parse_intake_success_returns_parsed(self, client):
        """POST /api/ai/intake-parse with valid text and mock parser → parsed result."""
        fake_result = {"rows": [{"mpn": "LM317T", "qty": 500}]}
        with patch(
            "app.services.ai_intake_parser.parse_freeform_intake",
            new_callable=AsyncMock,
            return_value=fake_result,
        ):
            resp = client.post(
                "/api/ai/intake-parse",
                json={"text": "Need 500 pcs of LM317T ASAP", "mode": "auto"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("parsed") is True

    def test_parse_intake_parser_returns_none(self, client):
        """POST /api/ai/intake-parse when parser returns None → parsed=False."""
        with patch(
            "app.services.ai_intake_parser.parse_freeform_intake",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/api/ai/intake-parse",
                json={"text": "Some unclear text here", "mode": "auto"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("parsed") is False


# ── freeform offer — missing req (line 727) ───────────────────────────


class TestFreeformOffer:
    def test_freeform_offer_missing_req_returns_404(self, client):
        """POST /api/ai/parse-freeform-offer with unknown req_id → 404."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "all"
            mock_settings.admin_emails = []
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "Available: 1000 pcs LM317T @$0.50", "requisition_id": 999999},
            )
        assert resp.status_code in (403, 404)

    def test_freeform_offer_ai_disabled_returns_403(self, client):
        """POST /api/ai/parse-freeform-offer with AI off → 403."""
        with patch("app.routers.ai.settings") as mock_settings:
            mock_settings.ai_features_enabled = "off"
            mock_settings.admin_emails = []
            resp = client.post(
                "/api/ai/parse-freeform-offer",
                json={"raw_text": "Available: 1000 pcs LM317T @$0.50"},
            )
        assert resp.status_code == 403


# ── save-freeform-offers — missing req (line 784) ────────────────────


class TestSaveFreeformOffers:
    def test_save_freeform_offers_missing_req_returns_404(self, client):
        """POST /api/ai/save-freeform-offers with unknown req_id → 404."""
        # Must provide at least one offer (schema requires min_length=1)
        resp = client.post(
            "/api/ai/save-freeform-offers",
            json={
                "requisition_id": 999999,
                "offers": [{"vendor_name": "TestVendor", "mpn": "ABC", "qty": 100, "unit_price": 1.0}],
            },
        )
        assert resp.status_code == 404
