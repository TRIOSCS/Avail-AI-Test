"""test_sprint8_proactive.py — Tests for Sprint 8 proactive selling + prospecting.

Verifies: Proactive draft/send/convert, scorecard, badge, do-not-offer,
prospecting stats, domain submission.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

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


# ── Proactive Send ──────────────────────────────────────────────────


class TestProactiveSend:
    def test_send(self, client: TestClient, proactive_match, db_session: Session):
        resp = client.post(
            f"/v2/partials/proactive/{proactive_match.id}/send",
            data={"subject": "Stock Available: LM317T", "body": "We have parts available."},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(proactive_match)
        assert proactive_match.status == "sent"

    def test_send_empty_body(self, client: TestClient, proactive_match):
        resp = client.post(
            f"/v2/partials/proactive/{proactive_match.id}/send",
            data={"subject": "Test", "body": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


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
    def test_suppress(self, client: TestClient, test_company):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "customer_site_id": str(test_company.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Suppressed" in resp.text

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
        assert "Prospecting Stats" in resp.text


# ── Add Domain ──────────────────────────────────────────────────────


class TestAddDomain:
    def test_add_domain_missing(self, client: TestClient):
        resp = client.post(
            "/v2/partials/prospecting/add-domain",
            data={"domain": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400
