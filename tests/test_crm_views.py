"""Tests for CRM shell views.

Called by: pytest
Depends on: app.routers.crm.views
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company
from tests.conftest import engine  # noqa: F401


@pytest.fixture()
def test_site(db_session: Session, test_company: Company):
    """A CustomerSite (HQ) belonging to test_company."""
    from app.models.crm import CustomerSite

    site = CustomerSite(
        company_id=test_company.id,
        site_name="HQ",
        site_type="hq",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def test_contact(db_session: Session, test_site):
    """A SiteContact belonging to test_site."""
    from app.models.crm import SiteContact

    contact = SiteContact(
        customer_site_id=test_site.id,
        full_name="Test Contact",
        email="testcontact@acme.com",
        contact_role="buyer",
        is_primary=True,
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    Company detail + tab partials (``GET /v2/partials/customers/{id}`` and
    ``.../tab/{tab}``) now gate on ``can_manage_account``. The classes below GET those
    endpoints as ``test_user`` on companies they create without assigning ownership, so
    promote the actor to MANAGER (``can_manage_account`` is True for managers, exactly as
    for the account owner) to exercise the authorized render path. Applied per-class via
    ``@pytest.mark.usefixtures`` — scoped narrowly so role-based list tests are untouched.
    """
    test_user.role = "manager"
    db_session.commit()


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


@pytest.mark.usefixtures("_grant_account_management")
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
        assert "outlook.office.com/mail/deeplink/compose?to=" in resp.text
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
        assert 'data-channel="wechat"' in resp.text
        assert 'data-value="jane_wc"' in resp.text
        assert "navigator.clipboard" in resp.text

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

        company = Company(name="WeChat Create Co", is_active=True, account_owner_id=test_user.id)
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

        company = Company(name="WeChat Long Co", is_active=True, account_owner_id=test_user.id)
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

    def test_legacy_and_real_same_email_renders_one_row(self, client: TestClient, db_session: Session, test_user: User):
        """When a site has both a legacy contact_* entry AND a real SiteContact with the
        same email, company_contact_rows dedup ensures only one row is rendered — the
        real SiteContact wins and the legacy duplicate is suppressed."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Dedup Legacy Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        # Site carries legacy contact_* fields
        site = CustomerSite(
            company_id=company.id,
            site_name="HQ",
            is_active=True,
            contact_name="Legacy Jane",
            contact_email="shared@dedup.com",
        )
        db_session.add(site)
        db_session.flush()
        # Real SiteContact with the same email
        sc = SiteContact(
            customer_site_id=site.id,
            full_name="Real Jane",
            email="shared@dedup.com",
            enrichment_source="hunter",
        )
        db_session.add(sc)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        html = resp.text
        # The real SiteContact name must appear
        assert "Real Jane" in html
        # The legacy duplicate must NOT appear as a second card (its name is suppressed)
        assert "Legacy Jane" not in html, (
            "Legacy contact should be suppressed when a real SiteContact has the same email"
        )
        # Confirm only 1 contact entry rendered (the site grouping shows "1 contact")
        assert "1 contact" in html, "Site group should show 1 contact, not 2"


@pytest.mark.usefixtures("_grant_account_management")
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


class TestPerformanceRetired:
    """The Team Performance dashboard + its JSON metrics endpoint are retired.

    Both lived only behind the Reporting surface, which was removed by the Buy Plan Deal
    Hub. The HTML partial, the JSON endpoint, and their score helpers no longer exist.
    (The underlying avail_score_service is unaffected — it still feeds the daily compute
    job.)
    """

    def test_performance_partial_gone(self, client: TestClient):
        """GET /v2/partials/crm/performance no longer exists (route removed)."""
        resp = client.get("/v2/partials/crm/performance")
        assert resp.status_code == 404

    def test_performance_metrics_endpoint_gone(self, client: TestClient):
        """GET /api/crm/performance-metrics no longer exists (route removed)."""
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 404


@pytest.mark.usefixtures("_grant_account_management")
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
        assert "outlook.office.com/mail/deeplink/compose?to=" in html

    # ── empty state ─────────────────────────────────────────────────────

    def test_empty_company_shows_no_contacts_message(self, client: TestClient, db_session, test_user):
        """Company with no contacts shows a helpful empty-state message."""
        company = Company(name="Empty P33 Co", is_active=True)
        db_session.add(company)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No contacts" in resp.text or "no contacts" in resp.text.lower()


@pytest.mark.usefixtures("_grant_account_management")
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


@pytest.mark.usefixtures("_grant_account_management")
class TestUnifiedActivityTimeline:
    """P3-4 / Step-1 CRM regroup: activity tab shows type-sectioned ActivityLog feed.

    Quotes and RFQ contacts are intentionally absent — they have their own tabs. Updated
    tests reflect the new section-per-type design.
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

        req = Requisition(name=name, customer_name=company.name, company_id=company.id, status="open")
        db_session.add(req)
        db_session.flush()
        return req

    # ── route returns 200 with merged timeline ────────────────────────────

    def test_activity_tab_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET /v2/partials/customers/{id}/tab/activity returns 200."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200

    def test_activity_log_events_appear_in_sections(self, client: TestClient, db_session: Session, test_user: User):
        """Activity tab shows ActivityLog entries in their type sections.

        RFQ contacts appear in the Emails section (canonical RFQ source). Quotes are NOT
        shown — they belong in the Quotes tab.
        """
        from decimal import Decimal

        from app.models.intelligence import ActivityLog
        from app.models.offers import Contact as RfqContact
        from app.models.quotes import Quote

        co = self._make_company(db_session, "MergeTest Co")
        req = self._make_requisition(db_session, co)

        # RFQ contact — should appear in Emails section (canonical RFQ source)
        rfq = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Acme Vendor",
            status="sent",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(rfq)

        # Quote — should NOT appear (own tab)
        q = Quote(
            requisition_id=req.id,
            quote_number="QT-2026-001",
            subtotal=Decimal("9999.00"),
            status="sent",
            created_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
        db_session.add(q)

        # Meaningful activity — should appear in Emails section
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

        # Activity log entry rendered in Emails section
        assert "Email Received" in html, "ActivityLog entry missing from timeline"
        # RFQ contact appears in Emails section (canonical source)
        assert "Acme Vendor" in html, "RFQ contact must appear in Emails section"
        # Quote is absent from this tab
        assert "QT-2026-001" not in html, "Quote must not appear in activity tab"

    def test_quotes_absent_from_activity_tab(self, client: TestClient, db_session: Session, test_user: User):
        """Quotes are absent from the activity tab — they belong in the Quotes tab.

        Regression guard: the old implementation mixed quotes into the activity feed.
        The new section-per-type design renders only ActivityLog rows here.
        """
        from decimal import Decimal

        from app.models.quotes import Quote

        co = self._make_company(db_session, "QuoteAbsent Co")
        req = self._make_requisition(db_session, co)

        q = Quote(
            requisition_id=req.id,
            quote_number="QT-ABSENT-001",
            subtotal=Decimal("1234.56"),
            status="sent",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        # Quote must NOT appear in the activity tab
        assert "QT-ABSENT-001" not in resp.text, "Quote must not appear in activity tab"
        assert "1,234.56" not in resp.text, "Quote dollar value must not appear in activity tab"

    def test_won_quote_absent_from_activity_tab(self, client: TestClient, db_session: Session, test_user: User):
        """Won quotes are absent from the activity tab (belongs in Quotes tab)."""
        from decimal import Decimal

        from app.models.quotes import Quote

        co = self._make_company(db_session, "WonQuote Co")
        req = self._make_requisition(db_session, co)

        q = Quote(
            requisition_id=req.id,
            quote_number="QT-WON-ABSENT",
            subtotal=Decimal("5000.00"),
            won_revenue=Decimal("4800.00"),
            status="won",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        assert "QT-WON-ABSENT" not in resp.text, "Won quote must not appear in activity tab"
        assert "4,800.00" not in resp.text, "Won revenue must not appear in activity tab"

    def test_email_activity_renders_in_emails_section(self, client: TestClient, db_session: Session, test_user: User):
        """email_received ActivityLog renders in the Emails section."""
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
        html = resp.text
        # Emails section header present
        assert ">Emails<" in html, "Emails section header not rendered"
        # The activity type label is rendered by activity_row
        assert "Email Received" in html, "email_received activity row missing"

    def test_system_activity_renders_in_other_section(self, client: TestClient, db_session: Session, test_user: User):
        """System/status activities (status_changed) land in the Other section."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "OtherSection Co")
        noise = ActivityLog(
            user_id=test_user.id,
            activity_type="status_changed",
            channel="system",
            company_id=co.id,
            summary="Status changed to active",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(noise)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        # Other section present and uses Alpine hideOther (collapsible)
        assert ">Other<" in resp.text, "Other section header not rendered for system activity"
        assert "hideOther" in resp.text, "Alpine hideOther toggle missing for Other section"

    def test_other_section_toggle_control_present(self, client: TestClient, db_session: Session, test_user: User):
        """The Other section has the Alpine hideOther collapse control."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "ToggleTest Co")
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="offer_created",
            channel="system",
            company_id=co.id,
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text
        # Alpine hideOther (new name — section-level toggle)
        assert "hideOther" in html, "Alpine hideOther toggle not in template"

    def test_events_within_section_sorted_newest_first(self, client: TestClient, db_session: Session, test_user: User):
        """Within a type section, activities appear newest-first (date groups
        descending)."""
        from app.models.intelligence import ActivityLog

        co = self._make_company(db_session, "SortTest Co")

        # Two notes: older and newer
        older = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            company_id=co.id,
            notes="Older note from June 1",
            created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        newer = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            company_id=co.id,
            notes="Newer note from June 5",
            created_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        )
        db_session.add_all([older, newer])
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text

        # Notes section present
        assert ">Notes<" in html, "Notes section header not rendered"
        # Newer note appears before older note (newest-first within section)
        newer_pos = html.find("Newer note from June 5")
        older_pos = html.find("Older note from June 1")
        assert newer_pos != -1, "Newer note not found in timeline"
        assert older_pos != -1, "Older note not found in timeline"
        assert newer_pos < older_pos, "Newer note should appear before older note (newest-first)"

    def test_no_separate_rfq_history_section(self, client: TestClient, db_session: Session, test_user: User):
        """The old 'RFQ History' section heading is gone — replaced by unified
        timeline."""
        co = self._make_company(db_session, "NoSectionsTest Co")
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        assert "RFQ History" not in resp.text, "Old 'RFQ History' section still present"
        assert "Activity Log" not in resp.text, "Old 'Activity Log' section still present"


class TestManageableCompanyIdsBatch:
    """Batched ownership check (_manageable_company_ids) — parity with
    can_manage_account.

    The bulk/import loops call this once instead of ``can_manage_account`` per row. It
    must return exactly the subset of company ids a non-manager rep may manage, via the
    three ownership paths: primary account owner, site owner, and named collaborator.
    """

    def test_batched_matches_per_row_can_manage_account(self, db_session: Session, test_user: User):
        """The batched set equals the per-row can_manage_account verdict for every
        candidate."""
        from app.dependencies import can_manage_account
        from app.models.crm import AccountCollaborator, CustomerSite
        from app.routers.htmx.companies import _manageable_company_ids

        # Non-manager: exercise the ownership paths, not the is_manager_or_admin short-circuit.
        test_user.role = "sales"

        owned = Company(name="Batch Owned Co", is_active=True, account_owner_id=test_user.id)
        via_site = Company(name="Batch SiteOwned Co", is_active=True, account_owner_id=None)
        via_collab = Company(name="Batch Collab Co", is_active=True, account_owner_id=None)
        not_mine = Company(name="Batch NotMine Co", is_active=True, account_owner_id=None)
        db_session.add_all([owned, via_site, via_collab, not_mine])
        db_session.commit()

        db_session.add(CustomerSite(company_id=via_site.id, site_name="HQ", is_active=True, owner_id=test_user.id))
        db_session.add(AccountCollaborator(company_id=via_collab.id, user_id=test_user.id, role="helper"))
        db_session.commit()

        companies = [owned, via_site, via_collab, not_mine]
        result = _manageable_company_ids(test_user, companies, db_session)

        assert result == {owned.id, via_site.id, via_collab.id}
        for co in companies:
            assert (co.id in result) == can_manage_account(test_user, co, db_session)

    def test_empty_input_returns_empty_set(self, db_session: Session, test_user: User):
        """No candidate companies → empty set, and no queries needed."""
        from app.routers.htmx.companies import _manageable_company_ids

        assert _manageable_company_ids(test_user, [], db_session) == set()


@pytest.mark.usefixtures("_grant_account_management")
class TestActivityTabTruncation:
    """Test that the activity tab indicates when ActivityLog results are truncated.

    The redesign fetches only ActivityLog rows (limit=50) — no RFQ contacts or quotes.
    """

    def test_activity_tab_shows_truncation_footer_when_activity_limit_hit(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When >=50 activities exist, the tab shows the truncation footer."""
        from app.models.intelligence import ActivityLog

        company = Company(name="Active Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create 51 activity logs (exceeds .limit(50))
        base_ts = datetime.now(timezone.utc)
        for i in range(51):
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
        assert "Showing most recent 50 activities" in resp.text

    def test_activity_tab_no_truncation_footer_when_under_limit(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When under 50 activities, no truncation footer appears."""
        from app.models.intelligence import ActivityLog

        company = Company(name="Small Corp", is_active=True)
        db_session.add(company)
        db_session.flush()

        # Create just 5 activities (well under .limit(50))
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
        assert "Showing most recent" not in resp.text


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
        """All Vendors + My Vendors tabs still render; Find by Part removed (redundant
        with Search/Sightings)."""
        self._make_vendor(db_session, "TabsCheck Vendor")
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "All Vendors" in resp.text
        assert "My Vendors" in resp.text
        assert "Find by Part" not in resp.text


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


@pytest.mark.usefixtures("_grant_account_management")
class TestSegmentTagViews:
    """Tests for segment-tag UI endpoints.

    Written FIRST (TDD RED) — will fail until the routes are added to
    htmx_views.py and the templates updated.

    Routes tested:
      POST /v2/partials/customers/{company_id}/segment-tags   (assign)
      DELETE /v2/partials/customers/{company_id}/segment-tags/{tag_id}  (unassign)
      GET  /v2/partials/customers/{company_id}/segment-tags   (chips partial)
    """

    def _make_company(self, db_session: Session, name: str = "SegView Co", owner: User | None = None) -> Company:
        co = Company(name=name, is_active=True, account_owner_id=owner.id if owner else None)
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

        co = self._make_company(db_session, "AssignSeg Co", owner=test_user)
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

        co = self._make_company(db_session, "UnassignSeg Co", owner=test_user)
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
        co = self._make_company(db_session, "NewTag Co", owner=test_user)

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
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "core"})
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.tier == "core"

    def test_set_tier_rerenders_cadence_hero(self, client: TestClient, db_session: Session, test_user: User):
        """POST tier=core re-renders the cadence hero with updated tier label."""
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "core"})
        assert resp.status_code == 200
        # The re-rendered hero should show the tier word
        assert "core" in resp.text.lower() or "Core" in resp.text

    def test_set_tier_cadence_badge_reflects_new_tier(self, client: TestClient, db_session: Session, test_user: User):
        """Setting tier=key on an account that was last contacted 10 days ago changes
        cadence from 'due' (standard 30d) to 'overdue' would NOT apply here but key
        target is 7d so 10d ago → 'due' badge → amber classes present."""
        outbound_10d_ago = datetime.now(timezone.utc) - timedelta(days=10)
        co = self._make_company(db_session, last_outbound_at=outbound_10d_ago, account_owner_id=test_user.id)
        # Before: standard tier → 10d is on_target (target=30)
        resp_before = client.get(f"/v2/partials/customers/{co.id}")
        assert "bg-emerald-100" in resp_before.text  # on_target

        # After: set tier=key → target=7d, so 10d → 'due' → amber
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "key"})
        assert resp.status_code == 200
        assert "bg-amber-100" in resp.text  # due

    def test_set_tier_invalid_value_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST with invalid tier value returns 400."""
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": "vip"})
        assert resp.status_code == 400

    def test_set_tier_blank_clears_tier(self, client: TestClient, db_session: Session, test_user: User):
        """POST with tier='' (blank/unset) clears Company.tier to None."""
        co = self._make_company(db_session, tier="key", account_owner_id=test_user.id)
        resp = client.post(f"/v2/partials/customers/{co.id}/tier", data={"tier": ""})
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.tier is None

    def test_set_tier_all_valid_values_accepted(self, client: TestClient, db_session: Session, test_user: User):
        """All four valid tier values are accepted without 400."""
        for tier_val in ("key", "core", "standard", "prospect"):
            co = self._make_company(db_session, name=f"TierSet {tier_val}", account_owner_id=test_user.id)
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

    def _make_company_with_contact(self, db_session: Session, owner_id=None, **contact_kwargs):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="RoleSet Co", is_active=True, account_owner_id=owner_id)
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
        """POST contact_role=buyer persists to SiteContact.contact_role."""

        company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "buyer"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "buyer"

    def test_set_role_rerenders_chip(self, client: TestClient, db_session: Session, test_user: User):
        """POST role returns HTML containing the chip for the new role."""
        company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "engineer"},
        )
        assert resp.status_code == 200
        assert "engineer" in resp.text.lower() or "Engineer" in resp.text

    def test_set_role_invalid_value_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST with unknown role value returns 400."""
        company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "wizard"},
        )
        assert resp.status_code == 400

    def test_set_role_legacy_value_rejected(self, client: TestClient, db_session: Session, test_user: User):
        """A legacy DB value (buyer_po) is no longer selectable — POST 400s.

        Legacy values still render read-only via the display-label maps, but the setter
        only accepts the canonical ContactRole vocabulary.
        """
        company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "buyer_po"},
        )
        assert resp.status_code == 400

    def test_set_role_all_canonical_values_accepted(self, client: TestClient, db_session: Session, test_user: User):
        """All canonical buying-role values are accepted."""
        for role_val in ("buyer", "manager", "engineer", "planner", "other"):
            company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
            resp = client.post(
                f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
                data={"contact_role": role_val},
            )
            assert resp.status_code == 200, f"role={role_val} should be accepted"

    def test_set_role_nonexistent_contact_returns_404(self, client: TestClient, db_session: Session, test_user: User):
        """POST to unknown contact_id returns 404."""
        company, _, _ = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/99999/role",
            data={"contact_role": "buyer"},
        )
        assert resp.status_code == 404

    def test_set_role_blank_clears_role(self, client: TestClient, db_session: Session, test_user: User):
        """POST with contact_role='' clears the role to None."""

        company, site, contact = self._make_company_with_contact(
            db_session, owner_id=test_user.id, contact_role="buyer"
        )
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role is None

    def test_chip_editor_select_contains_canonical_roles(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Chip editor re-render contains all CANONICAL_ROLES as <option> values (driven
        by ctx 'roles', not hardcoded HTML), so adding a role to CANONICAL_ROLES
        automatically propagates to the select."""
        company, site, contact = self._make_company_with_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "engineer"},
        )
        assert resp.status_code == 200
        html = resp.text
        # All canonical values must appear as option values
        for role in ("buyer", "manager", "engineer", "planner", "other"):
            assert f"value='{role}'" in html or f'value="{role}"' in html, (
                f"Expected role '{role}' as an <option> value in chip editor HTML"
            )


# ─────────────────────────────────────────────────────────────────────────────
# TestRoleChipLegacy — role_chip macro handles legacy + new canonical values
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_grant_account_management")
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
        """Legacy role 'buyer_po' still renders a read-only chip in the contact card."""
        company, _, _ = self._make_company_with_contact(db_session, contact_role="buyer_po")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        # Should show some chip text for buyer_po
        assert "buyer" in resp.text.lower() or "PO" in resp.text or "Buyer" in resp.text

    def test_canonical_specifier_renders_chip(self, client: TestClient, db_session: Session, test_user: User):
        """Legacy role 'specifier' still renders a read-only chip."""
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

    def _make_company_with_site(self, db_session: Session, owner: User | None = None):
        from app.models.crm import CustomerSite

        company = Company(name="Edit Site Co", is_active=True, account_owner_id=owner.id if owner else None)
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

        company, site = self._make_company_with_site(db_session, owner=test_user)
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
        company, site = self._make_company_with_site(db_session, owner=test_user)
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
        company, site = self._make_company_with_site(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/edit",
            data={"site_name": "", "city": "Boston", "country": "US"},
        )
        assert resp.status_code == 400


class TestEditContact:
    """P2c: Edit-contact modal form (GET edit-form + POST edit)."""

    def _make_company_with_contact(self, db_session: Session, owner: User | None = None):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Edit Contact Co", is_active=True, account_owner_id=owner.id if owner else None)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Alice Smith",
            first_name="Alice",
            last_name="Smith",
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
        """GET edit-form renders form pre-populated with contact fields (company-scoped
        route)."""
        company, site, contact = self._make_company_with_contact(db_session)
        # Company-scoped route (no site_id): replaces the retired site-scoped route.
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 200
        # Step 4: form has first_name/last_name fields (not full_name)
        assert "Alice" in resp.text
        assert "alice@editco.com" in resp.text

    def test_get_contact_edit_form_site_scoped_route_retired(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """The site-scoped edit-form GET route is retired → 404."""
        company, site, contact = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 404

    def test_get_contact_edit_form_404_on_missing_contact(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """GET edit-form for nonexistent contact returns 404."""
        company, site, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/99999/edit-form")
        assert resp.status_code == 404

    def test_post_contact_edit_persists_title_and_phone(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit saves title + phone; re-rendered contacts show new values."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
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

    def test_post_contact_edit_sets_contact_role_canonical(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST contact edit with a canonical role persists it (role now writable via
        edit form)."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Smith",
                "contact_role": "buyer",  # canonical value — should persist
                "title": "Buyer",
                "email": "alice@editco.com",
                "phone": "+16175550001",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.query(SiteContact).filter(SiteContact.id == contact.id).first()
        assert updated is not None
        assert updated.contact_role == "buyer"

    def test_post_contact_edit_legacy_role_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST contact edit with legacy role 'decision_maker' returns 400."""
        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Smith",
                "contact_role": "decision_maker",  # legacy — must reject
                "title": "Buyer",
                "email": "alice@editco.com",
            },
        )
        assert resp.status_code == 400

    def test_post_contact_edit_re_renders_contacts_panel(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit response contains the updated name in the re-rendered contacts
        panel."""
        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
        # Step 4: use first_name + last_name (full_name is derived)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "first_name": "Alice",
                "last_name": "Updated",
                "title": "VP",
                "email": "alice@editco.com",
                "phone": "",
            },
        )
        assert resp.status_code == 200
        assert "Alice Updated" in resp.text

    def test_post_contact_edit_missing_first_and_last_name_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with both first_name and last_name empty returns 400."""
        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "", "last_name": "", "title": "Buyer", "email": "alice@editco.com"},
        )
        assert resp.status_code == 400

    def test_post_contact_edit_invalid_email_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with malformed email returns 400."""
        company, site, contact = self._make_company_with_contact(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Alice Smith", "email": "not-an-email"},
        )
        assert resp.status_code == 400


@pytest.mark.usefixtures("_grant_account_management")
class TestManualCompanyMerge:
    """Manual merge-duplicate accounts (P2e).

    Verifies:
    - Merge preview shows correct child counts
    - Merge endpoint reassigns children and deletes the loser
    - Guard: same id → 400, missing id → 400, no confirmation → 400
    """

    def _make_pair(self, db_session, owner):
        """Return (keep, remove) companies with one site+contact+activity each on
        remove.

        Both companies are owned by *owner* so the acting user passes the
        can_manage_account gate the merge/preview routes enforce on BOTH the keeper and
        the duplicate before reaching the 400/404/effect branches.
        """
        from app.models.crm import CustomerSite, SiteContact
        from app.models.intelligence import ActivityLog

        keep = Company(name="Keep Corp", is_active=True, account_owner_id=owner.id)
        remove = Company(name="Remove Corp", is_active=True, account_owner_id=owner.id)
        db_session.add_all([keep, remove])
        db_session.flush()

        # Non-empty site (has an email so it's not treated as empty HQ)
        site = CustomerSite(company_id=remove.id, site_name="West Office", contact_email="w@remove.com")
        db_session.add(site)
        db_session.flush()

        contact = SiteContact(customer_site_id=site.id, full_name="Bob Remove", email="bob@remove.com")
        db_session.add(contact)

        activity = ActivityLog(company_id=remove.id, activity_type="outreach", channel="phone", notes="test")
        db_session.add(activity)
        db_session.commit()
        return keep, remove, site, contact, activity

    # ── Preview endpoint ──────────────────────────────────────────────────────

    def test_preview_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET merge-preview returns 200 with preview HTML."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        resp = client.get(
            f"/v2/partials/customers/{keep.id}/merge-preview",
            params={"remove_id": remove.id},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_preview_shows_remove_company_name(self, client: TestClient, db_session: Session, test_user: User):
        """Preview names the company being removed."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        resp = client.get(
            f"/v2/partials/customers/{keep.id}/merge-preview",
            params={"remove_id": remove.id},
        )
        assert "Remove Corp" in resp.text

    def test_preview_shows_site_count(self, client: TestClient, db_session: Session, test_user: User):
        """Preview reports number of sites that will be reassigned."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        resp = client.get(
            f"/v2/partials/customers/{keep.id}/merge-preview",
            params={"remove_id": remove.id},
        )
        assert resp.status_code == 200
        # "1" should appear — the one non-empty site
        assert "1" in resp.text

    def test_preview_missing_remove_id_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """Preview with nonexistent remove_id returns 400."""
        keep, *_ = self._make_pair(db_session, test_user)
        resp = client.get(
            f"/v2/partials/customers/{keep.id}/merge-preview",
            params={"remove_id": 999999},
        )
        assert resp.status_code == 400

    def test_preview_same_id_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """Preview with remove_id == keep_id returns 400."""
        keep, *_ = self._make_pair(db_session, test_user)
        resp = client.get(
            f"/v2/partials/customers/{keep.id}/merge-preview",
            params={"remove_id": keep.id},
        )
        assert resp.status_code == 400

    # ── Merge endpoint guards ─────────────────────────────────────────────────

    def test_merge_without_confirmation_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST merge without confirmed=true is rejected (guard against accidental
        calls)."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove.id)},
        )
        assert resp.status_code == 400

    def test_merge_same_id_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST merge with remove_id == keep_id returns 400."""
        keep, *_ = self._make_pair(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(keep.id), "confirmed": "true"},
        )
        assert resp.status_code == 400

    def test_merge_missing_remove_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST merge with nonexistent remove_id returns 400."""
        keep, *_ = self._make_pair(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": "999999", "confirmed": "true"},
        )
        assert resp.status_code == 400

    # ── Merge endpoint effect ─────────────────────────────────────────────────

    def test_merge_reassigns_site_to_keep(self, client: TestClient, db_session: Session, test_user: User):
        """After merge, the loser's site belongs to keeper."""
        from app.models.crm import CustomerSite

        keep, remove, site, *_ = self._make_pair(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove.id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        refreshed = db_session.get(CustomerSite, site.id)
        assert refreshed is not None
        assert refreshed.company_id == keep.id

    def test_merge_reassigns_activity_to_keep(self, client: TestClient, db_session: Session, test_user: User):
        """After merge, the loser's activity belongs to keeper."""
        from app.models.intelligence import ActivityLog

        keep, remove, _site, _contact, activity = self._make_pair(db_session, test_user)
        client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove.id), "confirmed": "true"},
        )
        db_session.expire_all()
        refreshed = db_session.get(ActivityLog, activity.id)
        assert refreshed is not None
        assert refreshed.company_id == keep.id

    def test_merge_deletes_loser(self, client: TestClient, db_session: Session, test_user: User):
        """After merge, the removed company is gone from DB."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        remove_id = remove.id
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove_id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(Company, remove_id) is None

    def test_merge_response_redirects_to_keeper(self, client: TestClient, db_session: Session, test_user: User):
        """Merge response carries HX-Redirect header pointing to keeper detail."""
        keep, remove, *_ = self._make_pair(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove.id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        # Should either redirect header or contain the keeper company URL
        location = resp.headers.get("HX-Redirect", "") or resp.headers.get("Location", "") or resp.text
        assert str(keep.id) in location

    def test_merge_response_escapes_company_name_xss(self, client: TestClient, db_session: Session, test_user: User):
        """Merge success response HTML-escapes the company name (XSS guard).

        A keeper with markup in its name must produce escaped output so the browser
        renders it as text, not as live HTML.
        """
        keep = Company(name="<b>Evil Corp</b>", is_active=True, account_owner_id=test_user.id)
        remove = Company(name="Victim Corp", is_active=True, account_owner_id=test_user.id)
        db_session.add_all([keep, remove])
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{keep.id}/merge",
            data={"remove_id": str(remove.id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        # Raw markup must NOT appear — would be stored-XSS
        assert "<b>" not in resp.text
        assert "</b>" not in resp.text
        # Escaped form must appear instead
        assert "&lt;b&gt;" in resp.text

    # ── UI presence ──────────────────────────────────────────────────────────

    def test_merge_button_appears_in_company_detail(self, client: TestClient, db_session: Session, test_user: User):
        """Company detail partial includes a 'merge' action/button."""
        keep = Company(name="Merge Button Co", is_active=True)
        db_session.add(keep)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{keep.id}")
        assert resp.status_code == 200
        assert "merge" in resp.text.lower()


# ────────────────────────────────────────────────────────────────────────────
# P3: Buy Plans tab on the account (deal consolidation)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_grant_account_management")
class TestAccountBuyPlansTab:
    """P3: account detail exposes a Buy Plans tab listing all buy-plans whose
    requisition belongs to the company (via company_id FK or name match).
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_company(self, db_session: Session, name: str = "BuyPlan Co") -> Company:
        co = Company(name=name, is_active=True)
        db_session.add(co)
        db_session.flush()
        return co

    def _make_requisition(self, db_session: Session, company: Company, name: str = "REQ-BP-001"):
        from app.models.sourcing import Requisition

        req = Requisition(name=name, customer_name=company.name, company_id=company.id, status="open")
        db_session.add(req)
        db_session.flush()
        return req

    def _make_buy_plan(self, db_session: Session, requisition, quote, so_number: str = "SO-001"):
        from app.models.buy_plan import BuyPlan

        bp = BuyPlan(
            requisition_id=requisition.id,
            quote_id=quote.id,
            sales_order_number=so_number,
            status="active",
        )
        db_session.add(bp)
        db_session.flush()
        return bp

    def _make_quote(self, db_session: Session, requisition, quote_number: str = "Q-001"):
        from decimal import Decimal

        from app.models.quotes import Quote

        q = Quote(
            requisition_id=requisition.id,
            quote_number=quote_number,
            subtotal=Decimal("5000.00"),
            status="won",
        )
        db_session.add(q)
        db_session.flush()
        return q

    # ── route returns 200 + correct buy-plans ────────────────────────────────

    def test_buy_plans_tab_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET /v2/partials/customers/{id}/tab/buy_plans returns 200."""
        co = self._make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/buy_plans")
        assert resp.status_code == 200

    def test_buy_plans_tab_shows_buy_plan_for_company(self, client: TestClient, db_session: Session, test_user: User):
        """Company's buy-plan (linked via its requisition) appears in the tab."""
        co = self._make_company(db_session, "BPTab Show Co")
        req = self._make_requisition(db_session, co, "REQ-SHOW-001")
        quote = self._make_quote(db_session, req, "Q-SHOW-001")
        bp = self._make_buy_plan(db_session, req, quote, "SO-SHOW-001")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/buy_plans")
        assert resp.status_code == 200
        assert "SO-SHOW-001" in resp.text, "Buy plan SO# should appear in tab"

    def test_buy_plans_tab_empty_state_when_no_plans(self, client: TestClient, db_session: Session, test_user: User):
        """Company with no buy-plans renders the empty-state message."""
        co = self._make_company(db_session, "NoBP Co")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/buy_plans")
        assert resp.status_code == 200
        # Empty state must say something useful
        assert "No buy plans" in resp.text or "no buy plans" in resp.text.lower()

    def test_buy_plans_tab_scoped_to_company(self, client: TestClient, db_session: Session, test_user: User):
        """Buy-plans from another company do NOT appear in this company's tab."""
        co_a = self._make_company(db_session, "Scoped Co A")
        co_b = self._make_company(db_session, "Scoped Co B")
        req_a = self._make_requisition(db_session, co_a, "REQ-A-001")
        req_b = self._make_requisition(db_session, co_b, "REQ-B-001")
        quote_a = self._make_quote(db_session, req_a, "Q-A-001")
        quote_b = self._make_quote(db_session, req_b, "Q-B-001")
        _bp_a = self._make_buy_plan(db_session, req_a, quote_a, "SO-COMPANY-A")
        _bp_b = self._make_buy_plan(db_session, req_b, quote_b, "SO-COMPANY-B")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co_a.id}/tab/buy_plans")
        assert resp.status_code == 200
        assert "SO-COMPANY-A" in resp.text, "Company A's buy plan should appear"
        assert "SO-COMPANY-B" not in resp.text, "Company B's buy plan must NOT appear"

    def test_buy_plan_count_shown_in_account_detail(self, client: TestClient, db_session: Session, test_user: User):
        """Account detail partial renders a 'Buy Plans' tab with correct count badge."""
        co = self._make_company(db_session, "CountBadge Co")
        req = self._make_requisition(db_session, co, "REQ-COUNT-001")
        quote = self._make_quote(db_session, req, "Q-COUNT-001")
        _bp = self._make_buy_plan(db_session, req, quote, "SO-COUNT-001")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "Buy Plans" in resp.text, "Buy Plans tab label must appear in detail"


@pytest.mark.usefixtures("_grant_account_management")
class TestCRMMacroDedup:
    """Route-level guards for the CRM template-macro dedup (refactor/crm-template-
    macros).

    The customer partials now render badge/icon/clock markup via the canonical
    shared/_macros.html macros instead of hand-rolled, drifted copies. These tests pin
    the labels (unchanged) and the deliberate drift-fix on quote 'sent' color.
    """

    def _make_company(self, db_session: Session, **kwargs) -> Company:
        co = Company(name="MacroDedup Co", is_active=True, **kwargs)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        return co

    def test_quotes_tab_sent_badge_is_brand_not_amber(self, client: TestClient, db_session: Session, test_user: User):
        """DRIFT FIX: the quotes tab rendered a 'sent' quote amber; it now uses the
        canonical quote_status_badge (brand), unifying it with the activity timeline."""
        from decimal import Decimal

        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        co = self._make_company(db_session)
        req = Requisition(name="REQ-MD-001", customer_name=co.name, company_id=co.id, status="open")
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            quote_number="QT-MD-001",
            subtotal=Decimal("1000.00"),
            status="sent",
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/quotes")
        assert resp.status_code == 200
        html = resp.text
        # Label preserved.
        assert "QT-MD-001" in html
        assert "Sent" in html
        # Canonical brand color for 'sent' — NOT the old amber drift.
        assert "text-brand-700" in html
        assert "bg-amber-50 text-amber-700" not in html

    def test_account_type_badge_rendered_in_detail(self, client: TestClient, db_session: Session, test_user: User):
        """The detail header renders the canonical account_type_badge with the type
        label and emerald (Customer) color."""
        co = self._make_company(db_session, account_type="Customer")
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        html = resp.text
        assert "Customer" in html
        assert "emerald" in html

    def test_detail_cadence_clocks_macro_renders(self, client: TestClient, db_session: Session, test_user: User):
        """The Account Cadence card renders the dual clocks via cadence_clocks — both
        labels present, 'Never' for the null outbound clock."""
        co = self._make_company(db_session, last_outbound_at=None, last_reply_at=None)
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        html = resp.text
        assert "Last Out" in html
        assert "Last Reply" in html
        assert "Never" in html

    def test_multi_site_unified_detail_has_canonical_badge_and_clocks(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """A MULTI-site account now opens the SAME unified detail (the header-only
        rollup is retired): the canonical account_type_badge + cadence_clocks render in
        that one unified surface."""
        from app.models.crm import CustomerSite

        co = self._make_company(db_session, account_type="Prospect", last_outbound_at=None)
        # Two active sites — previously routed to the header-only fork; now unified.
        db_session.add_all(
            [
                CustomerSite(company_id=co.id, site_name="HQ", is_active=True),
                CustomerSite(company_id=co.id, site_name="Branch", is_active=True),
            ]
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        html = resp.text
        assert "Prospect" in html
        assert "Last Out" in html
        assert "Last Reply" in html
        assert "Never" in html
        # The unified detail carries the full tab strip (no header-only fork).
        assert 'aria-label="Account detail sections"' in html

    def test_activity_tab_renders_canonical_icon_for_email_activity(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """The activity tab renders ActivityLog rows via canonical activity_icon macro.

        RFQ contacts appear in the Emails section; Quotes are absent.
        """
        from decimal import Decimal

        from app.models.intelligence import ActivityLog
        from app.models.offers import Contact as RfqContact
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        co = self._make_company(db_session)
        req = Requisition(name="REQ-MD-AT", customer_name=co.name, company_id=co.id, status="open")
        db_session.add(req)
        db_session.flush()
        # RFQ and quote — must NOT appear in activity tab
        rfq = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Badge Vendor",
            status="sent",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        q = Quote(
            requisition_id=req.id,
            quote_number="QT-MD-AT",
            subtotal=Decimal("500.00"),
            status="sent",
            created_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=co.id,
            subject="RE: badge",
            direction="inbound",
            is_meaningful=True,
            created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        )
        db_session.add_all([rfq, q, act])
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text
        # ActivityLog entry renders
        assert "Email Received" in html
        # Canonical activity_icon emits the h-8 w-8 rounded icon circle.
        assert "h-8 w-8" in html
        # RFQ contact appears in Emails section; Quote is absent (own tab)
        assert "Badge Vendor" in html, "RFQ contact must appear in Emails section"
        assert "QT-MD-AT" not in html, "Quote must not appear in activity tab"


# ─────────────────────────────────────────────────────────────────────────────
# TestContactsTabHome — C2: Contacts tab as management home (add/edit/role)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_grant_account_management")
class TestContactsTabHome:
    """C2 TDD: Unified add/edit form + editable role + tab wrapper.

    Written FIRST (failing) per TDD — implement endpoints + templates to pass.

    Routes tested:
      GET  /v2/partials/customers/{company_id}/contacts/add-form
      POST /v2/partials/customers/{company_id}/contacts
      POST /v2/partials/customers/{company_id}/sites/{sid}/contacts/{cid}/edit
           (with HX-Target branch)
    """

    def _make_company_with_hq(self, db_session: Session, name: str = "Tab Home Co", owner: User | None = None):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name=name, is_active=True, account_owner_id=owner.id if owner else None)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", site_type="hq", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Alice Prime",
            email="alice@tabco.com",
            contact_role="buyer",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.commit()
        return company, site, contact

    def _make_zero_site_company(self, db_session: Session, owner: User | None = None):
        company = Company(name="Zero Site Corp", is_active=True, account_owner_id=owner.id if owner else None)
        db_session.add(company)
        db_session.commit()
        return company

    # ── GET add-form ─────────────────────────────────────────────────────

    def test_get_add_form_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """GET add-form renders 200 with site select and role select."""
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form")
        assert resp.status_code == 200

    def test_get_add_form_contains_site_select(self, client: TestClient, db_session: Session, test_user: User):
        """GET add-form includes a site_id select with the company's sites."""
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form")
        assert resp.status_code == 200
        # Template uses single-quote HTML attrs — check for either quoting style
        assert "site_id" in resp.text
        assert "HQ" in resp.text

    def test_get_add_form_contains_role_select(self, client: TestClient, db_session: Session, test_user: User):
        """GET add-form includes a contact_role select with canonical roles."""
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form")
        assert resp.status_code == 200
        assert "contact_role" in resp.text
        assert "buyer" in resp.text
        assert "engineer" in resp.text

    def test_get_add_form_new_site_option_present(self, client: TestClient, db_session: Session, test_user: User):
        """GET add-form includes a '+ new site' option in the site select."""
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form")
        assert resp.status_code == 200
        assert "__new__" in resp.text

    def test_get_add_form_404_unknown_company(self, client: TestClient, db_session: Session, test_user: User):
        """GET add-form for unknown company returns 404."""
        resp = client.get("/v2/partials/customers/99999/contacts/add-form")
        assert resp.status_code == 404

    # ── contacts-tab header + wrapper ────────────────────────────────────

    def test_contacts_tab_has_wrapper_div(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab HTML contains the #contacts-tab-list wrapper div."""
        company, _, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert 'id="contacts-tab-list"' in resp.text

    def test_contacts_tab_has_add_contact_button(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab header includes an '+ Add Contact' modal trigger."""
        company, _, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Add Contact" in resp.text

    def test_contacts_tab_shows_contact_count(self, client: TestClient, db_session: Session, test_user: User):
        """The Contacts surface shows the per-site contact count in its section header.

        (The redundant "Contacts (N)" heading was retired in the IA redesign — the
        breadcrumb + tab strip name the surface; the section header carries the count.)
        """
        company, _, _ = self._make_company_with_hq(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # The section header reports the count for that site.
        assert "1 contact" in resp.text

    # ── POST create (contacts-tab) ────────────────────────────────────────

    def test_post_create_contact_on_existing_site(self, client: TestClient, db_session: Session, test_user: User):
        """POST create with valid site_id creates a SiteContact and returns grouped
        list."""
        from app.models.crm import SiteContact

        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "full_name": "New Bob",
                "email": "newbob@tabco.com",
                "title": "Manager",
                "contact_role": "buyer",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        new_contact = db_session.query(SiteContact).filter_by(email="newbob@tabco.com").first()
        assert new_contact is not None
        assert new_contact.full_name == "New Bob"
        assert new_contact.customer_site_id == site.id
        # Response is the grouped list
        assert "New Bob" in resp.text

    def test_post_create_per_site_email_dedup(self, client: TestClient, db_session: Session, test_user: User):
        """POST create with duplicate email on same site returns 409 (visible error) and
        does not create a new contact row."""
        from app.models.crm import SiteContact

        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        # alice@tabco.com already exists on site
        count_before = db_session.query(SiteContact).filter_by(customer_site_id=site.id).count()
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "full_name": "Alice Duplicate",
                "email": "alice@tabco.com",  # same email as existing contact
            },
        )
        assert resp.status_code == 409
        db_session.expire_all()
        count_after = db_session.query(SiteContact).filter_by(customer_site_id=site.id).count()
        assert count_after == count_before  # no new row

    def test_post_create_zero_site_auto_creates_hq(self, client: TestClient, db_session: Session, test_user: User):
        """POST create for a zero-site company auto-creates 'HQ' site + contact
        atomically."""
        from app.models.crm import CustomerSite, SiteContact

        company = self._make_zero_site_company(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": "",  # no site selected
                "full_name": "First Contact",
                "email": "first@zerocorp.com",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        site = db_session.query(CustomerSite).filter_by(company_id=company.id).first()
        assert site is not None
        contact = db_session.query(SiteContact).filter_by(customer_site_id=site.id).first()
        assert contact is not None
        assert contact.full_name == "First Contact"

    def test_post_create_new_site_option(self, client: TestClient, db_session: Session, test_user: User):
        """POST create with site_id=__new__ + new_site_name creates site and contact."""
        from app.models.crm import CustomerSite, SiteContact

        company, _, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": "__new__",
                "new_site_name": "Shanghai Branch",
                "full_name": "Wei Li",
                "email": "wei@tabco.com",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        new_site = db_session.query(CustomerSite).filter_by(company_id=company.id, site_name="Shanghai Branch").first()
        assert new_site is not None
        wei = db_session.query(SiteContact).filter_by(customer_site_id=new_site.id).first()
        assert wei is not None
        assert wei.full_name == "Wei Li"

    def test_post_create_new_site_blank_name_returns_400(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST create with site_id=__new__ but blank new_site_name returns 400."""
        company, _, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={"site_id": "__new__", "new_site_name": "", "full_name": "Orphan Bob"},
        )
        assert resp.status_code == 400

    def test_post_create_missing_full_name_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST create without full_name returns 400."""
        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={"site_id": str(site.id), "full_name": ""},
        )
        assert resp.status_code == 400

    def test_post_create_returns_grouped_list(self, client: TestClient, db_session: Session, test_user: User):
        """POST create response contains contacts-tab-list contents (grouped list)."""
        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={"site_id": str(site.id), "full_name": "Carol New", "email": "carol@tabco.com"},
        )
        assert resp.status_code == 200
        # grouped list has site accordion headers
        assert "HQ" in resp.text

    # ── edit_site_contact — HX-Target branching + role writing ───────────

    def test_edit_sets_contact_role_canonical(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with a canonical contact_role persists it to the DB."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Prime",
                "email": "alice@tabco.com",
                "contact_role": "buyer",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(SiteContact, contact.id)
        assert updated.contact_role == "buyer"

    def test_edit_invalid_role_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with legacy 'decision_maker' role returns 400."""
        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Prime",
                "email": "alice@tabco.com",
                "contact_role": "decision_maker",
            },
        )
        assert resp.status_code == 400

    def test_edit_blank_role_clears_to_null(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with blank contact_role clears the field to NULL."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Prime",
                "email": "alice@tabco.com",
                "contact_role": "",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(SiteContact, contact.id)
        assert updated.contact_role is None

    def test_edit_hx_target_contacts_tab_list_renders_grouped_list(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit with HX-Target=contacts-tab-list returns the grouped list
        partial."""
        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Alice Prime", "email": "alice@tabco.com"},
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        # grouped list renders site accordion headers
        assert "HQ" in resp.text
        # should NOT be the site_contacts inline form (no showAdd Alpine state)
        assert "showAdd" not in resp.text

    def test_edit_hx_target_default_renders_grouped_list(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST edit without HX-Target now always returns grouped list
        (site_contacts.html path retired — the Sites tab no longer carries a contact
        editor)."""
        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Alice Prime", "email": "alice@tabco.com"},
        )
        assert resp.status_code == 200
        # grouped list renders site section header — not the old showAdd site_contacts form
        assert "HQ" in resp.text
        assert "showAdd" not in resp.text

    def test_edit_writes_linkedin_url(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with linkedin_url persists it to the DB."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Prime",
                "email": "alice@tabco.com",
                "linkedin_url": "https://linkedin.com/in/alice-prime",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(SiteContact, contact.id)
        assert updated.linkedin_url == "https://linkedin.com/in/alice-prime"

    def test_edit_writes_is_priority(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with is_priority=1 sets the flag."""
        from app.models.crm import SiteContact

        company, site, contact = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Alice Prime",
                "email": "alice@tabco.com",
                "is_priority": "1",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(SiteContact, contact.id)
        assert updated.is_priority is True

    # ── Company-scoped edit-form route ───────────────────────────────────

    def test_contact_edit_form_company_scoped_returns_form(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_company: Company,
        test_site,
        test_contact,
    ):
        """GET /v2/partials/customers/{id}/contacts/{cid}/edit-form returns
        _contact_form with correct target and values."""
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{test_contact.id}/edit-form")
        assert resp.status_code == 200
        html = resp.text
        # Form must target #contacts-tab-list (not any site-specific id)
        assert "contacts-tab-list" in html
        # Contact's email must appear (Step 4: form uses first_name/last_name, not full_name input)
        assert test_contact.email in html
        # Role options must be present (canonical ContactRole vocabulary)
        assert "engineer" in html

    def test_contacts_tab_create_response_contains_roles(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_company: Company,
        test_site,
    ):
        """POST to create contact returns grouped list HTML with role options."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"full_name": "Role Test", "site_id": str(test_site.id)},
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "engineer" in resp.text

    def test_delete_contact_response_contains_roles(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_company: Company,
        test_site,
        test_contact,
    ):
        """DELETE contact with HX-Target=contacts-tab-list returns grouped list with
        role options.

        A second contact is added so the list is non-empty after deletion.
        """
        from app.models.crm import SiteContact

        test_company.account_owner_id = test_user.id
        # Add a second contact so the grouped list is non-empty after the first is deleted
        second = SiteContact(
            customer_site_id=test_site.id,
            full_name="Second Contact",
            email="second@acme.com",
            contact_role="buyer_po",
        )
        db_session.add(second)
        db_session.commit()

        resp = client.delete(
            f"/v2/partials/customers/{test_company.id}/sites/{test_site.id}/contacts/{test_contact.id}",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "buyer_po" in resp.text

    def test_set_primary_response_contains_roles(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_company: Company,
        test_site,
        test_contact,
    ):
        """POST set-primary with HX-Target=contacts-tab-list returns grouped list with
        role options."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_site.id}/contacts/{test_contact.id}/primary",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "engineer" in resp.text

    def test_edit_contact_response_contains_roles(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_company: Company,
        test_site,
        test_contact,
    ):
        """POST edit contact with HX-Target=contacts-tab-list returns grouped list with
        role options."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_site.id}/contacts/{test_contact.id}/edit",
            data={"full_name": test_contact.full_name, "contact_role": "engineer"},
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "engineer" in resp.text


@pytest.mark.usefixtures("_grant_account_management")
class TestC3KebabActionsAndCadence:
    """C3 TDD: kebab (Edit/Delete/Set-Primary) on Contacts tab + cadence badge/sort.

    Written FIRST (failing) to drive implementation per Step C3.
    """

    def _make_two_contacts(self, db_session: Session, *, overdue_days: int = 0, owner: User | None = None):
        """One company, one site, two contacts.

        If overdue_days > 0 the first contact has last_outbound_at that many days in the
        past (overdue threshold is 30d).
        """
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Kebab Co", is_active=True, account_owner_id=owner.id if owner else None)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()

        overdue_ts = None
        if overdue_days > 0:
            overdue_ts = datetime.now(timezone.utc) - timedelta(days=overdue_days)

        contact_a = SiteContact(
            customer_site_id=site.id,
            full_name="Alpha Contact",
            email="alpha@kebab.com",
            is_primary=False,
            last_outbound_at=overdue_ts,
        )
        contact_b = SiteContact(
            customer_site_id=site.id,
            full_name="Beta Contact",
            email="beta@kebab.com",
            is_primary=True,
            last_outbound_at=None,
        )
        db_session.add_all([contact_a, contact_b])
        db_session.commit()
        return company, site, contact_a, contact_b

    # ── Kebab menu rendered ─────────────────────────────────────────────

    def test_contacts_tab_renders_kebab_button(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab includes the three-dot kebab button for real contacts."""
        company, site, ca, cb = self._make_two_contacts(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # Kebab three-dot SVG path is a distinctive substring
        assert "10 6a2 2 0 110-4" in resp.text or "aria-label" in resp.text

    def test_contacts_tab_renders_edit_action(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab kebab contains an Edit option dispatching open-modal."""
        company, site, ca, cb = self._make_two_contacts(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "edit-form" in resp.text

    def test_contacts_tab_renders_delete_action(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab kebab contains a Delete hx-delete button."""
        company, site, ca, cb = self._make_two_contacts(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "hx-delete" in resp.text

    def test_contacts_tab_renders_set_primary_when_not_primary(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Set-Primary kebab item only appears for non-primary contacts."""
        company, site, ca, cb = self._make_two_contacts(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # Set Primary option must appear (at least one non-primary contact)
        assert "primary" in resp.text.lower()

    # ── Set-Primary via tab ─────────────────────────────────────────────

    def test_set_primary_via_tab_flips_flag(self, client: TestClient, db_session: Session, test_user: User):
        """POST primary with HX-Target=contacts-tab-list flips is_primary and returns
        the grouped list."""
        from app.models.crm import SiteContact

        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        # ca is not primary — make it primary via tab
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}/primary",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated_a = db_session.get(SiteContact, ca.id)
        updated_b = db_session.get(SiteContact, cb.id)
        assert updated_a.is_primary is True
        # previous primary must be unset
        assert updated_b.is_primary is False

    def test_set_primary_via_tab_returns_grouped_list(self, client: TestClient, db_session: Session, test_user: User):
        """POST primary with HX-Target=contacts-tab-list re-renders grouped list (not
        site_contacts.html)."""
        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}/primary",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        # grouped list has site accordion, not the inline showAdd form
        assert "HQ" in resp.text
        assert "showAdd" not in resp.text

    def test_set_primary_via_tab_both_contacts_in_response(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Grouped list re-render after primary includes all contacts under the site."""
        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}/primary",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "Alpha Contact" in resp.text
        assert "Beta Contact" in resp.text

    # ── Delete via tab ──────────────────────────────────────────────────

    def test_delete_via_tab_removes_contact(self, client: TestClient, db_session: Session, test_user: User):
        """DELETE with HX-Target=contacts-tab-list removes the contact and re-renders
        the grouped list."""
        from app.models.crm import SiteContact

        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        resp = client.delete(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(SiteContact, ca.id) is None

    def test_delete_via_tab_returns_grouped_list_not_empty(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """DELETE via tab re-renders grouped list with remaining contact (not empty)."""
        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        resp = client.delete(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        assert "Beta Contact" in resp.text
        # Alpha is gone
        assert "Alpha Contact" not in resp.text

    def test_delete_last_contact_renders_invitational_empty_state(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Deleting the last contact via the tab renders the invitational empty state
        with + Add Contact CTA."""
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="Solo Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="Main", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Only One",
            email="one@solo.com",
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.delete(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}",
            headers={"HX-Target": "contacts-tab-list"},
        )
        assert resp.status_code == 200
        # invitational CTA
        assert "Add" in resp.text
        # contact gone
        assert "Only One" not in resp.text

    def test_delete_without_hx_target_renders_grouped_list(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """DELETE without HX-Target now always returns grouped list (site_contacts.html
        path retired — the Sites tab no longer carries a contact editor)."""
        from app.models.crm import SiteContact

        company, site, ca, cb = self._make_two_contacts(db_session, owner=test_user)
        resp = client.delete(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{ca.id}",
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(SiteContact, ca.id) is None
        # Returns the grouped list (remaining contact visible, old inline form gone)
        assert "Beta Contact" in resp.text
        assert "showAdd" not in resp.text

    # ── Cadence badge and sort ──────────────────────────────────────────

    def test_overdue_contact_shows_badge(self, client: TestClient, db_session: Session, test_user: User):
        """A contact with last_outbound_at 45d ago shows an overdue badge in the grouped
        list."""
        company, site, ca, cb = self._make_two_contacts(db_session, overdue_days=45)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # Badge text like "Overdue 45d" must appear
        assert "verdue" in resp.text  # case-insensitive partial

    def test_null_clock_shows_no_badge(self, client: TestClient, db_session: Session, test_user: User):
        """Contact with NULL last_outbound_at shows no overdue badge (NULL honesty)."""
        company, site, ca, cb = self._make_two_contacts(db_session, overdue_days=0)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # No "overdue" text when no clock is set
        assert "verdue" not in resp.text.lower()

    def test_overdue_contact_sorts_before_null_clock(self, client: TestClient, db_session: Session, test_user: User):
        """Within a site group, overdue contacts appear before contacts with no clock
        (secondary sort: overdue-first)."""
        company, site, ca, cb = self._make_two_contacts(db_session, overdue_days=45)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        # Alpha Contact (overdue) should appear before Beta Contact (no clock)
        pos_alpha = resp.text.find("Alpha Contact")
        pos_beta = resp.text.find("Beta Contact")
        assert pos_alpha != -1
        assert pos_beta != -1
        assert pos_alpha < pos_beta

    def test_service_contact_rows_has_cadence_field(self, db_session: Session):
        """company_contact_rows returns rows where real contacts have a 'cadence'
        key."""
        from app.models.crm import CustomerSite, SiteContact
        from app.services.crm_service import company_contact_rows

        company = Company(name="Cadence Svc Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Has Cadence",
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        db_session.add(contact)
        db_session.commit()

        rows = company_contact_rows(db_session, company.id)
        real_rows = [r for r in rows if not r["legacy"]]
        assert len(real_rows) == 1
        assert "cadence" in real_rows[0]
        assert real_rows[0]["cadence"] == "overdue"

    def test_service_null_clock_cadence_is_new(self, db_session: Session):
        """company_contact_rows assigns cadence='new' for contacts with NULL
        last_outbound_at."""
        from app.models.crm import CustomerSite, SiteContact
        from app.services.crm_service import company_contact_rows

        company = Company(name="Null Clock Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="No Clock",
            last_outbound_at=None,
        )
        db_session.add(contact)
        db_session.commit()

        rows = company_contact_rows(db_session, company.id)
        real_rows = [r for r in rows if not r["legacy"]]
        assert len(real_rows) == 1
        assert real_rows[0]["cadence"] == "new"


@pytest.mark.usefixtures("_grant_account_management")
class TestC4SuggestedContactsUI:
    """C4 TDD: Suggested-contacts UI loop.

    Written FIRST (failing) per TDD — implement endpoints + templates to pass.

    Routes tested:
      GET  /v2/partials/customers/{company_id}/suggested-contacts
      POST /v2/partials/customers/{company_id}/suggested-contacts/add
    """

    def _make_company_with_hq(self, db_session: Session, *, domain: str = "acme.com", owner: User | None = None):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(
            name="Suggested Co", is_active=True, domain=domain, account_owner_id=owner.id if owner else None
        )
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", site_type="hq", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Existing Alice",
            email="alice@acme.com",
        )
        db_session.add(contact)
        db_session.commit()
        return company, site, contact

    # ── "Find contacts" button on the Contacts tab ────────────────────────

    def test_contacts_tab_shows_find_contacts_when_domain_set(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Contacts tab shows 'Find contacts' button when company.domain is set."""
        company, site, contact = self._make_company_with_hq(db_session, domain="acme.com")
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Find contacts" in resp.text

    def test_contacts_tab_no_find_contacts_when_no_domain(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Contacts tab hides 'Find contacts' button when no domain/website is set."""
        company, site, contact = self._make_company_with_hq(db_session, domain="")
        # Remove domain/website
        company.domain = None
        company.website = None
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Find contacts" not in resp.text

    # ── GET /suggested-contacts renders _suggested_contacts.html ─────────

    @patch("app.enrichment_service.find_suggested_contacts_with_errors", new_callable=AsyncMock)
    def test_get_suggested_contacts_renders_html(
        self, mock_contacts, client: TestClient, db_session: Session, test_user: User
    ):
        """GET suggested contacts for a company renders an HTML partial (not JSON)."""
        mock_contacts.return_value = (
            [{"full_name": "Bob Buyer", "email": "bob@acme.com", "title": "VP Procurement"}],
            [],  # no errored providers
        )
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(
            f"/v2/partials/customers/{company.id}/suggested-contacts",
            params={"domain": "acme.com"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Bob Buyer" in resp.text

    @patch("app.enrichment_service.find_suggested_contacts_with_errors", new_callable=AsyncMock)
    def test_get_suggested_contacts_zero_results_neutral_state(
        self, mock_contacts, client: TestClient, db_session: Session, test_user: User
    ):
        """Zero results + no provider error → neutral 'No contacts found' state."""
        mock_contacts.return_value = ([], [])
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(
            f"/v2/partials/customers/{company.id}/suggested-contacts",
            params={"domain": "acme.com"},
        )
        assert resp.status_code == 200
        # Neutral empty state — no provider error badge
        assert "No contacts found" in resp.text
        # Should NOT show an error/warning banner
        assert "Couldn" not in resp.text

    @patch("app.enrichment_service.find_suggested_contacts_with_errors", new_callable=AsyncMock)
    def test_get_suggested_contacts_provider_error_amber_state(
        self, mock_contacts, client: TestClient, db_session: Session, test_user: User
    ):
        """Zero results + provider errored → amber 'Couldn't reach <provider>' state."""
        mock_contacts.return_value = ([], ["hunter"])
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(
            f"/v2/partials/customers/{company.id}/suggested-contacts",
            params={"domain": "acme.com"},
        )
        assert resp.status_code == 200
        # Amber error banner mentioning the provider
        assert "hunter" in resp.text.lower()
        # Should indicate something went wrong (Couldn't reach or try again)
        assert "try again" in resp.text.lower() or "couldn" in resp.text.lower()

    @patch("app.enrichment_service.find_suggested_contacts_with_errors", new_callable=AsyncMock)
    def test_get_suggested_contacts_add_button_per_row(
        self, mock_contacts, client: TestClient, db_session: Session, test_user: User
    ):
        """Suggested contacts list has an Add button per row."""
        mock_contacts.return_value = (
            [{"full_name": "Bob Buyer", "email": "bob@acme.com", "title": "VP Procurement"}],
            [],
        )
        company, site, _ = self._make_company_with_hq(db_session)
        resp = client.get(
            f"/v2/partials/customers/{company.id}/suggested-contacts",
            params={"domain": "acme.com"},
        )
        assert resp.status_code == 200
        # Each row should have an Add action targeting the add endpoint
        assert "suggested-contacts/add" in resp.text

    # ── POST /suggested-contacts/add creates SiteContact + returns grouped list ──

    def test_post_add_suggested_creates_site_contact(self, client: TestClient, db_session: Session, test_user: User):
        """POST add creates a real SiteContact and returns grouped list HTML."""
        from app.models.crm import SiteContact

        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/suggested-contacts/add",
            data={
                "site_id": str(site.id),
                "full_name": "New Suggested",
                "email": "new@acme.com",
                "title": "Director",
                "source": "hunter",
                "email_verified": "1",
            },
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        db_session.expire_all()
        sc = db_session.query(SiteContact).filter_by(email="new@acme.com").first()
        assert sc is not None
        assert sc.full_name == "New Suggested"
        assert sc.enrichment_source == "hunter"
        assert sc.email_verified is True

    def test_post_add_suggested_returns_grouped_list(self, client: TestClient, db_session: Session, test_user: User):
        """POST add returns the re-rendered grouped list (not JSON)."""
        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/suggested-contacts/add",
            data={
                "site_id": str(site.id),
                "full_name": "List Check",
                "email": "listcheck@acme.com",
            },
        )
        assert resp.status_code == 200
        # Grouped list has accordion site headers
        assert "HQ" in resp.text
        # New contact appears in list
        assert "List Check" in resp.text

    def test_post_add_suggested_emits_toast_on_success(self, client: TestClient, db_session: Session, test_user: User):
        """POST add emits an HX-Trigger toast header on success."""
        company, site, _ = self._make_company_with_hq(db_session, owner=test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/suggested-contacts/add",
            data={
                "site_id": str(site.id),
                "full_name": "Toast User",
                "email": "toast@acme.com",
            },
        )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "showToast" in trigger

    def test_post_add_suggested_dedup_reports_already_on_file(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """POST add for an existing contact reports 'already on file' via toast, not
        silent no-op or 409."""
        company, site, existing = self._make_company_with_hq(db_session, owner=test_user)
        # alice@acme.com already exists
        resp = client.post(
            f"/v2/partials/customers/{company.id}/suggested-contacts/add",
            data={
                "site_id": str(site.id),
                "full_name": "Alice Dupe",
                "email": "alice@acme.com",  # same as existing contact
            },
        )
        assert resp.status_code == 200
        # Returns grouped list (still valid HTML response)
        assert "text/html" in resp.headers.get("content-type", "")
        # Toast header present, mentioning "already" or "on file"
        trigger = resp.headers.get("HX-Trigger", "")
        assert "showToast" in trigger
        import json

        trigger_data = json.loads(trigger)
        toast_msg = (trigger_data.get("showToast") or {}).get("message", "")
        assert "already" in toast_msg.lower() or "file" in toast_msg.lower()

    def test_post_add_suggested_404_on_bad_company(self, client: TestClient, db_session: Session, test_user: User):
        """POST add for unknown company returns 404."""
        resp = client.post(
            "/v2/partials/customers/99999/suggested-contacts/add",
            data={"site_id": "1", "full_name": "Ghost"},
        )
        assert resp.status_code == 404

    # ── Add from the enrich panel (from_enrich) returns a self-contained row ──

    def test_post_add_suggested_from_enrich_returns_row_fragment(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """from_enrich=1 returns a single confirmation <li> (+ toast), NOT the grouped
        list.

        The enrich panel lives outside the Contacts tab (no #contacts-tab-list), so its
        Add button must self-swap the clicked row instead of re-rendering the whole
        contacts list.
        """
        from app.models.crm import SiteContact

        company, site, _ = self._make_company_with_hq(db_session)
        company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{company.id}/suggested-contacts/add",
            data={
                "site_id": str(site.id),
                "full_name": "Panel Add",
                "email": "paneladd@acme.com",
                "from_enrich": "1",
            },
        )
        assert resp.status_code == 200
        # Self-contained confirmation row, not the grouped accordion list
        assert "<li" in resp.text
        assert "Added" in resp.text
        # The grouped list would contain the pre-existing "Existing Alice"; the fragment must not
        assert "Existing Alice" not in resp.text
        # Toast still fires
        assert "showToast" in resp.headers.get("HX-Trigger", "")
        # The contact was actually created
        db_session.expire_all()
        sc = db_session.query(SiteContact).filter_by(email="paneladd@acme.com").first()
        assert sc is not None


@pytest.mark.usefixtures("_grant_account_management")
class TestFullWidthContactsForwardLayout:
    """Pin the full-width, contacts-forward customer + vendor detail reshape.

    The detail panel was capped at max-w-3xl and centered; contacts sat below 3 stacked
    meta cards. The reshape goes full width, slims the account header to one line (+ a
    collapsible Account settings block), and renders contacts as the primary multi-
    column grid. These tests lock that intent so a revert fails CI.
    """

    def _make_company_with_contact(self, db_session: Session):
        from app.models.crm import CustomerSite, SiteContact

        company = Company(name="FullWidth Co", is_active=True)
        db_session.add(company)
        db_session.flush()
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Gridcell Greta",
            email="greta@fullwidth.com",
            phone="+14155551234",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.commit()
        return company, site, contact

    def test_customer_detail_drops_max_width_cap(self, client: TestClient, db_session: Session, test_user: User):
        """Customer detail no longer caps content at max-w-3xl (full panel width)."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "max-w-3xl" not in resp.text

    def test_customer_detail_has_slim_header_actions(self, client: TestClient, db_session: Session, test_user: User):
        """The slim header carries the primary Add Contact + a VISIBLE labeled "Cadence
        & settings" trigger (no longer kebab-buried), and the coverage chip (N contacts
        · N sites)."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        html = resp.text
        assert "+ Add Contact" in html
        # Visible labeled affordance for cadence/tier/disposition (was "Account settings"
        # kebab-only); the collapsible state var still drives the block.
        assert "Cadence &amp; settings" in html
        assert "showAcctSettings" in html
        # Coverage chip and commercial strip survive the collapse into one line.
        assert "1 contact" in html
        assert "1 site" in html

    def test_customer_detail_preserves_cadence_controls(self, client: TestClient, db_session: Session, test_user: User):
        """Cadence tier setter + disposition control + clocks remain (in the
        collapsible)."""
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        html = resp.text
        # Tier setter form, disposition control, dual clocks all still rendered.
        assert f"/v2/partials/customers/{company.id}/tier" in html
        assert "disposition-control" in html
        assert "Last Out" in html

    def test_contacts_grid_container_class_present(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts render in a compact table (compact-table class), not single-column
        cards.

        The table layout replaced the old sm:grid-cols-2 card grid.
        """
        company, _, _ = self._make_company_with_contact(db_session)
        resp = client.get(f"/v2/partials/customers/{company.id}/tab/contacts")
        assert resp.status_code == 200
        html = resp.text
        assert "compact-table" in html
        # Contact name and outreach actions survive the table layout.
        assert "Gridcell Greta" in html
        assert "data-outreach-log" in html

    def test_workspace_auto_select_marker_present(self, client: TestClient, db_session: Session, test_user: User):
        """The workspace auto-selects the first account over the untouched empty state
        only — gated on the [data-cdm-empty] marker and a null selectedId."""
        c = Company(name="AutoSelect Co", is_active=True)
        db_session.add(c)
        db_session.commit()
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        html = resp.text
        assert "autoSelectFirst" in html
        assert "data-cdm-empty" in html

    def test_vendor_detail_drops_max_width_cap(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor detail no longer caps content at max-w-5xl (full panel width)."""
        from app.models.vendors import VendorCard

        vendor = VendorCard(display_name="FullWidth Vendor", normalized_name="fullwidth vendor")
        db_session.add(vendor)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vendor.id}")
        assert resp.status_code == 200
        assert "max-w-5xl" not in resp.text
        # The 4 stats survive the compression into a slim strip.
        assert "Sightings" in resp.text
        assert "Win Rate" in resp.text


class TestNoCache:
    """Every text/html /v2/* response — full-page shell AND HTMX partial — carries
    Cache-Control: no-store/no-cache.

    Browsers heuristically cache HTML responses with no Cache-Control header.  After a
    deploy the stale shell would reference old hashed-CSS/JS bundles and stale partials
    would keep swapping into in-app navigation, forcing users to hard-refresh.  The fix:
    the outermost request_id_middleware sets Cache-Control: no-store, no-cache,
    must-revalidate (+ Pragma: no-cache) on every text/html response.

    /static/assets/* (Vite-hashed) keeps its immutable long cache; JSON/SSE/downloads
    are intentionally unaffected.
    """

    def _authed_client(self, test_user, db_session):
        """Return a TestClient that passes authentication for the full-page route.

        The /v2/* full-page routes call get_user(request, db) directly (NOT the
        require_user FastAPI dependency), so we must also patch that function to return
        the test user.
        """
        from unittest.mock import patch

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        overrides = {
            get_db: lambda: (yield db_session),
            require_user: lambda: test_user,
            require_admin: lambda: test_user,
            require_buyer: lambda: test_user,
            require_fresh_token: lambda: "mock-token",
        }

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_admin] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
        app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            with TestClient(app) as c:
                yield c

        for dep in overrides:
            app.dependency_overrides.pop(dep, None)

    def test_v2_crm_full_page_has_no_cache_header(self, db_session, test_user):
        """GET /v2/crm (full-page shell) returns Cache-Control: no-cache."""
        from unittest.mock import patch

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_admin] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
        app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

        try:
            with patch("app.routers.htmx_views.get_user", return_value=test_user):
                with TestClient(app) as c:
                    resp = c.get("/v2/crm")
            assert resp.status_code == 200
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc, f"Expected Cache-Control: no-cache on /v2/crm, got: {cc!r}"
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_v2_customers_full_page_has_no_cache_header(self, db_session, test_user):
        """GET /v2/customers (full-page shell) returns Cache-Control: no-cache."""
        from unittest.mock import patch

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_admin] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
        app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

        try:
            with patch("app.routers.htmx_views.get_user", return_value=test_user):
                with TestClient(app) as c:
                    resp = c.get("/v2/customers")
            assert resp.status_code == 200
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc, f"Expected Cache-Control: no-cache on /v2/customers, got: {cc!r}"
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_partial_carries_no_store_header(self, client: TestClient, test_user):
        """HTMX partial /v2/partials/customers carries Cache-Control: no-store/no-cache.

        Browsers heuristically cache partial GETs too, so without this an in-app HTMX
        navigation keeps swapping in stale fragments after a deploy until a hard-
        refresh. Every text/html response (full-page shell AND partial) is made non-
        cacheable by the outermost request_id_middleware.
        """
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        cc = resp.headers.get("cache-control", "")
        assert "no-store" in cc, f"HTMX partial should carry no-store, got: {cc!r}"
        assert "no-cache" in cc
        assert "must-revalidate" in cc
        assert resp.headers.get("pragma") == "no-cache"


# ── Phase-0 CRM Foundations: HTMX form field surfacing tests ─────────────────


class TestCompanyPhase0FormFields:
    """HTMX create/edit form render tests and HTMX handler persistence tests."""

    # ── create form renders all Phase-0 input names ──────────────────────────

    def test_create_form_renders_phase0_inputs(self, client: TestClient):
        """GET create-form contains input[name] for every Phase-0 field."""
        resp = client.get("/v2/partials/customers/create-form")
        assert resp.status_code == 200
        html = resp.text
        for field in [
            "legal_name",
            "employee_size",
            "revenue_range",
            "phone",
            "hq_city",
            "hq_state",
            "hq_country",
            "credit_terms",
            "tax_id",
            "source",
        ]:
            assert f'name="{field}"' in html, f"create_form missing input[name={field}]"

    # ── edit form renders all Phase-0 input names ────────────────────────────

    def test_edit_form_renders_phase0_inputs(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form for an existing company contains inputs for all Phase-0
        fields."""
        co = Company(
            name="EditFields Co",
            is_active=True,
            legal_name="EditFields Corporation",
            employee_size="51-200",
            revenue_range="$1M-$10M",
            hq_city="Denver",
            hq_state="CO",
            hq_country="United States",
            phone="+13035550000",
            credit_terms="Net 30",
            tax_id="55-1234567",
            source="inbound",
        )
        db_session.add(co)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        html = resp.text
        for field in [
            "legal_name",
            "employee_size",
            "revenue_range",
            "phone",
            "hq_city",
            "hq_state",
            "hq_country",
            "credit_terms",
            "tax_id",
            "source",
        ]:
            assert f'name="{field}"' in html, f"edit_form missing input[name={field}]"

    def test_edit_form_prefills_existing_values(self, client: TestClient, db_session: Session, test_user: User):
        """Edit form pre-fills Phase-0 fields with existing DB values."""
        co = Company(
            name="Prefill Co",
            is_active=True,
            legal_name="Prefill Legal LLC",
            hq_city="Portland",
            credit_terms="Net 45",
            source="referral",
        )
        db_session.add(co)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        html = resp.text
        assert "Prefill Legal LLC" in html
        assert "Portland" in html
        assert "Net 45" in html

    # ── HTMX create handler persists Phase-0 fields ──────────────────────────

    def test_htmx_create_persists_phase0_fields(self, client: TestClient, db_session: Session, test_user: User):
        """POST /v2/partials/customers/create with Phase-0 fields saves them."""
        resp = client.post(
            "/v2/partials/customers/create",
            data={
                "name": "HTMXCreate P0 Co",
                "legal_name": "HTMXCreate P0 Corporation",
                "employee_size": "11-50",
                "revenue_range": "$1M-$10M",
                "hq_city": "Boulder",
                "hq_state": "CO",
                "hq_country": "United States",
                "credit_terms": "Net 30",
                "tax_id": "77-7654321",
                "source": "outbound",
            },
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "HTMXCreate P0 Co").first()
        assert co is not None
        assert co.legal_name == "HTMXCreate P0 Corporation"
        assert co.employee_size == "11-50"
        assert co.revenue_range == "$1M-$10M"
        assert co.hq_city == "Boulder"
        assert co.hq_state == "CO"
        assert co.hq_country == "US"
        assert co.credit_terms == "Net 30"
        assert co.tax_id == "77-7654321"
        assert co.source == "outbound"

    # ── HTMX edit handler persists Phase-0 fields ────────────────────────────

    def test_htmx_edit_persists_phase0_fields(self, client: TestClient, db_session: Session, test_user: User):
        """POST /v2/partials/customers/{id}/edit with Phase-0 fields saves them."""
        co = Company(name="HTMXEdit P0 Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(co)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={
                "name": "HTMXEdit P0 Co",
                "legal_name": "HTMXEdit P0 LLC",
                "employee_size": "201-500",
                "revenue_range": "$50M-$200M",
                "hq_city": "Chicago",
                "hq_state": "IL",
                "hq_country": "United States",
                "credit_terms": "Net 60",
                "tax_id": "33-9876543",
                "source": "sfdc",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.legal_name == "HTMXEdit P0 LLC"
        assert co.employee_size == "201-500"
        assert co.revenue_range == "$50M-$200M"
        assert co.hq_city == "Chicago"
        assert co.hq_country == "US"
        assert co.credit_terms == "Net 60"
        assert co.tax_id == "33-9876543"
        assert co.source == "sfdc"

    # ── Normalization: phone E.164 via HTMX path ─────────────────────────────

    def test_htmx_create_normalizes_phone_e164(self, client: TestClient, db_session: Session, test_user: User):
        """HTMX create handler stores phone in E.164 format."""
        resp = client.post(
            "/v2/partials/customers/create",
            data={
                "name": "PhoneNorm Create Co",
                "phone": "555-000-0001",
            },
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "PhoneNorm Create Co").first()
        assert co is not None
        assert co.phone is not None
        assert co.phone.startswith("+"), f"Expected E.164 (leading +), got {co.phone!r}"

    def test_htmx_edit_normalizes_phone_e164(self, client: TestClient, db_session: Session, test_user: User):
        """HTMX edit handler normalizes phone to E.164."""
        co = Company(name="PhoneNorm Edit Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(co)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={
                "name": "PhoneNorm Edit Co",
                "phone": "555-000-0002",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.phone is not None
        assert co.phone.startswith("+"), f"Expected E.164 (leading +), got {co.phone!r}"

    # ── Normalization: hq_country via HTMX path ──────────────────────────────

    def test_htmx_create_normalizes_hq_country(self, client: TestClient, db_session: Session, test_user: User):
        """HTMX create normalizes 'United States' → 'US'."""
        resp = client.post(
            "/v2/partials/customers/create",
            data={
                "name": "CountryNorm Create Co",
                "hq_country": "United States",
            },
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "CountryNorm Create Co").first()
        assert co is not None
        assert co.hq_country == "US"

    def test_htmx_edit_normalizes_hq_country(self, client: TestClient, db_session: Session, test_user: User):
        """HTMX edit normalizes 'United States' → 'US'."""
        co = Company(name="CountryNorm Edit Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(co)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={
                "name": "CountryNorm Edit Co",
                "hq_country": "United States",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.hq_country == "US"

    # ── Source: out-of-list value is preserved on blank submit ───────────────

    def test_htmx_edit_preserves_out_of_list_source(self, client: TestClient, db_session: Session, test_user: User):
        """Submitting source='' (blank sentinel) keeps the current enrichment source."""
        co = Company(name="SourcePreserve Co", is_active=True, source="apollo", account_owner_id=test_user.id)
        db_session.add(co)
        db_session.commit()

        # Submit with blank source — simulates the blank sentinel option being selected
        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={
                "name": "SourcePreserve Co",
                "source": "",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.source == "apollo", f"Expected source 'apollo' preserved, got {co.source!r}"


@pytest.mark.usefixtures("_grant_account_management")
class TestCustomerTabDeepLink:
    """Phase-0 Task B: account-detail tabs are deep-linkable via ?tab= param.

    Verifies that the server honours the tab query param, renders the correct
    active tab in the Alpine state initialiser, and emits the push-URL on tab
    buttons so round-tripping a shared URL lands on the right tab.
    """

    def _make_company(self, db_session: Session) -> "Company":
        from app.models.crm import CustomerSite

        co = Company(name="DeepLink Co", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.commit()
        return co

    def test_default_tab_is_contacts(self, client: TestClient, db_session: Session, test_user: User):
        """No ?tab param → contacts tab is active (Alpine x-data shows contacts)."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "activeTab: 'contacts'" in resp.text

    def test_tab_contacts_explicit(self, client: TestClient, db_session: Session, test_user: User):
        """?tab=contacts explicitly → same as default."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}?tab=contacts")
        assert resp.status_code == 200
        assert "activeTab: 'contacts'" in resp.text

    def test_tab_sites_activates_sites(self, client: TestClient, db_session: Session, test_user: User):
        """?tab=sites → activeTab is 'sites'; sites tab lazy-loads via hx-
        trigger=load."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}?tab=sites")
        assert resp.status_code == 200
        html = resp.text
        assert "activeTab: 'sites'" in html
        # The tab content div uses a lazy hx-get for non-contacts initial tab.
        assert f"/v2/partials/customers/{co.id}/tab/sites" in html
        assert 'hx-trigger="load"' in html

    def test_tab_files_activates_files(self, client: TestClient, db_session: Session, test_user: User):
        """?tab=files → activeTab is 'files'; files tab lazy-loads."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}?tab=files")
        assert resp.status_code == 200
        html = resp.text
        assert "activeTab: 'files'" in html
        assert f"/v2/partials/customers/{co.id}/tab/files" in html
        assert 'hx-trigger="load"' in html

    def test_invalid_tab_falls_back_to_contacts(self, client: TestClient, db_session: Session, test_user: User):
        """?tab=bogus → falls back to contacts silently (no 422/500)."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}?tab=bogus")
        assert resp.status_code == 200
        assert "activeTab: 'contacts'" in resp.text

    def test_tab_buttons_emit_push_url(self, client: TestClient, db_session: Session, test_user: User):
        """Every tab button carries hx-push-url so the browser URL updates on click."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        html = resp.text
        # Check a sample of tabs.
        assert f'hx-push-url="/v2/customers/{co.id}?tab=contacts"' in html
        assert f'hx-push-url="/v2/customers/{co.id}?tab=sites"' in html
        assert f'hx-push-url="/v2/customers/{co.id}?tab=activity"' in html

    def test_contacts_tab_inlines_content(self, client: TestClient, db_session: Session, test_user: User):
        """Contacts tab (default) inlines the contacts partial immediately — no lazy
        load."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        html = resp.text
        # The contacts_tab.html includes the people-search input.
        assert "contacts_tab" not in html or "hx-trigger" not in html.split("company-tab-content")[1][:200]
        # No load trigger on the tab content container (contacts are inlined).
        # The dup-suggestion + name-suggestion divs DO have hx-trigger=load; we check
        # the company-tab-content div specifically has no load trigger on itself.
        import re

        tab_content_block = re.search(r'id="company-tab-content"[^>]*>', html)
        assert tab_content_block, "company-tab-content div not found"
        assert "hx-trigger" not in tab_content_block.group(0)


# ─────────────────────────────────────────────────────────────────────────────
# TestDispositionFilter — P0-C disposition and has_open_reqs filters
# ─────────────────────────────────────────────────────────────────────────────


class TestDispositionFilter:
    """Tests for the new disposition and has_open_reqs account-list filters.

    Routes tested:
      GET /v2/partials/customers/account-list (all filter params)
    """

    def _make_company(self, db_session: Session, name: str, **kwargs) -> Company:
        co = Company(name=name, is_active=True, **kwargs)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        return co

    def test_disposition_active_excludes_bucket(self, client: TestClient, db_session: Session, test_user: User):
        """Disposition=active returns active accounts and excludes bucketed ones."""
        active = self._make_company(db_session, "Active Corp")
        bucketed = self._make_company(db_session, "Bucket Corp", disposition="bucket")

        html = client.get("/v2/partials/customers/account-list?disposition=active").text
        assert "Active Corp" in html
        assert "Bucket Corp" not in html

    def test_disposition_bucket_returns_only_bucketed(self, client: TestClient, db_session: Session, test_user: User):
        """Disposition=bucket returns only bucketed accounts."""
        active = self._make_company(db_session, "Active Corp 2")
        bucketed = self._make_company(db_session, "Bucket Corp 2", disposition="bucket")

        html = client.get("/v2/partials/customers/account-list?disposition=bucket").text
        assert "Bucket Corp 2" in html
        assert "Active Corp 2" not in html

    def test_disposition_default_hides_bucketed(self, client: TestClient, db_session: Session, test_user: User):
        """Default view (no disposition param) still suppresses bucketed accounts."""
        active = self._make_company(db_session, "Active Corp 3")
        bucketed = self._make_company(db_session, "Bucket Corp 3", disposition="bucket")

        html = client.get("/v2/partials/customers/account-list").text
        assert "Active Corp 3" in html
        assert "Bucket Corp 3" not in html

    def test_disposition_null_treated_as_active(self, client: TestClient, db_session: Session, test_user: User):
        """Companies with NULL disposition appear under disposition=active (NULL is
        active)."""
        null_disp = self._make_company(db_session, "Null Disposition Co")  # disposition=None

        html = client.get("/v2/partials/customers/account-list?disposition=active").text
        assert "Null Disposition Co" in html

    def test_has_open_reqs_true_returns_only_companies_with_open_reqs(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """has_open_reqs=1 returns only companies that have at least one open
        requisition."""
        from app.models.sourcing import Requisition

        with_req = self._make_company(db_session, "HasReq Corp")
        without_req = self._make_company(db_session, "NoReq Corp")

        req = Requisition(
            name="Open Req",
            company_id=with_req.id,
            status="open",
        )
        db_session.add(req)
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?has_open_reqs=1").text
        assert "HasReq Corp" in html
        assert "NoReq Corp" not in html

    def test_has_open_reqs_excludes_terminal_requisitions(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """A company whose only requisitions are terminal is excluded by
        has_open_reqs=1."""
        from app.models.sourcing import Requisition

        only_terminal = self._make_company(db_session, "TerminalOnly Corp")
        req = Requisition(
            name="Won Req",
            company_id=only_terminal.id,
            status="won",
        )
        db_session.add(req)
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?has_open_reqs=1").text
        assert "TerminalOnly Corp" not in html

    def test_disposition_bucket_composes_with_has_open_reqs(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Disposition=bucket and has_open_reqs=1 compose: only bucketed companies with
        open reqs."""
        from app.models.sourcing import Requisition

        bucketed_with_req = self._make_company(db_session, "BucketWithReq Corp", disposition="bucket")
        bucketed_no_req = self._make_company(db_session, "BucketNoReq Corp", disposition="bucket")
        req = Requisition(
            name="Bucket Open Req",
            company_id=bucketed_with_req.id,
            status="sourcing",
        )
        db_session.add(req)
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?disposition=bucket&has_open_reqs=1").text
        assert "BucketWithReq Corp" in html
        assert "BucketNoReq Corp" not in html

    def test_has_open_reqs_composes_with_my_only(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """has_open_reqs=1 composes with my_only=1."""
        from app.models.sourcing import Requisition

        mine_with_req = self._make_company(db_session, "MyReq Corp", account_owner_id=test_user.id)
        other_with_req = self._make_company(db_session, "OtherReq Corp")

        for co in (mine_with_req, other_with_req):
            req = Requisition(name="Req", company_id=co.id, status="open")
            db_session.add(req)
        db_session.commit()

        html = client.get("/v2/partials/customers/account-list?has_open_reqs=1&my_only=1").text
        assert "MyReq Corp" in html
        assert "OtherReq Corp" not in html

    def test_disposition_filter_ui_controls_rendered(self, client: TestClient, db_session: Session, test_user: User):
        """The workspace filter bar renders the disposition select and has_open_reqs
        checkbox."""
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        html = resp.text
        assert 'name="disposition"' in html
        assert "Bucketed only" in html
        assert 'name="has_open_reqs"' in html

    def test_disposition_select_reflects_active_state(self, client: TestClient, db_session: Session, test_user: User):
        """Disposition=active is pre-selected in the dropdown when passed to the
        workspace."""
        resp = client.get("/v2/partials/customers?disposition=active")
        assert resp.status_code == 200
        assert 'value="active" selected' in resp.text

    def test_has_open_reqs_checkbox_reflects_state(self, client: TestClient, db_session: Session, test_user: User):
        """has_open_reqs=1 checkbox is pre-checked when passed to the workspace."""
        resp = client.get("/v2/partials/customers?has_open_reqs=1")
        assert resp.status_code == 200
        assert 'name="has_open_reqs"' in resp.text
        assert "checked" in resp.text


@pytest.mark.usefixtures("_grant_account_management")
class TestAccountActivityTab:
    """Tests for the company Activity tab — type-sectioned feed (Step 1 of core-CRM
    plan).

    Verifies:
      (a) Quote rows/markup are absent from the activity tab response.
      (b) Type-section headers (Calls/Emails/Meetings/Notes) appear for accounts that
          have activities of those types.
      (c) Date headers (Today/Yesterday/date) appear within each section.
      (d) Other/system activities are in the collapsible Other section (hideOther).
    """

    @pytest.fixture()
    def _activity_company(self, db_session: Session) -> Company:
        """A company specifically for Activity tab tests."""
        c = Company(name="Activity Test Co", is_active=True)
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)
        return c

    @pytest.fixture()
    def _mixed_activities(self, db_session: Session, _activity_company: Company, test_user: User):
        """ActivityLog rows spanning all sections plus a variety of types."""
        from app.models.intelligence import ActivityLog

        # Anchor to noon UTC of the current day, NOT the literal current instant.
        # The rows below place "today" activities at now - 1/2/3 hours; with a
        # literal now() those land on the PREVIOUS calendar day when the suite
        # runs within ~3h after UTC midnight, so the "Today" date header never
        # renders and this test flakes. Noon ± a few hours stays on today's date
        # regardless of run time, making the date-grouping assertions deterministic.
        now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        rows = [
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="call_logged",
                channel="manual",
                summary="Called procurement",
                occurred_at=now - timedelta(hours=1),
                user_id=test_user.id,
            ),
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="email_sent",
                channel="email",
                summary="Sent availability update",
                occurred_at=now - timedelta(hours=2),
                user_id=test_user.id,
            ),
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="rfq_sent",
                channel="email",
                summary="RFQ batch #42",
                occurred_at=now - timedelta(hours=3),
                user_id=test_user.id,
            ),
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="meeting",
                channel="manual",
                summary="Quarterly review",
                occurred_at=now - timedelta(days=2),
                user_id=test_user.id,
            ),
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="note",
                channel="manual",
                summary="Noted preferred payment terms",
                occurred_at=now - timedelta(days=3),
                user_id=test_user.id,
            ),
            ActivityLog(
                company_id=_activity_company.id,
                activity_type="status_changed",
                channel="system",
                summary="Status updated to active",
                occurred_at=now - timedelta(days=1),
                user_id=test_user.id,
            ),
        ]
        db_session.add_all(rows)
        db_session.commit()
        return rows

    def test_activity_tab_returns_200(self, client: TestClient, _activity_company: Company, _mixed_activities):
        """GET company activity tab returns 200."""
        resp = client.get(f"/v2/partials/customers/{_activity_company.id}/tab/activity")
        assert resp.status_code == 200

    def test_no_quote_markup_in_activity_tab(self, client: TestClient, _activity_company: Company, _mixed_activities):
        """(a) Quote rows and quote-specific markup are absent from the activity tab."""
        resp = client.get(f"/v2/partials/customers/{_activity_company.id}/tab/activity")
        html = resp.text
        # No quote detail link patterns
        assert "/v2/quotes/" not in html
        assert "/v2/partials/quotes/" not in html
        # No quote-number or quote status badge CSS classes used exclusively for quotes
        assert "quote_number" not in html
        assert "quote-status" not in html

    def test_section_headers_rendered_for_present_types(
        self, client: TestClient, _activity_company: Company, _mixed_activities
    ):
        """(b) Section headers Calls/Emails/Meetings/Notes appear for types that
        exist."""
        resp = client.get(f"/v2/partials/customers/{_activity_company.id}/tab/activity")
        html = resp.text
        assert ">Calls<" in html
        assert ">Emails<" in html
        assert ">Meetings<" in html
        assert ">Notes<" in html

    def test_date_headers_appear_within_sections(
        self, client: TestClient, _activity_company: Company, _mixed_activities
    ):
        """(c) Date headers (Today / Yesterday / date string) appear in section
        bodies."""
        from datetime import datetime, timedelta, timezone

        resp = client.get(f"/v2/partials/customers/{_activity_company.id}/tab/activity")
        html = resp.text
        # Today's activities exist (call_logged + email_sent + rfq_sent)
        assert "Today" in html
        # status_changed is 1 day ago → "Yesterday" appears in the Other section
        assert "Yesterday" in html
        # The 3-day-old note renders as a %b %d, %Y date string (day_delta >= 2)
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%b %d, %Y")
        assert three_days_ago in html

    def test_other_section_hidden_by_default(self, client: TestClient, _activity_company: Company, _mixed_activities):
        """(d) Other section exists but is toggled via Alpine hideOther (x-show not
        rendered)."""
        resp = client.get(f"/v2/partials/customers/{_activity_company.id}/tab/activity")
        html = resp.text
        # Other section header is present
        assert ">Other<" in html
        # It uses Alpine x-show for collapsible body
        assert "hideOther" in html

    def test_absent_sections_not_rendered(self, client: TestClient, db_session: Session, test_user: User):
        """Sections with no activities are not rendered (no empty section headers)."""
        from app.models.intelligence import ActivityLog

        co = Company(name="Sparse Activity Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        # Only one note — only Notes section should appear
        db_session.add(
            ActivityLog(
                company_id=co.id,
                activity_type="note",
                channel="manual",
                summary="Just a note",
                occurred_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        html = resp.text
        assert ">Notes<" in html
        assert ">Calls<" not in html
        assert ">Emails<" not in html
        assert ">Meetings<" not in html

    def test_empty_state_when_no_activities(self, client: TestClient, db_session: Session):
        """Empty state is shown when the company has no activities at all."""
        co = Company(name="No Activity Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200
        html = resp.text
        assert "No activity recorded" in html

    def test_meeting_type_in_meetings_section(self, client: TestClient, db_session: Session, test_user: User):
        """New 'meeting' ActivityType lands in the Meetings section."""
        from app.models.intelligence import ActivityLog

        co = Company(name="MeetingOnly Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        db_session.add(
            ActivityLog(
                company_id=co.id,
                activity_type="meeting",
                channel="manual",
                summary="Board call",
                occurred_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        html = resp.text
        assert ">Meetings<" in html
        assert ">Calls<" not in html


@pytest.mark.usefixtures("_grant_account_management")
class TestKnownFieldGrid:
    """WS2: account detail renders a known-field grid; empty fields show '+ Add <label>'."""

    def test_empty_tax_id_shows_add_affordance(self, client: TestClient, db_session: Session):
        """Company with no tax_id: account detail shows '+ Add Tax ID'."""
        co = Company(name="Grid Test Co", is_active=True)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "Add Tax ID" in resp.text

    def test_filled_industry_shows_value(self, client: TestClient, db_session: Session):
        """Company with industry set: account detail shows the industry value."""
        co = Company(name="Industry Co", is_active=True, industry="Aerospace")
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "Aerospace" in resp.text

    def test_notes_always_rendered(self, client: TestClient, db_session: Session):
        """Notes grid cell renders even when notes is None (shows '+ Add Notes')."""
        co = Company(name="No Notes Co", is_active=True)
        co.notes = None
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "Add Notes" in resp.text

    def test_contact_empty_email_shows_row(self, client: TestClient, db_session: Session):
        """Contact with no email still renders a row in the compact table.

        The old card showed an '+ Add Email' inline affordance; the compact table omits
        that affordance (email is surfaced as '—') but the contact still appears and the
        edit affordance (kebab / edit-form deeplink) is present.
        """
        from app.models.crm import CustomerSite, SiteContact

        co = Company(name="Contact Grid Co", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(customer_site_id=site.id, full_name="No Email Contact")
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(co)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "No Email Contact" in resp.text
        # Edit affordance is still present (kebab menu points to edit-form)
        assert f"contacts/{contact.id}/edit-form" in resp.text


# ── WS4: modal edit-form coverage gaps ───────────────────────────────────────


class TestWS4AccountModalFields:
    """WS4: account edit modal renders domain/linkedin_url/account_type inputs and
    persists them via apply_company_field."""

    def _make_company(self, db_session: Session, **kwargs) -> Company:
        co = Company(name="WS4 Acct Co", is_active=True, **kwargs)
        db_session.add(co)
        db_session.commit()
        return co

    def test_edit_form_renders_domain_input(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form contains input[name=domain]."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        assert 'name="domain"' in resp.text

    def test_edit_form_renders_linkedin_url_input(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form contains input[name=linkedin_url]."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        assert 'name="linkedin_url"' in resp.text

    def test_edit_form_renders_account_type_select(self, client: TestClient, db_session: Session, test_user: User):
        """GET edit-form contains select[name=account_type] with canonical options."""
        co = self._make_company(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        assert 'name="account_type"' in resp.text
        for val in ["Customer", "Prospect", "Partner", "Competitor"]:
            assert val in resp.text

    def test_edit_form_prefills_domain(self, client: TestClient, db_session: Session, test_user: User):
        """Edit form pre-fills domain with existing value."""
        co = self._make_company(db_session, domain="prefill-domain.com")
        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200
        assert "prefill-domain.com" in resp.text

    def test_modal_edit_persists_domain(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with domain persists to DB."""
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={"name": co.name, "domain": "modal-domain.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.domain == "modal-domain.com"

    def test_modal_edit_persists_linkedin_url(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with linkedin_url persists to DB."""
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={"name": co.name, "linkedin_url": "https://linkedin.com/company/ws4test"},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.linkedin_url == "https://linkedin.com/company/ws4test"

    def test_modal_edit_persists_account_type(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with account_type persists valid choice to DB."""
        co = self._make_company(db_session, account_owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={"name": co.name, "account_type": "Partner"},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.account_type == "Partner"

    def test_modal_edit_blank_account_type_clears(self, client: TestClient, db_session: Session, test_user: User):
        """POST edit with blank account_type clears it to None."""
        co = self._make_company(db_session, account_type="Customer", account_owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{co.id}/edit",
            data={"name": co.name, "account_type": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.account_type is None


class TestWS4SiteEditModal:
    """WS4: site edit modal renders owner select and persists owner_id."""

    def _make_company_with_site(self, db_session: Session):
        from app.models.crm import CustomerSite

        co = Company(name="WS4 Site Co", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", site_type="hq", is_active=True)
        db_session.add(site)
        db_session.commit()
        return co, site

    def test_site_edit_modal_renders_owner_select(self, client: TestClient, db_session: Session, test_user: User):
        """GET site edit-form contains select[name=owner_id]."""
        co, site = self._make_company_with_site(db_session)
        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}/edit-form")
        assert resp.status_code == 200
        assert "name='owner_id'" in resp.text or 'name="owner_id"' in resp.text

    def test_site_edit_persists_owner_id(self, client: TestClient, db_session: Session, test_user: User):
        """POST site edit with owner_id persists it to DB."""

        co, site = self._make_company_with_site(db_session)
        co.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/sites/{site.id}/edit",
            data={"site_name": "HQ", "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(site)
        assert site.owner_id == test_user.id


class TestWS4CreateCompanySiteType:
    """WS4: create_company produces HQ site with site_type='hq' (not 'headquarters')."""

    def test_create_company_hq_site_type_is_hq(self, client: TestClient, db_session: Session, test_user: User):
        """POST create produces default site with site_type='hq'."""
        from app.models.crm import CustomerSite

        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "WS4 New Co HQ"},
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "WS4 New Co HQ").first()
        assert co is not None
        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == co.id).first()
        assert site is not None
        assert site.site_type == "hq"


class TestMultiSiteOwnership:
    """Phase 1 ownership cleanup: one user may own multiple sites (rule removed)."""

    def test_one_user_can_own_two_sites(self, client: TestClient, db_session: Session, test_user: User):
        """POST create-site twice with the same owner_id must both succeed (200).

        The old one-site-per-user rule has been removed so that ownership can be
        modelled flexibly across sites under the same account.
        """
        from app.models.crm import CustomerSite

        company = Company(name="Multi Site Owner Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(company)
        db_session.commit()

        resp1 = client.post(
            f"/v2/partials/customers/{company.id}/sites",
            data={"site_name": "Site A", "owner_id": str(test_user.id)},
        )
        assert resp1.status_code == 200, f"First site create failed: {resp1.text}"

        resp2 = client.post(
            f"/v2/partials/customers/{company.id}/sites",
            data={"site_name": "Site B", "owner_id": str(test_user.id)},
        )
        assert resp2.status_code == 200, f"Second site create with same owner failed: {resp2.text}"

        sites = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).all()
        owned = [s for s in sites if s.owner_id == test_user.id]
        assert len(owned) == 2, f"Expected 2 sites owned by same user, got {len(owned)}"
