"""Round-trip tests for migrations 133, 134, and 135.

Tests each migration's upgrade→downgrade→upgrade cycle on a scratch SQLite engine via
the shared hermetic harness (tests/migration_harness.run_ops).

FK/constraint operations (create_foreign_key, drop_constraint, create_check_constraint)
silently no-op on SQLite by patching the alembic.op module-level wrappers with real
callables (SQLAlchemy inspects signatures so MagicMock won't work). Column adds/drops
and index ops are exercised portably. PG-only constraint semantics are verified on the
live Postgres instance at deploy time.

Migration 133: primary_contact_id + parent_company_id on companies.
Migration 134: first_name, last_name, contact_owner_id on site_contacts + backfill.
Migration 135: company_id + site_contact_id on requisition_tasks; requisition_id nullable.

Called by: pytest
Depends on: alembic/versions/133_company_links.py, 134_contact_fields.py,
            135_general_tasks.py, tests/migration_harness.py
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(filename: str):
    path = os.path.join(_REPO_ROOT, "alembic", "versions", filename)
    spec = importlib.util.spec_from_file_location(filename, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod133 = _load("133_company_links.py")
_mod134 = _load("134_contact_fields.py")
_mod135 = _load("135_general_tasks.py")


# ---------------------------------------------------------------------------
# SQLite compatibility: patch out FK/constraint operations that SQLite doesn't
# support via ALTER TABLE. We use real callables (not MagicMock) because alembic
# inspects __code__/__annotations__ on the Operations methods before dispatch.
# ---------------------------------------------------------------------------


def _noop_fk(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for create_foreign_key on SQLite."""


def _noop_constraint(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for drop_constraint / create_check_constraint on SQLite."""


def _noop_alter_column(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op replacement for alter_column nullability changes on SQLite.

    SQLite does not support ALTER TABLE ... ALTER COLUMN ... DROP/SET NOT NULL.
    The actual NOT NULL semantics for 135 are covered by the downgrade orphan-purge
    test and are enforced on live Postgres.
    """


@contextmanager
def _sqlite_compat():
    """Patch FK/constraint/alter ops to no-ops so SQLite tests run portably."""
    with (
        patch("alembic.operations.Operations.create_foreign_key", _noop_fk),
        patch("alembic.operations.Operations.drop_constraint", _noop_constraint),
        patch("alembic.operations.Operations.create_check_constraint", _noop_constraint),
        patch("alembic.operations.Operations.alter_column", _noop_alter_column),
    ):
        yield


def _run(engine, fn):
    with _sqlite_compat():
        run_ops(engine, fn)


# ---------------------------------------------------------------------------
# Prerequisite table builders
# ---------------------------------------------------------------------------


def _engine_for_133() -> sa.engine.Engine:
    """Scratch SQLite with only the tables 133 touches."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = sa.MetaData()
    sa.Table(
        "companies",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255)),
    )
    sa.Table(
        "site_contacts",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("full_name", sa.String(255)),
    )
    meta.create_all(engine)
    return engine


def _engine_for_134() -> sa.engine.Engine:
    """Scratch SQLite with only the tables 134 touches."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = sa.MetaData()
    sa.Table(
        "users",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "site_contacts",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("full_name", sa.String(255), nullable=True),
    )
    meta.create_all(engine)
    return engine


def _engine_for_135() -> sa.engine.Engine:
    """Scratch SQLite with only the tables 135 touches."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = sa.MetaData()
    sa.Table(
        "companies",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "site_contacts",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "requisition_tasks",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        # On SQLite we can't ALTER COLUMN nullability, so start nullable to allow
        # the orphan-purge test to insert a company-scoped row (requisition_id=NULL).
        sa.Column("requisition_id", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, default="todo"),
        sa.Column("title", sa.String(255), nullable=False),
    )
    meta.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _indexes(engine, table: str) -> set[str]:
    return {i["name"] for i in inspect(engine).get_indexes(table)}


# ---------------------------------------------------------------------------
# Migration 133 round-trip
# ---------------------------------------------------------------------------


class TestMigration133:
    def test_upgrade_adds_columns(self):
        engine = _engine_for_133()
        _run(engine, _mod133.upgrade)
        cols = _columns(engine, "companies")
        assert "primary_contact_id" in cols
        assert "parent_company_id" in cols

    def test_upgrade_adds_indexes(self):
        engine = _engine_for_133()
        _run(engine, _mod133.upgrade)
        idxs = _indexes(engine, "companies")
        assert "ix_companies_primary_contact_id" in idxs
        assert "ix_companies_parent_company_id" in idxs

    def test_downgrade_removes_columns(self):
        engine = _engine_for_133()
        _run(engine, _mod133.upgrade)
        _run(engine, _mod133.downgrade)
        cols = _columns(engine, "companies")
        assert "primary_contact_id" not in cols
        assert "parent_company_id" not in cols

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = _engine_for_133()
        _run(engine, _mod133.upgrade)
        _run(engine, _mod133.downgrade)
        _run(engine, _mod133.upgrade)
        cols = _columns(engine, "companies")
        assert "primary_contact_id" in cols
        assert "parent_company_id" in cols


# ---------------------------------------------------------------------------
# Migration 134 round-trip + backfill assertions
# ---------------------------------------------------------------------------


class TestMigration134:
    def test_upgrade_adds_columns(self):
        engine = _engine_for_134()
        _run(engine, _mod134.upgrade)
        cols = _columns(engine, "site_contacts")
        assert "first_name" in cols
        assert "last_name" in cols
        assert "contact_owner_id" in cols

    def test_upgrade_backfills_name_split(self):
        engine = _engine_for_134()
        # Seed contacts before upgrade
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO site_contacts (id, full_name) VALUES (:id, :n)"),
                [
                    {"id": 1, "n": "Mary Jane Watson"},
                    {"id": 2, "n": "Jane Doe"},
                    {"id": 3, "n": "Cher"},
                    {"id": 4, "n": None},
                ],
            )
        _run(engine, _mod134.upgrade)
        with engine.connect() as conn:
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    text("SELECT id, first_name, last_name FROM site_contacts ORDER BY id")
                ).fetchall()
            }
        # "Mary Jane Watson" → first="Mary", last="Jane Watson"
        assert rows[1] == ("Mary", "Jane Watson")
        # "Jane Doe" → first="Jane", last="Doe"
        assert rows[2] == ("Jane", "Doe")
        # "Cher" → first="Cher", last=None
        assert rows[3] == ("Cher", None)
        # NULL full_name → both remain NULL
        assert rows[4] == (None, None)

    def test_downgrade_removes_columns(self):
        engine = _engine_for_134()
        _run(engine, _mod134.upgrade)
        _run(engine, _mod134.downgrade)
        cols = _columns(engine, "site_contacts")
        assert "first_name" not in cols
        assert "last_name" not in cols
        assert "contact_owner_id" not in cols

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = _engine_for_134()
        _run(engine, _mod134.upgrade)
        _run(engine, _mod134.downgrade)
        _run(engine, _mod134.upgrade)
        cols = _columns(engine, "site_contacts")
        assert "first_name" in cols
        assert "last_name" in cols


# ---------------------------------------------------------------------------
# Migration 135 round-trip + orphan-purge assertion
# ---------------------------------------------------------------------------


class TestMigration135:
    def test_upgrade_adds_columns(self):
        engine = _engine_for_135()
        _run(engine, _mod135.upgrade)
        cols = _columns(engine, "requisition_tasks")
        assert "company_id" in cols
        assert "site_contact_id" in cols

    def test_upgrade_adds_indexes(self):
        engine = _engine_for_135()
        _run(engine, _mod135.upgrade)
        idxs = _indexes(engine, "requisition_tasks")
        assert "ix_rt_company_status" in idxs
        assert "ix_rt_contact_status" in idxs

    def test_downgrade_removes_columns(self):
        engine = _engine_for_135()
        _run(engine, _mod135.upgrade)
        _run(engine, _mod135.downgrade)
        cols = _columns(engine, "requisition_tasks")
        assert "company_id" not in cols
        assert "site_contact_id" not in cols

    def test_downgrade_purges_company_scoped_tasks(self):
        """Downgrade must DELETE company-scoped tasks (requisition_id IS NULL) before
        restoring NOT NULL — this is the FIX A orphan-purge guard."""
        engine = _engine_for_135()
        _run(engine, _mod135.upgrade)
        # Insert a company-scoped task (requisition_id NULL, company_id set)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO requisition_tasks (id, company_id, status, title)"
                    " VALUES (99, 1, 'todo', 'orphan task')"
                )
            )
        # Downgrade must not raise; orphan row must be gone
        _run(engine, _mod135.downgrade)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM requisition_tasks WHERE id = 99")).scalar()
        assert count == 0, "downgrade must purge company-scoped tasks before restoring NOT NULL"

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = _engine_for_135()
        _run(engine, _mod135.upgrade)
        _run(engine, _mod135.downgrade)
        _run(engine, _mod135.upgrade)
        cols = _columns(engine, "requisition_tasks")
        assert "company_id" in cols
        assert "site_contact_id" in cols
