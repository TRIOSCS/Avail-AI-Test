"""Task 11 — Reporting fold: dead route gone, analytics folded into slim chips.

Covers:
- GET /v2/partials/reporting → 404 (route + templates deleted)
- Pipeline chip renders on the parts workspace (open deals / open value / weighted)
- Coverage chip renders on the CRM account list ("Coverage NN%")
- No nav alias resolves to the removed "reporting" id
- No template includes the deleted reporting/ directory

Called by: pytest
Depends on: app.routers.htmx_views, app.routers.crm.views, app.services.reporting_service
"""

import pathlib

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company
from tests.conftest import engine  # noqa: F401

_TEMPLATE_ROOT = pathlib.Path("app/templates")


class TestReportingRouteRetired:
    """The standalone Reporting surface is gone."""

    def test_reporting_partial_returns_404(self, client: TestClient):
        """GET /v2/partials/reporting → 404 (route deleted)."""
        resp = client.get("/v2/partials/reporting")
        assert resp.status_code == 404

    def test_reporting_template_dir_deleted(self):
        """The reporting/ template directory no longer exists."""
        assert not (_TEMPLATE_ROOT / "htmx/partials/reporting").exists()

    def test_orphaned_buy_plans_list_deleted(self):
        """The orphaned buy_plans/list.html partial is gone."""
        assert not (_TEMPLATE_ROOT / "htmx/partials/buy_plans/list.html").exists()

    def test_no_template_includes_reporting_dir(self):
        """No template references the deleted reporting/ partial directory."""
        offenders = [p.name for p in _TEMPLATE_ROOT.rglob("*.html") if "partials/reporting/" in p.read_text()]
        assert offenders == [], f"templates still include reporting/: {offenders}"


class TestNavAliasNoReporting:
    """No nav highlight may resolve to the removed 'reporting' id."""

    def test_alias_has_no_reporting_value(self):
        from app.routers.htmx_views import _NAV_ID_ALIAS

        assert "reporting" not in _NAV_ID_ALIAS.values()

    def test_quote_detail_does_not_500(self, client: TestClient, db_session: Session, test_user: User):
        """/v2/quotes/{id} (the surviving quotes route) still renders the shell."""
        resp = client.get("/v2/quotes/999999")
        assert resp.status_code == 200


class TestPipelineChip:
    """Pipeline analytics folded into a slim strip on the parts workspace."""

    def test_workspace_renders_pipeline_strip(self, client: TestClient):
        """The workspace partial renders the open-deals / value / forecast strip."""
        resp = client.get("/v2/partials/parts/workspace")
        assert resp.status_code == 200
        body = resp.text
        assert "open deal" in body
        assert "open value" in body
        assert "weighted forecast" in body


class TestCoverageChip:
    """Coverage analytics folded into a slim span on the CRM account list."""

    def test_account_list_renders_coverage_span(self, client: TestClient, db_session: Session, test_user: User):
        """With an active company, the list header shows 'Coverage NN%'."""
        db_session.add(Company(name="Acme Coverage Co", tier="core", is_active=True))
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert "Coverage" in resp.text
        assert "%" in resp.text
