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


class TestTodaysCalls:
    """Test Today's Calls section on customer list."""

    def test_todays_calls_shows_overdue_accounts(self, client: TestClient, db_session: Session, test_user: User):
        """Customer list shows Today's Calls for overdue accounts owned by user."""
        test_user.role = "sales"
        db_session.flush()

        overdue = Company(
            name="Overdue Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.add(overdue)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Needs Attention" in resp.text
        assert "Overdue Corp" in resp.text

    def test_todays_calls_hidden_for_non_sales(self, client: TestClient, db_session: Session, test_user: User):
        """Today's Calls section is hidden for non-sales users."""
        # test_user defaults to "buyer" role
        overdue = Company(
            name="Hidden Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.add(overdue)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Needs Attention" not in resp.text

    def test_todays_calls_excludes_non_overdue(self, client: TestClient, db_session: Session, test_user: User):
        """Today's Calls excludes accounts with recent activity."""
        test_user.role = "sales"
        db_session.flush()

        recent = Company(
            name="Recent Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.add(recent)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Needs Attention" not in resp.text

    def test_todays_calls_includes_never_contacted(self, client: TestClient, db_session: Session, test_user: User):
        """Today's Calls includes accounts with no activity (never contacted)."""
        test_user.role = "sales"
        db_session.flush()

        never = Company(
            name="NeverContacted Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_activity_at=None,
        )
        db_session.add(never)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Needs Attention" in resp.text
        assert "NeverContacted Corp" in resp.text
        assert "Never contacted" in resp.text

    def test_todays_calls_excludes_other_owners(self, client: TestClient, db_session: Session, test_user: User):
        """Today's Calls only shows accounts owned by the current user."""
        test_user.role = "sales"
        db_session.flush()

        other = Company(
            name="OtherOwner Corp",
            is_active=True,
            account_owner_id=None,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.add(other)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Needs Attention" not in resp.text


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


class TestEmailIntelligenceInActivity:
    """Test email intelligence data shown in activity tabs."""

    def test_activity_tab_shows_email_classification(self, client: TestClient, db_session: Session, test_user: User):
        """Activity tab shows email classification when available."""
        from app.models.email_intelligence import EmailIntelligence
        from app.models.intelligence import ActivityLog

        company = Company(name="Email Intel Co", is_active=True)
        db_session.add(company)
        db_session.flush()

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=company.id,
            external_id="msg-123",
            subject="Quote for STM32F407",
            contact_name="John Vendor",
        )
        db_session.add(log)
        db_session.flush()

        ei = EmailIntelligence(
            user_id=test_user.id,
            message_id="msg-123",
            classification="offer",
            confidence=0.92,
            has_pricing=True,
            subject="Quote for STM32F407",
            sender_email="john@vendor.com",
            sender_domain="vendor.com",
        )
        db_session.add(ei)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Offer" in resp.text
        assert ">$</span>" in resp.text


class TestPerformanceMetrics:
    """Tests for CRM performance tab and JSON metrics endpoint."""

    def test_performance_metrics_json_returns_200(self, client: TestClient):
        """GET /api/crm/performance-metrics returns JSON with score arrays."""
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "names" in data
        assert "scores" in data
        assert "behaviors" in data
        assert "outcomes" in data

    def test_performance_metrics_json_arrays_same_length(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """JSON response arrays have matching lengths."""
        test_user.is_active = True
        db_session.flush()

        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["names"]) == len(data["scores"])
        assert len(data["names"]) == len(data["behaviors"])
        assert len(data["names"]) == len(data["outcomes"])

    def test_crm_performance_partial_returns_html(self, client: TestClient):
        """GET /v2/partials/crm/performance returns HTML."""
        resp = client.get("/v2/partials/crm/performance")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestComputeUserScore:
    """Tests for _compute_user_score helper (sales role and exception paths)."""

    def test_sales_user_uses_sales_score_function(self, client: TestClient, db_session: Session, test_user: User):
        """Sales users get compute_sales_avail_score called (not buyer)."""
        from unittest.mock import patch

        test_user.role = "sales"
        test_user.is_active = True
        db_session.flush()

        sales_result = {"behavior_total": 80.0, "outcome_total": 70.0, "total_score": 75.0}
        with (
            patch(
                "app.services.avail_score_service.compute_sales_avail_score",
                return_value=sales_result,
            ) as mock_sales,
            patch(
                "app.services.avail_score_service.compute_buyer_avail_score",
            ) as mock_buyer,
        ):
            resp = client.get("/api/crm/performance-metrics")

        assert resp.status_code == 200
        # Sales score function was invoked; buyer was not
        mock_sales.assert_called()
        mock_buyer.assert_not_called()

    def test_score_computation_exception_returns_zeros(self, client: TestClient, db_session: Session, test_user: User):
        """When avail score computation raises, user gets zero scores (no crash)."""
        from unittest.mock import patch

        test_user.is_active = True
        db_session.flush()

        with patch(
            "app.services.avail_score_service.compute_buyer_avail_score",
            side_effect=RuntimeError("score service down"),
        ):
            resp = client.get("/api/crm/performance-metrics")

        assert resp.status_code == 200
        data = resp.json()
        # User still appears in response with zero scores
        assert len(data["names"]) >= 1
        assert all(s == 0.0 for s in data["scores"])
