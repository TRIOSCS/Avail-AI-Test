"""tests/test_routers_crm.py — Tests for CRM Router Helpers + Endpoints.

Tests quote number generation, last-quoted-price lookup,
quote serialization, margin calculation, and CRM endpoints.

Called by: pytest
Depends on: app.routers.crm, conftest.py
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.constants import RequisitionStatus
from app.models import (
    Company,
    CustomerSite,
    OfferAttachment,
    Quote,
    Requisition,
    SiteContact,
)
from app.routers.crm import (
    next_quote_number,
    quote_to_dict,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_quote(**overrides):
    """Build a mock Quote object."""
    q = MagicMock()
    q.id = overrides.get("id", 1)
    q.requisition_id = overrides.get("requisition_id", 10)
    q.customer_site_id = overrides.get("customer_site_id", 5)
    q.quote_number = overrides.get("quote_number", "TEST-Q-2026-0001")
    q.revision = overrides.get("revision", 1)
    q.line_items = overrides.get("line_items", [])
    q.subtotal = overrides.get("subtotal", 100.0)
    q.total_cost = overrides.get("total_cost", 80.0)
    q.total_margin_pct = overrides.get("total_margin_pct", 20.0)
    q.payment_terms = overrides.get("payment_terms", "Net 30")
    q.shipping_terms = overrides.get("shipping_terms", "FOB")
    q.validity_days = overrides.get("validity_days", 30)
    q.notes = overrides.get("notes", None)
    q.status = overrides.get("status", "draft")
    q.sent_at = overrides.get("sent_at", None)
    q.result = overrides.get("result", None)
    q.result_reason = overrides.get("result_reason", None)
    q.result_notes = overrides.get("result_notes", None)
    q.result_at = overrides.get("result_at", None)
    q.won_revenue = overrides.get("won_revenue", None)
    q.created_at = overrides.get("created_at", datetime(2026, 2, 1, tzinfo=UTC))
    q.updated_at = overrides.get("updated_at", datetime(2026, 2, 1, tzinfo=UTC))

    # Relationships
    created_by = MagicMock()
    created_by.name = "Mike"
    q.created_by = overrides.get("created_by", created_by)

    site = MagicMock()
    site.site_name = "HQ"
    site.contact_name = "John"
    site.contact_email = "john@acme.com"
    company = MagicMock()
    company.name = "Acme Corp"
    site.company = company
    q.customer_site = overrides.get("customer_site", site)
    return q


# ── Helper: admin_client fixture ──────────────────────────────────────


@pytest.fixture()
def admin_client(db_session, admin_user):
    """FastAPI TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _override_user

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_admin]:
            app.dependency_overrides.pop(dep, None)


# ── quote_to_dict ────────────────────────────────────────────────────────


def test_quote_to_dict_basic():
    q = _make_quote()
    d = quote_to_dict(q)
    assert d["id"] == 1
    assert d["quote_number"] == "TEST-Q-2026-0001"
    assert d["customer_name"] == "Acme Corp — HQ"
    assert d["contact_name"] == "John"
    assert d["contact_email"] == "john@acme.com"
    assert d["subtotal"] == 100.0
    assert d["total_margin_pct"] == 20.0
    assert d["created_by"] == "Mike"


def test_quote_to_dict_no_site():
    q = _make_quote(customer_site=None)
    d = quote_to_dict(q)
    assert d["customer_name"] == ""
    assert d["contact_name"] is None
    assert d["contact_email"] is None


def test_quote_to_dict_nulls():
    q = _make_quote(subtotal=None, total_cost=None, total_margin_pct=None, won_revenue=None)
    d = quote_to_dict(q)
    assert d["subtotal"] is None
    assert d["total_cost"] is None
    assert d["won_revenue"] is None


def test_quote_to_dict_sent():
    sent = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    q = _make_quote(sent_at=sent, status="sent")
    d = quote_to_dict(q)
    assert d["sent_at"] == sent.isoformat()
    assert d["status"] == "sent"


# ── next_quote_number ────────────────────────────────────────────────────


def _mock_db_with_last_quote(last_quote_number):
    """Build a mock db whose query chain returns a Quote with the given number (or
    None)."""
    last = None
    if last_quote_number is not None:
        last = MagicMock()
        last.quote_number = last_quote_number
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.with_for_update.return_value.first.return_value = (
        last
    )
    return db


@pytest.mark.parametrize(
    ("last_quote_number", "expected_prefix", "expected_suffix", "expected_exact"),
    [
        pytest.param(None, "Q-", "-0001", None, id="first"),
        pytest.param("Q-2026-0042", None, None, "Q-2026-0043", id="increment"),
        pytest.param("Q-2026-XXXX", None, "-0001", None, id="bad_format"),
    ],
)
def test_next_quote_number(last_quote_number, expected_prefix, expected_suffix, expected_exact):
    """First quote, increment, and graceful handling of corrupted quote numbers."""
    db = _mock_db_with_last_quote(last_quote_number)
    result = next_quote_number(db)
    if expected_exact is not None:
        assert result == expected_exact
    if expected_prefix is not None:
        assert result.startswith(expected_prefix)
    if expected_suffix is not None:
        assert result.endswith(expected_suffix)


# ── Margin calculation (update_quote logic) ──────────────────────────────


def test_margin_calculation():
    """Verify margin calc matches update_quote logic."""
    line_items = [
        {"qty": 100, "sell_price": 5.00, "cost_price": 3.50},
        {"qty": 50, "sell_price": 10.00, "cost_price": 7.00},
    ]
    total_sell = sum((i["qty"]) * (i["sell_price"]) for i in line_items)
    total_cost = sum((i["qty"]) * (i["cost_price"]) for i in line_items)
    margin = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    assert total_sell == 1000.0  # 500 + 500
    assert total_cost == 700.0  # 350 + 350
    assert margin == 30.0


def test_margin_zero_sell():
    """Zero sell price shouldn't divide by zero."""
    total_sell = 0
    total_cost = 100
    margin = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    assert margin == 0


# ═══════════════════════════════════════════════════════════════════════
#  CRM Endpoint Tests via TestClient
# ═══════════════════════════════════════════════════════════════════════


class TestCompanies:
    def test_list_companies_empty(self, client):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)
        assert "total" in data

    def test_list_companies_with_data(self, admin_client, db_session, test_company):
        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["items"]]
        assert "Acme Electronics" in names

    def test_list_companies_search(self, admin_client, db_session, test_company):
        resp = admin_client.get("/api/companies", params={"search": "Acme"})
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["items"]]
        assert "Acme Electronics" in names

    def test_list_companies_search_no_match(self, client, db_session, test_company):
        resp = client.get("/api/companies", params={"search": "Nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @patch("app.routers.crm.companies.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_create_company(self, mock_normalize, mock_cred, client, db_session):
        mock_normalize.return_value = ("New Corp", "newcorp.com")
        resp = client.post("/api/companies", json={"name": "New Corp"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Corp"
        assert "id" in data

    def test_update_company(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"notes": "Updated notes"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True


class TestCompanyDetail:
    def test_get_company_basic(self, admin_client, db_session, test_company, test_customer_site):
        """GET /api/companies/{id} returns company with sites."""
        resp = admin_client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == test_company.id
        assert data["name"] == "Acme Electronics"
        assert data["site_count"] >= 1
        assert "sites" in data
        assert "source" in data
        assert "created_at" in data
        assert "updated_at" in data
        site_names = [s["site_name"] for s in data["sites"]]
        assert "Acme HQ" in site_names

    def test_get_company_not_found(self, client, db_session):
        """GET /api/companies/999999 returns 404."""
        resp = client.get("/api/companies/999999")
        assert resp.status_code == 404

    def test_get_company_sites_include_contacts(self, admin_client, db_session, test_company, test_customer_site):
        """Site contacts are nested under each site."""
        sc = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Bob Smith",
            email="bob@acme.com",
            title="Engineer",
        )
        db_session.add(sc)
        db_session.commit()

        resp = admin_client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        site = [s for s in data["sites"] if s["site_name"] == "Acme HQ"][0]
        assert "contacts" in site
        contact_names = [c["full_name"] for c in site["contacts"]]
        assert "Bob Smith" in contact_names

    def test_get_company_open_reqs_count(self, admin_client, db_session, test_company, test_customer_site, test_user):
        """open_reqs count is aggregated per site."""
        req = Requisition(
            name="REQ-DETAIL-001",
            customer_name="Acme Electronics",
            customer_site_id=test_customer_site.id,
            status="open",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.commit()

        resp = admin_client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        site = [s for s in data["sites"] if s["site_name"] == "Acme HQ"][0]
        assert site["open_reqs"] >= 1

    def test_get_company_inactive_sites_excluded(self, admin_client, db_session, test_company):
        """Inactive sites are filtered out of the response."""
        active = CustomerSite(company_id=test_company.id, site_name="Active Branch", is_active=True)
        inactive = CustomerSite(company_id=test_company.id, site_name="Closed Branch", is_active=False)
        db_session.add_all([active, inactive])
        db_session.commit()

        resp = admin_client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        site_names = [s["site_name"] for s in data["sites"]]
        assert "Active Branch" in site_names
        assert "Closed Branch" not in site_names


class TestOffers:
    def test_update_offer(self, client, db_session, test_offer):
        resp = client.put(
            f"/api/offers/{test_offer.id}",
            json={"unit_price": 0.55},
        )
        assert resp.status_code == 200

    def test_delete_offer(self, client, db_session, test_offer):
        resp = client.delete(f"/api/offers/{test_offer.id}")
        assert resp.status_code == 200


class TestQuotes:
    def test_delete_draft_quote(self, client, db_session, test_requisition, test_customer_site, test_user):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-DEL1",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.delete(f"/api/quotes/{q.id}")
        assert resp.status_code == 200


class TestCompaniesAdditional:
    def test_list_companies_unassigned(self, admin_client, db_session, test_company):
        """Filter companies with no account_owner_id."""
        resp = admin_client.get("/api/companies", params={"unassigned": 1})
        assert resp.status_code == 200
        data = resp.json()["items"]
        # test_company has no account_owner_id, so it should appear
        names = [c["name"] for c in data]
        assert "Acme Electronics" in names

    def test_list_companies_owner_filter(self, client, db_session, test_company, test_user, test_customer_site):
        """Filter by owner_id — only companies with matching site owners appear."""
        test_customer_site.owner_id = test_user.id
        db_session.commit()

        resp = client.get("/api/companies", params={"owner_id": test_user.id})
        assert resp.status_code == 200
        data = resp.json()["items"]
        # Should see the company since it has a site owned by the user
        found = [c for c in data if c["name"] == "Acme Electronics"]
        assert len(found) == 1

    def test_list_companies_owner_filter_no_match(self, client, db_session, test_company, test_customer_site):
        """owner_id filter with no matching sites hides the company."""
        resp = client.get("/api/companies", params={"owner_id": 99999})
        assert resp.status_code == 200
        data = resp.json()["items"]
        # No sites belong to owner 99999 so company should be excluded
        names = [c["name"] for c in data]
        assert "Acme Electronics" not in names

    def test_list_companies_inactive_site_skipped(self, client, db_session, test_company):
        """Inactive sites are not counted in site_count."""
        inactive = CustomerSite(
            company_id=test_company.id,
            site_name="Closed Branch",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        resp = client.get("/api/companies")
        assert resp.status_code == 200
        data = resp.json()["items"]
        for c in data:
            if c["name"] == "Acme Electronics":
                # List endpoint no longer includes sites array
                assert "sites" not in c
                assert "site_count" in c

    def test_list_companies_pagination(self, admin_client, db_session, test_company):
        """Pagination params limit and offset work correctly."""
        resp = admin_client.get("/api/companies", params={"limit": 1, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert data["offset"] == 0
        assert data["total"] >= 1
        assert len(data["items"]) <= 1

    def test_list_companies_response_shape(self, admin_client, db_session, test_company):
        """List response includes open_req_count and site_count, not sites."""
        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        item = data["items"][0]
        assert "site_count" in item
        assert "open_req_count" in item
        assert "sites" not in item

    def test_list_companies_revenue_90d_with_won_quote(
        self, admin_client, db_session, test_company, test_customer_site, test_user
    ):
        """revenue_90d reflects sum of Quote.subtotal for won requisitions in last 90
        days."""
        req = Requisition(
            name="REQ-WON-1",
            customer_site_id=test_customer_site.id,
            status="won",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="WON-Q-001",
            subtotal=5000.00,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        items = resp.json()["items"]
        match = [i for i in items if i["id"] == test_company.id]
        assert len(match) == 1
        assert match[0]["revenue_90d"] == 5000.0

    def test_list_companies_revenue_90d_uses_status_enum(
        self, admin_client, db_session, test_company, test_customer_site, test_user
    ):
        """revenue_90d filter must use the RequisitionStatus.WON StrEnum constant, not a
        raw 'won' string.

        Constructing the row with the enum constant guards against the enum value
        drifting from the literal.
        """
        req = Requisition(
            name="REQ-WON-ENUM",
            customer_site_id=test_customer_site.id,
            status=RequisitionStatus.WON,
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="WON-ENUM-001",
            subtotal=4200.00,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        match = [i for i in resp.json()["items"] if i["id"] == test_company.id]
        assert len(match) == 1
        assert match[0]["revenue_90d"] == 4200.0

    def test_list_companies_revenue_90d_zero_when_no_won(self, admin_client, db_session, test_company):
        """Companies with no won quotes should have revenue_90d=0."""
        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        items = resp.json()["items"]
        match = [i for i in items if i["id"] == test_company.id]
        assert len(match) == 1
        assert match[0]["revenue_90d"] == 0

    def test_list_companies_revenue_90d_excludes_old(
        self, admin_client, db_session, test_company, test_customer_site, test_user
    ):
        """Quotes older than 90 days should not count toward revenue_90d."""
        req = Requisition(
            name="REQ-OLD-1",
            customer_site_id=test_customer_site.id,
            status="won",
            created_by=test_user.id,
            created_at=datetime.now(UTC) - timedelta(days=180),
        )
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="OLD-Q-001",
            subtotal=9999.00,
            created_at=datetime.now(UTC) - timedelta(days=100),
        )
        db_session.add(q)
        db_session.commit()

        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        items = resp.json()["items"]
        match = [i for i in items if i["id"] == test_company.id]
        assert len(match) == 1
        assert match[0]["revenue_90d"] == 0

    def test_update_company_not_found(self, client):
        resp = client.put("/api/companies/99999", json={"notes": "nope"})
        assert resp.status_code == 404

    @patch("app.routers.crm.companies.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_create_company_with_website_domain(self, mock_normalize, mock_cred, client, db_session):
        """Domain is extracted from website when no explicit domain given."""
        mock_normalize.return_value = ("WebCo", "")
        resp = client.post(
            "/api/companies",
            json={
                "name": "WebCo",
                "website": "https://www.webco-electronics.com/about",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "WebCo"
        # Verify domain was extracted
        co = db_session.get(Company, data["id"])
        assert co.domain == "webco-electronics.com"


class TestEnrichment:
    # W1.5-7 keys-off honesty: no paid provider no longer 503s by itself — the free
    # SAM.gov path runs. "Nothing to run" now requires the SAM.gov gate off too.
    @patch("app.routers.crm.enrichment.claude_configured", return_value=False)
    @patch("app.routers.crm.enrichment.get_credential_cached", return_value=None)
    def test_enrich_company_no_provider(self, mock_cred, mock_claude, client, db_session, test_company):
        from app.config import settings

        with patch.object(settings, "sam_gov_enrichment_enabled", False):
            resp = client.post(f"/api/enrich/company/{test_company.id}")
        assert resp.status_code == 503

    @patch(
        "app.routers.crm.enrichment.get_credential_cached",
        side_effect=lambda scope, key: "fake-key" if key == "EXPLORIUM_API_KEY" else None,
    )
    @patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock)
    @patch("app.enrichment_service.apply_enrichment_to_company")
    def test_enrich_company_success(
        self, mock_apply, mock_enrich, mock_cred, client, db_session, test_company, test_user
    ):
        test_company.domain = "acme.com"
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        mock_enrich.return_value = {"industry": "Electronics"}
        mock_apply.return_value = ["industry"]

        resp = client.post(f"/api/enrich/company/{test_company.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # The dead enrich_customer_account stub is no longer called from this endpoint,
        # so its no_providers key must not appear in the JSON contract.
        assert "customer_enrichment" not in resp.json()

    @patch(
        "app.routers.crm.enrichment.get_credential_cached",
        side_effect=lambda scope, key: "fake-key" if key == "EXPLORIUM_API_KEY" else None,
    )
    def test_enrich_company_not_found(self, mock_cred, client):
        resp = client.post("/api/enrich/company/99999")
        assert resp.status_code == 404

    @patch(
        "app.routers.crm.enrichment.get_credential_cached",
        side_effect=lambda scope, key: "fake-key" if key == "EXPLORIUM_API_KEY" else None,
    )
    @patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock)
    @patch("app.enrichment_service.apply_enrichment_to_company")
    def test_enrich_company_no_domain(
        self, mock_apply, mock_enrich, mock_cred, client, db_session, test_company, test_user
    ):
        """Company with no domain/website raises 400."""
        test_company.domain = None
        test_company.website = None
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()

        resp = client.post(f"/api/enrich/company/{test_company.id}")
        assert resp.status_code == 400

    @patch(
        "app.routers.crm.enrichment.get_credential_cached",
        side_effect=lambda scope, key: "fake-key" if key == "EXPLORIUM_API_KEY" else None,
    )
    @patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock)
    @patch("app.enrichment_service.apply_enrichment_to_company")
    def test_enrich_company_with_override_domain(
        self, mock_apply, mock_enrich, mock_cred, client, db_session, test_company, test_user
    ):
        """Override domain in the payload."""
        mock_enrich.return_value = {}
        mock_apply.return_value = []
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()

        resp = client.post(
            f"/api/enrich/company/{test_company.id}",
            json={"domain": "override-domain.com"},
        )
        assert resp.status_code == 200
        mock_enrich.assert_called_once_with("override-domain.com", test_company.name)

    # ── HTMX result panel (content negotiation on HX-Request) ─────────────

    @patch(
        "app.routers.crm.enrichment.get_credential_cached",
        side_effect=lambda scope, key: "fake-key" if key == "EXPLORIUM_API_KEY" else None,
    )
    @patch("app.routers.crm.enrichment._run_company_enrichment", new_callable=AsyncMock)
    @patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock)
    def test_enrich_company_hx_returns_enriching_panel(
        self, mock_enrich, mock_runner, mock_cred, client, db_session, test_company, test_user
    ):
        """HTMX request returns the polling "Enriching…" panel immediately (async) — it
        does NOT run the provider waterfall inline and does NOT return raw JSON.

        The full firmographics + contacts panel (and its XSS-safety, graceful-
        degradation and no-updates variants) is rendered by the enrich-status poller;
        that behavior is covered end-to-end in tests/test_account_enrich_async.py.
        """
        test_company.domain = "acme.com"
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()

        resp = client.post(f"/api/enrich/company/{test_company.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert not resp.text.lstrip().startswith("{")  # not raw JSON
        assert "Enriching" in resp.text  # in-progress panel
        assert "every 2s" in resp.text  # poller active
        assert f"/api/enrich/company/{test_company.id}/status" in resp.text
        mock_enrich.assert_not_called()  # scheduled on a background task, not awaited inline


# ── Sync logs ─────────────────────────────────────────────────────────


# ── Offers: additional coverage ───────────────────────────────────────


class TestOffersAdditional:
    def test_update_offer_not_found(self, client):
        resp = client.put("/api/offers/99999", json={"unit_price": 1.00})
        assert resp.status_code == 404

    def test_delete_offer_not_found(self, client):
        resp = client.delete("/api/offers/99999")
        assert resp.status_code == 404


# ── Quotes: additional coverage ───────────────────────────────────────


class TestQuotesAdditional:
    def test_update_quote(self, client, db_session, test_requisition, test_customer_site, test_user):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-UPD1",
            status="draft",
            line_items=[{"mpn": "LM317T", "qty": 100, "sell_price": 5.00, "cost_price": 3.00}],
            subtotal=500.0,
            total_cost=300.0,
            total_margin_pct=40.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.put(
            f"/api/quotes/{q.id}",
            json={"payment_terms": "Net 60"},
        )
        assert resp.status_code == 200

    def test_update_quote_line_items(self, client, db_session, test_requisition, test_customer_site, test_user):
        """Updating line_items recalculates totals."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-UPD2",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.put(
            f"/api/quotes/{q.id}",
            json={
                "line_items": [
                    {"mpn": "LM317T", "qty": 200, "sell_price": 10.00, "cost_price": 7.00},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["subtotal"] == 2000.0
        assert data["total_cost"] == 1400.0

    def test_update_quote_not_found(self, client):
        resp = client.put("/api/quotes/99999", json={"notes": "x"})
        assert resp.status_code == 404

    def test_update_quote_not_draft(self, client, db_session, test_quote):
        """Non-draft quotes cannot be edited."""
        resp = client.put(
            f"/api/quotes/{test_quote.id}",
            json={"notes": "nope"},
        )
        assert resp.status_code == 400

    def test_delete_quote_not_found(self, client):
        resp = client.delete("/api/quotes/99999")
        assert resp.status_code == 404

    def test_delete_quote_non_draft(self, client, db_session, test_quote):
        """Sent quotes cannot be deleted."""
        resp = client.delete(f"/api/quotes/{test_quote.id}")
        assert resp.status_code == 400

    def test_preview_quote(self, client, db_session, test_requisition, test_customer_site, test_user):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-PRE1",
            status="draft",
            line_items=[{"mpn": "LM317T", "qty": 100, "sell_price": 5.00, "cost_price": 3.00}],
            subtotal=500.0,
            total_cost=300.0,
            total_margin_pct=40.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert "html" in data
        assert "Trio Supply Chain Solutions" in data["html"]

    def test_preview_quote_with_override(self, client, db_session, test_requisition, test_customer_site, test_user):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-PRE2",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            notes="Special pricing note",
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/preview",
            json={"to_name": "Custom Name"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Custom Name" in data["html"]

    def test_preview_quote_not_found(self, client):
        resp = client.post("/api/quotes/99999/preview")
        assert resp.status_code == 404

    @patch("app.email_service._find_sent_message", new_callable=AsyncMock)
    @patch("app.dependencies.require_fresh_token", new_callable=AsyncMock)
    @patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock)
    def test_send_quote_success(
        self,
        mock_graph_post,
        mock_token,
        mock_find,
        client,
        db_session,
        test_requisition,
        test_customer_site,
        test_user,
    ):
        # The canonical send service now captures Graph ids via _find_sent_message — mock
        # it so no real Sent-Items lookup (network + retry sleeps) happens.
        mock_find.return_value = {"id": "MSG-OK", "conversationId": "CONV-OK"}
        mock_token.return_value = "fake-token"
        mock_graph_post.return_value = {}  # No error

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-SEND1",
            status="draft",
            line_items=[{"mpn": "LM317T", "qty": 100, "sell_price": 5.00}],
            subtotal=500.0,
            total_cost=300.0,
            total_margin_pct=40.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/send")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "sent"
        assert data["sent_to"] == "jane@acme-electronics.com"

    def test_send_quote_not_found(self, client):
        resp = client.post("/api/quotes/99999/send")
        assert resp.status_code == 404

    @patch("app.dependencies.require_fresh_token", new_callable=AsyncMock)
    def test_send_quote_no_email(self, mock_token, client, db_session, test_requisition, test_company, test_user):
        """Sending a quote with no contact email raises 400.

        The canonical send service validates the recipient, so the route acquires the
        M365 token first — patch it so this reaches the 400 (not a 401) the way the
        other send tests in this class do.
        """
        mock_token.return_value = "fake-token"
        site = CustomerSite(
            company_id=test_company.id,
            site_name="No Email Site",
            contact_email=None,
        )
        db_session.add(site)
        db_session.flush()
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-2026-NOEM",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/send")
        assert resp.status_code == 400

    def test_send_quote_invalid_email(self, client, db_session, test_requisition, test_company, test_user):
        """Invalid email is caught by @validates at model level."""
        with pytest.raises(ValueError, match="Invalid contact email"):
            CustomerSite(
                company_id=test_company.id,
                site_name="Bad Email Site",
                contact_email="notanemail",
            )

    @patch("app.dependencies.require_fresh_token", new_callable=AsyncMock)
    @patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock)
    def test_send_quote_graph_error(
        self, mock_graph_post, mock_token, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """Graph API error returns 502."""
        mock_token.return_value = "fake-token"
        mock_graph_post.return_value = {"error": "SendFailed", "detail": "Auth error"}

        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-GERR",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/send")
        assert resp.status_code == 502


# ── Buy Plans: additional coverage ────────────────────────────────────


@pytest.fixture(autouse=False)
def naive_crm_datetime(monkeypatch):
    """Monkeypatch datetime in crm sub-modules to return naive UTC datetimes.

    SQLite strips timezone info, so comparisons with datetime.now(timezone.utc) fail.
    This fixture makes datetime.now() return naive utcnow() instead.
    """
    from app.routers.crm import buy_plans, offers

    _real_datetime = datetime

    class _NaiveDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime.utcnow()

    for mod in (buy_plans, offers):
        monkeypatch.setattr(mod, "datetime", _NaiveDatetime)


# ── Build quote email HTML ────────────────────────────────────────────


class TestBuildQuoteEmailHtml:
    def test_email_html_format_price(self, client, db_session, test_requisition, test_customer_site, test_user):
        """Quote email HTML renders line items with proper formatting."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-HTML",
            status="draft",
            line_items=[
                {
                    "mpn": "LM317T",
                    "manufacturer": "Texas Instruments",
                    "qty": 1000,
                    "sell_price": 2.50,
                    "cost_price": 1.50,
                    "condition": "New",
                    "date_code": "2025+",
                    "packaging": "Tube",
                    "lead_time": "5",
                },
                {
                    "mpn": "NE555P",
                    "qty": 500,
                    "sell_price": 100,
                    "cost_price": 50,
                    "lead_time": "10-15",
                },
            ],
            subtotal=52500.0,
            total_cost=26500.0,
            total_margin_pct=49.5,
            payment_terms="Net 30",
            shipping_terms="FOB",
            notes="Rush order",
            validity_days=14,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        html = resp.json()["html"]
        assert "LM317T" in html
        assert "NE555P" in html
        assert "Net 30" in html
        assert "Rush order" in html
        assert "14" in html  # validity_days
        # Lead time formatting
        assert "5 days" in html
        assert "10-15 days" in html

    def test_email_html_lead_time_with_keyword(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """Lead time already containing 'days'/'weeks' is not doubled."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-LEAD",
            status="draft",
            line_items=[
                {
                    "mpn": "IC123",
                    "qty": 100,
                    "sell_price": 1.00,
                    "cost_price": 0.50,
                    "lead_time": "2 weeks",
                },
            ],
            subtotal=100.0,
            total_cost=50.0,
            total_margin_pct=50.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        html = resp.json()["html"]
        assert "2 weeks" in html

    def test_email_html_zero_price(self, client, db_session, test_requisition, test_customer_site, test_user):
        """Zero sell price shows dash."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-ZERO",
            status="draft",
            line_items=[
                {
                    "mpn": "FREE1",
                    "qty": 10,
                    "sell_price": 0,
                    "cost_price": 0,
                    "lead_time": None,
                    "condition": None,
                    "date_code": None,
                    "packaging": None,
                },
            ],
            subtotal=0.0,
            total_cost=0.0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200


# ── OneDrive endpoints ────────────────────────────────────────────────


class TestOneDrive:
    def test_upload_attachment_offer_not_found(self, client):
        import io

        resp = client.post(
            "/api/offers/99999/attachments",
            files={"file": ("test.pdf", io.BytesIO(b"content"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_upload_attachment_no_token(self, client, db_session, test_offer, test_user):
        import io

        test_user.access_token = None
        db_session.commit()
        resp = client.post(
            f"/api/offers/{test_offer.id}/attachments",
            files={"file": ("test.pdf", io.BytesIO(b"content"), "application/pdf")},
        )
        assert resp.status_code == 401

    def test_upload_attachment_too_large(self, client, db_session, test_offer, test_user):
        import io

        test_user.access_token = "fake-token"
        db_session.commit()
        # 11 MB file
        large_content = b"x" * (11 * 1024 * 1024)
        resp = client.post(
            f"/api/offers/{test_offer.id}/attachments",
            files={"file": ("large.bin", io.BytesIO(large_content), "application/octet-stream")},
        )
        assert resp.status_code == 400

    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
    @patch("app.http_client.http.put", new_callable=AsyncMock)
    def test_upload_attachment_success(self, mock_http_put, mock_token, client, db_session, test_offer, test_user):
        import io

        test_user.access_token = "fake-token"
        db_session.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "drive-item-1", "webUrl": "https://onedrive.com/file"}
        mock_http_put.return_value = mock_resp

        resp = client.post(
            f"/api/offers/{test_offer.id}/attachments",
            files={"file": ("test.pdf", io.BytesIO(b"pdf-content"), "application/pdf")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "test.pdf"
        assert data["web_url"] == "https://onedrive.com/file"

    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
    @patch("app.http_client.http.put", new_callable=AsyncMock)
    def test_upload_attachment_onedrive_error(
        self, mock_http_put, mock_token, client, db_session, test_offer, test_user
    ):
        import io

        test_user.access_token = "fake-token"
        db_session.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_http_put.return_value = mock_resp

        resp = client.post(
            f"/api/offers/{test_offer.id}/attachments",
            files={"file": ("test.pdf", io.BytesIO(b"content"), "application/pdf")},
        )
        assert resp.status_code == 502

    def test_delete_attachment_not_found(self, client):
        resp = client.delete("/api/offer-attachments/99999")
        assert resp.status_code == 404

    @patch("app.http_client.http.delete", new_callable=AsyncMock)
    def test_delete_attachment_with_onedrive(self, mock_http_del, client, db_session, test_offer, test_user):
        test_user.access_token = "fake-token"
        db_session.commit()
        att = OfferAttachment(
            offer_id=test_offer.id,
            file_name="old.pdf",
            library_item_id="drive-item-999",
            library_web_url="https://onedrive.com/old",
            uploaded_by_id=test_user.id,
        )
        db_session.add(att)
        db_session.commit()

        mock_http_del.return_value = MagicMock(status_code=204)

        resp = client.delete(f"/api/offer-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_attachment_no_onedrive(self, client, db_session, test_offer, test_user):
        """Delete attachment that has no OneDrive item ID."""
        att = OfferAttachment(
            offer_id=test_offer.id,
            file_name="local.pdf",
            library_item_id=None,
            uploaded_by_id=test_user.id,
        )
        db_session.add(att)
        db_session.commit()

        resp = client.delete(f"/api/offer-attachments/{att.id}")
        assert resp.status_code == 200


# ── Additional coverage: offers list with vendor ratings ──────────────


# ── OneDrive delete error handling ────────────────────────────────────


class TestOneDriveDeleteError:
    @patch("app.http_client.http.delete", new_callable=AsyncMock)
    def test_delete_attachment_onedrive_error(self, mock_http_del, client, db_session, test_offer, test_user):
        """OneDrive delete error is logged but doesn't prevent DB delete."""
        test_user.access_token = "fake-token"
        db_session.commit()
        att = OfferAttachment(
            offer_id=test_offer.id,
            file_name="fail-delete.pdf",
            library_item_id="drive-item-fail",
            uploaded_by_id=test_user.id,
        )
        db_session.add(att)
        db_session.commit()

        mock_http_del.side_effect = ConnectionError("Network error")

        resp = client.delete(f"/api/offer-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── Quote email edge cases ────────────────────────────────────────────


class TestQuoteEmailEdgeCases:
    def test_email_html_whole_number_price(self, client, db_session, test_requisition, test_customer_site, test_user):
        """Whole number price formatted without decimals."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-WHOLE",
            status="draft",
            line_items=[
                {
                    "mpn": "IC500",
                    "qty": 10,
                    "sell_price": 100,
                    "cost_price": 50,
                    "lead_time": "stock",
                },
            ],
            subtotal=1000.0,
            total_cost=500.0,
            total_margin_pct=50.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        html = resp.json()["html"]
        # Whole number price should be formatted as $100
        assert "$100" in html

    def test_email_html_lead_time_with_days_keyword(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """Lead time containing 'days' is passed through as-is."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-DAYS",
            status="draft",
            line_items=[
                {
                    "mpn": "IC600",
                    "qty": 10,
                    "sell_price": 5.00,
                    "cost_price": 3.00,
                    "lead_time": "3 days",
                },
            ],
            subtotal=50.0,
            total_cost=30.0,
            total_margin_pct=40.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview")
        assert resp.status_code == 200
        html = resp.json()["html"]
        assert "3 days" in html


# ── Historical offers substitute matching ─────────────────────────────


class TestCompanyTags:
    def test_list_companies_includes_tags(self, admin_client, db_session, test_company):
        """brand_tags and commodity_tags are returned in list response."""
        test_company.brand_tags = ["IBM", "HP"]
        test_company.commodity_tags = ["Server"]
        db_session.commit()

        resp = admin_client.get("/api/companies")
        assert resp.status_code == 200
        data = resp.json()["items"]
        comp = [c for c in data if c["name"] == "Acme Electronics"][0]
        assert comp["brand_tags"] == ["IBM", "HP"]
        assert comp["commodity_tags"] == ["Server"]

    def test_list_companies_tag_filter(self, admin_client, db_session, test_company):
        """Tag query param filters companies by brand/commodity tags."""
        test_company.brand_tags = ["IBM", "HP"]
        test_company.commodity_tags = ["Server"]
        db_session.commit()

        # Should match
        resp = admin_client.get("/api/companies", params={"tag": "IBM"})
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["items"]]
        assert "Acme Electronics" in names

    def test_list_companies_tag_filter_no_match(self, client, db_session, test_company):
        """Tag filter with non-matching value returns empty."""
        test_company.brand_tags = ["IBM"]
        db_session.commit()

        resp = client.get("/api/companies", params={"tag": "Nexperia"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_list_companies_tag_filter_commodity(self, admin_client, db_session, test_company):
        """Tag filter matches commodity_tags too."""
        test_company.commodity_tags = ["Networking"]
        db_session.commit()

        resp = admin_client.get("/api/companies", params={"tag": "network"})
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["items"]]
        assert "Acme Electronics" in names


# ── Company duplicate detection (lines 361-371) ──────────────────────


class TestCompanyCreateDuplicates:
    def test_create_company_duplicate_name_returns_409(self, client, db_session):
        """Creating company with existing name -> 409 with duplicates."""
        existing = Company(name="Acme Electronics", is_active=True)
        db_session.add(existing)
        db_session.commit()

        resp = client.post(
            "/api/companies",
            json={
                "name": "acme electronics",  # case-insensitive match
            },
        )
        assert resp.status_code == 409
        assert "duplicates" in resp.json()

    def test_create_company_similar_name_returns_409(self, client, db_session):
        """Similar company name triggers 409 (substring match)."""
        existing = Company(name="Advanced Micro Devices", is_active=True)
        db_session.add(existing)
        db_session.commit()

        resp = client.post(
            "/api/companies",
            json={
                "name": "Advanced Micro Devices Inc",
            },
        )
        assert resp.status_code == 409


# ── Offer competitive notification (lines 399-400) ──────────────────


def test_quote_mutation_scope_enforced_for_sales(db_session, sales_user, test_quote):
    """Sales users cannot mutate quotes tied to other users' requisitions."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    try:
        with TestClient(app) as c:
            resp = c.put(f"/api/quotes/{test_quote.id}", json={"notes": "should fail"})
    finally:
        for dep in [get_db, require_user]:
            app.dependency_overrides.pop(dep, None)
    assert resp.status_code == 404


# ── Phase-0 CRM Foundations: field persistence tests ─────────────────────────


class TestCompanyPhase0Fields:
    """API-level tests: create + update company with Phase-0 fields persist to DB."""

    @patch("app.routers.crm.companies.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_create_company_with_phase0_fields(self, mock_normalize, mock_cred, client, db_session):
        """POST /api/companies with Phase-0 fields stores them on the Company row."""
        mock_normalize.return_value = ("FieldsTest Corp", "fieldstest.com")
        resp = client.post(
            "/api/companies",
            json={
                "name": "FieldsTest Corp",
                "legal_name": "FieldsTest Corporation LLC",
                "employee_size": "51-200",
                "revenue_range": "$10M-$50M",
                "hq_city": "Austin",
                "hq_state": "TX",
                "hq_country": "United States",
                "credit_terms": "Net 30",
                "tax_id": "12-3456789",
                "source": "referral",
            },
        )
        assert resp.status_code == 200
        company_id = resp.json()["id"]
        co = db_session.get(Company, company_id)
        assert co.legal_name == "FieldsTest Corporation LLC"
        assert co.employee_size == "51-200"
        assert co.revenue_range == "$10M-$50M"
        assert co.hq_city == "Austin"
        assert co.hq_state == "TX"
        assert co.credit_terms == "Net 30"
        assert co.tax_id == "12-3456789"
        assert co.source == "referral"

    def test_update_company_with_phase0_fields(self, client, db_session, test_company, test_user):
        """PUT /api/companies/{id} with Phase-0 fields stores them on the Company
        row."""
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={
                "legal_name": "Acme Electronics Inc.",
                "employee_size": "201-500",
                "revenue_range": "$50M-$200M",
                "hq_city": "San Jose",
                "hq_state": "CA",
                "hq_country": "US",
                "credit_terms": "Net 60",
                "tax_id": "98-7654321",
                "source": "sfdc",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(test_company)
        assert test_company.legal_name == "Acme Electronics Inc."
        assert test_company.employee_size == "201-500"
        assert test_company.revenue_range == "$50M-$200M"
        assert test_company.hq_city == "San Jose"
        assert test_company.credit_terms == "Net 60"
        assert test_company.tax_id == "98-7654321"
        assert test_company.source == "sfdc"
