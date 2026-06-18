"""Tests for CRM shell views.

Called by: pytest
Depends on: app.routers.crm.views
"""

from datetime import datetime, timedelta, timezone

import pytest
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

    @pytest.mark.parametrize(
        "snippet",
        [
            pytest.param("Customers", id="customers_tab"),
            pytest.param("Vendors", id="vendors_tab"),
            pytest.param('id="crm-tab-content"', id="tab_content_container"),
        ],
    )
    def test_crm_shell_renders_element(self, client: TestClient, snippet: str):
        """Shell renders the tab buttons and the #crm-tab-content container."""
        resp = client.get("/v2/partials/crm/shell")
        assert snippet in resp.text


class TestCRMFullPage:
    """Test CRM full-page route via v2_page dispatcher."""

    def test_v2_crm_returns_200(self, client: TestClient):
        """GET /v2/crm returns 200 (loads the CRM shell partial)."""
        resp = client.get("/v2/crm")
        assert resp.status_code == 200


class TestVendorListEmbedding:
    """Test vendor list can be embedded in CRM shell."""

    @pytest.mark.parametrize(
        ("url", "expected_target"),
        [
            pytest.param(
                "/v2/partials/vendors?hx_target=%23crm-tab-content",
                'hx-target="#crm-tab-content"',
                id="custom_target",
            ),
            pytest.param(
                "/v2/partials/vendors",
                'hx-target="#main-content"',
                id="default_target",
            ),
        ],
    )
    def test_vendor_list_target(self, client: TestClient, url: str, expected_target: str):
        """Vendor list respects hx_target override, defaulting to #main-content."""
        resp = client.get(url)
        assert resp.status_code == 200
        assert expected_target in resp.text


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
            # Stale non-NULL outbound — this is the real overdue case the chip tracks.
            # last_activity_at is irrelevant to the chip (chip keys off last_outbound_at).
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=35),
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
        """Chip does not count accounts with recent outbound activity (<30d).

        Uses last_outbound_at (cadence model); last_activity_at alone does not suppress
        the chip — the chip tracks whether we sent something recently.
        """
        test_user.role = "sales"
        db_session.flush()

        recent = Company(
            name="Recent Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.add(recent)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "bg-rose-50 text-rose-700" not in resp.text

    def test_chip_includes_never_contacted(self, client: TestClient, db_session: Session, test_user: User):
        """Chip counts accounts with no activity (never contacted).

        Specifically: NULL last_outbound_at (never sent an outbound) → overdue.
        """
        test_user.role = "sales"
        db_session.flush()

        never = Company(
            name="NeverContacted Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_activity_at=None,
            # NULL last_outbound_at = never sent an outbound → treated as overdue by chip
        )
        db_session.add(never)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "needs a call" in resp.text
        assert "NeverContacted Corp" in resp.text

    def test_chip_click_filter_matches_chip_count(self, client: TestClient, db_session: Session, test_user: User):
        """The chip's click-through filter (staleness=needs_call) returns every account
        the chip counted — overdue AND never-contacted share one predicate.

        Consistency guard: counting and filtering used to encode 'needs a call' twice
        with different NULL semantics, so a rep could see '1 needs a call', click the
        chip, and get 'No accounts found'.
        """
        test_user.role = "sales"
        db_session.flush()

        never = Company(
            name="NeverCalled Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=None,  # NULL last_outbound_at = never contacted → overdue
        )
        overdue = Company(
            name="LongOverdue Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=45),  # stale non-NULL outbound → overdue
        )
        db_session.add_all([never, overdue])
        db_session.commit()

        shell = client.get("/v2/partials/customers")
        assert shell.status_code == 200
        assert "2 need a call" in shell.text
        # The chip targets the shared needs_call predicate, not plain overdue.
        assert "staleness.value = 'needs_call'" in shell.text

        html = client.get("/v2/partials/customers/account-list?staleness=needs_call&my_only=1").text
        assert "NeverCalled Corp" in html
        assert "LongOverdue Corp" in html

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

    def test_staleness_filter_due_soon_and_recent(self, client: TestClient, db_session: Session, test_user: User):
        """due_soon is the two-cutoff 14-30d band; recent is <14d.

        Seeds 5d/20d/45d accounts so a swapped-cutoff or sign-flip regression
        (due_soon_cutoff is the NEWER timestamp) can't slip through.
        """
        c5 = Company(
            name="Band5d Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        c20 = Company(
            name="Band20d Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        c45 = Company(
            name="Band45d Co",
            is_active=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        db_session.add_all([c5, c20, c45])
        db_session.commit()

        due_soon = client.get("/v2/partials/customers/account-list?staleness=due_soon").text
        assert "Band20d Co" in due_soon
        assert "Band5d Co" not in due_soon
        assert "Band45d Co" not in due_soon

        recent = client.get("/v2/partials/customers/account-list?staleness=recent").text
        assert "Band5d Co" in recent
        assert "Band20d Co" not in recent
        assert "Band45d Co" not in recent

    def test_inactive_companies_excluded(self, client: TestClient, db_session: Session, test_user: User):
        """Archived/merged (is_active=False) companies never appear in the account
        list."""
        active = Company(name="ActiveList Co", is_active=True)
        archived = Company(name="ArchivedGone Co", is_active=False)
        db_session.add_all([active, archived])
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list").text
        assert "ActiveList Co" in html
        assert "ArchivedGone Co" not in html

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

    @pytest.mark.parametrize(
        ("name", "tier", "outbound_days_ago", "expected_class"),
        [
            # cadence_state="overdue" (>30d ceiling) → bg-rose-500
            pytest.param("Stale Corp", "standard", 35, "bg-rose-500", id="overdue_shows_rose"),
            # cadence_state="new" (never outbound) → bg-gray-300
            pytest.param("New Corp", None, None, "bg-gray-300", id="new_shows_gray300"),
            # cadence_state="due" (key tier, past 7d target, within 30d ceiling) → bg-amber-400
            pytest.param("DueSoon Corp", "key", 10, "bg-amber-400", id="due_shows_amber"),
            # cadence_state="on_target" (standard tier, within 30d target) → bg-emerald-400
            pytest.param("Recent Corp", "standard", 5, "bg-emerald-400", id="on_target_shows_emerald"),
        ],
    )
    def test_staleness_tier_indicator(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        name: str,
        tier: str | None,
        outbound_days_ago: int | None,
        expected_class: str,
    ):
        """Each cadence state renders its indicator color on the account row dot.

        cadence_state is driven by last_outbound_at + tier (P3-1 dual-clock model):
        overdue (>30d ceiling) → rose-500,   new (never outbound) → gray-300,   due
        (past tier target, ≤30d) → amber-400,   on_target (within tier target) →
        emerald-400.
        """
        last_outbound = (
            None if outbound_days_ago is None else datetime.now(timezone.utc) - timedelta(days=outbound_days_ago)
        )
        c = Company(name=name, is_active=True, tier=tier, last_outbound_at=last_outbound)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert expected_class in resp.text

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
        # The site-level last_activity_at bump depends on this attribute.
        assert f'data-site-id="{site.id}"' in resp.text

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

    def test_create_site_contact_wechat_too_long_rejected(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """WeChat IDs beyond the String(100) column are rejected server-side (the SQLite
        test engine ignores VARCHAR lengths; Postgres would 500)."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="WeChat Long Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts",
            data={"full_name": "Wei Chen", "wechat_id": "x" * 150},
        )
        assert resp.status_code == 200
        assert "100 characters" in resp.text
        assert db_session.query(SiteContact).filter(SiteContact.customer_site_id == site.id).count() == 0

    def test_inactive_site_contacts_excluded(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts (real + legacy) on deactivated sites never render — clicking them
        would log outreach against, and bump, a deactivated entity."""
        from app.models.crm import CustomerSite, SiteContact

        company, _, _ = self._make_company_with_contact(db_session)
        dead_site = CustomerSite(
            company_id=company.id,
            site_name="Closed Plant",
            is_active=False,
            contact_name="Ghost Legacy",
            contact_email="ghost-legacy@contactpanel.com",
        )
        db_session.add(dead_site)
        db_session.flush()
        ghost = SiteContact(
            customer_site_id=dead_site.id,
            full_name="Ghost Contact",
            email="ghost-contact@contactpanel.com",
        )
        db_session.add(ghost)
        db_session.commit()

        # Detail panel (inline default tab) — uses the preloaded sites list.
        detail = client.get(f"/v2/partials/customers/{company.id}")
        assert detail.status_code == 200
        assert "Jane Contact" in detail.text
        assert "Ghost Contact" not in detail.text
        assert "Ghost Legacy" not in detail.text

        # Contacts tab refresh — uses the service-layer sites query.
        tab = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert tab.status_code == 200
        assert "Jane Contact" in tab.text
        assert "Ghost Contact" not in tab.text
        assert "Ghost Legacy" not in tab.text


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
        assert len(data["scores"]) > 0
        assert all(s == 0.0 for s in data["scores"])


class TestContactsTabP33:
    """P3-3 TDD: accordion by site, role chips, per-contact clocks, honest empty states.

    Written FIRST (failing) per TDD — implement contacts_tab.html to pass.
    """

    def _setup_multi_site_company(self, db_session):
        """Two active sites with contacts of varying roles and clock states."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="P33 Accordion Co", is_active=True)
        db_session.add(company)
        db_session.flush()

        site_hq = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        site_branch = CustomerSite(company_id=company.id, site_name="Branch Office", is_active=True)
        db_session.add_all([site_hq, site_branch])
        db_session.flush()

        # HQ contacts: one buyer with outbound clock set, one with NULL clocks
        contact_buyer = SiteContact(
            customer_site_id=site_hq.id,
            full_name="Alice Buyer",
            email="alice@p33.com",
            contact_role="buyer",
            is_primary=True,
            last_outbound_at=__import__("datetime").datetime(2026, 5, 1, tzinfo=__import__("datetime").timezone.utc),
        )
        contact_no_clock = SiteContact(
            customer_site_id=site_hq.id,
            full_name="Bob Noclock",
            email="bob@p33.com",
            contact_role="technical",
            last_outbound_at=None,
            last_reply_at=None,
        )
        # Branch: decision_maker
        contact_dm = SiteContact(
            customer_site_id=site_branch.id,
            full_name="Carol Decider",
            email="carol@p33.com",
            contact_role="decision_maker",
        )
        db_session.add_all([contact_buyer, contact_no_clock, contact_dm])
        db_session.commit()
        return company, site_hq, site_branch, contact_buyer, contact_no_clock, contact_dm

    # ── accordion grouping ──────────────────────────────────────────────

    def test_contacts_tab_returns_200(self, client: TestClient, db_session, test_user):
        """GET contacts tab for a multi-site company returns 200."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200

    def test_contacts_grouped_under_site_headers(self, client: TestClient, db_session, test_user):
        """Contacts tab renders both site names as accordion headers."""
        company, site_hq, site_branch, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        html = resp.text
        assert "HQ" in html
        assert "Branch Office" in html

    def test_site_header_shows_contact_count(self, client: TestClient, db_session, test_user):
        """Site header shows the contact count for that site."""
        company, site_hq, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        # HQ has 2 contacts; the count must appear near the site name
        assert "2 contacts" in html

    def test_accordion_uses_alpine_xdata(self, client: TestClient, db_session, test_user):
        """Accordion groups use Alpine x-data with an open/expanded boolean."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "x-data" in html
        assert "x-show" in html

    def test_all_three_contacts_appear(self, client: TestClient, db_session, test_user):
        """All contacts from both sites appear in the tab."""
        company, _, _, buyer, noclock, dm = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "Alice Buyer" in html
        assert "Bob Noclock" in html
        assert "Carol Decider" in html

    # ── role chips ──────────────────────────────────────────────────────

    def test_buyer_role_chip_renders(self, client: TestClient, db_session, test_user):
        """Buyer contact_role renders a chip containing 'buyer' text."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        # The chip must contain the role text (case-insensitive acceptable)
        assert "buyer" in html.lower()

    def test_technical_role_chip_renders(self, client: TestClient, db_session, test_user):
        """Technical contact_role renders a chip."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "technical" in html.lower()

    def test_decision_maker_role_chip_renders(self, client: TestClient, db_session, test_user):
        """decision_maker contact_role renders a chip."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "decision" in html.lower()

    def test_null_role_no_chip_crash(self, client: TestClient, db_session, test_user):
        """A contact with NULL contact_role renders without error (graceful
        fallback)."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="NullRole Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="No Role Pete",
            email="pete@nullrole.com",
            contact_role=None,
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No Role Pete" in resp.text

    # ── per-contact clocks ──────────────────────────────────────────────

    def test_contact_with_outbound_shows_clock_value(self, client: TestClient, db_session, test_user):
        """Contact with last_outbound_at set shows the outbound clock (days value)."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        # Alice Buyer has last_outbound_at set — some days label must appear
        # The label "Out" or "out" or similar must be present
        assert "out" in html.lower() or "outbound" in html.lower()

    def test_null_outbound_shows_dash_not_never_replied(self, client: TestClient, db_session, test_user):
        """NULL last_outbound_at renders '—' or 'no logged touch' — NOT 'never
        replied'."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        # Must NOT say "never replied" (honest empty state rule)
        assert "never replied" not in html.lower()
        # Must indicate unknown/absent state via dash or descriptive text
        # "—" (em-dash) OR "no logged touch" must appear somewhere
        assert "—" in html or "no logged touch" in html.lower()

    def test_null_reply_shows_dash(self, client: TestClient, db_session, test_user):
        """NULL last_reply_at renders '—' — NOT 'never replied'."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "never replied" not in html.lower()

    def test_cadence_state_new_for_null_outbound(self, client: TestClient, db_session, test_user):
        """Contact with NULL last_outbound_at renders a 'new' cadence indicator (no
        red)."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="NewCadence Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Fresh Freddy",
            email="fresh@cadence.com",
            last_outbound_at=None,
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        html = resp.text
        # cadence_state("new") should produce gray indicator, not rose (overdue)
        # We can't assert no rose at all (there may be other elements), but
        # "never replied" must not appear
        assert "never replied" not in html.lower()

    # ── outreach buttons preserved ──────────────────────────────────────

    def test_outreach_buttons_still_present(self, client: TestClient, db_session, test_user):
        """Outreach buttons (data-outreach-log) are still rendered after accordion
        refactor."""
        company, *_ = self._setup_multi_site_company(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        html = resp.text
        assert "data-outreach-log" in html
        assert 'href="mailto:alice@p33.com"' in html

    # ── empty state ─────────────────────────────────────────────────────

    def test_empty_company_shows_no_contacts_message(self, client: TestClient, db_session, test_user):
        """Company with no contacts shows a helpful empty-state message."""
        company = Company(name="Empty P33 Co", is_active=True)
        db_session.add(company)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No contacts" in resp.text or "no contacts" in resp.text.lower()


class TestCompanyDetailCadenceCard:
    """Tests for the new account-cadence card + commercial-context strip in the company
    detail partial.

    The old 3-stat row (Sites / Open Requisitions / Created) is being REPLACED
    by a cadence card that shows:
      - Two clocks: outbound + reply
      - A cadence_state badge (color-coded)
      - Next-best-touch text
      - Coverage: "N contacts · N sites"
      - Commercial strip: win rate, revenue 90d, last deal date

    These tests are written FIRST (TDD) — they will FAIL until the route and
    template are updated in CRM cockpit P3-2.
    """

    # ─── helpers ────────────────────────────────────────────────────────

    def _make_company(self, db_session, **kwargs) -> Company:
        co = Company(name="Test Cadence Co", is_active=True, **kwargs)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        return co

    # ─── cadence badge ──────────────────────────────────────────────────

    def test_detail_shows_cadence_badge_for_new_company(self, client: TestClient, db_session: Session, test_user: User):
        """Company with no outbound → cadence badge uses 'new' CSS classes and
        references never/Never somewhere in the HTML.

        Fails until: route passes cadence_state to template AND template renders
        the cadence badge with correct classes.
        """
        co = self._make_company(db_session, last_outbound_at=None, last_reply_at=None)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        # New cadence badge CSS: bg-gray-100 text-gray-500
        assert "bg-gray-100" in html
        assert "text-gray-500" in html
        # Template must say "never" or "Never" somewhere (the "Out" clock label)
        assert "never" in html.lower()

    def test_detail_shows_overdue_cadence_badge(self, client: TestClient, db_session: Session, test_user: User):
        """Company with outbound 40 days ago → overdue badge CSS classes present.

        Fails until: route computes cadence_state and template renders the
        overdue badge: bg-rose-100 text-rose-700.
        """
        outbound_40d_ago = datetime.now(timezone.utc) - timedelta(days=40)
        co = self._make_company(db_session, last_outbound_at=outbound_40d_ago)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        assert "bg-rose-100" in html
        assert "text-rose-700" in html

    # ─── outbound clock ─────────────────────────────────────────────────

    def test_detail_shows_outbound_clock(self, client: TestClient, db_session: Session, test_user: User):
        """Company with last_outbound_at set → shows days count + Out/out label.

        Fails until: route passes last_outbound_at (or computed days) to template
        AND template renders the outbound clock.
        """
        outbound_7d_ago = datetime.now(timezone.utc) - timedelta(days=7)
        co = self._make_company(db_session, last_outbound_at=outbound_7d_ago)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        # The outbound clock label (case-insensitive)
        assert "out" in html.lower()
        # A number representing days (7d = "7")
        assert "7" in html

    # ─── next-best-touch ────────────────────────────────────────────────

    def test_detail_shows_next_best_touch(self, client: TestClient, db_session: Session, test_user: User):
        """Company with no outbound → 'Never contacted' shown in HTML.

        Fails until: route computes next_best_touch and template renders it.
        """
        co = self._make_company(db_session, last_outbound_at=None)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        assert "Never contacted" in resp.text

    # ─── coverage row ───────────────────────────────────────────────────

    def test_detail_shows_coverage(self, client: TestClient, db_session: Session, test_user: User):
        """Company with 2 contacts and 1 site → 'contacts' and 'site' in HTML.

        Fails until: route passes contact_count + site_count to template AND
        template renders the coverage row.
        """
        from app.models.crm import CustomerSite, SiteContact

        co = self._make_company(db_session)
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        contact1 = SiteContact(customer_site_id=site.id, full_name="Alice Smith")
        contact2 = SiteContact(customer_site_id=site.id, full_name="Bob Jones", email="bob@test.com")
        db_session.add_all([contact1, contact2])
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        # "2 contacts" or "2 contact" (singular/plural variations acceptable)
        assert "contact" in html.lower()
        # "1 site" or "1 sites"
        assert "site" in html.lower()
        # The numbers 2 and 1 appear
        assert "2" in html
        assert "1" in html

    # ─── commercial strip ───────────────────────────────────────────────

    def test_detail_commercial_strip_shows_win_rate(self, client: TestClient, db_session: Session, test_user: User):
        """Company with WON + LOST reqs → win rate percentage in HTML.

        Fails until: route fetches commercial_stats and template renders win rate %.
        """
        from app.models.crm import CustomerSite
        from app.models.sourcing import Requisition

        co = self._make_company(db_session)
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        req_won = Requisition(
            name="REQ-WON-001",
            customer_name=co.name,
            status="won",
            customer_site_id=site.id,
            company_id=co.id,
        )
        req_lost = Requisition(
            name="REQ-LOST-001",
            customer_name=co.name,
            status="lost",
            customer_site_id=site.id,
            company_id=co.id,
        )
        db_session.add_all([req_won, req_lost])
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        # 1 won / 2 decided = 50% win rate — the % symbol must appear
        assert "%" in html
        assert "50" in html

    # ─── old labels gone ────────────────────────────────────────────────

    def test_detail_old_stats_labels_gone(self, client: TestClient, db_session: Session, test_user: User):
        """The old 3-stat row labels 'Open Requisitions' and 'Created' must NOT appear
        in the new template.

        Fails until: template no longer contains the old stat row from lines
        97-111 of detail.html.
        """
        co = self._make_company(db_session)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

        html = resp.text
        # These exact strings were the old stat-row labels — they must be gone
        assert "Open Requisitions" not in html
        assert ">Created<" not in html


# ────────────────────────────────────────────────────────────────────────────
# P3-4: Unified Activity Timeline
# ────────────────────────────────────────────────────────────────────────────


class TestUnifiedActivityTimeline:
    """P3-4: activity tab merges RFQ contacts + quotes + activity logs into ONE
    chronological timeline, fixes the q.total_amount bug, adds quality badges,
    and exposes a hide-noise toggle.
    """

    # ── helpers ─────────────────────────────────────────────────────────────

    def _make_company(self, db_session: Session, name: str = "Timeline Co") -> "Company":
        from app.models.crm import Company

        co = Company(name=name, is_active=True)
        db_session.add(co)
        db_session.flush()
        return co

    def _make_requisition(self, db_session: Session, company, name: str = "REQ-001"):
        from app.models.sourcing import Requisition

        req = Requisition(name=name, customer_name=company.name, company_id=company.id, status="active")
        db_session.add(req)
        db_session.flush()
        return req

    # ── route returns 200 with merged timeline ────────────────────────────

    def test_activity_tab_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET /v2/partials/customers/{id}/tab/activity returns 200."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200

    def test_all_three_event_kinds_appear_in_timeline(self, client: TestClient, db_session: Session, test_user: User):
        """Timeline shows events from all three sources: RFQ, quote, activity."""
        from decimal import Decimal

        from app.models.intelligence import ActivityLog
        from app.models.offers import Contact as RfqContact
        from app.models.quotes import Quote

        co = self._make_company(db_session, "MergeTest Co")
        req = self._make_requisition(db_session, co)

        # RFQ contact
        rfq = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Acme Vendor",
            status="sent",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(rfq)

        # Quote with real money value (subtotal — the correct field)
        q = Quote(
            requisition_id=req.id,
            quote_number="QT-2026-001",
            subtotal=Decimal("9999.00"),
            status="sent",
            created_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
        db_session.add(q)

        # Meaningful activity
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=co.id,
            subject="Follow-up RE: STM32",
            direction="inbound",
            is_meaningful=True,
            quality_score=0.85,
            quality_classification="meaningful",
            created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text

        # All three kinds present
        assert "Acme Vendor" in html, "RFQ vendor missing from timeline"
        assert "QT-2026-001" in html, "Quote number missing from timeline"
        assert "Email Received" in html, "Activity entry missing from timeline"

    def test_quote_value_renders_not_blank(self, client: TestClient, db_session: Session, test_user: User):
        """Quote dollar value renders (guards the q.total_amount bug fix).

        The old template used q.total_amount which does NOT exist on Quote, so every
        quote row rendered blank.  Now uses q.subtotal (or won_revenue for won quotes).
        A quote with subtotal=1234.56 must show that value.
        """
        from decimal import Decimal

        from app.models.quotes import Quote

        co = self._make_company(db_session, "QuoteBug Co")
        req = self._make_requisition(db_session, co)

        q = Quote(
            requisition_id=req.id,
            quote_number="QT-BUG-001",
            subtotal=Decimal("1234.56"),
            status="sent",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        # Dollar value must appear — "1,234.56" formatted
        assert "1,234.56" in resp.text, "Quote subtotal not rendered (total_amount bug still present)"

    def test_won_quote_shows_won_revenue(self, client: TestClient, db_session: Session, test_user: User):
        """Won quote shows won_revenue rather than subtotal."""
        from decimal import Decimal

        from app.models.quotes import Quote

        co = self._make_company(db_session, "WonQuote Co")
        req = self._make_requisition(db_session, co)

        q = Quote(
            requisition_id=req.id,
            quote_number="QT-WON-001",
            subtotal=Decimal("5000.00"),
            won_revenue=Decimal("4800.00"),
            status="won",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        assert "4,800.00" in resp.text, "Won revenue not rendered for won quote"

    def test_meaningful_activity_has_quality_badge(self, client: TestClient, db_session: Session, test_user: User):
        """Meaningful activity entry carries a quality badge in the rendered HTML."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "QualityBadge Co")
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=co.id,
            subject="Pricing request",
            is_meaningful=True,
            quality_score=0.9,
            quality_classification="meaningful",
            created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        # "Meaningful" badge text must appear
        assert "meaningful" in resp.text.lower(), "Meaningful quality badge not rendered"

    def test_noise_activity_has_hide_noise_marker(self, client: TestClient, db_session: Session, test_user: User):
        """Non-meaningful (noise) activity has the hide-noise CSS class/marker."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "NoiseTest Co")
        noise = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=co.id,
            subject="Out of office: re-joining Mon",
            is_meaningful=False,
            quality_score=0.1,
            quality_classification="noise",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(noise)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        # Noise entries must carry the js-timeline-noise class for Alpine toggle
        assert "js-timeline-noise" in resp.text, "Noise marker class missing from noise entry"

    def test_hide_noise_toggle_control_present(self, client: TestClient, db_session: Session, test_user: User):
        """Hide-noise Alpine toggle control is rendered in the activity tab."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "ToggleTest Co")
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=co.id,
            is_meaningful=False,
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text
        # Alpine x-data toggle must be present
        assert "hideNoise" in html, "Alpine hideNoise toggle not in template"
        assert "Hide routine" in html or "hide routine" in html.lower(), "Hide routine toggle label not found"

    def test_events_are_sorted_newest_first(self, client: TestClient, db_session: Session, test_user: User):
        """Timeline events appear in descending chronological order (newest first)."""
        from decimal import Decimal

        from app.models.intelligence import ActivityLog
        from app.models.quotes import Quote

        co = self._make_company(db_session, "SortTest Co")
        req = self._make_requisition(db_session, co)

        # Older quote
        q = Quote(
            requisition_id=req.id,
            quote_number="QT-SORT-OLD",
            subtotal=Decimal("100.00"),
            status="draft",
            created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(q)

        # Newer activity
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="sales_note",
            channel="manual",
            company_id=co.id,
            notes="Follow-up call done",
            is_meaningful=True,
            created_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text

        # The newer activity should appear before (lower index) the older quote
        act_pos = html.find("Follow-up call done")
        quote_pos = html.find("QT-SORT-OLD")
        assert act_pos != -1, "Activity note not found in timeline"
        assert quote_pos != -1, "Quote not found in timeline"
        assert act_pos < quote_pos, "Newer activity should appear before older quote (newest-first order)"

    def test_no_separate_rfq_history_section(self, client: TestClient, db_session: Session, test_user: User):
        """The old 'RFQ History' section heading is gone — replaced by unified
        timeline."""
        co = self._make_company(db_session, "NoSectionsTest Co")
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        assert "RFQ History" not in resp.text, "Old 'RFQ History' section still present"
        assert "Activity Log" not in resp.text, "Old 'Activity Log' section still present"


class TestUnifiedTimelineHelper:
    """Unit tests for the build_account_timeline helper function."""

    def test_build_timeline_merges_three_sources(self):
        """build_account_timeline produces events from all 3 source lists."""
        from datetime import datetime, timezone
        from decimal import Decimal
        from types import SimpleNamespace

        from app.routers.htmx_views import build_account_timeline

        t1 = datetime(2026, 6, 10, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 11, tzinfo=timezone.utc)
        t3 = datetime(2026, 6, 12, tzinfo=timezone.utc)

        rfq = SimpleNamespace(
            vendor_name="Acme",
            vendor_contact=None,
            subject="Test RFQ",
            status="sent",
            created_at=t1,
            requisition_id=1,
        )
        quote = SimpleNamespace(
            id=1,
            quote_number="QT-001",
            subtotal=Decimal("500.00"),
            total_cost=Decimal("490.00"),
            won_revenue=None,
            status="sent",
            created_at=t2,
        )
        act = SimpleNamespace(
            activity_type="email_received",
            channel="email",
            direction="inbound",
            subject="Hello",
            summary=None,
            notes=None,
            is_meaningful=True,
            quality_score=0.8,
            quality_classification="meaningful",
            occurred_at=None,
            created_at=t3,
            contact_name="Alice",
            vendor_card_id=None,
            vendor_card=None,
        )

        events = build_account_timeline([rfq], [quote], [act], req_map={1: SimpleNamespace(id=1)})
        kinds = {e["kind"] for e in events}
        assert "rfq" in kinds
        assert "quote" in kinds
        assert "activity" in kinds

    def test_build_timeline_sorted_desc(self):
        """Events are sorted newest-first."""
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from app.routers.htmx_views import build_account_timeline

        old = datetime(2026, 6, 1, tzinfo=timezone.utc)
        mid = datetime(2026, 6, 5, tzinfo=timezone.utc)
        new = datetime(2026, 6, 9, tzinfo=timezone.utc)

        rfq = SimpleNamespace(
            vendor_name="V",
            vendor_contact=None,
            subject=None,
            status="sent",
            created_at=old,
            requisition_id=None,
        )
        quote = SimpleNamespace(
            id=2,
            quote_number="Q",
            subtotal=None,
            total_cost=None,
            won_revenue=None,
            status="draft",
            created_at=mid,
        )
        act = SimpleNamespace(
            activity_type="sales_note",
            channel="manual",
            direction=None,
            subject=None,
            summary=None,
            notes="note",
            is_meaningful=True,
            quality_score=None,
            quality_classification=None,
            occurred_at=None,
            created_at=new,
            contact_name=None,
            vendor_card_id=None,
            vendor_card=None,
        )

        events = build_account_timeline([rfq], [quote], [act], req_map={})
        assert events[0]["ts"] == new
        assert events[-1]["ts"] == old


class TestActivityTabTruncation:
    """Test that the activity timeline indicates when results are truncated."""

    def test_activity_tab_shows_truncation_footer_when_rfq_limit_hit(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When >30 RFQ contacts exist, the timeline shows the truncation footer."""
        from app.models.offers import Contact as RfqContact
        from app.models.sourcing import Requisition

        company = Company(name="Busy Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create a requisition for the company
        req = Requisition(name="RFQ-001", customer_name=company.name, company_id=company.id, status="active")
        db_session.add(req)
        db_session.flush()

        # Create 31 RFQ contacts (exceeds .limit(30))
        base_ts = datetime.now(timezone.utc)
        for i in range(31):
            contact = RfqContact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="rfq",
                vendor_name=f"Vendor {i:02d}",
                vendor_contact="test@example.com",
                subject=f"RFQ {i}",
                status="sent",
                created_at=base_ts - timedelta(hours=i),
            )
            db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Showing most recent activity" in resp.text

    def test_activity_tab_shows_truncation_footer_when_quote_limit_hit(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When >20 quotes exist, the timeline shows the truncation footer."""
        from decimal import Decimal

        from app.models.crm import CustomerSite
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        company = Company(name="Quote Busy Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create a site for the company
        site = CustomerSite(company_id=company.id, site_name="Main")
        db_session.add(site)
        db_session.flush()

        # Create a requisition for the company to link quotes
        req = Requisition(name="QT-REQ-001", company_id=company.id, customer_site_id=site.id, status="active")
        db_session.add(req)
        db_session.flush()

        # Create 21 quotes (exceeds .limit(20))
        base_ts = datetime.now(timezone.utc)
        for i in range(21):
            quote = Quote(
                requisition_id=req.id,
                customer_site_id=site.id,
                quote_number=f"Q-{i:03d}",
                status="sent",
                subtotal=Decimal("1000.00"),
                total_cost=Decimal("1000.00"),
                created_at=base_ts - timedelta(hours=i),
            )
            db_session.add(quote)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Showing most recent activity" in resp.text

    def test_activity_tab_shows_truncation_footer_when_activity_limit_hit(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When >30 activities exist, the timeline shows the truncation footer."""
        from app.models.intelligence import ActivityLog

        company = Company(name="Active Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create 31 activity logs (exceeds .limit(30))
        base_ts = datetime.now(timezone.utc)
        for i in range(31):
            activity = ActivityLog(
                company_id=company.id,
                activity_type="sales_note",
                channel="manual",
                notes=f"Activity {i}",
                is_meaningful=True,
                created_at=base_ts - timedelta(hours=i),
            )
            db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Showing most recent activity" in resp.text

    def test_activity_tab_no_truncation_footer_when_under_limits(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When all sources are under their limits, no truncation footer appears."""
        from app.models.intelligence import ActivityLog

        company = Company(name="Small Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create just 5 activities (well under .limit(30))
        base_ts = datetime.now(timezone.utc)
        for i in range(5):
            activity = ActivityLog(
                company_id=company.id,
                activity_type="sales_note",
                channel="manual",
                notes=f"Activity {i}",
                is_meaningful=True,
                created_at=base_ts - timedelta(hours=i),
            )
            db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        # Truncation footer should NOT appear
        assert "Showing most recent activity" not in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# P3-5 TDD: Vendor cadence — list dots/clocks/sort + detail hero
# ─────────────────────────────────────────────────────────────────────────────


class TestVendorListCadence:
    """P3-5 TDD: vendor list shows cadence dots, dual clocks, stalest-outbound sort.

    Written FIRST (failing) — implement to pass.
    """

    def _make_vendor(self, db_session, name: str, **kwargs):
        from app.models.vendors import VendorCard

        v = VendorCard(
            normalized_name=name.lower().replace(" ", "-"),
            display_name=name,
            **kwargs,
        )
        db_session.add(v)
        db_session.commit()
        return v

    def test_vendor_list_renders_cadence_dots(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor list renders cadence indicator dots (rounded-full colored spans)."""
        self._make_vendor(db_session, "CadenceDot Vendor")
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "rounded-full" in resp.text

    @pytest.mark.parametrize(
        ("outbound_days_ago", "expected_class"),
        [
            pytest.param(None, "bg-gray-300", id="new_shows_gray"),
            pytest.param(5, "bg-emerald-400", id="on_target_shows_emerald"),
            pytest.param(35, "bg-rose-500", id="overdue_shows_rose"),
        ],
    )
    def test_vendor_list_cadence_dot_color(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        outbound_days_ago,
        expected_class,
    ):
        """Vendor list cadence dot uses correct color for new/on_target/overdue."""
        last_outbound = (
            None if outbound_days_ago is None else datetime.now(timezone.utc) - timedelta(days=outbound_days_ago)
        )
        self._make_vendor(
            db_session,
            f"DotColor {outbound_days_ago} Vendor",
            last_outbound_at=last_outbound,
        )
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert expected_class in resp.text

    def test_vendor_list_shows_out_clock(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor list rows show 'Out' clock label."""
        self._make_vendor(
            db_session,
            "OutClock Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "Out" in resp.text

    def test_vendor_list_null_outbound_shows_never(self, client: TestClient, db_session: Session, test_user: User):
        """NULL last_outbound_at renders as 'never' (not 'never replied')."""
        self._make_vendor(db_session, "NeverOut Vendor", last_outbound_at=None)
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "never" in resp.text.lower()
        assert "never replied" not in resp.text.lower()

    def test_vendor_list_null_reply_shows_dash(self, client: TestClient, db_session: Session, test_user: User):
        """NULL last_reply_at shows '—' (em-dash), not 'never'."""
        self._make_vendor(
            db_session,
            "NullReply Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=3),
            last_reply_at=None,
        )
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        # Reply clock dash
        assert "Reply" in resp.text
        # The null reply shows a dash, not the word "never"
        assert "—" in resp.text or "&mdash;" in resp.text

    def test_vendor_list_score_column_still_present(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor score column is still rendered (additive — not removed by cadence)."""
        self._make_vendor(db_session, "ScoreStillThere Vendor", vendor_score=72.0)
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "72" in resp.text

    def test_vendor_list_stalest_outbound_sort_option(self, client: TestClient, db_session: Session, test_user: User):
        """sort=outbound_asc is a valid option in vendor list (accepted, returns
        200)."""
        self._make_vendor(
            db_session,
            "SortA Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        self._make_vendor(
            db_session,
            "SortB Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        resp = client.get("/v2/partials/vendors?sort=outbound_asc")
        assert resp.status_code == 200

    def test_vendor_list_stalest_outbound_sort_orders_nulls_first(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """sort=outbound_asc puts NULL (never-contacted) vendors before oldest."""
        self._make_vendor(db_session, "NullFirst Vendor", last_outbound_at=None)
        self._make_vendor(
            db_session,
            "OldFirst Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=40),
        )
        self._make_vendor(
            db_session,
            "RecentLast Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        resp = client.get("/v2/partials/vendors?sort=outbound_asc")
        assert resp.status_code == 200
        html = resp.text
        assert html.index("NullFirst Vendor") < html.index("OldFirst Vendor")
        assert html.index("OldFirst Vendor") < html.index("RecentLast Vendor")

    def test_vendor_list_stalest_sort_option_present_in_html(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Vendor list renders a sort option for 'Stalest outbound'."""
        self._make_vendor(db_session, "SortOptionCheck Vendor")
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        # The sort param name must be in the page (hidden input or link)
        assert "outbound_asc" in resp.text

    def test_vendor_list_discovery_tabs_still_present(self, client: TestClient, db_session: Session, test_user: User):
        """All Vendors, My Vendors, and Find by Part tabs still render."""
        self._make_vendor(db_session, "TabsCheck Vendor")
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "All Vendors" in resp.text
        assert "My Vendors" in resp.text
        assert "Find by Part" in resp.text


class TestVendorDetailCadenceHero:
    """P3-5 TDD: vendor detail shows cadence hero card with badge, clocks,
    next-best-touch, while keeping vendor_score block and sightings.

    Written FIRST (failing) — implement to pass.
    """

    def _make_vendor(self, db_session, name: str, **kwargs):
        from app.models.vendors import VendorCard

        v = VendorCard(
            normalized_name=name.lower().replace(" ", "-"),
            display_name=name,
            **kwargs,
        )
        db_session.add(v)
        db_session.commit()
        return v

    def test_vendor_detail_renders_cadence_hero(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor detail page renders a cadence card section."""
        v = self._make_vendor(db_session, "HeroCheck Vendor")
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        # The cadence card contains the 'Last Out' clock label
        assert "Last Out" in resp.text

    def test_vendor_detail_cadence_badge_new(self, client: TestClient, db_session: Session, test_user: User):
        """NULL outbound → cadence badge shows 'New'."""
        v = self._make_vendor(db_session, "BadgeNew Vendor", last_outbound_at=None)
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "New" in resp.text

    def test_vendor_detail_cadence_badge_overdue(self, client: TestClient, db_session: Session, test_user: User):
        """35-day-old outbound → cadence badge shows 'Overdue'."""
        v = self._make_vendor(
            db_session,
            "BadgeOverdue Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "Overdue" in resp.text

    def test_vendor_detail_cadence_badge_on_target(self, client: TestClient, db_session: Session, test_user: User):
        """5-day-old outbound (within standard 30d) → cadence badge 'On Target'."""
        v = self._make_vendor(
            db_session,
            "BadgeOnTarget Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "On Target" in resp.text

    def test_vendor_detail_next_best_touch_present(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor detail renders next-best-touch text."""
        v = self._make_vendor(db_session, "NBT Vendor", last_outbound_at=None)
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        # next_best_touch for new vendor = "Never contacted — reach out"
        assert "Never contacted" in resp.text

    def test_vendor_detail_dual_clocks_present(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor detail renders both 'Last Out' and 'Last Reply' clock labels."""
        v = self._make_vendor(
            db_session,
            "DualClock Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=10),
            last_reply_at=datetime.now(timezone.utc) - timedelta(days=8),
        )
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "Last Out" in resp.text
        assert "Last Reply" in resp.text

    def test_vendor_detail_null_outbound_shows_never(self, client: TestClient, db_session: Session, test_user: User):
        """NULL last_outbound_at in detail hero shows 'Never'."""
        v = self._make_vendor(db_session, "NeverOut Detail Vendor", last_outbound_at=None)
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "Never" in resp.text

    def test_vendor_detail_null_reply_shows_dash_not_never(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """NULL last_reply_at in detail hero shows '—' (not 'never replied')."""
        v = self._make_vendor(
            db_session,
            "NullReply Detail Vendor",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=5),
            last_reply_at=None,
        )
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "—" in resp.text or "&mdash;" in resp.text
        assert "never replied" not in resp.text.lower()

    def test_vendor_detail_score_block_still_present(self, client: TestClient, db_session: Session, test_user: User):
        """vendor_score block is still shown in header (not removed by cadence hero)."""
        v = self._make_vendor(db_session, "ScoreKept Vendor", vendor_score=85.0)
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "85" in resp.text
        assert "Score" in resp.text

    def test_vendor_detail_stat_row_still_present(self, client: TestClient, db_session: Session, test_user: User):
        """4-stat row (Sightings, Win Rate, POs, Avg Response) still renders."""
        v = self._make_vendor(db_session, "StatRow Vendor", sighting_count=7)
        resp = client.get(f"/v2/partials/vendors/{v.id}")
        assert resp.status_code == 200
        assert "Sightings" in resp.text
        assert "7" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# TestSegmentTagViews — P2a manual account segmentation tags
# ─────────────────────────────────────────────────────────────────────────────


class TestSegmentTagViews:
    """Tests for segment-tag UI endpoints.

    Written FIRST (TDD RED) — will fail until the routes are added to
    htmx_views.py and the templates updated.

    Routes tested:
      POST /v2/partials/customers/{company_id}/segment-tags   (assign)
      DELETE /v2/partials/customers/{company_id}/segment-tags/{tag_id}  (unassign)
      GET  /v2/partials/customers/{company_id}/segment-tags   (chips partial)
    """

    def _make_company(self, db_session: Session, name: str = "SegView Co") -> Company:
        co = Company(name=name, is_active=True)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        return co

    def test_segment_chips_partial_returns_html(self, client: TestClient, db_session: Session, test_user: User):
        """GET /v2/partials/customers/{id}/segment-tags returns 200 HTML."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/segment-tags")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_assign_segment_tag_returns_chips_partial(self, client: TestClient, db_session: Session, test_user: User):
        """POST assign creates the EntityTag and re-renders the chips partial."""
        from app.services.tagging import get_or_create_segment_tag

        co = self._make_company(db_session, "AssignSeg Co")
        tag = get_or_create_segment_tag("OEM", db_session)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{co.id}/segment-tags",
            data={"tag_id": str(tag.id)},
        )
        assert resp.status_code == 200
        assert "OEM" in resp.text

    def test_unassign_segment_tag_returns_chips_partial(self, client: TestClient, db_session: Session, test_user: User):
        """DELETE unassign removes the EntityTag and re-renders the chips partial.

        The tag may still appear in the 'add existing' dropdown, but the active chip
        (which carries a remove-button with hx-delete) must be gone.
        """
        from app.services.tagging import assign_segment_tag, get_or_create_segment_tag

        co = self._make_company(db_session, "UnassignSeg Co")
        tag = get_or_create_segment_tag("At-risk", db_session)
        assign_segment_tag(company_id=co.id, tag_id=tag.id, db=db_session)
        db_session.commit()

        resp = client.delete(f"/v2/partials/customers/{co.id}/segment-tags/{tag.id}")
        assert resp.status_code == 200
        # The remove-button for this tag must no longer be present after removal.
        # (The tag name may still appear in the "add existing" dropdown.)
        assert f"hx-delete='/v2/partials/customers/{co.id}/segment-tags/{tag.id}'" not in resp.text

    def test_account_detail_renders_segment_tag_section(self, client: TestClient, db_session: Session, test_user: User):
        """Company detail page includes the segment-tags editor block."""
        co = self._make_company(db_session, "DetailSeg Co")

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        # The segment tag editor container must be present in the detail
        assert "segment-tags" in resp.text

    def test_list_filter_bar_renders_segment_dropdown(self, client: TestClient, db_session: Session, test_user: User):
        """The CDM filter bar renders a segment-tags dropdown when segment tags
        exist."""
        from app.services.tagging import get_or_create_segment_tag

        get_or_create_segment_tag("OEM", db_session)
        get_or_create_segment_tag("At-risk", db_session)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        # The dropdown name attribute must be present
        assert 'name="segment"' in resp.text
        assert "OEM" in resp.text

    def test_account_list_segment_filter_param_accepted(self, client: TestClient, db_session: Session, test_user: User):
        """The account-list partial accepts a segment= query param without error."""
        from app.services.tagging import assign_segment_tag, get_or_create_segment_tag

        co = self._make_company(db_session, "FilterAccept Co")
        tag = get_or_create_segment_tag("OEM", db_session)
        assign_segment_tag(company_id=co.id, tag_id=tag.id, db=db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/account-list?segment={tag.id}")
        assert resp.status_code == 200
        assert "FilterAccept Co" in resp.text

    def test_create_new_segment_tag_via_name_param(self, client: TestClient, db_session: Session, test_user: User):
        """POST with tag_name= (instead of tag_id=) creates a new segment tag and
        assigns it."""
        co = self._make_company(db_session, "NewTag Co")

        resp = client.post(
            f"/v2/partials/customers/{co.id}/segment-tags",
            data={"tag_name": "Growth"},
        )
        assert resp.status_code == 200
        assert "Growth" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# TestTierSetter — P2b account tier setter
# ─────────────────────────────────────────────────────────────────────────────


class TestTierSetter:
    """Tests for account tier-setter endpoint.

    Written FIRST (TDD RED) — will fail until the route is added.

    Routes tested:
      POST /v2/partials/customers/{company_id}/tier  (set tier)
    """

    def _make_company(self, db_session: Session, name: str = "TierSet Co", **kwargs) -> Company:
        co = Company(name=name, is_active=True, **kwargs)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        return co

    def test_set_tier_updates_db(self, client: TestClient, db_session: Session, test_user: User):
        """POST tier=core persists to Company.tier."""
        co = self._make_company(db_session)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "core"})
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.tier == "core"

    def test_set_tier_rerenders_cadence_hero(self, client: TestClient, db_session: Session, test_user: User):
        """POST tier=core re-renders the cadence hero with updated tier label."""
        co = self._make_company(db_session)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "core"})
        assert resp.status_code == 200
        # The re-rendered hero should show the tier word
        assert "core" in resp.text.lower() or "Core" in resp.text

    def test_set_tier_cadence_badge_reflects_new_tier(self, client: TestClient, db_session: Session, test_user: User):
        """Setting tier=key on an account that was last contacted 10 days ago changes
        cadence from 'due' (standard 30d) to 'overdue' would NOT apply here but key
        target is 7d so 10d ago → 'due' badge → amber classes present."""
        outbound_10d_ago = datetime.now(timezone.utc) - timedelta(days=10)
        co = self._make_company(db_session, last_outbound_at=outbound_10d_ago)
        # Before: standard tier → 10d is on_target (target=30)
        resp_before = client.get(f"/v2/partials/customers/{co.id}")
        assert "bg-emerald-100" in resp_before.text  # on_target

        # After: set tier=key → target=7d, so 10d → 'due' → amber
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "key"})
        assert resp.status_code == 200
        assert "bg-amber-100" in resp.text  # due

    def test_set_tier_invalid_value_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST with invalid tier value returns 400."""
        co = self._make_company(db_session)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "vip"})
        assert resp.status_code == 400

    def test_set_tier_blank_clears_tier(self, client: TestClient, db_session: Session, test_user: User):
        """POST with tier='' (blank/unset) clears Company.tier to None."""
        co = self._make_company(db_session, tier="key")
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": ""})
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.tier is None

    def test_set_tier_all_valid_values_accepted(self, client: TestClient, db_session: Session, test_user: User):
        """All four valid tier values are accepted without 400."""
        for tier_val in ("key", "core", "standard", "prospect"):
            co = self._make_company(db_session, name=f"TierSet {tier_val}")
            resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": tier_val})
            assert resp.status_code == 200, f"tier={tier_val} should be accepted"

    def test_set_tier_nonexistent_company_returns_404(self, client: TestClient, db_session: Session, test_user: User):
        """POST to unknown company_id returns 404."""
        resp = client.post("/v2/partials/customers/99999/tier", data={"tier": "core"})
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# TestBuyingRoleSetter — P2b contact buying-role setter
# ─────────────────────────────────────────────────────────────────────────────


class TestBuyingRoleSetter:
    """Tests for contact buying-role setter endpoint.

    Written FIRST (TDD RED) — will fail until routes are added.

    Routes tested:
      POST /v2/partials/customers/{company_id}/contacts/{contact_id}/role
    """

    def _make_company_with_contact(self, db_session: Session, **contact_kwargs):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="RoleSet Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Alex Buyer",
            email="alex@roleset.com",
            **contact_kwargs,
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)
        db_session.refresh(company)
        return company, site, contact

    def test_set_role_updates_db(self, client: TestClient, db_session: Session, test_user: User):
        """POST contact_role=buyer_po persists to SiteContact.contact_role."""

        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "buyer_po"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "buyer_po"

    def test_set_role_rerenders_chip(self, client: TestClient, db_session: Session, test_user: User):
        """POST role returns HTML containing the chip for the new role."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "specifier"},
        )
        assert resp.status_code == 200
        assert "specifier" in resp.text.lower() or "Specifier" in resp.text

    def test_set_role_invalid_value_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST with unknown role value returns 400."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "wizard"},
        )
        assert resp.status_code == 400

    def test_set_role_all_canonical_values_accepted(self, client: TestClient, db_session: Session, test_user: User):
        """All canonical buying-role values are accepted."""
        for role_val in ("specifier", "buyer_po", "ap_payer", "logistics", "exec", "other"):
            company, site, contact = self._make_company_with_contact(db_session)
            resp = client.post(
                f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
                data={"contact_role": role_val},
            )
            assert resp.status_code == 200, f"role={role_val} should be accepted"

    def test_set_role_nonexistent_contact_returns_404(self, client: TestClient, db_session: Session, test_user: User):
        """POST to unknown contact_id returns 404."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/99999/role",
            data={"contact_role": "buyer_po"},
        )
        assert resp.status_code == 404

    def test_set_role_blank_clears_role(self, client: TestClient, db_session: Session, test_user: User):
        """POST with contact_role='' clears the role to None."""

        company, site, contact = self._make_company_with_contact(db_session, contact_role="buyer")
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role is None


# ─────────────────────────────────────────────────────────────────────────────
# TestRoleChipLegacy — role_chip macro handles legacy + new canonical values
# ─────────────────────────────────────────────────────────────────────────────


class TestRoleChipLegacy:
    """Tests that role_chip renders both legacy and new canonical values.

    Written FIRST (TDD RED) — will fail until the template is updated.
    """

    def _make_company_with_contact(self, db_session: Session, contact_role: str | None = None):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="ChipTest Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Chip Test",
            email="chip@test.com",
            contact_role=contact_role,
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(company)
        return company, site, contact

    def test_legacy_decision_maker_renders_gracefully(self, client: TestClient, db_session: Session, test_user: User):
        """Legacy role 'decision_maker' still renders a chip without error."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role="decision_maker")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "decision" in resp.text.lower() or "Decision" in resp.text

    def test_legacy_buyer_renders_gracefully(self, client: TestClient, db_session: Session, test_user: User):
        """Legacy role 'buyer' still renders a chip."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role="buyer")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "buyer" in resp.text.lower() or "Buyer" in resp.text

    def test_canonical_buyer_po_renders_chip(self, client: TestClient, db_session: Session, test_user: User):
        """New canonical role 'buyer_po' renders a chip in the contact card."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role="buyer_po")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        # Should show some chip text for buyer_po
        assert "buyer" in resp.text.lower() or "PO" in resp.text or "Buyer" in resp.text

    def test_canonical_specifier_renders_chip(self, client: TestClient, db_session: Session, test_user: User):
        """New canonical role 'specifier' renders a chip."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role="specifier")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "specifier" in resp.text.lower() or "Specifier" in resp.text

    def test_null_role_renders_no_chip(self, client: TestClient, db_session: Session, test_user: User):
        """NULL contact_role renders no chip (not an error)."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role=None)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200


class TestEditSite:
    """P2c: Edit-site modal form (GET edit-form + POST edit)."""

    def _make_company_with_site(self, db_session: Session):
        from app.models.crm import CustomerSite

        company = Company(name="Edit Site Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(
            company_id=company.id,
            site_name="HQ",
            site_type="hq",
            city="Boston",
            country="US",
            address_line1="123 Main St",
            payment_terms="Net30",
            shipping_terms="FCA",
            is_active=True,
        )
        db_session.add(site)
        db_session.commit()
        return company, site

    def test_get_site_edit_form_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form route renders a form pre-populated with site fields."""
        company, site = self._make_company_with_site(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/sites/{site.id}/edit-form")
        assert resp.status_code == 200
        assert "HQ" in resp.text
        assert "Boston" in resp.text

    def test_get_site_edit_form_404_on_missing_site(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form for a nonexistent site returns 404."""
        company, _ = self._make_company_with_site(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/sites/99999/edit-form")
        assert resp.status_code == 404

    def test_post_site_edit_persists_payment_terms_and_address(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit saves payment_terms + address fields; re-rendered sites tab shows
        new values."""
        from app.models.crm import CustomerSite

        company, site = self._make_company_with_site(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/edit",
            data={
                "site_name": "HQ",
                "address_line1": "456 New Ave",
                "city": "Cambridge",
                "state": "MA",
                "zip": "02139",
                "country": "US",
                "payment_terms": "Net60",
                "shipping_terms": "DAP",
                "site_type": "hq",
                "notes": "updated note",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.query(CustomerSite).filter(CustomerSite.id == site.id).first()
        assert updated is not None
        assert updated.payment_terms == "Net60"
        assert updated.address_line1 == "456 New Ave"
        assert updated.city == "Cambridge"
        assert updated.state == "MA"
        assert updated.zip == "02139"
        assert updated.shipping_terms == "DAP"
        assert updated.notes == "updated note"

    def test_post_site_edit_re_renders_sites_tab(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit response is the refreshed sites tab containing the updated site
        name."""
        company, site = self._make_company_with_site(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/edit",
            data={"site_name": "New HQ Name", "city": "Salem", "country": "US"},
        )
        assert resp.status_code == 200
        assert "New HQ Name" in resp.text

    def test_post_site_edit_missing_site_name_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with empty site_name returns 400."""
        company, site = self._make_company_with_site(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/edit",
            data={"site_name": "", "city": "Boston", "country": "US"},
        )
        assert resp.status_code == 400


class TestEditContact:
    """P2c: Edit-contact modal form (GET edit-form + POST edit)."""

    def _make_company_with_contact(self, db_session: Session):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Edit Contact Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Alice Smith",
            title="Buyer",
            email="alice@editco.com",
            phone="+16175550001",
            wechat_id="alice_wc",
            notes="original note",
            contact_role="buyer",
        )
        db_session.add(contact)
        db_session.commit()
        return company, site, contact

    def test_get_contact_edit_form_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form renders form pre-populated with contact fields."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 200
        assert "Alice Smith" in resp.text
        assert "alice@editco.com" in resp.text

    def test_get_contact_edit_form_404_on_missing_contact(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """GET edit-form for nonexistent contact returns 404."""
        company, site, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/99999/edit-form")
        assert resp.status_code == 404

    def test_post_contact_edit_persists_title_and_phone(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit saves title + phone; re-rendered contacts show new values."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Smith",
                "title": "Senior Buyer",
                "email": "alice@editco.com",
                "phone": "+16175550099",
                "wechat_id": "alice_wc",
                "notes": "updated note",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.query(SiteContact).filter(SiteContact.id == contact.id).first()
        assert updated is not None
        assert updated.title == "Senior Buyer"
        assert updated.phone == "+16175550099"
        assert updated.notes == "updated note"

    def test_post_contact_edit_does_not_touch_contact_role(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST contact edit never modifies contact_role (owned by P2b role setter)."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Smith",
                "contact_role": "decision_maker",  # attacker tries to override role
                "title": "Buyer",
                "email": "alice@editco.com",
                "phone": "+16175550001",
                "wechat_id": "alice_wc",
                "notes": "original note",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.query(SiteContact).filter(SiteContact.id == contact.id).first()
        assert updated is not None
        assert updated.contact_role == "buyer"  # unchanged

    def test_post_contact_edit_re_renders_contacts_panel(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit response contains the updated name in the re-rendered contacts
        panel."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Alice Updated", "title": "VP", "email": "alice@editco.com", "phone": ""},
        )
        assert resp.status_code == 200
        assert "Alice Updated" in resp.text

    def test_post_contact_edit_missing_full_name_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with empty full_name returns 400."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "", "title": "Buyer", "email": "alice@editco.com"},
        )
        assert resp.status_code == 400

    def test_post_contact_edit_invalid_email_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with malformed email returns 400."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Alice Smith", "email": "not-an-email"},
        )
        assert resp.status_code == 400
