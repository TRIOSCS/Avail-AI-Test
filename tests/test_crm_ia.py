"""tests/test_crm_ia.py — CRM IA redesign: one unified account workspace.

TDD spec for the IA redesign (docs/superpowers/specs/
2026-06-23-crm-ia-redesign.md, Stage A "Unify workspace + Contacts canonical").

The old single-vs-multi-site fork is GONE: every account row — regardless of
site_count — loads the SAME unified detail at /v2/partials/customers/{id}, with
Contacts as the default + primary right-panel surface. The header-only multi-site
view, the left-panel sites-accordion, and the site-scoped right-panel view are
retired as navigation.

Covers:
  * _account_list.html — single AND multi-site rows both target the unified
    detail (/v2/partials/customers/{id}); no accordion/header/site-detail routes.
  * unified detail (company_detail_partial) for a MULTI-site account — renders the
    full tab strip with Contacts default, breadcrumb "Customers › {Account}", and
    no header-only fork.
  * Contacts canonical surface — light per-site section headers, a people-search,
    and a site filter shown only when the account has >1 active site (single-site →
    one section, no filter).
  * retired routes (/header, /sites-accordion, /sites/{id}) return 404.

Called by: pytest
Depends on: app.models, app.routers.htmx_views (via the TestClient `client`
            fixture), the in-memory SQLite test engine.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User
from app.models.sourcing import Requisition

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_company(
    db: Session,
    *,
    name: str = "IA Co",
    owner_id: int | None = None,
    site_count: int = 0,
) -> Company:
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner_id,
        site_count=site_count,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(
    db: Session,
    company: Company,
    *,
    site_name: str = "HQ",
    site_type: str | None = None,
    city: str | None = None,
    is_active: bool = True,
) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name=site_name,
        site_type=site_type,
        city=city,
        is_active=is_active,
    )
    db.add(site)
    db.flush()
    return site


def _make_contact(db: Session, site: CustomerSite, *, full_name: str) -> SiteContact:
    c = SiteContact(customer_site_id=site.id, full_name=full_name, is_active=True)
    db.add(c)
    db.flush()
    return c


def _make_req(db: Session, *, name: str, site_id: int, status: str = "active") -> Requisition:
    r = Requisition(name=name, customer_site_id=site_id, status=status)
    db.add(r)
    db.flush()
    return r


def _render_account_list(companies) -> str:
    """Render _account_list.html with a minimal ctx (no DB needed)."""
    from app.template_env import templates

    tmpl = templates.get_template("htmx/partials/customers/_account_list.html")
    return tmpl.render(
        companies=companies,
        total=len(companies),
        limit=50,
        offset=0,
        alert_markers={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# TestUnifiedAccountList — every row targets the unified detail (no fork)
# ─────────────────────────────────────────────────────────────────────────────


class TestUnifiedAccountList:
    def test_single_site_targets_unified_detail(self, db_session: Session):
        co = _make_company(db_session, name="SingleSite Co", site_count=1)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        assert f'hx-get="/v2/partials/customers/{co.id}"' in html

    def test_multi_site_targets_unified_detail_not_header(self, db_session: Session):
        # site_count seeded explicitly — the Postgres trigger does not run under
        # SQLite, so we must not rely on it.
        co = _make_company(db_session, name="MultiSite Corp", site_count=4)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        # Unified detail target — the SAME as a single-site row.
        assert f'hx-get="/v2/partials/customers/{co.id}"' in html
        # The retired forks must be gone from the row markup.
        assert f"/v2/partials/customers/{co.id}/header" not in html
        assert f"/v2/partials/customers/{co.id}/sites-accordion" not in html

    def test_zero_site_count_targets_unified_detail(self, db_session: Session):
        co = _make_company(db_session, name="ZeroSite Co", site_count=0)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        assert f'hx-get="/v2/partials/customers/{co.id}"' in html
        assert f"/v2/partials/customers/{co.id}/sites-accordion" not in html

    def test_multi_site_shows_site_count_hint(self, db_session: Session):
        co = _make_company(db_session, name="HintCo", site_count=3)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        # The picker still tells the rep this is a multi-site account.
        assert "3 sites" in html


# ─────────────────────────────────────────────────────────────────────────────
# TestUnifiedDetailMultiSite — full detail for a multi-site account
# ─────────────────────────────────────────────────────────────────────────────


class TestUnifiedDetailMultiSite:
    def test_multi_site_renders_full_unified_detail(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="UnifiedMulti Co", owner_id=test_user.id, site_count=2)
        _make_site(db_session, co, site_name="Detroit HQ", city="Detroit")
        _make_site(db_session, co, site_name="Austin Plant", city="Austin")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        # Full tab strip is present (NOT the header-only fork).
        assert 'aria-label="Account detail sections"' in resp.text
        assert f"/v2/partials/customers/{co.id}/tab/contacts" in resp.text
        # Contacts is the default tab.
        assert "activeTab: 'contacts'" in resp.text

    def test_unified_detail_has_breadcrumb(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="Breadcrumb Co", owner_id=test_user.id, site_count=2)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        # "Customers › {Account}" breadcrumb at the top of the right panel.
        assert "Customers" in resp.text
        assert "Breadcrumb Co" in resp.text
        assert 'aria-label="Breadcrumb"' in resp.text

    def test_unified_detail_surfaces_cadence_affordance(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="CadenceVisible Co", owner_id=test_user.id, site_count=1)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        # Cadence/tier/disposition is reachable via a visible LABELED affordance,
        # not buried behind a kebab-only item. ("&" renders as the &amp; entity.)
        assert "Cadence &amp; settings" in resp.text
        assert 'aria-controls="acct-settings-' in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# TestContactsCanonicalSurface — site sections + people-search + site filter
# ─────────────────────────────────────────────────────────────────────────────


class TestContactsCanonicalSurface:
    def test_multi_site_contacts_show_site_sections(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="SectionsCo", owner_id=test_user.id, site_count=2)
        s1 = _make_site(db_session, co, site_name="Detroit HQ", city="Detroit")
        s2 = _make_site(db_session, co, site_name="Austin Plant", city="Austin")
        _make_contact(db_session, s1, full_name="Jane Smith")
        _make_contact(db_session, s2, full_name="Carl Ek")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts")
        assert resp.status_code == 200
        # Light per-site section headers name the site + city.
        assert "Detroit HQ" in resp.text
        assert "Austin Plant" in resp.text
        assert "Jane Smith" in resp.text
        assert "Carl Ek" in resp.text

    def test_multi_site_contacts_show_site_filter(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="FilterCo", owner_id=test_user.id, site_count=2)
        s1 = _make_site(db_session, co, site_name="North Site", city="Reno")
        s2 = _make_site(db_session, co, site_name="South Site", city="Tucson")
        _make_contact(db_session, s1, full_name="North Person")
        _make_contact(db_session, s2, full_name="South Person")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts")
        assert resp.status_code == 200
        # Site filter present (>1 active site) — both site options selectable.
        assert "All sites" in resp.text
        # People-search present.
        assert "Search people" in resp.text or "Search contacts" in resp.text

    def test_single_site_contacts_no_site_filter(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="LoneSite Co", owner_id=test_user.id, site_count=1)
        s1 = _make_site(db_session, co, site_name="Only Site", city="Omaha")
        _make_contact(db_session, s1, full_name="Solo Person")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Solo Person" in resp.text
        # Single-site → no "All sites" filter clutter.
        assert "All sites" not in resp.text

    def test_contact_card_shows_site_label(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="SiteLabelCo", owner_id=test_user.id, site_count=2)
        s1 = _make_site(db_session, co, site_name="Memphis Depot", city="Memphis")
        _make_contact(db_session, s1, full_name="Labeled Person")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts")
        assert resp.status_code == 200
        # The site label appears on the card AND the section header.
        assert resp.text.count("Memphis Depot") >= 2


# ─────────────────────────────────────────────────────────────────────────────
# TestRetiredSurfaces — old fork routes are gone
# ─────────────────────────────────────────────────────────────────────────────


class TestRetiredSurfaces:
    def test_header_route_retired(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="NoHeader Co", owner_id=test_user.id, site_count=2)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/header")
        assert resp.status_code == 404

    def test_sites_accordion_route_retired(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="NoAccordion Co", owner_id=test_user.id, site_count=2)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/sites-accordion")
        assert resp.status_code == 404

    def test_site_detail_route_retired(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="NoSiteDetail Co", owner_id=test_user.id, site_count=2)
        site = _make_site(db_session, co, site_name="Gone")
        db_session.commit()
        # The GET site-detail view is retired. The path prefix survives only for the
        # remaining POST/DELETE site-contact CRUD routes, so a GET now resolves to
        # "method not allowed" (405) rather than rendering a right-panel view.
        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}")
        assert resp.status_code in (404, 405)


# ─────────────────────────────────────────────────────────────────────────────
# TestSiteCardNavFixes — blockers 1, 2, 3, 4, 5, 7
# ─────────────────────────────────────────────────────────────────────────────


class TestSiteCardNavFixes:
    # Blocker 1 — dead hx-target "#detail-tab-body" fixed to "#company-tab-content"
    def test_site_card_view_contacts_targets_company_tab_content(self, db_session: Session):
        co = _make_company(db_session, name="SiteCardNav Co", site_count=1)
        site = _make_site(db_session, co, site_name="HQ")
        _make_contact(db_session, site, full_name="Alice")
        db_session.flush()
        # Attach minimal site_contacts attribute for the template
        site.site_contacts = [
            c
            for c in db_session.query(__import__("app.models", fromlist=["SiteContact"]).SiteContact)
            .filter_by(customer_site_id=site.id)
            .all()
        ]
        from app.template_env import templates

        html = templates.get_template("htmx/partials/customers/tabs/site_card.html").render(s=site, company=co)
        # Must target the REAL tab body, not the stale "#detail-tab-body".
        assert 'hx-target="#company-tab-content"' in html
        assert "#detail-tab-body" not in html
        # Must dispatch crm-switch-tab so the tab indicator follows.
        assert "crm-switch-tab" in html

    # Blocker 1 — detail.html tab wrapper listens for crm-switch-tab
    def test_detail_html_listens_for_crm_switch_tab(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="EventListenerCo", owner_id=test_user.id, site_count=1)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "@crm-switch-tab.window" in resp.text

    # Blocker 2 — site-scoped edit-form GET route is retired
    def test_site_scoped_edit_form_route_retired(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="RetiredEditForm Co", owner_id=test_user.id, site_count=1)
        site = _make_site(db_session, co, site_name="Site")
        c = _make_contact(db_session, site, full_name="Edit Me")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}/contacts/{c.id}/edit-form")
        # Route removed → 404 (or 405 if the path pattern still matches differently)
        assert resp.status_code in (404, 405)

    # Blocker 3 — create_site_contact POST now returns canonical grouped contacts list
    def test_create_site_contact_post_returns_grouped_list(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="CreateSiteContact Co", owner_id=test_user.id, site_count=1)
        site = _make_site(db_session, co, site_name="Plant")
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{co.id}/sites/{site.id}/contacts",
            data={"full_name": "New Person", "email": "new@plant.com"},
        )
        assert resp.status_code == 200
        # Returns the canonical grouped list fragment, not the retired site_contacts.html.
        assert "contacts-tab-list" in resp.text
        # The created contact appears.
        assert "New Person" in resp.text

    # Blocker 3 — no template file named site_contacts.html exists in the tree
    def test_site_contacts_template_deleted(self):
        from pathlib import Path

        tpl = Path("app/templates/htmx/partials/customers/tabs/site_contacts.html")
        assert not tpl.exists(), "site_contacts.html must be deleted (spec retired it)"

    # Blocker 3 — no template file named contact_edit_modal.html exists in the tree
    def test_contact_edit_modal_template_deleted(self):
        from pathlib import Path

        tpl = Path("app/templates/htmx/partials/customers/tabs/contact_edit_modal.html")
        assert not tpl.exists(), "contact_edit_modal.html must be deleted (route retired)"

    # Blocker 4 — x-text attribute in contacts_tab.html uses &quot; entities
    def test_contacts_tab_search_hint_uses_quot_entities(self):
        from pathlib import Path

        src = Path("app/templates/htmx/partials/customers/tabs/contacts_tab.html").read_text()
        # Must NOT contain a bare double-quote inside x-text="..." that closes the attr early.
        # The correct form uses &quot; entities.
        assert "&quot;" in src, "contacts_tab.html search-hint x-text must use &quot; entities"
        # The old broken form (unescaped " inside double-quoted attr) must be gone.
        assert """x-text="q ? '"' + q + '"'""" not in src

    # Blocker 5 — company_tab contacts branch accepts site_id query param
    def test_company_tab_contacts_preselects_site(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="PreSelectSite Co", owner_id=test_user.id, site_count=2)
        s1 = _make_site(db_session, co, site_name="Alpha")
        s2 = _make_site(db_session, co, site_name="Beta")
        _make_contact(db_session, s1, full_name="Alpha Person")
        _make_contact(db_session, s2, full_name="Beta Person")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts?site_id={s1.id}")
        assert resp.status_code == 200
        # data-initial-site attribute carries the preselected site id.
        assert f'data-initial-site="{s1.id}"' in resp.text

    # Finding 7 — active contact count excludes archived contacts
    def test_detail_contact_count_excludes_archived(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="ArchiveCount Co", owner_id=test_user.id, site_count=1)
        site = _make_site(db_session, co, site_name="HQ")
        # 2 active, 1 archived
        _make_contact(db_session, site, full_name="Active One")
        _make_contact(db_session, site, full_name="Active Two")
        archived = _make_contact(db_session, site, full_name="Archived Three")
        archived.is_archived = True
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        # The chip strip uses contact_count (active only = 2) not len(contact_rows) = 3.
        # The badge span renders "2" inside rounded-full bg-brand-100, so "2 contact"
        # appears in the tab label area; "3 contact" must not be there.
        assert "2 contact" in resp.text
        assert "3 contact" not in resp.text
