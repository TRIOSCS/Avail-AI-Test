import os

os.environ["TESTING"] = "1"
"""test_crm_companies_coverage.py — Coverage tests for app/routers/crm/companies.py."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from tests.conftest import engine

_ = engine

from app.models import Company, CustomerSite, Requisition

# ── GET /api/companies ────────────────────────────────────────────────


class TestListCompanies:
    def test_returns_items_and_total(self, client, db_session, test_company):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] >= 1

    def test_search_filter_returns_matching(self, client, db_session, test_company):
        resp = client.get("/api/companies?search=Acme")
        assert resp.status_code == 200
        data = resp.json()
        names = [c["name"] for c in data["items"]]
        assert any("Acme" in n for n in names)

    def test_search_filter_no_match_returns_empty(self, client, db_session, test_company):
        resp = client.get("/api/companies?search=ZZZNoMatchXXX999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_unassigned_filter(self, client, db_session, test_company):
        resp = client.get("/api/companies?unassigned=1")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_owner_id_filter(self, client, db_session, test_company, test_user):
        resp = client.get(f"/api/companies?owner_id={test_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_limit_and_offset(self, client, db_session, test_company):
        resp = client.get("/api/companies?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 1

    def test_tag_filter(self, client, db_session):
        co = Company(name="Tagged Corp", is_active=True, brand_tags=["TI"])
        db_session.add(co)
        db_session.commit()
        resp = client.get("/api/companies?tag=ti")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_inactive_companies_excluded(self, client, db_session):
        co = Company(name="Inactive Corp", is_active=False)
        db_session.add(co)
        db_session.commit()
        resp = client.get("/api/companies?search=Inactive Corp")
        assert resp.status_code == 200
        data = resp.json()
        assert all(c["name"] != "Inactive Corp" for c in data["items"])

    def test_response_fields_present(self, client, db_session, test_company):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        items = resp.json()["items"]
        if items:
            c = items[0]
            for field in ("id", "name", "site_count", "open_req_count"):
                assert field in c


# ── GET /api/companies/typeahead ──────────────────────────────────────


class TestCompaniesTypeahead:
    def test_returns_list(self, client, db_session, test_company):
        resp = client.get("/api/companies/typeahead")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_includes_sites(self, client, db_session, test_company):
        site = CustomerSite(company_id=test_company.id, site_name="Main HQ")
        db_session.add(site)
        db_session.commit()
        resp = client.get("/api/companies/typeahead")
        assert resp.status_code == 200
        data = resp.json()
        company_entry = next((c for c in data if c["id"] == test_company.id), None)
        assert company_entry is not None
        assert "sites" in company_entry

    def test_inactive_companies_excluded(self, client, db_session):
        co = Company(name="Inactive Typeahead Corp", is_active=False)
        db_session.add(co)
        db_session.commit()
        resp = client.get("/api/companies/typeahead")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Inactive Typeahead Corp" not in names


# ── GET /api/companies/check-duplicate ───────────────────────────────


class TestCheckCompanyDuplicate:
    def test_no_match_returns_empty(self, client, db_session):
        resp = client.get("/api/companies/check-duplicate?name=ZZZNeverExist999XYZ")
        assert resp.status_code == 200
        assert resp.json()["matches"] == []

    def test_exact_match_found(self, client, db_session, test_company):
        resp = client.get("/api/companies/check-duplicate?name=Acme Electronics")
        assert resp.status_code == 200
        matches = resp.json()["matches"]
        assert any(m["match"] == "exact" for m in matches)

    def test_empty_name_returns_empty(self, client, db_session):
        resp = client.get("/api/companies/check-duplicate?name=   ")
        assert resp.status_code == 200
        assert resp.json()["matches"] == []

    def test_suffix_stripped_for_matching(self, client, db_session):
        co = Company(name="Global Systems Inc", is_active=True)
        db_session.add(co)
        db_session.commit()
        # "Inc" suffix stripped → should match "Global Systems"
        resp = client.get("/api/companies/check-duplicate?name=Global Systems LLC")
        assert resp.status_code == 200
        matches = resp.json()["matches"]
        assert len(matches) >= 1

    def test_prefix_match_found(self, client, db_session):
        co = Company(name="Trinamics Corp", is_active=True)
        db_session.add(co)
        db_session.commit()
        resp = client.get("/api/companies/check-duplicate?name=Trinamics Solutions")
        assert resp.status_code == 200
        matches = resp.json()["matches"]
        # prefix match (first 6 chars "trinam")
        assert len(matches) >= 1


# ── GET /api/companies/{id} ───────────────────────────────────────────


class TestGetCompany:
    def test_returns_company_data(self, client, db_session, test_company):
        resp = client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == test_company.id
        assert data["name"] == test_company.name

    def test_not_found_returns_404(self, client):
        resp = client.get("/api/companies/99999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_includes_sites_list(self, client, db_session, test_company):
        site = CustomerSite(company_id=test_company.id, site_name="Branch Office")
        db_session.add(site)
        db_session.commit()
        resp = client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        assert "sites" in resp.json()

    def test_includes_tags(self, client, db_session, test_company):
        resp = client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        assert "tags" in resp.json()

    def test_site_open_reqs_count(self, client, db_session, test_company, test_user):
        site = CustomerSite(company_id=test_company.id, site_name="Active Site")
        db_session.add(site)
        db_session.flush()
        req = Requisition(
            name="Open Req",
            customer_site_id=site.id,
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        resp = client.get(f"/api/companies/{test_company.id}")
        assert resp.status_code == 200
        sites = resp.json()["sites"]
        site_data = next((s for s in sites if s["id"] == site.id), None)
        assert site_data is not None
        assert site_data["open_reqs"] >= 1


# ── POST /api/companies ───────────────────────────────────────────────


def _no_credentials(name: str, env_var: str) -> None:
    """Return None for all credential lookups — disables auto-enrich in tests."""
    return None


class TestCreateCompany:
    def test_creates_company_successfully(self, client, db_session):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("New Tech Corp", "newtech.com"),
            ),
            patch(
                "app.routers.crm.companies.get_credential_cached",
                side_effect=_no_credentials,
            ),
        ):
            resp = client.post(
                "/api/companies",
                json={"name": "New Tech Corp", "domain": "newtech.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["name"] == "New Tech Corp"
        assert "default_site_id" in data

    def test_creates_default_hq_site(self, client, db_session):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Site Test Corp", "sitetest.com"),
            ),
            patch(
                "app.routers.crm.companies.get_credential_cached",
                side_effect=_no_credentials,
            ),
        ):
            resp = client.post(
                "/api/companies",
                json={"name": "Site Test Corp"},
            )
        assert resp.status_code == 200
        company_id = resp.json()["id"]
        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company_id).first()
        assert site is not None
        assert site.site_name == "HQ"

    def test_returns_409_on_duplicate(self, client, db_session, test_company):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme Electronics", ""),
            ),
            patch(
                "app.routers.crm.companies.get_credential_cached",
                side_effect=_no_credentials,
            ),
        ):
            resp = client.post(
                "/api/companies",
                json={"name": "Acme Electronics"},
            )
        assert resp.status_code == 409
        assert "duplicates" in resp.json()

    def test_force_true_skips_duplicate_check(self, client, db_session, test_company):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme Electronics", ""),
            ),
            patch(
                "app.routers.crm.companies.get_credential_cached",
                side_effect=_no_credentials,
            ),
        ):
            resp = client.post(
                "/api/companies?force=true",
                json={"name": "Acme Electronics"},
            )
        assert resp.status_code == 200
        assert "id" in resp.json()

    def test_domain_extracted_from_website(self, client, db_session):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Website Domain Corp", ""),
            ),
            patch(
                "app.routers.crm.companies.get_credential_cached",
                side_effect=_no_credentials,
            ),
        ):
            resp = client.post(
                "/api/companies",
                json={"name": "Website Domain Corp", "website": "https://www.websitedomain.com/path"},
            )
        assert resp.status_code == 200
        company_id = resp.json()["id"]
        company = db_session.get(Company, company_id)
        assert company is not None

    def test_blank_name_returns_422(self, client, db_session):
        resp = client.post(
            "/api/companies",
            json={"name": "   "},
        )
        assert resp.status_code == 422


# ── PUT /api/companies/{id} ───────────────────────────────────────────


class TestUpdateCompany:
    def test_update_name(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"name": "Acme Electronics Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(test_company)
        assert test_company.name == "Acme Electronics Updated"

    def test_update_industry(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"industry": "Aerospace"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.industry == "Aerospace"

    def test_update_is_strategic(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"is_strategic": True},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.is_strategic is True

    def test_not_found_returns_404(self, client):
        resp = client.put(
            "/api/companies/99999",
            json={"name": "Ghost Company"},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_update_notes(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"notes": "Strategic account — handle with care"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.notes == "Strategic account — handle with care"

    def test_update_deactivate(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.is_active is False

    def test_update_hq_country(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"hq_country": "United States"},
        )
        assert resp.status_code == 200


# ── POST /api/companies/{id}/summarize ───────────────────────────────


class TestSummarizeCompany:
    def test_not_found_returns_404(self, client):
        resp = client.post("/api/companies/99999/summarize")
        assert resp.status_code == 404

    def test_returns_empty_when_no_result(self, client, db_session, test_company):
        with patch(
            "app.services.account_summary_service.generate_account_summary",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(f"/api/companies/{test_company.id}/summarize")
        assert resp.status_code == 200
        data = resp.json()
        assert data["situation"] == ""
        assert data["development"] == ""
        assert data["next_steps"] == []

    def test_returns_summary_when_available(self, client, db_session, test_company):
        fake_summary = {
            "situation": "Acme is a growing electronics distributor.",
            "development": "Recent orders for capacitors and MCUs.",
            "next_steps": ["Follow up on Q2 quote", "Send product catalog"],
        }
        with patch(
            "app.services.account_summary_service.generate_account_summary",
            new_callable=AsyncMock,
            return_value=fake_summary,
        ):
            resp = client.post(f"/api/companies/{test_company.id}/summarize")
        assert resp.status_code == 200
        data = resp.json()
        assert data["situation"] == fake_summary["situation"]
        assert len(data["next_steps"]) == 2


# ── POST /api/companies/{id}/analyze-tags ────────────────────────────


class TestAnalyzeCompanyTags:
    def test_not_found_returns_404(self, client):
        resp = client.post("/api/companies/99999/analyze-tags")
        assert resp.status_code == 404

    def test_returns_tags(self, client, db_session, test_company):
        with patch(
            "app.services.customer_analysis_service.analyze_customer_materials",
            new_callable=AsyncMock,
        ) as mock_analyze:
            mock_analyze.return_value = None
            resp = client.post(f"/api/companies/{test_company.id}/analyze-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "brand_tags" in data
        assert "commodity_tags" in data

    def test_returns_saved_tags(self, client, db_session, test_company):
        async def _mock_analyze(company_id, db_session):
            co = db_session.get(Company, company_id)
            if co:
                co.brand_tags = ["TI", "ST"]
                co.commodity_tags = ["MCU", "ANALOG"]
                db_session.commit()

        with patch(
            "app.services.customer_analysis_service.analyze_customer_materials",
            side_effect=_mock_analyze,
        ):
            resp = client.post(f"/api/companies/{test_company.id}/analyze-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert "TI" in data["brand_tags"]
        assert "MCU" in data["commodity_tags"]
