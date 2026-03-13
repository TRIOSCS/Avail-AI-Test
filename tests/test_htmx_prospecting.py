"""
test_htmx_prospecting.py — Tests for Phase 3 Task 10: Prospecting + Enrichment views.
Verifies prospect pool page, rows partial with filters, detail drawer,
enrich button rendering, and claim action.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/prospecting/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import ProspectAccount, User


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/prospecting" not in route_paths:
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
def sample_prospects(db_session):
    """Create multiple prospects for list/filter tests."""
    prospects = []
    for name, domain, industry, revenue, region, source, fit, status in [
        ("Alpha Electronics", "alpha.com", "Electronics", "$10M-$50M", "North America", "web_scrape", 85, "suggested"),
        ("Beta Semiconductors", "beta.com", "Semiconductors", "$50M-$100M", "Europe", "sf_import", 72, "suggested"),
        ("Gamma Defense", "gamma.com", "Defense", "$10M-$50M", "North America", "referral", 60, "claimed"),
        ("Delta Chips", "delta.com", "Electronics", "$1M-$10M", "Asia", "web_scrape", 45, "suggested"),
        ("Dismissed Corp", "dismissed.com", "Other", "$1M-$10M", "Europe", "sf_import", 20, "dismissed"),
    ]:
        p = ProspectAccount(
            name=name,
            domain=domain,
            industry=industry,
            revenue_range=revenue,
            region=region,
            discovery_source=source,
            fit_score=fit,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(p)
        prospects.append(p)
    db_session.commit()
    for p in prospects:
        db_session.refresh(p)
    return prospects


class TestProspectingPoolPage:
    """Tests for GET /views/prospecting — full page HTML."""

    def test_pool_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/prospecting")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_pool_page_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/prospecting")
        assert "prospect-table-body" in resp.text
        assert "Prospect Pool" in resp.text

    def test_pool_page_has_search_input(self, htmx_client):
        resp = htmx_client.get("/views/prospecting")
        assert 'type="search"' in resp.text
        assert "delay:300ms" in resp.text

    def test_pool_page_has_filter_dropdowns(self, htmx_client):
        resp = htmx_client.get("/views/prospecting")
        assert "All Industries" in resp.text
        assert "All Revenue Ranges" in resp.text
        assert "All Regions" in resp.text
        assert "All Sources" in resp.text

    def test_pool_page_shows_active_prospects(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting")
        assert "Alpha Electronics" in resp.text
        assert "Beta Semiconductors" in resp.text
        # Dismissed should not appear
        assert "Dismissed Corp" not in resp.text


class TestProspectingRowsPartial:
    """Tests for GET /views/prospecting/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_prospect_names(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows")
        assert "Alpha Electronics" in resp.text
        assert "Beta Semiconductors" in resp.text
        assert "Delta Chips" in resp.text

    def test_rows_exclude_dismissed(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows")
        assert "Dismissed Corp" not in resp.text

    def test_rows_with_filter_params(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?industry=Electronics")
        assert "Alpha Electronics" in resp.text
        assert "Delta Chips" in resp.text
        assert "Beta Semiconductors" not in resp.text


class TestProspectingIndustryFilter:
    """Tests for industry filter narrowing results."""

    def test_industry_filter_narrows(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?industry=Semiconductors")
        assert "Beta Semiconductors" in resp.text
        assert "Alpha Electronics" not in resp.text
        assert "Gamma Defense" not in resp.text

    def test_industry_filter_via_hx_include(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?industry_filter=Defense")
        assert "Gamma Defense" in resp.text
        assert "Alpha Electronics" not in resp.text

    def test_region_filter(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?region=Europe")
        assert "Beta Semiconductors" in resp.text
        assert "Alpha Electronics" not in resp.text

    def test_source_filter(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?source=referral")
        assert "Gamma Defense" in resp.text
        assert "Alpha Electronics" not in resp.text

    def test_search_no_results(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows?q=nonexistent_xyz")
        assert resp.status_code == 200
        assert "No prospects found" in resp.text


class TestProspectDetail:
    """Tests for GET /views/prospecting/{id} — detail drawer."""

    def test_detail_returns_html(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_contains_prospect_name(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert "Alpha Electronics" in resp.text

    def test_detail_shows_domain(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert "alpha.com" in resp.text

    def test_detail_shows_scores(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert "85/100" in resp.text

    def test_detail_has_close_button(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert "close-drawer" in resp.text

    def test_detail_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/prospecting/99999")
        assert resp.status_code == 404


class TestEnrichButtonRenders:
    """Tests for enrich button partial rendering."""

    def test_enrich_button_in_prospect_row(self, htmx_client, sample_prospects):
        resp = htmx_client.get("/views/prospecting/rows")
        # Enrich button should render with correct hx-post URL
        assert "Enrich" in resp.text
        assert f"/api/enrich/prospect/{sample_prospects[0].id}" in resp.text

    def test_enrich_button_in_detail(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]
        resp = htmx_client.get(f"/views/prospecting/{prospect.id}")
        assert f"/api/enrich/prospect/{prospect.id}" in resp.text


class TestClaimProspect:
    """Tests for POST /views/prospecting/{id}/claim."""

    def test_claim_returns_updated_row(self, htmx_client, sample_prospects):
        prospect = sample_prospects[0]  # suggested status
        resp = htmx_client.post(f"/views/prospecting/{prospect.id}/claim")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "claimed" in resp.text

    def test_claim_updates_status(self, htmx_client, db_session, sample_prospects):
        prospect = sample_prospects[0]
        htmx_client.post(f"/views/prospecting/{prospect.id}/claim")
        db_session.refresh(prospect)
        assert prospect.status == "claimed"
        assert prospect.claimed_by is not None
        assert prospect.claimed_at is not None

    def test_claim_missing_prospect_404(self, htmx_client):
        resp = htmx_client.post("/views/prospecting/99999/claim")
        assert resp.status_code == 404
