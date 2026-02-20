"""
tests/test_routers_proactive.py -- Tests for routers/proactive.py

Covers: matches list/count, dismiss, send, offers, convert-to-win,
scorecard, and site contacts endpoints.

Called by: pytest
Depends on: app/routers/proactive.py, conftest.py
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ProactiveMatch, SiteContact, User

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def sales_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient with sales user auth override."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Matches ──────────────────────────────────────────────────────────


@patch("app.services.proactive_service.get_matches_for_user", return_value=[])
def test_matches_empty(mock_fn, client):
    """No matches -> empty list."""
    resp = client.get("/api/proactive/matches")
    assert resp.status_code == 200
    assert resp.json() == []


@patch("app.services.proactive_service.get_matches_for_user",
       return_value=[{"site": "Acme", "matches": []}])
def test_matches_with_data(mock_fn, client):
    """Returns grouped matches."""
    resp = client.get("/api/proactive/matches")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["site"] == "Acme"


# ── Count ────────────────────────────────────────────────────────────


@patch("app.services.proactive_service.get_match_count", return_value=5)
def test_count_badge(mock_fn, client):
    """Returns count for current user."""
    resp = client.get("/api/proactive/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 5


# ── Dismiss ──────────────────────────────────────────────────────────


def test_dismiss_success(client, db_session, test_user, test_requisition, test_offer, test_customer_site):
    """Dismiss match IDs -> 200."""
    match = ProactiveMatch(
        offer_id=test_offer.id,
        requirement_id=test_requisition.id,
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        salesperson_id=test_user.id,
        mpn="LM317T",
        status="new",
    )
    db_session.add(match)
    db_session.commit()

    resp = client.post("/api/proactive/dismiss", json={"match_ids": [match.id]})
    assert resp.status_code == 200
    assert resp.json()["dismissed"] >= 1


def test_dismiss_empty_list(client):
    """Empty match_ids -> 400."""
    resp = client.post("/api/proactive/dismiss", json={"match_ids": []})
    assert resp.status_code == 400


# ── Send ─────────────────────────────────────────────────────────────


@patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock,
       return_value={"ok": True, "sent_to": 1})
@patch("app.routers.proactive.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
def test_send_success(mock_token, mock_send, client):
    """Mock Graph send -> 200."""
    resp = client.post("/api/proactive/send", json={
        "match_ids": [1], "contact_ids": [1],
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@patch("app.routers.proactive.get_valid_token", new_callable=AsyncMock, return_value=None)
def test_send_no_m365_token(mock_token, client):
    """Missing token -> 400."""
    resp = client.post("/api/proactive/send", json={
        "match_ids": [1], "contact_ids": [1],
    })
    assert resp.status_code == 400


# ── Offers ───────────────────────────────────────────────────────────


@patch("app.services.proactive_service.get_sent_offers", return_value=[{"id": 1}])
def test_offers_list(mock_fn, client):
    """Returns sent offers for user."""
    resp = client.get("/api/proactive/offers")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── Convert ──────────────────────────────────────────────────────────


@patch("app.services.proactive_service.convert_proactive_to_win",
       return_value={"ok": True, "requisition_id": 1, "quote_id": 1})
def test_convert_to_win(mock_fn, client):
    """Creates requisition + quote from offer."""
    resp = client.post("/api/proactive/convert/1")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@patch("app.services.proactive_service.convert_proactive_to_win",
       side_effect=ValueError("Proactive offer not found"))
def test_convert_not_found(mock_fn, client):
    """Invalid offer_id -> 400."""
    resp = client.post("/api/proactive/convert/99999")
    assert resp.status_code == 400


# ── Scorecard ────────────────────────────────────────────────────────


@patch("app.services.proactive_service.get_scorecard",
       return_value={"total_sent": 10, "total_converted": 2})
def test_scorecard_admin(mock_fn, client):
    """Admin sees scorecard."""
    resp = client.get("/api/proactive/scorecard")
    assert resp.status_code == 200
    assert resp.json()["total_sent"] == 10


@patch("app.services.proactive_service.get_scorecard",
       return_value={"total_sent": 3, "total_converted": 1})
def test_scorecard_sales_own(mock_fn, sales_client):
    """Sales user sees own stats (salesperson_id forced to user.id)."""
    resp = sales_client.get("/api/proactive/scorecard")
    assert resp.status_code == 200
    mock_fn.assert_called_once()


# ── Site Contacts ────────────────────────────────────────────────────


def test_contacts_for_site(client, db_session, test_customer_site):
    """Returns site contacts."""
    contact = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Jane Doe",
        email="jane@acme.com",
        is_primary=True,
    )
    db_session.add(contact)
    db_session.commit()

    resp = client.get(f"/api/proactive/contacts/{test_customer_site.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["full_name"] == "Jane Doe"
    assert data[0]["is_primary"] is True


def test_contacts_site_empty(client):
    """Invalid/empty site_id -> empty list."""
    resp = client.get("/api/proactive/contacts/99999")
    assert resp.status_code == 200
    assert resp.json() == []
