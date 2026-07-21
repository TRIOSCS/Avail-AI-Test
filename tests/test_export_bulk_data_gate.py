"""tests/test_export_bulk_data_gate.py — ISS-022 role-matrix guard for bulk dataset
export routes.

Bulk dataset exports (companies, contacts, vendors, requisitions, sightings) must be
manager/admin only (AccessKey.EXPORT_BULK_DATA). Quote-builder single-deal Excel/PDF
exports stay open to sales via the unaffected AccessKey.EXPORT_DATA gate.

Called by: pytest
Depends on: app.constants (AccessKey), app.dependencies (require_access, require_user),
    conftest fixtures (db_session, test_user, sales_user, trader_user, manager_user,
    admin_user, test_requisition, test_customer_site)
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.constants import AccessKey
from app.models import Quote, Requisition


def _client_as(db_session, user):
    """A TestClient whose require_user resolves to *user* (require_access depends on
    require_user via Depends, so the override flows straight into the gate)."""
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


@pytest.fixture()
def _role_users(test_user, sales_user, trader_user, manager_user, admin_user):
    """Maps role name -> user fixture for parametrized role-matrix tests."""
    return {
        "buyer": test_user,
        "sales": sales_user,
        "trader": trader_user,
        "manager": manager_user,
        "admin": admin_user,
    }


_DENIED_ROLES = ("buyer", "sales", "trader")
_ALLOWED_ROLES = ("manager", "admin")


def _assert_csv_export(resp, filename: str, first_col: str):
    """A granted export must stream a real CSV attachment, not merely answer 200 —
    assert the download headers and that the header row leads with the expected
    column."""
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert filename in resp.headers["content-disposition"]
    assert resp.text.splitlines()[0].split(",")[0].strip('"') == first_col


class TestCompaniesExportGate:
    @pytest.mark.parametrize("role", _DENIED_ROLES)
    def test_companies_csv_403_for_non_manager(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            assert c.get("/v2/customers/export.csv").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_companies_csv_200_for_manager_admin(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            _assert_csv_export(c.get("/v2/customers/export.csv"), "customers.csv", "name")
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _DENIED_ROLES)
    def test_contacts_csv_403_for_non_manager(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            assert c.get("/v2/customers/contacts/export.csv").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_contacts_csv_200_for_manager_admin(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            _assert_csv_export(
                c.get("/v2/customers/contacts/export.csv"), "contacts.csv", "full_name"
            )
        finally:
            _drop_overrides(c)


class TestVendorsExportGate:
    @pytest.mark.parametrize("role", _DENIED_ROLES)
    def test_403_for_non_manager(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            assert c.get("/v2/partials/vendors/export").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_200_for_manager_admin(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            _assert_csv_export(c.get("/v2/partials/vendors/export"), "vendors_export.csv", "Vendor")
        finally:
            _drop_overrides(c)


class TestRequisitionsExportGate:
    @pytest.mark.parametrize("role", _DENIED_ROLES)
    def test_403_for_non_manager(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            assert c.get("/v2/partials/requisitions/export").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_200_for_manager_admin(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            _assert_csv_export(
                c.get("/v2/partials/requisitions/export"), "requisitions_export.csv", "Name"
            )
        finally:
            _drop_overrides(c)


class TestSightingsExportGate:
    @pytest.mark.parametrize("role", _DENIED_ROLES)
    def test_403_for_non_manager(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            assert c.get("/v2/sightings/export").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    def test_200_for_manager_admin(self, db_session, _role_users, role):
        c = _client_as(db_session, _role_users[role])
        try:
            _assert_csv_export(
                c.get("/v2/sightings/export"), "sightings_export.csv", "Requirement ID"
            )
        finally:
            _drop_overrides(c)


class TestManagerAccessDefault:
    def test_manager_has_export_bulk_data_by_default(self, manager_user):
        from app.dependencies import user_has_access

        assert user_has_access(manager_user, AccessKey.EXPORT_BULK_DATA) is True

    def test_buyer_sales_trader_lack_export_bulk_data_by_default(self, test_user, sales_user, trader_user):
        from app.dependencies import user_has_access

        for user in (test_user, sales_user, trader_user):
            assert user_has_access(user, AccessKey.EXPORT_BULK_DATA) is False, user.role


class TestCanExportBulkDataJinjaGlobal:
    """can_export_bulk_data — the Jinja global the list toolbars call directly — must
    mirror user_has_access for real users AND degrade to False (never raise) for a non-
    User stand-in, so template-compilation smoke tests rendering with a bare/fake
    context never blow up."""

    def test_true_for_manager(self, manager_user):
        from app.dependencies import can_export_bulk_data

        assert can_export_bulk_data(manager_user) is True

    def test_false_for_buyer(self, test_user):
        from app.dependencies import can_export_bulk_data

        assert can_export_bulk_data(test_user) is False

    def test_false_for_none(self):
        from app.dependencies import can_export_bulk_data

        assert can_export_bulk_data(None) is False

    def test_false_for_non_user_stand_in_object(self):
        from app.dependencies import can_export_bulk_data

        stub = SimpleNamespace(id=1, role="admin")  # not an app.models.User instance
        assert can_export_bulk_data(stub) is False


class TestQuoteBuilderExportUnaffected:
    """Quote-builder Excel/PDF exports stay on EXPORT_DATA (open to sales) — ISS-022
    only splits off the five BULK dataset exports, not single-deal quote docs."""

    @pytest.fixture()
    def sales_owned_quote(self, db_session, sales_user, test_customer_site):
        req = Requisition(
            name="REQ-SALES-EXPORT-001",
            customer_name="Acme Electronics",
            status="open",
            created_by=sales_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="TEST-Q-SALES-0001",
            status="sent",
            line_items=[],
            subtotal=1000.00,
            total_cost=500.00,
            total_margin_pct=50.00,
            created_by_id=sales_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(quote)
        db_session.commit()
        db_session.refresh(quote)
        return quote

    def test_export_excel_200_for_sales(self, db_session, sales_user, sales_owned_quote):
        from unittest.mock import patch

        c = _client_as(db_session, sales_user)
        try:
            with patch(
                "app.services.quote_builder_service.build_excel_export",
                return_value=b"fake-xlsx-content",
            ):
                r = c.get(
                    f"/v2/partials/quote-builder/{sales_owned_quote.requisition_id}/export/excel",
                    params={"quote_id": sales_owned_quote.id},
                )
            assert r.status_code == 200
            assert "spreadsheetml" in r.headers["content-type"]
        finally:
            _drop_overrides(c)
