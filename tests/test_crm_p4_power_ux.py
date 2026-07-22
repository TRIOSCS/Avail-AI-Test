"""test_crm_p4_power_ux.py — CRM P4 power-UX: contacts bulk actions, Saved Views,
filter-aware CSV export.

Covers:
- saved_views_service: create/list/delete, upsert-on-name, filter whitelist (drops
  unknown keys + "all" sentinels), invalid list_key, per-user delete scoping
- Saved-views routes: POST creates (round-trips via GET render), DELETE removes,
  another user cannot delete someone else's view
- Contacts bulk: archive/dnc applies to selected set; rep skips non-manageable
  contacts (DENY); invalid action 400; HX-Trigger showToast
- CSV export honors the CURRENT filtered view (companies: search/account_type;
  contacts: company_id/cadence_state)

Called by: pytest
Depends on: conftest.py (db_session, manager_user, sales_user),
    app.services.saved_views_service, app.routers.htmx.companies, app.routers.crm.export
"""

from __future__ import annotations

import csv
import io
import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import AccessKey
from app.models import Company, CustomerSite, SavedView, SiteContact, User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mgr_user(db_session: Session) -> User:
    """A manager-role user.

    ISS-028: EXPORT_BULK_DATA is admin-only by default (supersedes ISS-022's
    manager+admin default), so TestFilteredCsvExport (which exercises the export
    ROUTE's filter/header CONTENT, not the access gate itself) grants this user an
    explicit per-user override — mirrors tests/test_crm_csv_export.py's manager_client
    fixture.
    """
    u = User(
        email="p4.mgr@trioscs.com",
        name="P4 Mgr",
        role="manager",
        azure_id="p4-mgr",
        access_overrides={AccessKey.EXPORT_BULK_DATA.value: True},
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def rep_user(db_session: Session) -> User:
    u = User(
        email="p4.rep@trioscs.com",
        name="P4 Rep",
        role="sales",
        azure_id="p4-rep",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_user(db_session: Session) -> User:
    u = User(
        email="p4.other@trioscs.com",
        name="P4 Other",
        role="sales",
        azure_id="p4-other",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _company(db: Session, name: str, owner: User | None = None, account_type: str = "customer") -> Company:
    co = Company(
        name=name,
        domain=f"{name.lower().replace(' ', '')}.com",
        account_type=account_type,
        is_active=True,
        account_owner_id=owner.id if owner else None,
        created_at=datetime.now(UTC),
    )
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _contact(db: Session, company: Company, full_name: str, email: str) -> SiteContact:
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True, created_at=datetime.now(UTC))
    db.add(site)
    db.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name=full_name,
        email=email,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _client(db_session: Session, user: User):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _u():
        return user

    async def _ft():
        return "mock-token"

    overrides = {get_db: _db, require_user: _u, require_admin: _u, require_buyer: _u, require_fresh_token: _ft}
    for dep, fn in overrides.items():
        app.dependency_overrides[dep] = fn
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


def _parse_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# UNIT: saved_views_service
# ---------------------------------------------------------------------------


class TestSavedViewsService:
    def test_create_and_list(self, db_session: Session, rep_user: User):
        from app.services.saved_views_service import create_saved_view, list_saved_views

        view = create_saved_view(
            db_session, rep_user, "customers", "Overdue", {"staleness": "overdue", "search": "acme"}
        )
        assert view.id is not None
        assert view.filters == {"staleness": "overdue", "search": "acme"}
        listed = list_saved_views(db_session, rep_user, "customers")
        assert [v.name for v in listed] == ["Overdue"]

    def test_filter_whitelist_drops_unknown_and_sentinels(self, db_session: Session, rep_user: User):
        from app.services.saved_views_service import create_saved_view

        view = create_saved_view(
            db_session,
            rep_user,
            "customers",
            "V1",
            {"search": "x", "offset": "50", "limit": "50", "segment": "0", "my_only": "1", "bogus": "1"},
        )
        # offset/limit/bogus not whitelisted; segment "0" is an "all" sentinel → dropped.
        assert view.filters == {"search": "x", "my_only": "1"}

    def test_upsert_overwrites_same_name(self, db_session: Session, rep_user: User):
        from app.services.saved_views_service import create_saved_view, list_saved_views

        create_saved_view(db_session, rep_user, "customers", "Dup", {"search": "a"})
        create_saved_view(db_session, rep_user, "customers", "Dup", {"search": "b"})
        listed = list_saved_views(db_session, rep_user, "customers")
        assert len(listed) == 1
        assert listed[0].filters == {"search": "b"}

    def test_invalid_list_key_raises(self, db_session: Session, rep_user: User):
        from app.services.saved_views_service import create_saved_view

        with pytest.raises(ValueError):
            create_saved_view(db_session, rep_user, "bogus", "X", {})

    def test_blank_name_raises(self, db_session: Session, rep_user: User):
        from app.services.saved_views_service import create_saved_view

        with pytest.raises(ValueError):
            create_saved_view(db_session, rep_user, "customers", "   ", {})

    def test_delete_is_user_scoped(self, db_session: Session, rep_user: User, other_user: User):
        from app.services.saved_views_service import create_saved_view, delete_saved_view

        view = create_saved_view(db_session, rep_user, "contacts", "Mine", {"cadence_state": "overdue"})
        # Other user cannot delete it.
        assert delete_saved_view(db_session, other_user, view.id) is False
        assert db_session.get(SavedView, view.id) is not None
        # Owner can.
        assert delete_saved_view(db_session, rep_user, view.id) is True
        assert db_session.get(SavedView, view.id) is None

    def test_list_scoped_by_user_and_key(self, db_session: Session, rep_user: User, other_user: User):
        from app.services.saved_views_service import create_saved_view, list_saved_views

        create_saved_view(db_session, rep_user, "customers", "C1", {"search": "a"})
        create_saved_view(db_session, rep_user, "contacts", "K1", {"search": "b"})
        create_saved_view(db_session, other_user, "customers", "O1", {"search": "c"})
        assert {v.name for v in list_saved_views(db_session, rep_user, "customers")} == {"C1"}
        assert {v.name for v in list_saved_views(db_session, rep_user, "contacts")} == {"K1"}


# ---------------------------------------------------------------------------
# ROUTES: saved-views create / list / delete
# ---------------------------------------------------------------------------


class TestSavedViewsRoutes:
    def test_post_creates_and_get_renders_chip(self, db_session: Session, mgr_user: User):
        for c in _client(db_session, mgr_user):
            resp = c.post(
                "/v2/partials/customers/saved-views",
                data={"list_key": "customers", "name": "Big Accounts", "search": "acme", "offset": "10"},
            )
            assert resp.status_code == 200
            assert "Big Accounts" in resp.text

        rows = db_session.query(SavedView).filter(SavedView.user_id == mgr_user.id).all()
        assert len(rows) == 1
        # offset is not a whitelisted filter key — dropped.
        assert rows[0].filters == {"search": "acme"}

        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/partials/customers/saved-views?list_key=customers")
            assert resp.status_code == 200
            assert "Big Accounts" in resp.text

    def test_delete_removes_view(self, db_session: Session, mgr_user: User):
        from app.services.saved_views_service import create_saved_view

        view = create_saved_view(db_session, mgr_user, "customers", "Temp", {"search": "z"})
        for c in _client(db_session, mgr_user):
            resp = c.delete(f"/v2/partials/customers/saved-views/{view.id}?list_key=customers")
            assert resp.status_code == 200
            assert "Temp" not in resp.text
        assert db_session.get(SavedView, view.id) is None

    def test_user_cannot_delete_another_users_view(self, db_session: Session, mgr_user: User, rep_user: User):
        from app.services.saved_views_service import create_saved_view

        view = create_saved_view(db_session, rep_user, "customers", "RepView", {"search": "z"})
        for c in _client(db_session, mgr_user):
            resp = c.delete(f"/v2/partials/customers/saved-views/{view.id}?list_key=customers")
            assert resp.status_code == 200
        # Still present — delete was a no-op for the non-owner.
        assert db_session.get(SavedView, view.id) is not None

    def test_invalid_list_key_400(self, db_session: Session, mgr_user: User):
        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/partials/customers/saved-views?list_key=bogus")
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# ROUTES: contacts bulk actions
# ---------------------------------------------------------------------------


class TestContactsBulkActions:
    def test_archive_applies_to_selected(self, db_session: Session, mgr_user: User):
        co = _company(db_session, "Bulk Co", owner=mgr_user)
        c1 = _contact(db_session, co, "Alice", "alice@bulk.com")
        c2 = _contact(db_session, co, "Bob", "bob@bulk.com")
        for c in _client(db_session, mgr_user):
            resp = c.post("/v2/partials/contacts/bulk/archive", data={"ids": f"{c1.id},{c2.id}"})
            assert resp.status_code == 200
            assert resp.headers.get("HX-Trigger")
            assert "showToast" in resp.headers["HX-Trigger"]
        db_session.expire_all()
        assert db_session.get(SiteContact, c1.id).is_archived is True
        assert db_session.get(SiteContact, c2.id).is_archived is True

    def test_dnc_applies_to_selected(self, db_session: Session, mgr_user: User):
        co = _company(db_session, "DNC Co", owner=mgr_user)
        c1 = _contact(db_session, co, "Carol", "carol@dnc.com")
        for c in _client(db_session, mgr_user):
            resp = c.post("/v2/partials/contacts/bulk/dnc", data={"ids": str(c1.id)})
            assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(SiteContact, c1.id).do_not_contact is True

    def test_rep_skips_non_manageable_contacts(self, db_session: Session, rep_user: User, other_user: User):
        owned = _company(db_session, "Rep Owned", owner=rep_user)
        unowned = _company(db_session, "Not Rep", owner=other_user)
        mine = _contact(db_session, owned, "Mine", "mine@owned.com")
        theirs = _contact(db_session, unowned, "Theirs", "theirs@notrep.com")
        for c in _client(db_session, rep_user):
            resp = c.post("/v2/partials/contacts/bulk/archive", data={"ids": f"{mine.id},{theirs.id}"})
            assert resp.status_code == 200
        db_session.expire_all()
        # Rep archived their own contact only; the other is untouched (DENY).
        assert db_session.get(SiteContact, mine.id).is_archived is True
        assert db_session.get(SiteContact, theirs.id).is_archived is False

    def test_invalid_action_400(self, db_session: Session, mgr_user: User):
        for c in _client(db_session, mgr_user):
            resp = c.post("/v2/partials/contacts/bulk/explode", data={"ids": "1"})
            assert resp.status_code == 400

    def test_empty_ids_is_noop_200(self, db_session: Session, mgr_user: User):
        for c in _client(db_session, mgr_user):
            resp = c.post("/v2/partials/contacts/bulk/archive", data={"ids": ""})
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# ROUTES: filter-aware CSV export
# ---------------------------------------------------------------------------


class TestFilteredCsvExport:
    def test_companies_export_honors_account_type(self, db_session: Session, mgr_user: User):
        # CDM_ACCOUNT_TYPES are capitalized ("Customer", "Prospect", ...); a value
        # outside that set is ignored (no filter) — so use the canonical labels.
        _company(db_session, "Acme Customer", owner=mgr_user, account_type="Customer")
        _company(db_session, "Beta Prospect", owner=mgr_user, account_type="Prospect")
        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/customers/export.csv?account_type=Customer")
            assert resp.status_code == 200
            names = {r["name"] for r in _parse_csv(resp.text)}
            assert "Acme Customer" in names
            assert "Beta Prospect" not in names

    def test_companies_export_honors_search(self, db_session: Session, mgr_user: User):
        _company(db_session, "Findme Corp", owner=mgr_user)
        _company(db_session, "Hidden Corp", owner=mgr_user)
        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/customers/export.csv?search=Findme")
            names = {r["name"] for r in _parse_csv(resp.text)}
            assert "Findme Corp" in names
            assert "Hidden Corp" not in names

    def test_companies_export_unfiltered_returns_all(self, db_session: Session, mgr_user: User):
        _company(db_session, "One Co", owner=mgr_user)
        _company(db_session, "Two Co", owner=mgr_user)
        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/customers/export.csv")
            names = {r["name"] for r in _parse_csv(resp.text)}
            assert {"One Co", "Two Co"} <= names

    def test_contacts_export_honors_company_id(self, db_session: Session, mgr_user: User):
        co_a = _company(db_session, "Alpha Co", owner=mgr_user)
        co_b = _company(db_session, "Bravo Co", owner=mgr_user)
        _contact(db_session, co_a, "Anna Alpha", "anna@alpha.com")
        _contact(db_session, co_b, "Ben Bravo", "ben@bravo.com")
        for c in _client(db_session, mgr_user):
            resp = c.get(f"/v2/customers/contacts/export.csv?company_id={co_a.id}")
            names = {r["full_name"] for r in _parse_csv(resp.text)}
            assert "Anna Alpha" in names
            assert "Ben Bravo" not in names

    def test_contacts_export_headers_unchanged(self, db_session: Session, mgr_user: User):
        co = _company(db_session, "Hdr Co", owner=mgr_user)
        _contact(db_session, co, "Head Er", "head@hdr.com")
        for c in _client(db_session, mgr_user):
            resp = c.get("/v2/customers/contacts/export.csv")
            rows = _parse_csv(resp.text)
            assert set(rows[0].keys()) == {
                "full_name",
                "title",
                "email",
                "phone",
                "contact_role",
                "company_name",
                "site_name",
                "is_primary",
            }
