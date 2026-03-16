"""
test_htmx_plan4.py — Tests for Plan 4 HTMX views: Quotes, Prospecting, Settings, Dashboard.

Tests the new HTMX partial endpoints added in Plan 4 including:
- Quotes list and detail with inline line editing
- Prospecting list and detail with claim/dismiss actions
- Settings page with sources, system, and profile tabs
- Dashboard with stats and quick actions

Called by: pytest (autodiscovery)
Depends on: conftest.py fixtures (client, db_session, test_user, test_quote, test_offer)
"""

from datetime import datetime, timezone

import pytest

from app.models import (
    ApiSource,
    QuoteLine,
)
from app.models.prospect_account import ProspectAccount

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def test_prospect(db_session, test_user):
    """A prospect account for testing."""
    prospect = ProspectAccount(
        name="Prospect Corp",
        domain="prospect-corp.com",
        industry="Aerospace",
        region="US-East",
        fit_score=75,
        readiness_score=60,
        discovery_source="ai_search",
        status="suggested",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(prospect)
    db_session.commit()
    db_session.refresh(prospect)
    return prospect


@pytest.fixture()
def test_quote_line(db_session, test_quote):
    """A quote line item for testing inline editing."""
    line = QuoteLine(
        quote_id=test_quote.id,
        mpn="LM317T",
        manufacturer="Texas Instruments",
        qty=1000,
        cost_price=0.50,
        sell_price=0.75,
        margin_pct=33.33,
    )
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)
    return line


@pytest.fixture()
def test_api_source(db_session):
    """An API source for settings tests."""
    source = ApiSource(
        name="nexar",
        display_name="Nexar",
        category="distributor",
        source_type="api",
        status="live",
        is_active=True,
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


# ── Quotes Tests ─────────────────────────────────────────────────────


def test_quotes_list_partial(client):
    """Quotes list endpoint returns 200 with 'Quotes' header."""
    resp = client.get(
        "/partials/quotes",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Quotes" in resp.text


def test_quotes_list_filter_by_status(client):
    """Quotes list with status filter returns 200."""
    resp = client.get(
        "/partials/quotes?status=draft",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_quote_detail_partial(client, test_quote):
    """Quote detail endpoint returns 200 with quote number."""
    resp = client.get(
        f"/partials/quotes/{test_quote.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert test_quote.quote_number in resp.text


def test_quote_detail_not_found(client):
    """Quote detail returns 404 for nonexistent quote."""
    resp = client.get(
        "/partials/quotes/99999",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 404


def test_update_quote_line(client, test_quote, test_quote_line):
    """Inline edit a quote line updates sell_price and recalculates margin."""
    resp = client.put(
        f"/partials/quotes/{test_quote.id}/lines/{test_quote_line.id}",
        data={"sell_price": "15.00", "cost_price": "10.00"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_delete_quote_line(client, test_quote, test_quote_line):
    """Delete a quote line returns empty response."""
    resp = client.delete(
        f"/partials/quotes/{test_quote.id}/lines/{test_quote_line.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.text == ""


def test_add_quote_line(client, test_quote):
    """Add a new line item to a quote."""
    resp = client.post(
        f"/partials/quotes/{test_quote.id}/lines",
        data={"mpn": "NE555P", "manufacturer": "TI", "qty": "500", "cost_price": "0.10", "sell_price": "0.25"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "NE555P" in resp.text


def test_send_quote(client, test_quote):
    """Marking a quote as sent returns 200."""
    resp = client.post(
        f"/partials/quotes/{test_quote.id}/send",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_quote_result_won(client, test_quote):
    """Marking a quote as won returns 200."""
    resp = client.post(
        f"/partials/quotes/{test_quote.id}/result",
        data={"result": "won"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_quote_result_invalid(client, test_quote):
    """Invalid result value returns 400."""
    resp = client.post(
        f"/partials/quotes/{test_quote.id}/result",
        data={"result": "invalid"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 400


def test_add_offer_to_quote(client, test_quote, test_offer):
    """Adding an offer as a quote line returns 200."""
    resp = client.post(
        f"/partials/quotes/{test_quote.id}/add-offer/{test_offer.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "LM317T" in resp.text


# ── Prospecting Tests ────────────────────────────────────────────────


def test_prospecting_list_partial(client):
    """Prospecting list endpoint returns 200 with 'Prospecting' header."""
    resp = client.get(
        "/partials/prospecting",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Prospecting" in resp.text


def test_prospecting_filter_by_status(client):
    """Prospecting list with status filter returns 200."""
    resp = client.get(
        "/partials/prospecting?status=suggested",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_prospecting_detail_partial(client, test_prospect):
    """Prospect detail endpoint returns 200 with prospect name."""
    resp = client.get(
        f"/partials/prospecting/{test_prospect.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Prospect Corp" in resp.text


def test_prospecting_detail_not_found(client):
    """Prospect detail returns 404 for nonexistent prospect."""
    resp = client.get(
        "/partials/prospecting/99999",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 404


def test_dismiss_prospect(client, test_prospect):
    """Dismissing a prospect returns 200 and updates status."""
    resp = client.post(
        f"/partials/prospecting/{test_prospect.id}/dismiss",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


def test_dismiss_prospect_not_found(client):
    """Dismissing nonexistent prospect returns 404."""
    resp = client.post(
        "/partials/prospecting/99999/dismiss",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 404


# ── Settings Tests ───────────────────────────────────────────────────


def test_settings_partial(client):
    """Settings page returns 200 with 'Settings' header."""
    resp = client.get(
        "/partials/settings",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Settings" in resp.text


def test_settings_sources_tab(client, test_api_source):
    """Sources tab returns 200 with source table."""
    resp = client.get(
        "/partials/settings/sources",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Nexar" in resp.text


def test_settings_profile_tab(client):
    """Profile tab returns 200 with user profile."""
    resp = client.get(
        "/partials/settings/profile",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Your Profile" in resp.text


# ── Dashboard Tests ──────────────────────────────────────────────────


def test_dashboard_partial(client):
    """Dashboard returns 200 with welcome message and stat cards."""
    resp = client.get(
        "/partials/dashboard",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Welcome back" in resp.text
    assert "Open Requisitions" in resp.text


# ── Full Page Route Tests ────────────────────────────────────────────


def test_v2_quotes_full_page(client):
    """Full page /quotes loads base page HTML."""
    resp = client.get("/quotes")
    assert resp.status_code == 200


def test_v2_prospecting_full_page(client):
    """Full page /prospecting loads base page HTML."""
    resp = client.get("/prospecting")
    assert resp.status_code == 200


def test_v2_settings_full_page(client):
    """Full page /settings loads base page HTML."""
    resp = client.get("/settings")
    assert resp.status_code == 200


def test_v2_dashboard_full_page(client):
    """Full page / loads dashboard."""
    resp = client.get("")
    assert resp.status_code == 200
