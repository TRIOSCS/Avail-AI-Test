"""tests/test_settings_data_export.py — ISS-028 Settings "Data export" page gate.

Covers GET /v2/partials/settings/data-export: the tab route and the Settings nav
button are gated on the EXPORT_BULK_DATA capability — the SAME
`require_access(AccessKey.EXPORT_BULK_DATA)` mechanism the five bulk export routes
enforce — NOT on a strict admin-role check. Admin sees the five dataset-export links
(content asserted, not just status 200); roles without the capability are blocked;
a manager granted the documented per-user `access_overrides` escape hatch reaches
BOTH the tab route and the nav button (otherwise the override would pass the export
routes yet have no UI path to them).

Called by: pytest
Depends on: app.routers.htmx.settings (settings_data_export_tab, settings_partial),
    conftest fixtures (db_session, admin_user, manager_user, test_user)
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

DATA_EXPORT_URL = "/v2/partials/settings/data-export"


def _client_as(db_session: Session, user: User) -> TestClient:
    """A TestClient whose require_user resolves to *user* (the data-export tab's
    require_access(AccessKey.EXPORT_BULK_DATA) gate depends on require_user via Depends,
    so overriding require_user alone exercises it, same as the five bulk export
    routes)."""
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
    def test_roles_without_capability_403(self, db_session, role_fixture, request):
        """No interactive role holds EXPORT_BULK_DATA by default, so every non-admin
        without an explicit override is 403 — same denial matrix as the five bulk export
        routes."""
        user = request.getfixturevalue(role_fixture)
        c = _client_as(db_session, user)
        try:
            assert c.get(DATA_EXPORT_URL).status_code == 403
        finally:
            _drop_overrides(c)

    def test_manager_with_export_bulk_data_override_200_on_tab(self, db_session, manager_user):
        """A manager granted the documented per-user EXPORT_BULK_DATA override passes
        the five export routes, so the tab that links them must open too — the tab gate
        is the capability, not the admin role (ISS-028 escape hatch)."""
        from app.constants import AccessKey

        manager_user.access_overrides = {AccessKey.EXPORT_BULK_DATA.value: True}
        db_session.commit()
        c = _client_as(db_session, manager_user)
        try:
            resp = c.get(DATA_EXPORT_URL)
            assert resp.status_code == 200
            assert resp.text.count("Export CSV") == 5
        finally:
            _drop_overrides(c)


class TestDataExportNavEntry:
    """The Settings nav shows the "Data export" tab button iff the viewer holds the
    EXPORT_BULK_DATA capability (admins by default; non-admins via an explicit per-user
    override) — same predicate as the tab route, so the button is never a dead 403
    (mirrors the SET-06 Connectors pattern)."""

    def test_nav_shows_data_export_for_admin(self, db_session, admin_user):
        c = _client_as(db_session, admin_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" in html
            assert "/v2/partials/settings/data-export" in html
        finally:
            _drop_overrides(c)

    def test_nav_shows_data_export_for_manager_with_override(self, db_session, manager_user):
        from app.constants import AccessKey

        manager_user.access_overrides = {AccessKey.EXPORT_BULK_DATA.value: True}
        db_session.commit()
        c = _client_as(db_session, manager_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" in html
            assert "/v2/partials/settings/data-export" in html
        finally:
            _drop_overrides(c)

    def test_nav_hides_data_export_for_plain_manager(self, db_session, manager_user):
        c = _client_as(db_session, manager_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" not in html
        finally:
            _drop_overrides(c)

    def test_nav_hides_data_export_for_buyer(self, db_session, test_user):
        c = _client_as(db_session, test_user)
        try:
            html = c.get("/v2/partials/settings").text
            assert "Data export" not in html
        finally:
            _drop_overrides(c)
