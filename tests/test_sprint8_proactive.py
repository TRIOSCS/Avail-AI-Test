"""test_sprint8_proactive.py — Tests for Sprint 8 proactive selling + prospecting.

Verifies: Proactive draft/send/convert, scorecard, badge, do-not-offer,
prospecting stats, domain submission.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def proactive_match(db_session: Session, test_offer, test_requisition, test_customer_site, test_user: User):
    """A proactive match for testing."""
    from app.models.intelligence import ProactiveMatch

    m = ProactiveMatch(
        offer_id=test_offer.id,
        requirement_id=test_requisition.id,  # Using req id since we need a valid requirement
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        salesperson_id=test_user.id,
        mpn="LM317T",
        status="new",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


# ── Proactive Draft ─────────────────────────────────────────────────


class TestProactiveDraft:
    def test_draft_renders(self, client: TestClient, proactive_match):
        resp = client.post(
            "/v2/partials/proactive/draft",
            data={"match_ids": str(proactive_match.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_draft_nonexistent(self, client: TestClient):
        resp = client.post(
            "/v2/partials/proactive/draft",
            data={"match_ids": "99999"},
            headers={"HX-Request": "true"},
        )
        # No valid matches found returns 200 with error message in HTML
        assert resp.status_code == 200
        assert "No valid matches" in resp.text


# ── Proactive Scorecard ─────────────────────────────────────────────


class TestProactiveScorecard:
    def test_scorecard_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive/scorecard",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Proactive Scorecard" in resp.text


# ── Proactive Badge ─────────────────────────────────────────────────


class TestProactiveBadge:
    def test_badge_with_matches(self, client: TestClient, proactive_match):
        resp = client.get(
            "/v2/partials/proactive/badge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_badge_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive/badge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""


# ── Do Not Offer ────────────────────────────────────────────────────


class TestDoNotOffer:
    def test_suppress(self, client: TestClient, db_session: Session, test_company, test_user: User):
        test_company.account_owner_id = test_user.id  # actor must manage the account (authz gate)
        db_session.commit()
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "customer_site_id": str(test_company.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # SP2: do-not-offer returns a collapsed/hidden row (htmx removal), not a "Suppressed" message
        assert "display:none" in resp.text

    def test_suppress_missing_fields(self, client: TestClient):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "", "customer_site_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


# ── Prospecting Stats ───────────────────────────────────────────────


class TestProspectingStats:
    def test_stats_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/prospecting/stats",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Buyer-ready" in resp.text


# ── Add Domain ──────────────────────────────────────────────────────


class TestAddDomain:
    def test_add_domain_missing(self, client: TestClient):
        resp = client.post(
            "/v2/partials/prospecting/add-domain",
            data={"domain": ""},
            headers={"HX-Request": "true"},
        )
        # Empty domain returns an inline error chip (200), not a 400.
        assert resp.status_code == 200
        assert "domain" in resp.text.lower()


# ── Fix 3: proactive_convert 403/409 ────────────────────────────────


class TestProactiveConvert:
    """Tests for POST /v2/partials/proactive/{offer_id}/convert (fix 3)."""

    def test_cross_user_convert_returns_403(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_customer_site,
    ):
        """A different user's proactive offer → 403 (ownership check)."""
        from app.models import ProactiveOffer

        other_user = User(
            email="other@trioscs.com",
            name="Other User",
            role="buyer",
            azure_id="other-azure-999",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.flush()

        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=other_user.id,  # NOT test_user
            line_items=[],
            recipient_emails=[],
            subject="Test",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/proactive/{po.id}/convert",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        side_effect=ValueError("Already converted"),
    )
    def test_double_convert_returns_409(
        self,
        mock_convert,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_proactive_offer,
    ):
        """Second conversion of the same offer → 409 Conflict."""
        resp = client.post(
            f"/v2/partials/proactive/{test_proactive_offer.id}/convert",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        return_value={"ok": True, "requisition_id": 1, "quote_id": 1},
    )
    def test_valid_convert_succeeds(
        self,
        mock_convert,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_proactive_offer,
    ):
        """Valid conversion → 200 success response."""
        resp = client.post(
            f"/v2/partials/proactive/{test_proactive_offer.id}/convert",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        return_value={"ok": True, "requisition_id": 1, "quote_id": 1},
    )
    def test_null_salesperson_convert_succeeds(
        self,
        mock_convert,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_customer_site,
    ):
        """An offer with salesperson_id=None is convertible by any authenticated user
        (not 403)."""
        from app.models import ProactiveOffer

        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=None,  # no owner
            line_items=[],
            recipient_emails=[],
            subject="Unowned offer",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/proactive/{po.id}/convert",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200, f"Expected 200 for null salesperson, got {resp.status_code}"
