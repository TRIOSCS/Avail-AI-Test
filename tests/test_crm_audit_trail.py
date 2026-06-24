"""test_crm_audit_trail.py — Tests for the CRM created-by/modified-by audit trail.

Covers: migration 147 round-trip, contextvar-driven stamping on insert/update,
        NULL behaviour for background writes, cross-request isolation,
        company-detail template rendering, and a full HTTP-path integration
        test that exercises AuditUserMiddleware → contextvar → ORM listener → DB.

Called by: pytest
Depends on: conftest.py, app/audit_listeners.py, app/request_context.py,
            alembic/versions/147_crm_audit_trail.py
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.audit_listeners import register_audit_listeners
from app.models import Company, CustomerSite, SiteContact, User
from app.request_context import current_user_id_var
from tests.migration_harness import run_ops

# ── Load the migration module once ──────────────────────────────────────────

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "147_crm_audit_trail.py")
_spec = importlib.util.spec_from_file_location("migration_147", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _scratch_engine_with_tables():
    """SQLite in-memory engine with minimal schema for the migration round-trip."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    md = MetaData()
    Table("users", md, Column("id", Integer, primary_key=True))
    Table("companies", md, Column("id", Integer, primary_key=True), Column("name", String(255)))
    Table("customer_sites", md, Column("id", Integer, primary_key=True), Column("site_name", String(255)))
    Table("site_contacts", md, Column("id", Integer, primary_key=True), Column("full_name", String(255)))
    md.create_all(engine)
    return engine


def _noop_fk(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for create_foreign_key — SQLite doesn't support ALTER FK."""


def _noop_constraint(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for drop_constraint — SQLite doesn't support ALTER FK."""


@contextmanager
def _sqlite_compat():
    """Patch FK/constraint ops to no-ops so migration tests run portably on SQLite."""
    with (
        patch("alembic.operations.Operations.create_foreign_key", _noop_fk),
        patch("alembic.operations.Operations.drop_constraint", _noop_constraint),
    ):
        yield


# ── Migration metadata tests ─────────────────────────────────────────────────


class TestMigration147Metadata:
    def test_revision_id(self):
        assert _mod.revision == "147_crm_audit_trail"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres.
        assert len(_mod.revision) <= 32, f"revision id too long: {len(_mod.revision)} chars"

    def test_down_revision(self):
        assert _mod.down_revision == "146_req_win_probability"

    def test_branch_labels_none(self):
        assert _mod.branch_labels is None


# ── Migration round-trip ──────────────────────────────────────────────────────


class TestMigration147RoundTrip:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine.

    FK/constraint ops are patched to no-ops (SQLite doesn't support ALTER FK). Column
    add/drop is exercised portably — FK semantics are verified on Postgres.
    """

    _AUDIT_COLS = {"created_by_id", "modified_by_id"}
    _TABLES = ("companies", "customer_sites", "site_contacts")

    def test_upgrade_adds_6_columns(self):
        engine = _scratch_engine_with_tables()
        with _sqlite_compat():
            run_ops(engine, _mod.upgrade)
        insp = inspect(engine)
        for table in self._TABLES:
            cols = {c["name"] for c in insp.get_columns(table)}
            assert self._AUDIT_COLS <= cols, f"{table} missing audit cols after upgrade"

    def test_downgrade_removes_6_columns(self):
        engine = _scratch_engine_with_tables()
        with _sqlite_compat():
            run_ops(engine, _mod.upgrade)
            run_ops(engine, _mod.downgrade)
        insp = inspect(engine)
        for table in self._TABLES:
            cols = {c["name"] for c in insp.get_columns(table)}
            assert not (self._AUDIT_COLS & cols), f"{table} still has audit cols after downgrade"

    def test_upgrade_downgrade_upgrade(self):
        engine = _scratch_engine_with_tables()
        with _sqlite_compat():
            run_ops(engine, _mod.upgrade)
            run_ops(engine, _mod.downgrade)
            run_ops(engine, _mod.upgrade)
        insp = inspect(engine)
        for table in self._TABLES:
            cols = {c["name"] for c in insp.get_columns(table)}
            assert self._AUDIT_COLS <= cols, f"{table} missing audit cols after second upgrade"


# ── Contextvar-driven audit stamping ─────────────────────────────────────────
#
# These tests use the shared conftest SQLite DB (conftest.db_session).  The
# audit listeners are registered once at import time via register_audit_listeners().


@pytest.fixture(autouse=True)
def _ensure_listeners():
    """Ensure listeners are registered for each test (idempotent)."""
    register_audit_listeners()


def _make_user(db: Session, email: str = "audit@test.com") -> User:
    u = User(email=email, name="Audit User", role="buyer", created_at=datetime.now(timezone.utc))
    db.add(u)
    db.flush()
    return u


def _make_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="Test Site")
    db.add(site)
    db.flush()
    return site


def _make_contact(db: Session, site: CustomerSite) -> SiteContact:
    contact = SiteContact(customer_site_id=site.id, full_name="Jane Doe", email="jane@test.com")
    db.add(contact)
    db.flush()
    return contact


class TestContextvarSetOnCreate:
    def test_company_created_by_set(self, db_session: Session):
        user = _make_user(db_session)
        token = current_user_id_var.set(user.id)
        try:
            co = Company(name="AuditCo")
            db_session.add(co)
            db_session.flush()
            assert co.created_by_id == user.id
            assert co.modified_by_id == user.id
        finally:
            current_user_id_var.reset(token)

    def test_customer_site_created_by_set(self, db_session: Session):
        user = _make_user(db_session)
        co = Company(name="AuditCo2")
        db_session.add(co)
        db_session.flush()

        token = current_user_id_var.set(user.id)
        try:
            site = CustomerSite(company_id=co.id, site_name="Site 1")
            db_session.add(site)
            db_session.flush()
            assert site.created_by_id == user.id
            assert site.modified_by_id == user.id
        finally:
            current_user_id_var.reset(token)

    def test_site_contact_created_by_set(self, db_session: Session):
        user = _make_user(db_session)
        co = Company(name="AuditCo3")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Site 2")
        db_session.add(site)
        db_session.flush()

        token = current_user_id_var.set(user.id)
        try:
            contact = SiteContact(customer_site_id=site.id, full_name="Bob Smith", email="bob@test.com")
            db_session.add(contact)
            db_session.flush()
            assert contact.created_by_id == user.id
            assert contact.modified_by_id == user.id
        finally:
            current_user_id_var.reset(token)


class TestContextvarSetOnUpdate:
    def test_modified_by_updated_on_write(self, db_session: Session):
        creator = _make_user(db_session, "creator@test.com")
        editor = User(email="editor@test.com", name="Editor", role="buyer", created_at=datetime.now(timezone.utc))
        db_session.add(editor)
        db_session.flush()

        # Create with creator
        create_token = current_user_id_var.set(creator.id)
        try:
            co = Company(name="UpdateCo")
            db_session.add(co)
            db_session.flush()
            assert co.created_by_id == creator.id
        finally:
            current_user_id_var.reset(create_token)

        # Update with editor
        edit_token = current_user_id_var.set(editor.id)
        try:
            co.name = "UpdateCo Renamed"
            db_session.flush()
            assert co.modified_by_id == editor.id
            # created_by_id must NOT change
            assert co.created_by_id == creator.id
        finally:
            current_user_id_var.reset(edit_token)


class TestNoContextvarNulls:
    def test_create_without_contextvar_leaves_nulls(self, db_session: Session):
        # Ensure contextvar is definitely None (default)
        assert current_user_id_var.get() is None
        co = Company(name="NullAuditCo")
        db_session.add(co)
        db_session.flush()
        assert co.created_by_id is None
        assert co.modified_by_id is None

    def test_update_without_contextvar_leaves_nulls(self, db_session: Session):
        assert current_user_id_var.get() is None
        co = Company(name="NullUpdateCo")
        db_session.add(co)
        db_session.flush()
        co.name = "NullUpdateCo Changed"
        db_session.flush()
        assert co.modified_by_id is None


class TestNoCrossRequestLeak:
    def test_second_request_uses_new_user(self, db_session: Session):
        user1 = _make_user(db_session, "user1@test.com")
        user2 = User(email="user2@test.com", name="User2", role="buyer", created_at=datetime.now(timezone.utc))
        db_session.add(user2)
        db_session.flush()

        # Simulate first request
        token1 = current_user_id_var.set(user1.id)
        try:
            co1 = Company(name="Request1Co")
            db_session.add(co1)
            db_session.flush()
            assert co1.created_by_id == user1.id
        finally:
            current_user_id_var.reset(token1)

        # After first request ends, contextvar should be back to None
        assert current_user_id_var.get() is None

        # Simulate second request with different user
        token2 = current_user_id_var.set(user2.id)
        try:
            co2 = Company(name="Request2Co")
            db_session.add(co2)
            db_session.flush()
            assert co2.created_by_id == user2.id
        finally:
            current_user_id_var.reset(token2)

        # Verify no cross-request contamination
        assert co1.created_by_id == user1.id
        assert co2.created_by_id == user2.id
        assert co1.created_by_id != co2.created_by_id


class TestDetailRendersCreatedBy:
    def test_template_renders_created_by_name(self, client, db_session: Session, test_user: User):
        """Company detail partial renders 'Created by {name}' when created_by is set."""
        # Create company with created_by_id set directly (simulating a request write)
        co = Company(
            name="TemplateAuditCo",
            created_by_id=test_user.id,
            modified_by_id=test_user.id,
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        body = resp.text
        # The template renders "Created by {name}" in the audit trail paragraph
        assert test_user.name in body


# ── Real HTTP-path integration test ──────────────────────────────────────────
#
# This class exercises the full chain:
#   signed session cookie → SessionMiddleware populates scope["session"]
#   → AuditUserMiddleware reads scope["session"]["user_id"] → sets contextvar
#   → before_insert listener stamps created_by_id → DB persists it.
#
# It deliberately bypasses the conftest `client` fixture (which overrides
# require_user) and uses a raw TestClient with a crafted signed session
# cookie so that AuditUserMiddleware is the ONLY mechanism that could set
# the contextvar.  If the middleware ordering bug were reintroduced (middleware
# running before Session decodes the cookie) the test would fail with
# created_by_id == None.


def _make_signed_session_cookie(user_id: int, secret_key: str) -> str:
    """Craft a valid Starlette SessionMiddleware signed cookie for the given user_id.

    Replicates SessionMiddleware._sign(): base64-encode the JSON payload, then sign with
    itsdangerous.TimestampSigner using the app's secret_key.
    """
    import json
    from base64 import b64encode

    import itsdangerous

    payload = json.dumps({"user_id": user_id}).encode("utf-8")
    data = b64encode(payload)
    signer = itsdangerous.TimestampSigner(str(secret_key))
    return signer.sign(data).decode("utf-8")


class TestHTTPPathAuditIntegration:
    """Integration tests that exercise the real middleware → contextvar → DB path."""

    def test_create_company_via_http_stamps_created_by_id(self, db_session: Session, test_user: User):
        """POST /api/companies through the real middleware stack stamps created_by_id.

        Uses a crafted signed session cookie (no require_user override) so that
        AuditUserMiddleware is the only mechanism that could populate the contextvar.
        Asserts created_by_id == test_user.id, proving the middleware→listener→DB chain
        is intact.
        """
        from app.config import settings
        from app.database import get_db
        from app.main import app

        # Wire DB override only — auth deps deliberately NOT overridden so the
        # middleware must do the work via the session cookie.
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            cookie_value = _make_signed_session_cookie(test_user.id, settings.secret_key)
            with TestClient(app, raise_server_exceptions=True) as raw_client:
                raw_client.cookies.set("session", cookie_value)
                resp = raw_client.post(
                    "/api/companies",
                    json={"name": "HTTPAuditCo", "force": True},
                    params={"force": "true"},
                )
            # 200 or 201 — either means the company was created
            assert resp.status_code in (200, 201), f"Unexpected status {resp.status_code}: {resp.text}"
            company_id = resp.json()["id"]

            db_session.expire_all()
            co = db_session.get(Company, company_id)
            assert co is not None, "Company not found in DB"
            assert co.created_by_id == test_user.id, (
                f"created_by_id expected {test_user.id}, got {co.created_by_id} — "
                "AuditUserMiddleware may be running before SessionMiddleware"
            )
            assert co.modified_by_id == test_user.id
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_update_company_via_http_stamps_modified_by_id(self, db_session: Session, test_user: User):
        """PUT /api/companies/{id} through the real middleware stack stamps
        modified_by_id.

        A second user edits the company; created_by_id must stay as the original creator
        and modified_by_id must switch to the editor.
        """
        from datetime import datetime, timezone

        from app.config import settings
        from app.database import get_db
        from app.main import app

        # Create a second user who will be the editor
        editor = User(
            email="editor_http@test.com",
            name="HTTP Editor",
            role="buyer",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(editor)
        db_session.flush()

        # Create the company attributed to test_user via direct ORM (not under test here)
        token = current_user_id_var.set(test_user.id)
        try:
            co = Company(name="HTTPUpdateCo")
            db_session.add(co)
            db_session.flush()
            assert co.created_by_id == test_user.id
        finally:
            current_user_id_var.reset(token)

        company_id = co.id

        app.dependency_overrides[get_db] = lambda: db_session
        try:
            # Now PUT as editor via real middleware + signed session cookie
            cookie_value = _make_signed_session_cookie(editor.id, settings.secret_key)
            with TestClient(app, raise_server_exceptions=True) as raw_client:
                raw_client.cookies.set("session", cookie_value)
                resp = raw_client.put(
                    f"/api/companies/{company_id}",
                    json={"name": "HTTPUpdateCo Renamed"},
                )
            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"

            db_session.expire_all()
            co = db_session.get(Company, company_id)
            assert co is not None
            # created_by_id must remain the original creator
            assert co.created_by_id == test_user.id
            # modified_by_id must reflect the editor who made the PUT request
            assert co.modified_by_id == editor.id, (
                f"modified_by_id expected {editor.id} (editor), got {co.modified_by_id}"
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
