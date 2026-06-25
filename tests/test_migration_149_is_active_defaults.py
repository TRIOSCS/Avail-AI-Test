"""Round-trip + backfill tests for migration 149 (is_active hardening).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 148) plus an executable
      upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine. SQLite
      cannot ALTER COLUMN nullability/server_default, so alter_column is patched to a no-op
      (same pattern as tests/test_migration_136_137_138.py); the portable part — the
      backfill UPDATE that turns NULL is_active into true — IS exercised. The NOT NULL +
      server_default semantics are enforced/verified on live Postgres at deploy.
Called by: pytest
Depends on: alembic/versions/149_is_active_defaults.py, tests/migration_harness.run_ops
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "149_is_active_defaults.py")
_spec = importlib.util.spec_from_file_location("migration_149", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _noop_alter_column(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """No-op for alter_column on SQLite (no ALTER COLUMN nullability/server_default)."""


@contextmanager
def _sqlite_compat():
    with patch("alembic.operations.Operations.alter_column", _noop_alter_column):
        yield


def _run(engine, fn):
    with _sqlite_compat():
        run_ops(engine, fn)


def _engine() -> sa.engine.Engine:
    """Scratch SQLite with the three tables migration 149 touches, is_active
    nullable."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = sa.MetaData()
    for tbl in ("companies", "customer_sites", "users"):
        sa.Table(
            tbl,
            meta,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("is_active", sa.Boolean, nullable=True),
        )
    meta.create_all(engine)
    return engine


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "149_is_active_defaults"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_148(self):
        assert _mod.down_revision == "148_site_dnc"


class TestBackfill:
    def test_upgrade_backfills_null_is_active_to_true(self):
        engine = _engine()
        with engine.begin() as conn:
            for tbl in ("companies", "customer_sites", "users"):
                conn.execute(text(f"INSERT INTO {tbl} (id, is_active) VALUES (1, NULL), (2, 0)"))
        _run(engine, _mod.upgrade)
        with engine.connect() as conn:
            for tbl in ("companies", "customer_sites", "users"):
                rows = {r[0]: r[1] for r in conn.execute(text(f"SELECT id, is_active FROM {tbl} ORDER BY id"))}
                assert rows[1] == 1, f"{tbl}: NULL is_active should backfill to true"
                assert rows[2] == 0, f"{tbl}: explicit false must be left untouched"

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = _engine()
        _run(engine, _mod.upgrade)
        _run(engine, _mod.downgrade)
        _run(engine, _mod.upgrade)  # must not raise


class TestModelsDeclareNotNull:
    def test_models_declare_not_null_and_server_default(self):
        from app.models.auth import User
        from app.models.crm import Company, CustomerSite

        for model in (Company, CustomerSite, User):
            col = model.__table__.c.is_active
            assert col.nullable is False, f"{model.__name__}.is_active should be NOT NULL"
            assert col.server_default is not None, f"{model.__name__}.is_active needs a server_default"
