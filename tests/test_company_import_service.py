"""test_company_import_service.py — unit tests for the extracted CSV import service.

Covers the P4.2 extraction of import_companies_preview/confirm + import_contacts_
preview/confirm from app/routers/htmx/companies.py into
app/services/company_import_service.py: one happy-path + one edge case per function,
exercised directly against the service (not through the HTTP layer — the router-level
behavior is already pinned by tests/test_crm_bulk_import.py).

Called by: pytest
Depends on: app.services.company_import_service, conftest.py (db_session)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.services.company_import_service import (
    confirm_company_import,
    confirm_contact_import,
    parse_csv_rows,
    preview_company_import,
    preview_contact_import,
)


@pytest.fixture()
def sales_rep(db_session: Session) -> User:
    u = User(
        email="import.rep@trioscs.com",
        name="Import Rep",
        role="sales",
        azure_id="import-svc-rep-001",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def owned_company(db_session: Session, sales_rep: User) -> Company:
    co = Company(name="Owned Import Corp", is_active=True, account_owner_id=sales_rep.id, created_at=datetime.now(UTC))
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


# ── parse_csv_rows ────────────────────────────────────────────────────────


def test_parse_csv_rows_happy_path():
    """Well-formed CSV bytes decode into row dicts keyed by header."""
    content = b"name,website\nAcme Inc,acme.com\n"
    rows = parse_csv_rows(content)
    assert rows == [{"name": "Acme Inc", "website": "acme.com"}]


def test_parse_csv_rows_returns_none_on_binary_garbage(monkeypatch):
    """A file that blows up the CSV reader returns None (router renders the graceful
    error) rather than raising."""
    import csv

    def _boom(*a, **k):
        raise csv.Error("boom")

    monkeypatch.setattr("app.services.company_import_service.csv.DictReader", _boom)
    assert parse_csv_rows(b"name\nX\n") is None


# ── preview_company_import ──────────────────────────────────────────────────


def test_preview_company_import_happy_path(db_session: Session):
    """A valid, non-duplicate row is flagged 'valid' and included in valid_rows."""
    result = preview_company_import(db_session, [{"name": "Brand New Co", "website": "new.com"}])
    assert result["valid_count"] == 1
    assert result["dup_count"] == 0
    assert result["invalid_count"] == 0
    assert result["valid_rows"] == [{"name": "Brand New Co", "website": "new.com", "account_type": ""}]


def test_preview_company_import_flags_duplicate(db_session: Session, owned_company: Company):
    """A row whose normalized name matches an existing Company is flagged duplicate, not
    included in valid_rows."""
    result = preview_company_import(db_session, [{"name": owned_company.name}])
    assert result["dup_count"] == 1
    assert result["valid_count"] == 0
    assert result["rows"][0]["status"] == "duplicate"


def test_preview_company_import_raises_over_row_cap(db_session: Session):
    from app.services.company_import_service import IMPORT_MAX_ROWS

    rows = [{"name": f"Co {i}"} for i in range(IMPORT_MAX_ROWS + 1)]
    with pytest.raises(ValueError, match="row limit"):
        preview_company_import(db_session, rows)


# ── confirm_company_import ──────────────────────────────────────────────────


def test_confirm_company_import_happy_path(db_session: Session, sales_rep: User):
    """A valid row creates a Company owned by the importing user."""
    result = confirm_company_import(db_session, [{"name": "Freshly Imported Co"}], sales_rep)
    assert result["created"] == 1
    assert result["skipped_dup"] == 0
    co = db_session.query(Company).filter_by(name="Freshly Imported Co").first()
    assert co is not None
    assert co.account_owner_id == sales_rep.id


def test_confirm_company_import_skips_duplicate(db_session: Session, sales_rep: User, owned_company: Company):
    """A row matching an existing normalized_name is skipped, not double-created."""
    result = confirm_company_import(db_session, [{"name": owned_company.name}], sales_rep)
    assert result["created"] == 0
    assert result["skipped_dup"] == 1
    assert db_session.query(Company).filter_by(name=owned_company.name).count() == 1


# ── preview_contact_import ───────────────────────────────────────────────────


def test_preview_contact_import_happy_path(db_session: Session, sales_rep: User, owned_company: Company):
    """A valid row matching a company the rep manages is flagged 'valid'."""
    result = preview_contact_import(
        db_session,
        [{"company_name": owned_company.name, "contact_name": "Jane Doe", "email": "jane@owned.com"}],
        sales_rep,
    )
    assert result["valid_count"] == 1
    assert result["unauthorized_count"] == 0


def test_preview_contact_import_flags_unauthorized_company(db_session: Session, sales_rep: User):
    """A row matching a company the rep does NOT manage is flagged 'unauthorized'."""
    other_owner = User(
        email="other.owner@trioscs.com",
        name="Other Owner",
        role="sales",
        azure_id="import-svc-other-001",
        created_at=datetime.now(UTC),
    )
    db_session.add(other_owner)
    db_session.commit()
    db_session.refresh(other_owner)
    unowned = Company(
        name="Not Mine Corp", is_active=True, account_owner_id=other_owner.id, created_at=datetime.now(UTC)
    )
    db_session.add(unowned)
    db_session.commit()

    result = preview_contact_import(
        db_session,
        [{"company_name": "Not Mine Corp", "contact_name": "John Roe", "email": "john@notmine.com"}],
        sales_rep,
    )
    assert result["unauthorized_count"] == 1
    assert result["rows"][0]["status"] == "unauthorized"


# ── confirm_contact_import ───────────────────────────────────────────────────


def test_confirm_contact_import_happy_path(db_session: Session, sales_rep: User, owned_company: Company):
    """A valid row creates a SiteContact on the company's (auto-created) HQ site."""
    result = confirm_contact_import(
        db_session,
        [{"company_name": owned_company.name, "contact_name": "Jane Doe", "email": "jane@owned.com"}],
        sales_rep,
    )
    assert result["created"] == 1
    site = db_session.query(CustomerSite).filter_by(company_id=owned_company.id).first()
    assert site is not None
    contact = db_session.query(SiteContact).filter_by(customer_site_id=site.id).first()
    assert contact.full_name == "Jane Doe"
    assert contact.email == "jane@owned.com"


def test_confirm_contact_import_skips_unmatched_company(db_session: Session, sales_rep: User):
    """A row whose company can't be matched is counted under skipped_no_company."""
    result = confirm_contact_import(
        db_session,
        [{"company_name": "Nonexistent Co", "contact_name": "Ghost", "email": "ghost@nowhere.com"}],
        sales_rep,
    )
    assert result["created"] == 0
    assert result["skipped_no_company"] == 1
