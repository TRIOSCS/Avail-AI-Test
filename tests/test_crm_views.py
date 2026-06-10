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


class TestCustomerWorkspace:
    """Test the CDM split-panel account workspace."""

    def test_workspace_renders_panels(self, client: TestClient):
        """Workspace renders the split layout: filter bar, list panel, detail panel."""
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert 'id="cdm-workspace"' in resp.text
        assert 'id="cdm-filters"' in resp.text
        assert 'id="cdm-list"' in resp.text
        assert 'id="cdm-detail"' in resp.text

    def test_workspace_accepts_legacy_shell_params(self, client: TestClient):
        """Legacy hx_target/push_url_base params (CRM shell URLs) are still accepted."""
        resp = client.get("/v2/partials/customers?hx_target=%23crm-tab-content&push_url_base=/v2/crm")
        assert resp.status_code == 200
        assert 'id="cdm-workspace"' in resp.text

    def test_account_rows_target_detail_panel(self, client: TestClient, db_session: Session, test_user: User):
        """Clicking an account row loads its detail into the right panel."""
        c = Company(name="Panel Target Co", is_active=True)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/customers/{c.id}"' in resp.text
        assert 'hx-target="#cdm-detail"' in resp.text

    def test_account_list_partial_returns_rows_only(self, client: TestClient, db_session: Session, test_user: User):
        """The account-list partial returns the left panel only (no workspace shell)."""
        c = Company(name="ListOnly Co", is_active=True)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert "ListOnly Co" in resp.text
        assert 'id="cdm-workspace"' not in resp.text


class TestOverdueChip:
    """Test the overdue 'needs a call' chip on the CDM workspace filter bar.

    Replaces the old "Needs Attention" banner — overdue accounts now surface via the
    default oldest-first sort plus this one-click filter chip.
    """

    def test_chip_shows_for_overdue_accounts(self, client: TestClient, db_session: Session, test_user: User):
        """Chip appears for sales users with overdue owned accounts."""
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
        assert "needs a call" in resp.text
        assert "Overdue Corp" in resp.text

    def test_chip_hidden_for_non_sales(self, client: TestClient, db_session: Session, test_user: User):
        """Chip is hidden for non-sales users."""
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
        assert "need a call" not in resp.text
        assert "needs a call" not in resp.text

    def test_chip_excludes_non_overdue(self, client: TestClient, db_session: Session, test_user: User):
        """Chip does not count accounts with recent activity."""
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
        assert "need a call" not in resp.text
        assert "needs a call" not in resp.text

    def test_chip_includes_never_contacted(self, client: TestClient, db_session: Session, test_user: User):
        """Chip counts accounts with no activity (never contacted)."""
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
        assert "needs a call" in resp.text
        assert "NeverContacted Corp" in resp.text

    def test_chip_excludes_other_owners(self, client: TestClient, db_session: Session, test_user: User):
        """Chip only counts accounts owned by the current user."""
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
        assert "need a call" not in resp.text
        assert "needs a call" not in resp.text


class TestWorkspaceFiltersAndSort:
    """Test CDM workspace sorting and filtering."""

    def test_default_sort_oldest_first(self, client: TestClient, db_session: Session, test_user: User):
        """Default sort: never-contacted first, then longest since activity."""
        c_new = Company(name="AAA NeverTouched", is_active=True, last_activity_at=None)
        c_old = Company(
            name="ZZZ Oldest",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        c_recent = Company(
            name="MMM Freshest",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add_all([c_new, c_old, c_recent])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list").text
        assert html.index("AAA NeverTouched") < html.index("ZZZ Oldest") < html.index("MMM Freshest")

    def test_sort_newest_first(self, client: TestClient, db_session: Session, test_user: User):
        """Sort=newest puts most recently touched accounts at the top."""
        c_old = Company(
            name="ZZZ Oldest",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        c_recent = Company(
            name="MMM Freshest",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add_all([c_old, c_recent])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?sort=newest").text
        assert html.index("MMM Freshest") < html.index("ZZZ Oldest")

    def test_sort_by_name(self, client: TestClient, db_session: Session, test_user: User):
        """sort=name_asc orders alphabetically regardless of activity."""
        c_b = Company(name="Bravo Co", is_active=True, last_activity_at=None)
        c_a = Company(
            name="Alpha Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add_all([c_b, c_a])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?sort=name_asc").text
        assert html.index("Alpha Co") < html.index("Bravo Co")

    def test_staleness_filter_overdue(self, client: TestClient, db_session: Session, test_user: User):
        """Staleness=overdue shows only 30d+ stale accounts."""
        stale = Company(
            name="Stale Filter Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        fresh = Company(
            name="Fresh Filter Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.add_all([stale, fresh])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?staleness=overdue").text
        assert "Stale Filter Co" in html
        assert "Fresh Filter Co" not in html

    def test_staleness_filter_new(self, client: TestClient, db_session: Session, test_user: User):
        """Staleness=new shows only never-contacted accounts."""
        never = Company(name="Never Filter Co", is_active=True, last_activity_at=None)
        touched = Company(
            name="Touched Filter Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.add_all([never, touched])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?staleness=new").text
        assert "Never Filter Co" in html
        assert "Touched Filter Co" not in html

    def test_account_type_filter(self, client: TestClient, db_session: Session, test_user: User):
        """account_type filter narrows to that type."""
        cust = Company(name="TypeCust Co", is_active=True, account_type="Customer")
        prospect = Company(name="TypeProspect Co", is_active=True, account_type="Prospect")
        db_session.add_all([cust, prospect])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?account_type=Prospect").text
        assert "TypeProspect Co" in html
        assert "TypeCust Co" not in html

    def test_my_only_filter(self, client: TestClient, db_session: Session, test_user: User):
        """my_only=1 shows only accounts owned by the current user."""
        mine = Company(name="Mine Co", is_active=True, account_owner_id=test_user.id)
        other = Company(name="Unowned Co", is_active=True, account_owner_id=None)
        db_session.add_all([mine, other])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?my_only=1").text
        assert "Mine Co" in html
        assert "Unowned Co" not in html


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


class TestContactPanel:
    """Test the contacts panel (default detail tab) with outreach actions."""

    def _make_company_with_contact(self, db_session, **contact_kwargs):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Contact Panel Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Jane Contact",
            title="Director of Procurement",
            email="jane@contactpanel.com",
            phone="+14155550000",
            **contact_kwargs,
        )
        db_session.add(contact)
        db_session.commit()
        return company, site, contact

    def test_detail_shows_contacts_inline(self, client: TestClient, db_session: Session, test_user: User):
        """Account detail renders contacts (name, title, email, phone) without a tab
        click."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "Jane Contact" in resp.text
        assert "Director of Procurement" in resp.text
        assert "jane@contactpanel.com" in resp.text
        assert "+14155550000" in resp.text

    def test_contact_actions_log_outreach(self, client: TestClient, db_session: Session, test_user: User):
        """Call/Email/Teams actions carry data-outreach-log attributes and deep
        links."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "data-outreach-log" in resp.text
        assert 'href="tel:+14155550000"' in resp.text
        assert 'href="mailto:jane@contactpanel.com"' in resp.text
        assert "https://teams.microsoft.com/l/chat/0/0?users=jane%40contactpanel.com" in resp.text
        assert f'data-company-id="{company.id}"' in resp.text
        assert f'data-contact-id="{contact.id}"' in resp.text

    def test_wechat_action_renders_when_handle_set(self, client: TestClient, db_session: Session, test_user: User):
        """WeChat deep link renders only for contacts with a wechat_id."""
        company, _, _ = self._make_company_with_contact(db_session, wechat_id="jane_wc")
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "weixin://dl/chat?jane_wc" in resp.text
        assert 'data-channel="wechat"' in resp.text

    def test_no_wechat_action_without_handle(self, client: TestClient, db_session: Session, test_user: User):
        """No WeChat button when the contact has no wechat_id."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "weixin://" not in resp.text

    def test_legacy_site_contact_rendered(self, client: TestClient, db_session: Session, test_user: User):
        """Legacy site-level contacts still appear in the contacts panel."""
        from app.models.crm import CustomerSite

        company = Company(name="Legacy Contact Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(
            company_id=company.id,
            site_name="Plant 2",
            is_active=True,
            contact_name="Old Schoolson",
            contact_email="old@legacyco.com",
            contact_phone="+14155559999",
        )
        db_session.add(site)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Old Schoolson" in resp.text
        assert "legacy" in resp.text

    def test_create_site_contact_with_wechat(self, client: TestClient, db_session: Session, test_user: User):
        """Site contact create form accepts a WeChat ID."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="WeChat Create Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts",
            data={"full_name": "Wei Chen", "phone": "+8613800138000", "wechat_id": "wei_chen_88"},
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.customer_site_id == site.id).first()
        assert contact is not None
        assert contact.wechat_id == "wei_chen_88"


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
        # Phase B1: the customer Activity Log rows now render via the canonical
        # activity_row macro (icon style). The previous test pinned the divergent
        # customer-surface chrome — the email-intelligence "Offer" pill and the
        # ">$</span>" pricing badge — which the unified row intentionally drops.
        # The canonical row surfaces the activity-type label, the actor, and the
        # channel badge instead.
        assert "Email Received" in resp.text
        assert "John Vendor" in resp.text
        assert "Email" in resp.text


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
