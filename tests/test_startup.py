"""
test_startup.py — Tests for app/startup.py

startup.py is PostgreSQL-specific (FTS triggers, extensions, CHECK constraints).
Under TESTING=1 it short-circuits entirely. We test the guard and the _exec
error handling on SQLite.

Full coverage requires a real PG instance — deferred to integration tests.

Called by: pytest
Depends on: app/startup.py
"""

import os

from app.startup import _exec, run_startup_migrations


class TestStartupGuard:
    def test_testing_mode_skips_migrations(self):
        """TESTING=1 → run_startup_migrations does nothing, doesn't crash on SQLite."""
        assert os.environ.get("TESTING") == "1"
        # Should return without error — no PG-specific DDL attempted
        run_startup_migrations()


class TestExec:
    def test_pg_ddl_fails_on_sqlite_gracefully(self, db_session):
        """PG-specific DDL fails on SQLite but _exec swallows the error."""
        from tests.conftest import engine  # Test SQLite engine

        with engine.connect() as conn:
            # This PG-specific statement should fail silently on SQLite
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
            # If we get here, _exec handled the error gracefully
