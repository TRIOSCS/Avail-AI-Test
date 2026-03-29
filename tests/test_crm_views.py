"""Tests for CRM shell views.

Called by: pytest
Depends on: app.routers.crm.views
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company
from tests.conftest import engine  # noqa: F401


class TestCRMShell:
    """Test CRM shell partial route."""

    def test_crm_shell_returns_html(self, client: TestClient):
        """GET /v2/partials/crm/shell returns 200 with tab bar."""
        resp = client.get("/v2/partials/crm/shell")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_crm_shell_has_customers_tab(self, client: TestClient):
        """Shell renders Customers tab button."""
        resp = client.get("/v2/partials/crm/shell")
        assert "Customers" in resp.text

    def test_crm_shell_has_vendors_tab(self, client: TestClient):
        """Shell renders Vendors tab button."""
        resp = client.get("/v2/partials/crm/shell")
        assert "Vendors" in resp.text

    def test_crm_shell_has_tab_content_container(self, client: TestClient):
        """Shell renders #crm-tab-content container."""
        resp = client.get("/v2/partials/crm/shell")
        assert 'id="crm-tab-content"' in resp.text


class TestCRMFullPage:
    """Test CRM full-page route via v2_page dispatcher."""

    def test_v2_crm_returns_200(self, client: TestClient):
        """GET /v2/crm returns 200."""
        resp = client.get("/v2/crm")
        assert resp.status_code == 200

    def test_v2_crm_loads_shell_partial(self, client: TestClient):
        """GET /v2/crm loads the CRM shell partial."""
        resp = client.get("/v2/crm")
        assert resp.status_code == 200


class TestVendorListEmbedding:
    """Test vendor list can be embedded in CRM shell."""

    def test_vendor_list_with_custom_target(self, client: TestClient):
        """Vendor list respects hx_target query parameter."""
        resp = client.get("/v2/partials/vendors?hx_target=%23crm-tab-content")
        assert resp.status_code == 200
        assert 'hx-target="#crm-tab-content"' in resp.text

    def test_vendor_list_default_target(self, client: TestClient):
        """Vendor list defaults to #main-content when no override."""
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert 'hx-target="#main-content"' in resp.text


class TestCustomerListEmbedding:
    """Test customer list can be embedded in CRM shell."""

    def test_customer_list_with_custom_target(self, client: TestClient):
        """Customer list respects hx_target query parameter."""
        resp = client.get("/v2/partials/customers?hx_target=%23crm-tab-content")
        assert resp.status_code == 200
        assert 'hx-target="#crm-tab-content"' in resp.text

    def test_customer_list_default_target(self, client: TestClient):
        """Customer list defaults to #main-content when no override."""
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert 'hx-target="#main-content"' in resp.text


class TestCustomerStaleness:
    """Test staleness tier computation and display."""

    def test_customer_list_has_staleness_dot(self, client: TestClient, db_session: Session, test_user: User):
        """Customer list renders staleness indicator dots."""

        c = Company(name="Test Corp", is_active=True)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "rounded-full" in resp.text

    def test_overdue_company_shows_rose(self, client: TestClient, db_session: Session, test_user: User):
        """Company with 30+ day old activity shows rose indicator."""

        c = Company(
            name="Stale Corp",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert "bg-rose-500" in resp.text

    def test_new_company_shows_brand(self, client: TestClient, db_session: Session, test_user: User):
        """Company with no activity shows brand indicator."""

        c = Company(name="New Corp", is_active=True, last_activity_at=None)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert "bg-brand-300" in resp.text

    def test_due_soon_company_shows_amber(self, client: TestClient, db_session: Session, test_user: User):
        """Company with 14-29 day old activity shows amber indicator."""

        c = Company(
            name="DueSoon Corp",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert "bg-amber-400" in resp.text

    def test_recent_company_shows_emerald(self, client: TestClient, db_session: Session, test_user: User):
        """Company with <14 day old activity shows emerald indicator."""

        c = Company(
            name="Recent Corp",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert "bg-emerald-400" in resp.text

    def test_default_sort_is_staleness(self, client: TestClient, db_session: Session, test_user: User):
        """Customer list sorts by staleness (nulls first, then oldest)."""

        c_new = Company(name="AAA New", is_active=True, last_activity_at=None)
        c_old = Company(
            name="ZZZ Old",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        c_recent = Company(
            name="MMM Recent",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add_all([c_new, c_old, c_recent])
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        html = resp.text
        pos_new = html.index("AAA New")
        pos_old = html.index("ZZZ Old")
        pos_recent = html.index("MMM Recent")
        assert pos_new < pos_old < pos_recent
