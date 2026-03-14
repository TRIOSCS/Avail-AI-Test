"""
test_htmx_quotes.py — Tests for Phase 3 Task 8: Quotes + Offers HTMX views.
Verifies quotes list page, rows partial with status filter, quote detail with
line items, and offers gallery rendering.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/quotes/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Company, CustomerSite, Offer, Quote, QuoteLine, Requirement, Requisition, User


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/quotes" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sample_site(db_session):
    """A company + site for quote foreign keys."""
    co = Company(
        name="TestCo",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(
        company_id=co.id,
        site_name="TestCo HQ",
        contact_name="Jane",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def sample_requisition(db_session, test_user):
    """A requisition for linking quotes and offers."""
    req = Requisition(
        name="REQ-QUOTE-001",
        customer_name="TestCo",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def sample_quotes(db_session, test_user, sample_requisition, sample_site):
    """Create quotes with different statuses for filter tests."""
    quotes = []
    for num, status in [("Q-001", "draft"), ("Q-002", "sent"), ("Q-003", "won"), ("Q-004", "lost")]:
        q = Quote(
            requisition_id=sample_requisition.id,
            customer_site_id=sample_site.id,
            quote_number=num,
            status=status,
            line_items=[],
            subtotal=1000.00,
            total_cost=500.00,
            total_margin_pct=50.00,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        quotes.append(q)
    db_session.commit()
    for q in quotes:
        db_session.refresh(q)
    return quotes


@pytest.fixture()
def quote_with_lines(db_session, test_user, sample_requisition, sample_site):
    """A quote with line items for detail tests."""
    q = Quote(
        requisition_id=sample_requisition.id,
        customer_site_id=sample_site.id,
        quote_number="Q-DETAIL-001",
        status="draft",
        line_items=[],
        subtotal=2500.00,
        total_cost=1200.00,
        total_margin_pct=52.00,
        payment_terms="Net 30",
        notes="Test quote notes here.",
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.flush()

    lines = []
    for mpn, qty, cost, sell in [("LM317T", 100, 0.50, 0.75), ("NE555P", 200, 0.30, 0.50)]:
        line = QuoteLine(
            quote_id=q.id,
            mpn=mpn,
            qty=qty,
            cost_price=cost,
            sell_price=sell,
        )
        db_session.add(line)
        lines.append(line)
    db_session.commit()
    db_session.refresh(q)
    for ln in lines:
        db_session.refresh(ln)
    return q, lines


@pytest.fixture()
def sample_offers(db_session, test_user, sample_requisition):
    """Create offers for the offers gallery test."""
    offers = []
    for vendor, mpn, price in [("Arrow", "LM317T", 0.50), ("Mouser", "NE555P", 0.30)]:
        o = Offer(
            requisition_id=sample_requisition.id,
            vendor_name=vendor,
            mpn=mpn,
            qty_available=1000,
            unit_price=price,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        offers.append(o)
    db_session.commit()
    for o in offers:
        db_session.refresh(o)
    return offers


class TestQuotesListPage:
    """Tests for GET /views/quotes — full page HTML."""

    def test_quotes_list_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/quotes")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_quotes_list_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/quotes")
        assert "quote-table-body" in resp.text
        assert "Quotes" in resp.text

    def test_quotes_list_has_filter_tabs(self, htmx_client):
        resp = htmx_client.get("/views/quotes")
        for tab in ["All", "Draft", "Sent", "Won", "Lost"]:
            assert tab in resp.text

    def test_quotes_list_has_search(self, htmx_client):
        resp = htmx_client.get("/views/quotes")
        assert 'type="search"' in resp.text
        assert "delay:300ms" in resp.text

    def test_quotes_list_shows_data(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes")
        assert resp.status_code == 200
        assert "Q-001" in resp.text
        assert "Q-002" in resp.text


class TestQuotesRowsPartial:
    """Tests for GET /views/quotes/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_quote_numbers(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows")
        assert "Q-001" in resp.text
        assert "Q-002" in resp.text

    def test_rows_empty_shows_message(self, htmx_client):
        resp = htmx_client.get("/views/quotes/rows")
        assert resp.status_code == 200
        assert "No quotes found" in resp.text


class TestQuoteStatusFilter:
    """Tests for status filter on quotes rows."""

    def test_filter_draft(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows?status=draft")
        assert "Q-001" in resp.text
        assert "Q-002" not in resp.text

    def test_filter_sent(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows?status=sent")
        assert "Q-002" in resp.text
        assert "Q-001" not in resp.text

    def test_filter_won(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows?status=won")
        assert "Q-003" in resp.text
        assert "Q-001" not in resp.text

    def test_filter_lost(self, htmx_client, sample_quotes):
        resp = htmx_client.get("/views/quotes/rows?status=lost")
        assert "Q-004" in resp.text
        assert "Q-001" not in resp.text


class TestQuoteDetail:
    """Tests for GET /views/quotes/{id} — detail page with line items."""

    def test_detail_returns_html(self, htmx_client, quote_with_lines):
        quote, _ = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_shows_quote_number(self, htmx_client, quote_with_lines):
        quote, _ = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        assert "Q-DETAIL-001" in resp.text

    def test_detail_shows_line_items(self, htmx_client, quote_with_lines):
        quote, lines = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        assert "LM317T" in resp.text
        assert "NE555P" in resp.text

    def test_detail_shows_summary(self, htmx_client, quote_with_lines):
        quote, _ = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        assert "2500.00" in resp.text
        assert "1200.00" in resp.text

    def test_detail_shows_notes(self, htmx_client, quote_with_lines):
        quote, _ = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        assert "Test quote notes here." in resp.text

    def test_detail_has_action_buttons(self, htmx_client, quote_with_lines):
        quote, _ = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        # Draft quote should have Send button
        assert "Send Quote" in resp.text

    def test_detail_has_inline_edit_inputs(self, htmx_client, quote_with_lines):
        quote, lines = quote_with_lines
        resp = htmx_client.get(f"/views/quotes/{quote.id}")
        # Line items should have editable inputs with hx-put triggers
        assert "hx-put" in resp.text
        assert 'hx-trigger="change"' in resp.text

    def test_detail_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/quotes/99999")
        assert resp.status_code == 404


class TestOfferCardRender:
    """Tests for GET /views/offers — offers gallery with offer cards."""

    def test_offers_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/offers")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_offers_page_shows_title(self, htmx_client):
        resp = htmx_client.get("/views/offers")
        assert "Offers" in resp.text

    def test_offers_page_shows_data(self, htmx_client, sample_offers):
        resp = htmx_client.get("/views/offers")
        assert "Arrow" in resp.text
        assert "Mouser" in resp.text
        assert "LM317T" in resp.text

    def test_offers_page_has_expand_toggle(self, htmx_client, sample_offers):
        resp = htmx_client.get("/views/offers")
        assert "x-data" in resp.text
        assert "expanded" in resp.text

    def test_offers_page_has_accept_reject(self, htmx_client, sample_offers):
        resp = htmx_client.get("/views/offers")
        assert "Accept" in resp.text
        assert "Reject" in resp.text

    def test_offers_empty_shows_message(self, htmx_client):
        resp = htmx_client.get("/views/offers")
        assert "No offers found" in resp.text

    def test_offers_search_filter(self, htmx_client, sample_offers):
        resp = htmx_client.get("/views/offers?q=Arrow")
        assert "Arrow" in resp.text
        assert "Mouser" not in resp.text
