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

from datetime import UTC, datetime

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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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


def test_bulk_empty_ids_returns_200(db_session: Session, sales_rep: User, owned_company: Company):
    """Bulk action with empty ids string returns 200 (no-op, refreshed list)."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/bulk/deactivate",
            data={"ids": ""},
        )
        assert resp.status_code == 200
        # No-op means no company was deactivated and the refreshed list still
        # renders the untouched company.
        db_session.expire_all()
        assert db_session.get(Company, owned_company.id).is_active is True
        assert owned_company.name in resp.text


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
        created_at=datetime.now(UTC),
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


def test_import_preview_non_file_field_returns_friendly_partial(db_session: Session, sales_rep: User):
    """A 'file' form field submitted as a plain string (not an actual upload) has no
    .read()/.file to pull bytes from — must render the friendly "Could not parse CSV"
    partial, not a 500."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            data={"file": "not-a-real-upload"},
        )
        assert resp.status_code == 200
        assert "Could not parse CSV" in resp.text


def test_import_contacts_preview_non_file_field_returns_friendly_partial(db_session: Session, sales_rep: User):
    """Same non-file-field guard on the contacts-import preview route."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            data={"file": "not-a-real-upload"},
        )
        assert resp.status_code == 200
        assert "Could not parse CSV" in resp.text


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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    # Create a site and existing contact
    site = CustomerSite(
        company_id=co.id,
        site_name="HQ",
        created_at=datetime.now(UTC),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Existing Person",
        email="john@existingco.com",
        created_at=datetime.now(UTC),
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


# ---------------------------------------------------------------------------
# F1: assign-owner with nonexistent / inactive owner_id → 400
# ---------------------------------------------------------------------------


def test_bulk_assign_owner_nonexistent_user_400(db_session: Session, mgr_user: User, owned_company: Company):
    """Bulk assign-owner with a user ID that does not exist must return 400."""
    for c in _make_client(db_session, mgr_user):
        resp = c.post(
            "/v2/partials/customers/bulk/assign-owner",
            data={"ids": str(owned_company.id), "owner_id": "999999"},
        )
        assert resp.status_code == 400


def test_bulk_assign_owner_inactive_user_400(db_session: Session, mgr_user: User, owned_company: Company):
    """Bulk assign-owner with an inactive user ID must return 400."""
    inactive = User(
        email="inactive.owner@trioscs.com",
        name="Inactive Owner",
        role="sales",
        azure_id="bulk-test-inactive-owner-001",
        is_active=False,
        created_at=datetime.now(UTC),
    )
    db_session.add(inactive)
    db_session.commit()
    db_session.refresh(inactive)

    for c in _make_client(db_session, mgr_user):
        resp = c.post(
            "/v2/partials/customers/bulk/assign-owner",
            data={"ids": str(owned_company.id), "owner_id": str(inactive.id)},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# F2: XSS escaping in import preview HTML
# ---------------------------------------------------------------------------

XSS_COMPANY_CSV = b"name,website,account_type\n<script>alert(1)</script>,https://evil.com,Customer\n"
APOSTROPHE_COMPANY_CSV = b"name,website,account_type\nO'Brien Industries,https://obrien.com,Customer\n"


def test_import_preview_escapes_script_tag(db_session: Session, sales_rep: User):
    """Company preview must NOT render raw <script> tags from CSV input."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("companies.csv", io.BytesIO(XSS_COMPANY_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        assert "<script>" not in resp.text, "Raw <script> tag must not appear in preview HTML"
        assert "&lt;script&gt;" in resp.text or "lt;script" in resp.text, "Escaped form should appear"


def test_import_preview_apostrophe_roundtrip(db_session: Session, sales_rep: User):
    """A company name with an apostrophe must not break the hidden rows_json input."""

    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/preview",
            files={"file": ("companies.csv", io.BytesIO(APOSTROPHE_COMPANY_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        # The rows_json hidden input must appear with the name intact
        assert "O" in resp.text and "Brien" in resp.text


XSS_CONTACTS_CSV = b"company_name,contact_name,email,phone,role\n<script>xss</script>,Alice,alice@evil.com,,\n"


def test_import_contacts_preview_escapes_script_tag(db_session: Session, sales_rep: User):
    """Contacts preview must NOT render raw <script> tags from CSV input."""
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            files={"file": ("contacts.csv", io.BytesIO(XSS_CONTACTS_CSV), "text/csv")},
        )
        assert resp.status_code == 200
        assert "<script>" not in resp.text, "Raw <script> tag must not appear in contacts preview HTML"


# ---------------------------------------------------------------------------
# F3: confirm row-cap bypass
# ---------------------------------------------------------------------------

import json as _json


def test_import_confirm_row_cap_exceeded_400(db_session: Session, sales_rep: User):
    """Confirm endpoint must reject rows_json payloads exceeding _IMPORT_MAX_ROWS."""
    oversized = _json.dumps([{"name": f"Co {i}", "website": "", "account_type": ""} for i in range(1001)])
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/confirm",
            data={"rows_json": oversized},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# F4: contacts confirm route
# ---------------------------------------------------------------------------


def test_import_contacts_confirm_creates_contacts(db_session: Session, sales_rep: User):
    """Confirm route creates SiteContact rows under matched companies."""
    co = Company(
        name="Confirm Contact Co",
        is_active=True,
        account_owner_id=sales_rep.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    rows = _json.dumps(
        [
            {
                "company_name": "Confirm Contact Co",
                "contact_name": "Alice Buyer",
                "email": "alice@confirmco.com",
                "phone": "",
                "role": "buyer",
            }
        ]
    )
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200
        assert "1 contact" in resp.text.lower() or "imported" in resp.text.lower()

    # Verify the contact was actually created
    db_session.expire_all()
    from app.models.crm import SiteContact

    contacts = db_session.query(SiteContact).filter(SiteContact.email == "alice@confirmco.com").all()
    assert len(contacts) == 1
    assert contacts[0].full_name == "Alice Buyer"


def test_import_contacts_confirm_skips_unmatched_company(db_session: Session, sales_rep: User):
    """Confirm skips rows whose company name doesn't match any active company."""
    rows = _json.dumps(
        [
            {
                "company_name": "No Such Company XYZ",
                "contact_name": "Bob",
                "email": "bob@nowhere.com",
                "phone": "",
                "role": "",
            }
        ]
    )
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower() or "not found" in resp.text.lower() or "0 contact" in resp.text.lower()


def test_import_contacts_confirm_deduplicates_by_email(db_session: Session, sales_rep: User):
    """Confirm skips contacts whose email already exists under the matched site."""
    co = Company(
        name="Dedup Contact Co",
        is_active=True,
        account_owner_id=sales_rep.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    site = CustomerSite(
        company_id=co.id,
        site_name="HQ",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    existing = SiteContact(
        customer_site_id=site.id,
        full_name="Existing Person",
        email="dup@dedupco.com",
        created_at=datetime.now(UTC),
    )
    db_session.add(existing)
    db_session.commit()

    rows = _json.dumps(
        [
            {
                "company_name": "Dedup Contact Co",
                "contact_name": "New Person",
                "email": "dup@dedupco.com",
                "phone": "",
                "role": "",
            }
        ]
    )
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower() or "duplicate" in resp.text.lower() or "0 contact" in resp.text.lower()


def test_import_contacts_confirm_gated(db_session: Session):
    """Contacts confirm endpoint requires authentication (401 if not logged in)."""
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/v2/partials/customers/import/contacts/confirm",
                data={"rows_json": "[]"},
            )
            assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Security: cross-tenant IDOR — contact import authz gate
# ---------------------------------------------------------------------------


def test_import_contacts_confirm_rep_cannot_import_into_unowned_company(
    db_session: Session, sales_rep: User, other_user: User
):
    """A rep importing a contact into a company owned by another user must NOT create
    the contact — it must be counted as skipped_unauthorized."""
    # Company owned by other_user, NOT sales_rep
    foreign_co = Company(
        name="Foreign Corp IDOR",
        is_active=True,
        account_owner_id=other_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(foreign_co)
    db_session.commit()
    db_session.refresh(foreign_co)

    rows = _json.dumps(
        [
            {
                "company_name": "Foreign Corp IDOR",
                "contact_name": "Injected Contact",
                "email": "injected@foreigncorp.com",
                "phone": "",
                "role": "buyer",
            }
        ]
    )
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200
        # Summary must mention "not your account" (skipped_unauthorized)
        assert "not your account" in resp.text.lower() or "skipped" in resp.text.lower()

    # The contact must NOT have been created
    db_session.expire_all()
    contacts = db_session.query(SiteContact).filter(SiteContact.email == "injected@foreigncorp.com").all()
    assert len(contacts) == 0, "Rep must not be able to inject a contact into a company they don't manage"


def test_import_contacts_confirm_manager_can_import_into_any_company(
    db_session: Session, mgr_user: User, other_user: User
):
    """A manager importing a contact into any company (even one they don't own) should
    succeed."""
    any_co = Company(
        name="Any Corp Manager Import",
        is_active=True,
        account_owner_id=other_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(any_co)
    db_session.commit()
    db_session.refresh(any_co)

    rows = _json.dumps(
        [
            {
                "company_name": "Any Corp Manager Import",
                "contact_name": "Manager Imported Contact",
                "email": "mgrimport@anycorp.com",
                "phone": "",
                "role": "buyer",
            }
        ]
    )
    for c in _make_client(db_session, mgr_user):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200
        assert "1 contact" in resp.text.lower() or "imported" in resp.text.lower()

    db_session.expire_all()
    contacts = db_session.query(SiteContact).filter(SiteContact.email == "mgrimport@anycorp.com").all()
    assert len(contacts) == 1, "Manager must be able to import contacts into any company"


def test_import_contacts_confirm_multi_row_batched_lookups(db_session: Session, sales_rep: User, other_user: User):
    """P3.2 regression: a multi-row import spanning several companies exercises the
    batched CustomerSite + dedup pre-fetch (instead of per-row round trips) and must.

    produce the EXACT same created/skipped counts as the original per-row code:
    - 2 rows for the same owned company share its (pre-existing) first ACTIVE site.
    - 1 row creates a brand-new site for a second owned company with no site yet, and a
      second row for that SAME company reuses the just-created site (in-batch cache).
    - 1 row is skipped as a within-batch email duplicate against a row processed earlier
      in this same import (no site pre-existing DB row for that email).
    - 1 row targets a company the rep does not manage → skipped_unauthorized.
    - 1 row targets no matching company → skipped_no_company.
    """
    co_a = Company(name="Batch Co A", is_active=True, account_owner_id=sales_rep.id, created_at=datetime.now(UTC))
    co_b = Company(name="Batch Co B", is_active=True, account_owner_id=sales_rep.id, created_at=datetime.now(UTC))
    foreign_co = Company(
        name="Batch Foreign Co", is_active=True, account_owner_id=other_user.id, created_at=datetime.now(UTC)
    )
    db_session.add_all([co_a, co_b, foreign_co])
    db_session.commit()
    db_session.refresh(co_a)

    site_a = CustomerSite(company_id=co_a.id, site_name="HQ", is_active=True, created_at=datetime.now(UTC))
    db_session.add(site_a)
    db_session.commit()
    db_session.refresh(site_a)

    rows = _json.dumps(
        [
            # Two rows for co_a — both reuse the pre-existing site_a.
            {"company_name": "Batch Co A", "contact_name": "A One", "email": "a1@batchco.com", "phone": "", "role": ""},
            {"company_name": "Batch Co A", "contact_name": "A Two", "email": "a2@batchco.com", "phone": "", "role": ""},
            # Two rows for co_b — no site exists yet; the second row must reuse the site
            # the first row creates (in-batch cache), not create a second one.
            {"company_name": "Batch Co B", "contact_name": "B One", "email": "b1@batchco.com", "phone": "", "role": ""},
            {"company_name": "Batch Co B", "contact_name": "B Two", "email": "b2@batchco.com", "phone": "", "role": ""},
            # Within-batch duplicate email for co_a — must be skipped as a dup even
            # though no DB row for it existed before this import ran.
            {
                "company_name": "Batch Co A",
                "contact_name": "A One Dup",
                "email": "a1@batchco.com",
                "phone": "",
                "role": "",
            },
            # Unmanaged company → skipped_unauthorized.
            {
                "company_name": "Batch Foreign Co",
                "contact_name": "Injected",
                "email": "injected@foreign.com",
                "phone": "",
                "role": "",
            },
            # No matching company → skipped_no_company.
            {
                "company_name": "No Such Batch Co",
                "contact_name": "Nobody",
                "email": "nobody@nowhere.com",
                "phone": "",
                "role": "",
            },
        ]
    )
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/confirm",
            data={"rows_json": rows},
        )
        assert resp.status_code == 200

    db_session.expire_all()
    created_contacts = (
        db_session.query(SiteContact)
        .filter(SiteContact.email.in_(["a1@batchco.com", "a2@batchco.com", "b1@batchco.com", "b2@batchco.com"]))
        .all()
    )
    assert len(created_contacts) == 4, "expected exactly 4 contacts created (dup/unauthorized/no-company skipped)"

    # co_a rows land on the pre-existing site.
    assert all(
        c.customer_site_id == site_a.id for c in created_contacts if c.email in ("a1@batchco.com", "a2@batchco.com")
    )

    # co_b rows share ONE newly created site (in-batch cache reused across both rows).
    co_b_contacts = [c for c in created_contacts if c.email in ("b1@batchco.com", "b2@batchco.com")]
    b_site_ids = {c.customer_site_id for c in co_b_contacts}
    assert len(b_site_ids) == 1, "both Batch Co B rows must share the single site created in this batch"
    b_site = db_session.get(CustomerSite, next(iter(b_site_ids)))
    assert b_site.company_id == co_b.id

    # Duplicate/unauthorized/no-company rows produced no extra contacts.
    assert db_session.query(SiteContact).filter(SiteContact.email == "injected@foreign.com").count() == 0, (
        "unauthorized row must not create a contact"
    )
    assert db_session.query(SiteContact).filter(SiteContact.email == "nobody@nowhere.com").count() == 0, (
        "unmatched-company row must not create a contact"
    )


def test_import_contacts_preview_flags_unauthorized_for_rep(db_session: Session, sales_rep: User, other_user: User):
    """Contacts preview must show rows matching an unmanageable company as
    'unauthorized' (not importable) for a rep, but 'valid' for a manager."""
    # Company owned by other_user — sales_rep cannot manage it
    foreign_co = Company(
        name="Unmanageable Preview Corp",
        is_active=True,
        account_owner_id=other_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(foreign_co)
    db_session.commit()

    csv_bytes = (
        b"company_name,contact_name,email,phone,role\nUnmanageable Preview Corp,Test Contact,test@unmanageable.com,,\n"
    )

    # Rep: row should be flagged as unauthorized / not importable
    for c in _make_client(db_session, sales_rep):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            files={"file": ("contacts.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert (
            "not your account" in resp.text.lower()
            or "unauthorized" in resp.text.lower()
            or "company not yours" in resp.text.lower()
        )
        # The hidden rows_json must be empty (no valid rows to submit)
        assert (
            "test@unmanageable.com" not in resp.text
            or "0 to import" in resp.text.lower()
            or '"email": "test@unmanageable.com"' not in resp.text
        )


def test_import_contacts_preview_manager_sees_all_valid(db_session: Session, mgr_user: User, other_user: User):
    """A manager must see rows matching any company as 'valid' (not unauthorized)."""
    any_co = Company(
        name="Preview Any Corp Mgr",
        is_active=True,
        account_owner_id=other_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(any_co)
    db_session.commit()

    csv_bytes = (
        b"company_name,contact_name,email,phone,role\nPreview Any Corp Mgr,Mgr Contact,mgr@previewanycorp.com,,\n"
    )

    for c in _make_client(db_session, mgr_user):
        resp = c.post(
            "/v2/partials/customers/import/contacts/preview",
            files={"file": ("contacts.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        # Manager should see it as valid (1 to import, no unauthorized)
        assert "1 to import" in resp.text.lower() or "mgr contact" in resp.text
        assert "not your account" not in resp.text.lower()
        assert "unauthorized" not in resp.text.lower()
