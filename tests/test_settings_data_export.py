"""tests/test_settings_data_export.py — ISS-028 admin-only Settings "Data export" page.

Covers GET /v2/partials/settings/data-export: admin sees the five dataset-export
links (content asserted, not just status 200); any non-admin role is blocked exactly
like the sibling admin-only settings tabs (system/users/ops-group/data-ops), which use
the same `if user.role != UserRole.ADMIN: raise HTTPException(403, ...)` gate.

Called by: pytest
Depends on: app.routers.htmx.settings (settings_data_export_tab), conftest fixtures
    (db_session, admin_user, manager_user, test_user)
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

DATA_EXPORT_URL = "/v2/partials/settings/data-export"


def _client_as(db_session: Session, user: User) -> TestClient:
    """A TestClient whose require_user resolves to *user* (the data-export tab's admin
    gate is a plain `user.role != UserRole.ADMIN` check on the require_user- injected
    user, so overriding require_user alone exercises it, same as the ops-
    group/users/system sibling tabs)."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    client = TestClient(app)
    client._overridden = (get_db, require_user)
    return client


def _drop_overrides(client):
    from app.main import app

    for dep in getattr(client, "_overridden", ()):
        app.dependency_overrides.pop(dep, None)


class TestDataExportTabAdminAccess:
    def test_admin_200_with_five_links_present(self, db_session, admin_user):
        c = _client_as(db_session, admin_user)
        try:
            resp = c.get(DATA_EXPORT_URL)
            assert resp.status_code == 200
            html = resp.text
            assert "Data export" in html
            assert '/v2/customers/export.csv"' in html
            assert '/v2/customers/contacts/export.csv"' in html
            assert '/v2/partials/vendors/export"' in html
            assert '/v2/partials/requisitions/export"' in html
            assert '/v2/sightings/export"' in html
            # Five distinct "Export CSV" download links, one per dataset.
            assert html.count("Export CSV") == 5
        finally:
            _drop_overrides(c)

    def test_admin_links_are_real_downloads_not_htmx_boosted(self, db_session, admin_user):
        c = _client_as(db_session, admin_user)
        try:
            html = c.get(DATA_EXPORT_URL).text
            assert html.count('hx-boost="false"') == 5
        finally:
            _drop_overrides(c)


class TestDataExportTabNonAdminBlocked:
    @pytest.mark.parametrize("role_fixture", ["test_user", "sales_user", "trader_user", "manager_user"])
    def test_non_admin_403(self, db_session, role_fixture, request):
        user = request.getfixturevalue(role_fixture)
        c = _client_as(db_session, user)
        try:
            assert c.get(DATA_EXPORT_URL).status_code == 403
        finally:
            _drop_overrides(c)

    def test_manager_with_export_bulk_data_override_still_403_on_tab(self, db_session, manager_user):
        """The Settings tab gate is admin-only by role, independent of the
        EXPORT_BULK_DATA capability grant — an override widens which export ROUTES a
        manager may hit, not whether they can open the admin Settings tab itself."""
        from app.constants import AccessKey

        manager_user.access_overrides = {AccessKey.EXPORT_BULK_DATA.value: True}
        db_session.commit()
        c = _client_as(db_session, manager_user)
        try:
            assert c.get(DATA_EXPORT_URL).status_code == 403
        finally:
            _drop_overrides(c)


class TestDataExportNavEntry:
    """The Settings nav only shows the "Data export" tab button in the admin-only block
    (mirrors System/Data Ops/Ops Group/Users)."""

    def test_nav_shows_data_export_for_admin(self, db_session, admin_user):
        c = _client_as(db_session, admin_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" in html
            assert "/v2/partials/settings/data-export" in html
        finally:
            _drop_overrides(c)

    def test_nav_hides_data_export_for_buyer(self, db_session, test_user):
        c = _client_as(db_session, test_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" not in html
        finally:
            _drop_overrides(c)
