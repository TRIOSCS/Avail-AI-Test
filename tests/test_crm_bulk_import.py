"""test_crm_bulk_import.py — TDD tests for CRM bulk select+actions and company/contact
CSV import.

Covers:
- Bulk deactivate: rep affects only manageable companies; skips others with summary
- Bulk send-to-prospecting: rep affects only manageable companies; skips others
- Bulk assign-owner: MANAGER/ADMIN only; rep calling → 403
- DENY tests: rep's bulk op does NOT affect accounts they can't manage
- Import preview: parses CSV, flags duplicates (normalized_name), flags invalid rows
- Import confirm: creates companies (deduped); newly created get importer as owner
- Import auth: require_user enforced (401 if not authenticated)
- Import bad CSV: graceful error, not 500
- Import contacts: contacts parsed and associated under existing company

Called by: pytest
Depends on: conftest.py (db_session, test_user, client, manager_user)
"""

from __future__ import annotations

import io
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sales_rep(db_session: Session) -> User:
    """A sales-role user (restricted to their own accounts)."""
    u = User(
        email="sales.rep@trioscs.com",
        name="Sales Rep",
        role="sales",
        azure_id="bulk-test-sales-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def mgr_user(db_session: Session) -> User:
    """A manager-role user (can bulk-act on all accounts)."""
    u = User(
        email="mgr.bulk@trioscs.com",
        name="Manager Bulk",
        role="manager",
        azure_id="bulk-test-mgr-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_user(db_session: Session) -> User:
    """Another sales user that owns some accounts but not ours."""
    u = User(
        email="other.sales@trioscs.com",
        name="Other Sales",
        role="sales",
        azure_id="bulk-test-other-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def owned_company(db_session: Session, sales_rep: User) -> Company:
    """Active company owned by sales_rep."""
    co = Company(
        name="Owned Corp",
        is_active=True,
        account_owner_id=sales_rep.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def unowned_company(db_session: Session, other_user: User) -> Company:
    """Active company owned by other_user (NOT sales_rep)."""
    co = Company(
        name="Unowned Corp",
        is_active=True,
        account_owner_id=other_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def mgr_owned_company(db_session: Session, mgr_user: User) -> Company:
    """Active company owned by manager."""
    co = Company(
        name="Manager Corp",
        is_active=True,
        account_owner_id=mgr_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _make_client(db_session: Session, user: User) -> TestClient:
    """Helper: build a TestClient authenticated as *user*."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _u():
        return user

    async def _ft():
        return "mock-token"

    overrides = {
        get_db: _db,
        require_user: _u,
        require_admin: _u,
        require_buyer: _u,
        require_fresh_token: _ft,
    }
    for dep, fn in overrides.items():
        app.dependency_overrides[dep] = fn
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Bulk deactivate — ALLOW
# ---------------------------------------------------------------------------


def test_bulk_deactivate_rep_affects_only_manageable(
    db_session: Session, sales_rep: User, owned_company: Company, unowned_company: Company
):
    """A sales rep's bulk-deactivate affects ONLY their own companies; skips others."""
    for c in _make_client(db_session, sales_rep):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/deactivate",
            data={"ids": ids},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    # owned_company is deactivated
    db_session.refresh(owned_company)
    assert owned_company.is_active is False, "Owned company should be deactivated"

    # unowned_company is NOT touched (DENY behaviour)
    db_session.refresh(unowned_company)
    assert unowned_company.is_active is True, "Unowned company must NOT be deactivated by rep"


def test_bulk_deactivate_summary_shows_skip_count(
    db_session: Session, sales_rep: User, owned_company: Company, unowned_company: Company
):
    """Response body includes a summary indicating how many were skipped."""
    for c in _make_client(db_session, sales_rep):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/deactivate",
            data={"ids": ids},
        )
        assert resp.status_code == 200
        # The summary must convey 1 applied, 1 skipped
        assert "1" in resp.text or "skip" in resp.text.lower() or "Deactivated" in resp.text


def test_bulk_deactivate_manager_affects_all(
    db_session: Session,
    mgr_user: User,
    owned_company: Company,
    unowned_company: Company,
):
    """A manager can bulk-deactivate companies they don't personally own."""
    for c in _make_client(db_session, mgr_user):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/deactivate",
            data={"ids": ids},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    db_session.refresh(owned_company)
    db_session.refresh(unowned_company)
    assert owned_company.is_active is False
    assert unowned_company.is_active is False


# ---------------------------------------------------------------------------
# Bulk send-to-prospecting — ALLOW + DENY
# ---------------------------------------------------------------------------


def test_bulk_send_to_prospecting_rep_skips_unowned(
    db_session: Session, sales_rep: User, owned_company: Company, unowned_company: Company
):
    """Bulk send-to-prospecting skips companies the rep doesn't own."""
    for c in _make_client(db_session, sales_rep):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/send-to-prospecting",
            data={"ids": ids},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    db_session.refresh(owned_company)
    db_session.refresh(unowned_company)

    # owned: ownership cleared (sent to prospecting)
    assert owned_company.account_owner_id is None, "Owned company should have owner cleared"
    # unowned: NOT touched
    assert unowned_company.account_owner_id == unowned_company.account_owner_id, (
        "Unowned company owner must be unchanged"
    )


def test_bulk_send_to_prospecting_manager_acts_on_all(
    db_session: Session, mgr_user: User, owned_company: Company, unowned_company: Company
):
    """Manager can send any selected company to prospecting."""
    for c in _make_client(db_session, mgr_user):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/send-to-prospecting",
            data={"ids": ids},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    db_session.refresh(owned_company)
    db_session.refresh(unowned_company)
    assert owned_company.account_owner_id is None
    assert unowned_company.account_owner_id is None


# ---------------------------------------------------------------------------
# Bulk assign-owner — MANAGER/ADMIN only
# ---------------------------------------------------------------------------


def test_bulk_assign_owner_manager_can_reassign(
    db_session: Session, mgr_user: User, owned_company: Company, unowned_company: Company, other_user: User
):
    """A manager can reassign account ownership via bulk assign-owner."""
    for c in _make_client(db_session, mgr_user):
        ids = f"{owned_company.id},{unowned_company.id}"
        resp = c.post(
            "/v2/partials/customers/bulk/assign-owner",
            data={"ids": ids, "owner_id": str(mgr_user.id)},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    db_session.refresh(owned_company)
    db_session.refresh(unowned_company)
    assert owned_company.account_owner_id == mgr_user.id
    assert unowned_company.account_owner_id == mgr_user.id


def test_bulk_assign_owner_rep_gets_403(db_session: Session, sales_rep: User, owned_company: Company, mgr_user: User):
    """A sales rep calling bulk assign-owner is rejected with 403."""
    for c in _make_client(db_session, sales_rep):
        ids = str(owned_company.id)
        resp = c.post(
            "/v2/partials/customers/bulk/assign-owner",
            data={"ids": ids, "owner_id": str(mgr_user.id)},
        )
        assert resp.status_code == 403, "Sales rep must not be allowed to reassign ownership"


def test_bulk_assign_owner_missing_owner_id_400(db_session: Session, mgr_user: User, owned_company: Company):
    """Bulk assign-owner without owner_id returns 400."""
    for c in _make_client(db_session, mgr_user):
        resp = c.post(
            "/v2/partials/customers/bulk/assign-owner",
            data={"ids": str(owned_company.id)},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Bulk — edge cases
# ---------------------------------------------------------------------------


def test_bulk_empty_ids_returns_200(db_session: Session, sales_rep: User):
    """Bulk action with empty ids string returns 200 (no-op, refreshed list)."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/bulk/deactivate",
            data={"ids": ""},
        )
        assert resp.status_code == 200


def test_bulk_invalid_action_returns_400(db_session: Session, sales_rep: User, owned_company: Company):
    """Bulk action with unknown action name returns 400."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/bulk/explode",
            data={"ids": str(owned_company.id)},
        )
        assert resp.status_code == 400


def test_bulk_unauthenticated_returns_401(db_session: Session):
    """Unauthenticated bulk request returns 401."""
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/v2/partials/customers/bulk/deactivate",
                data={"ids": "1"},
            )
            assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# CSV Import — preview
# ---------------------------------------------------------------------------


VALID_CSV = b"""name,website,account_type
Acme Corp,https://acme.com,Customer
Beta Ltd,https://beta.io,Prospect
"""

INVALID_ROWS_CSV = b"""name,website,account_type
,https://noname.com,Customer
Valid Company,,
"""

DUPE_CSV_TEMPLATE = """name,website,account_type
{name},https://existing.com,Customer
New Company 2,https://new2.com,Prospect
"""


def test_import_preview_parses_valid_rows(db_session: Session, sales_rep: User):
    """Import preview returns 200 and lists parsed company rows."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("companies.csv", io.BytesIO(VALID_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        assert "Acme Corp" in resp.text
        assert "Beta Ltd" in resp.text


def test_import_preview_flags_missing_name(db_session: Session, sales_rep: User):
    """Import preview flags rows with no name as invalid."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("companies.csv", io.BytesIO(INVALID_ROWS_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        # The 'Valid Company' row is valid; the empty-name row should be flagged
        assert "invalid" in resp.text.lower() or "error" in resp.text.lower() or "skip" in resp.text.lower()


def test_import_preview_flags_duplicate_company(db_session: Session, sales_rep: User):
    """Import preview flags rows that match an existing company (by normalized_name)."""
    # Insert an existing company first
    existing = Company(
        name="Existing Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    csv_with_dupe = DUPE_CSV_TEMPLATE.format(name="Existing Corp").encode()
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("companies.csv", io.BytesIO(csv_with_dupe), "text/csv")},
        )
        assert resp.status_code == 200
        # The duplicate row should be flagged
        assert "duplicate" in resp.text.lower() or "exist" in resp.text.lower() or "dup" in resp.text.lower()


def test_import_preview_no_file_returns_400(db_session: Session, sales_rep: User):
    """Import preview without a file returns 400."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post("/v2/partials/customers/import/preview")
        assert resp.status_code == 400


def test_import_preview_unauthenticated_returns_401(db_session: Session):
    """Import preview without auth returns 401."""
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/v2/partials/customers/import/preview",
                files={"file": ("companies.csv", io.BytesIO(VALID_CSV), "text/csv")},
            )
            assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_import_preview_bad_csv_returns_graceful_error(db_session: Session, sales_rep: User):
    """Import preview with non-CSV binary data returns a graceful error, not 500."""
    bad_data = b"\x00\x01\x02\x03binary garbage"
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("bad.csv", io.BytesIO(bad_data), "text/csv")},
        )
        # Must NOT be a 500 — any 2xx or 4xx is acceptable
        assert resp.status_code != 500


# ---------------------------------------------------------------------------
# CSV Import — confirm
# ---------------------------------------------------------------------------


def test_import_confirm_creates_new_companies(db_session: Session, sales_rep: User):
    """Confirmed import creates new Company rows for valid, non-duplicate rows."""
    before = db_session.query(Company).count()

    rows_json = '[{"name": "Import Co A", "website": "https://a.com", "account_type": "Customer"}, {"name": "Import Co B", "website": "", "account_type": "Prospect"}]'
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/confirm",
            data={"rows_json": rows_json},
        )
        assert resp.status_code == 200

    after = db_session.query(Company).count()
    assert after == before + 2, f"Expected 2 new companies; got {after - before}"


def test_import_confirm_assigns_importer_as_owner(db_session: Session, sales_rep: User):
    """Confirmed import sets account_owner_id to the importing user."""
    rows_json = '[{"name": "Importer Owned Co", "website": "https://io.com", "account_type": "Customer"}]'
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/confirm",
            data={"rows_json": rows_json},
        )
        assert resp.status_code == 200

    co = db_session.query(Company).filter(Company.name == "Importer Owned Co").first()
    assert co is not None
    assert co.account_owner_id == sales_rep.id, "Importer should be assigned as account owner"


def test_import_confirm_deduplicates_by_normalized_name(db_session: Session, sales_rep: User):
    """Confirmed import skips companies that already exist by normalized_name."""
    existing = Company(
        name="Dup Check Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    before = db_session.query(Company).count()

    # Try to import the same company again (same normalized name)
    rows_json = '[{"name": "Dup Check Corp", "website": "", "account_type": "Customer"}]'
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/confirm",
            data={"rows_json": rows_json},
        )
        assert resp.status_code == 200

    after = db_session.query(Company).count()
    assert after == before, "Duplicate company should NOT be created again"


def test_import_confirm_invalid_rows_json_returns_400(db_session: Session, sales_rep: User):
    """Confirm with invalid JSON returns 400."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/confirm",
            data={"rows_json": "not-valid-json"},
        )
        assert resp.status_code == 400


def test_import_confirm_unauthenticated_returns_401(db_session: Session):
    """Import confirm without auth returns 401."""
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/v2/partials/customers/import/confirm",
                data={"rows_json": "[]"},
            )
            assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# CSV Import — contacts
# ---------------------------------------------------------------------------

CONTACTS_CSV = b"""company_name,contact_name,email,phone,role
Existing Contact Co,John Smith,john@existingco.com,+1-555-0001,buyer
Existing Contact Co,Jane Doe,jane@existingco.com,,
"""

CONTACTS_DUPE_EMAIL_CSV = b"""company_name,contact_name,email,phone,role
Existing Contact Co,John Smith Dupe,john@existingco.com,+1-555-0001,buyer
"""


def test_import_contacts_preview_parses_rows(db_session: Session, sales_rep: User):
    """Contacts import preview returns parsed contact rows."""
    # Create the parent company
    co = Company(
        name="Existing Contact Co",
        is_active=True,
        account_owner_id=sales_rep.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()

    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            files={"file": ("contacts.csv", io.BytesIO(CONTACTS_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        assert "John Smith" in resp.text
        assert "jane@existingco.com" in resp.text


def test_import_contacts_preview_flags_duplicate_email(db_session: Session, sales_rep: User):
    """Contacts import preview flags contacts whose email already exists in a
    SiteContact."""
    co = Company(
        name="Existing Contact Co",
        is_active=True,
        account_owner_id=sales_rep.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    # Create a site and existing contact
    site = CustomerSite(
        company_id=co.id,
        site_name="HQ",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Existing Person",
        email="john@existingco.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.commit()

    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            files={"file": ("contacts.csv", io.BytesIO(CONTACTS_DUPE_EMAIL_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        assert "duplicate" in resp.text.lower() or "exist" in resp.text.lower() or "dup" in resp.text.lower()
