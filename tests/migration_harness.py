"""Hermetic alembic-migration execution for migration unit tests.

What: ``run_ops(engine, fn)`` executes a migration module's ``upgrade()``/``downgrade()``
      directly through the MigrationContext + Operations.context pattern on a caller-
      provided scratch engine — NO alembic CLI, no alembic/env.py, no ``alembic.op``
      PROCESS-GLOBAL proxy, no os.environ DATABASE_URL channel. The in-process CLI path
      (command.stamp/upgrade on a temp-file DB) proved load-flaky under pytest-xdist
      (intermittent "table missing" skips from env.py's idempotent wrappers while the
      full suite runs in parallel), so migration round-trip tests share THIS helper
      instead of each file re-deriving the pattern.
Called by: tests/test_migration_096_spec_provenance.py,
      tests/test_migration_097_dual_brand.py (and any future migration round-trip test).
Depends on: alembic.migration.MigrationContext, alembic.operations.Operations.
"""

from __future__ import annotations

from typing import Callable

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Engine


def run_ops(engine: Engine, fn: Callable[[], None]) -> None:
    """Run a migration module's ``upgrade``/``downgrade`` hermetically on *engine*."""
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()
