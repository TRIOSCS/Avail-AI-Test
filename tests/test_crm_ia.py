"""tests/test_crm_ia.py — Increment 2: left-panel company→site IA.

TDD spec for the IA feature (docs/superpowers/specs/
2026-06-18-crm-disposition-ia-aiorg-design.md, "Increment 2 — Left-panel IA").

Covers:
  * site-detail route GET /v2/partials/customers/{company_id}/sites/{site_id}
    — 200 for a valid (company, site); 404 IDOR (site under a different
    company); 404 for an inactive site.
  * site-detail renders the site's contacts + an "Open requisitions at this
    site" section (seeded Requisition.customer_site_id == site_id).
  * company-header partial GET .../{company_id}/header — 200, contains the
    company name, does NOT contain the per-site contacts tab markup.
  * sites-accordion partial GET .../{company_id}/sites-accordion — 200 lists
    the company's active sites.
  * _account_list.html rendering — site_count > 1 renders the accordion
    affordance; site_count <= 1 renders the direct detail hx-get. site_count
    is SEEDED explicitly (the Postgres trigger does not fire under SQLite).

Written FIRST (TDD) — fails until the production code lands.

Called by: pytest
Depends on: app.models, app.routers.htmx_views (via the TestClient `client`
            fixture), the in-memory SQLite test engine.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User
from app.models.sourcing import Requisition

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


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
# TestSiteDetailRoute
# ─────────────────────────────────────────────────────────────────────────────


class TestSiteDetailRoute:
    def test_valid_company_site_200(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="SiteDetailCo", owner_id=test_user.id, site_count=2)
        site = _make_site(db_session, co, site_name="Austin Plant", site_type="warehouse", city="Austin")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}")
        assert resp.status_code == 200
        assert "Austin Plant" in resp.text

    def test_idor_site_under_different_company_404(self, client, db_session: Session, test_user: User):
        co_a = _make_company(db_session, name="CoA", owner_id=test_user.id, site_count=1)
        co_b = _make_company(db_session, name="CoB", owner_id=test_user.id, site_count=1)
        site_b = _make_site(db_session, co_b, site_name="UnderB")
        db_session.commit()

        # Address site_b under company A's path — IDOR scope filter must 404.
        resp = client.get(f"/v2/partials/customers/{co_a.id}/sites/{site_b.id}")
        assert resp.status_code == 404

    def test_inactive_site_404(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="InactiveSiteCo", owner_id=test_user.id, site_count=1)
        site = _make_site(db_session, co, site_name="Closed", is_active=False)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}")
        assert resp.status_code == 404

    def test_renders_site_contacts(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="ContactsCo", owner_id=test_user.id, site_count=2)
        site = _make_site(db_session, co, site_name="Detroit")
        _make_contact(db_session, site, full_name="Dana Buyer")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}")
        assert resp.status_code == 200
        assert "Dana Buyer" in resp.text

    def test_renders_open_requisitions_at_site(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="ReqsCo", owner_id=test_user.id, site_count=2)
        site = _make_site(db_session, co, site_name="Memphis")
        _make_req(db_session, name="REQ-IA-001", site_id=site.id, status="active")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/sites/{site.id}")
        assert resp.status_code == 200
        assert "REQ-IA-001" in resp.text
        # Section heading present.
        assert "Open requisitions at this site" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# TestCompanyHeaderPartial
# ─────────────────────────────────────────────────────────────────────────────


class TestCompanyHeaderPartial:
    def test_header_200_contains_name(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="HeaderOnly Inc", owner_id=test_user.id, site_count=3)
        _make_site(db_session, co, site_name="One")
        _make_site(db_session, co, site_name="Two")
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/header")
        assert resp.status_code == 200
        assert "HeaderOnly Inc" in resp.text

    def test_header_excludes_company_tab_strip(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="NoTabsCo", owner_id=test_user.id, site_count=2)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/header")
        assert resp.status_code == 200
        # The detail.html tab strip wires per-tab hx-gets to /tab/{tab_id}; the
        # header-only partial must NOT carry that tab navigation.
        assert "/tab/contacts" not in resp.text
        assert 'aria-label="Account detail sections"' not in resp.text

    def test_header_missing_company_404(self, client, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/customers/999999/header")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# TestSitesAccordionPartial
# ─────────────────────────────────────────────────────────────────────────────


class TestSitesAccordionPartial:
    def test_lists_active_sites(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="AccordionCo", owner_id=test_user.id, site_count=3)
        _make_site(db_session, co, site_name="North Branch")
        _make_site(db_session, co, site_name="South Branch")
        _make_site(db_session, co, site_name="Shuttered", is_active=False)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{co.id}/sites-accordion")
        assert resp.status_code == 200
        assert "North Branch" in resp.text
        assert "South Branch" in resp.text
        # Inactive site must not appear.
        assert "Shuttered" not in resp.text

    def test_missing_company_404(self, client, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/customers/999999/sites-accordion")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# TestAccountListBranching — site_count drives accordion vs direct detail
# ─────────────────────────────────────────────────────────────────────────────


class TestAccountListBranching:
    def test_multi_site_renders_accordion_affordance(self, db_session: Session):
        # site_count seeded explicitly — the Postgres trigger does not run under
        # SQLite, so we must not rely on it.
        co = _make_company(db_session, name="MultiSite Corp", site_count=4)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        # Accordion header lazy-loads its children from the sites-accordion route.
        assert f"/v2/partials/customers/{co.id}/sites-accordion" in html
        # The company-header partial is the detail target for a multi-site row.
        assert f"/v2/partials/customers/{co.id}/header" in html

    def test_single_site_renders_direct_detail(self, db_session: Session):
        co = _make_company(db_session, name="SingleSite Co", site_count=1)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        # Direct detail hx-get (today's behavior) — no accordion / header partial.
        assert f'hx-get="/v2/partials/customers/{co.id}"' in html
        assert f"/v2/partials/customers/{co.id}/sites-accordion" not in html
        assert f"/v2/partials/customers/{co.id}/header" not in html

    def test_zero_site_count_behaves_as_single(self, db_session: Session):
        co = _make_company(db_session, name="ZeroSite Co", site_count=0)
        db_session.flush()
        co.cadence_state = "on_target"
        html = _render_account_list([co])
        assert f'hx-get="/v2/partials/customers/{co.id}"' in html
        assert f"/v2/partials/customers/{co.id}/sites-accordion" not in html
