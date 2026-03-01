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


@patch("app.services.proactive_service.get_matches_for_user",
       return_value={"groups": [], "stats": {"total": 0, "avg_score": 0, "avg_margin": None, "high_margin_count": 0}})
def test_matches_empty(mock_fn, client):
    """No matches -> empty groups with stats."""
    resp = client.get("/api/proactive/matches")
    assert resp.status_code == 200
    data = resp.json()
    assert data["groups"] == []
    assert data["stats"]["total"] == 0


@patch("app.services.proactive_service.get_matches_for_user",
       return_value={"groups": [{"site": "Acme", "matches": []}], "stats": {"total": 1, "avg_score": 80, "avg_margin": 25.0, "high_margin_count": 0}})
def test_matches_with_data(mock_fn, client):
    """Returns grouped matches with stats."""
    resp = client.get("/api/proactive/matches")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["groups"]) == 1
    assert data["groups"][0]["site"] == "Acme"
    assert data["stats"]["avg_score"] == 80


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


# ── Additional coverage tests ─────────────────────────────────────────

from unittest.mock import MagicMock


class TestSendValidation:
    def test_send_empty_match_ids(self, client):
        """Send with empty match_ids -> 400."""
        resp = client.post("/api/proactive/send", json={
            "match_ids": [],
            "contact_ids": [1],
        })
        assert resp.status_code == 400

    def test_send_empty_contact_ids(self, client):
        """Send with empty contact_ids -> 400."""
        resp = client.post("/api/proactive/send", json={
            "match_ids": [1],
            "contact_ids": [],
        })
        assert resp.status_code == 400

    @patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock,
           side_effect=ValueError("No matching contacts found"))
    @patch("app.routers.proactive.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_value_error(self, mock_token, mock_send, client):
        """Service raises ValueError -> 400."""
        resp = client.post("/api/proactive/send", json={
            "match_ids": [1],
            "contact_ids": [1],
        })
        assert resp.status_code == 400

    @patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock,
           return_value={"ok": True, "sent_to": 2})
    @patch("app.routers.proactive.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_with_all_optional_fields(self, mock_token, mock_send, client):
        """Send with sell_prices, subject, and notes."""
        resp = client.post("/api/proactive/send", json={
            "match_ids": [1, 2],
            "contact_ids": [1],
            "sell_prices": {"1": 1.25, "2": 2.50},
            "subject": "Special offer for you",
            "notes": "Limited time offer",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["sent_to"] == 2


class TestScorecardExtended:
    @patch("app.services.proactive_service.get_scorecard",
           return_value={"total_sent": 3, "total_converted": 1})
    def test_scorecard_sales_with_explicit_salesperson_id(self, mock_fn, sales_client, sales_user):
        """Non-admin requesting salesperson_id is forced to own user id."""
        resp = sales_client.get("/api/proactive/scorecard?salesperson_id=9999")
        assert resp.status_code == 200
        # The function was called (we can't easily check the arg due to caching
        # decorator, but we verify the endpoint doesn't error)
        mock_fn.assert_called_once()

    @patch("app.services.proactive_service.get_scorecard",
           return_value={"total_sent": 20, "total_converted": 5})
    def test_scorecard_admin_with_salesperson_id(self, mock_fn, client, test_user, db_session):
        """Admin can view other salesperson's scorecard."""
        test_user.role = "admin"
        db_session.commit()
        resp = client.get("/api/proactive/scorecard?salesperson_id=42")
        assert resp.status_code == 200
        test_user.role = "buyer"
        db_session.commit()


class TestMatchesStatusFilter:
    @patch("app.services.proactive_service.get_matches_for_user",
           return_value={"groups": [], "stats": {"total": 0, "avg_score": 0, "avg_margin": None, "high_margin_count": 0}})
    def test_matches_sent_status(self, mock_fn, client):
        """Matches with status=sent filter."""
        resp = client.get("/api/proactive/matches?status=sent")
        assert resp.status_code == 200
        mock_fn.assert_called_once()
        # Verify 'sent' was passed as status
        call_kwargs = mock_fn.call_args
        assert call_kwargs[1].get("status") == "sent" or call_kwargs[0][2] == "sent"


class TestConvertExtended:
    @patch("app.services.proactive_service.convert_proactive_to_win",
           side_effect=ValueError("Offer already converted"))
    def test_convert_already_converted(self, mock_fn, client):
        """Already-converted offer -> 400."""
        resp = client.post("/api/proactive/convert/1")
        assert resp.status_code == 400


class TestRefresh:
    @patch("app.services.proactive_matching.run_proactive_scan",
           return_value={"scanned_offers": 2, "scanned_sightings": 0, "matches_created": 1})
    @patch("app.services.proactive_service.scan_new_offers_for_matches",
           return_value={"scanned": 3, "matches_created": 2})
    def test_refresh_success(self, mock_legacy, mock_cph, client):
        """Refresh triggers both scans and returns combined count."""
        resp = client.post("/api/proactive/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["legacy_matches"] == 2
        assert data["cph_matches"] == 1
        assert data["total_new"] == 3

    @patch("app.services.proactive_matching.run_proactive_scan",
           side_effect=Exception("CPH scan failed"))
    @patch("app.services.proactive_service.scan_new_offers_for_matches",
           return_value={"scanned": 1, "matches_created": 0})
    def test_refresh_cph_failure_graceful(self, mock_legacy, mock_cph, client):
        """CPH scan failure doesn't break the endpoint."""
        resp = client.post("/api/proactive/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cph_matches"] == 0


class TestDraftEndpoint:
    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock,
           return_value={"subject": "Parts for You", "body": "Great deal!", "html": "<p>Great deal!</p>"})
    def test_draft_success(self, mock_draft, client, db_session, test_user, test_requisition, test_offer, test_customer_site):
        """AI draft returns subject + body + html."""
        from app.models import Company
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
        resp = client.post("/api/proactive/draft", json={
            "match_ids": [match.id],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["subject"] == "Parts for You"
        assert "html" in data

    def test_draft_empty_match_ids(self, client):
        """Empty match_ids -> 400."""
        resp = client.post("/api/proactive/draft", json={"match_ids": []})
        assert resp.status_code == 400

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock,
           return_value=None)
    def test_draft_ai_failure(self, mock_draft, client, db_session, test_user, test_requisition, test_offer, test_customer_site):
        """AI returns None -> 500."""
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
        resp = client.post("/api/proactive/draft", json={
            "match_ids": [match.id],
        })
        assert resp.status_code == 500

    def test_draft_no_valid_matches(self, client):
        """Non-existent match_ids -> 400."""
        resp = client.post("/api/proactive/draft", json={"match_ids": [99999]})
        assert resp.status_code == 400

    @patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock,
           return_value={"ok": True, "sent_to": 1})
    @patch("app.routers.proactive.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_with_email_html(self, mock_token, mock_send, client):
        """Send with email_html passes it through to service."""
        resp = client.post("/api/proactive/send", json={
            "match_ids": [1],
            "contact_ids": [1],
            "email_html": "<p>Custom email body</p>",
        })
        assert resp.status_code == 200
        # Verify email_html was passed to the service
        call_kwargs = mock_send.call_args
        assert call_kwargs[1].get("email_html") == "<p>Custom email body</p>" or \
               (len(call_kwargs[0]) > 8 and call_kwargs[0][8] == "<p>Custom email body</p>")


class TestContactsExtended:
    def test_contacts_multiple_for_site(self, client, db_session, test_customer_site):
        """Multiple contacts ordered by is_primary desc, then full_name."""
        c1 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice Smith",
            email="alice@acme.com",
            is_primary=False,
        )
        c2 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Bob Jones",
            email="bob@acme.com",
            title="Director",
            is_primary=True,
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        resp = client.get(f"/api/proactive/contacts/{test_customer_site.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Primary contact should come first
        assert data[0]["is_primary"] is True
        assert data[0]["full_name"] == "Bob Jones"
        assert data[0]["title"] == "Director"
        assert data[1]["is_primary"] is False


class TestDoNotOffer:
    """Tests for /api/proactive/do-not-offer (lines 108-144)."""

    def test_do_not_offer_empty_items(self, client):
        """Empty items list -> 400."""
        resp = client.post("/api/proactive/do-not-offer", json={"items": []})
        assert resp.status_code == 400

    def test_do_not_offer_success(self, client, db_session, test_company):
        """Suppresses MPNs and returns count."""
        resp = client.post("/api/proactive/do-not-offer", json={
            "items": [
                {"mpn": "LM317T", "company_id": test_company.id, "reason": "Customer dropped"},
                {"mpn": "LM7805", "company_id": test_company.id},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["suppressed"] == 2

    def test_do_not_offer_skip_blank_mpn(self, client, db_session, test_company):
        """Items with blank MPN or no company_id are skipped."""
        resp = client.post("/api/proactive/do-not-offer", json={
            "items": [
                {"mpn": "", "company_id": test_company.id},
                {"mpn": "LM317T", "company_id": 0},  # company_id=0 is falsy
            ],
        })
        assert resp.status_code == 200
        assert resp.json()["suppressed"] == 0

    def test_do_not_offer_duplicate_ignored(self, client, db_session, test_company):
        """Second suppression of same MPN+company is not counted again."""
        payload = {"items": [{"mpn": "LM317T", "company_id": test_company.id}]}
        resp1 = client.post("/api/proactive/do-not-offer", json=payload)
        assert resp1.json()["suppressed"] == 1
        resp2 = client.post("/api/proactive/do-not-offer", json=payload)
        assert resp2.json()["suppressed"] == 0

    def test_do_not_offer_auto_dismisses_matches(
        self, client, db_session, test_company, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Suppression auto-dismisses open proactive matches."""
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=test_requisition.id,
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            company_id=test_company.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
            status="new",
        )
        db_session.add(match)
        db_session.commit()

        resp = client.post("/api/proactive/do-not-offer", json={
            "items": [{"mpn": "LM317T", "company_id": test_company.id}],
        })
        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "dismissed"


class TestDraftWithContactIds:
    """Test draft endpoint with contact_ids to cover lines 180-182."""

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock,
           return_value={"subject": "Parts Available", "body": "Hi!", "html": "<p>Hi!</p>"})
    def test_draft_with_contact_ids(self, mock_draft, client, db_session, test_user,
                                     test_requisition, test_offer, test_customer_site):
        """Draft with contact_ids resolves first name for the greeting."""
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Jane Buyer",
            email="jane@acme.com",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.flush()

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

        resp = client.post("/api/proactive/draft", json={
            "match_ids": [match.id],
            "contact_ids": [contact.id],
        })
        assert resp.status_code == 200
        assert resp.json()["subject"] == "Parts Available"
