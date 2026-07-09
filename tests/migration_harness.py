"""Hermetic alembic-migration execution for migration unit tests.

What: ``run_ops(engine, fn)`` executes a migration module's ``upgrade()``/``downgrade()``
      directly through a MigrationContext + a LOCALLY-BOUND ``Operations`` on a caller-
      provided scratch engine — NO alembic CLI, no alembic/env.py, no ``alembic.op``
      PROCESS-GLOBAL proxy, no os.environ DATABASE_URL channel. The in-process CLI path
      (command.stamp/upgrade on a temp-file DB) proved load-flaky under pytest-xdist
      (intermittent "table missing" skips from env.py's idempotent wrappers while the
      full suite runs in parallel), so migration round-trip tests share THIS helper
      instead of each file re-deriving the pattern.

      IMPORTANT (issue #470): ``run_ops`` binds the migration module's module-level ``op``
      name to a ctx-local ``Operations`` for the duration of the call. It deliberately does
      NOT use ``Operations.context(ctx)`` — that installs alembic's *process-global*
      ``alembic.op`` proxy, which is shared mutable state. Under pytest-xdist another test
      that drives alembic could clobber that proxy mid-call, so a migration's
      ``op.create_table`` would run against the wrong connection (the "attachment tables not
      created" flake). A locally-bound ``op`` is immune to that.
Called by: tests/test_migration_096_spec_provenance.py,
      tests/test_migration_097_dual_brand.py (and any future migration round-trip test).
Depends on: alembic.migration.MigrationContext, alembic.operations.Operations.
"""

from __future__ import annotations

from collections.abc import Callable

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Engine


def run_ops(engine: Engine, fn: Callable[[], None]) -> None:
    """Run a migration module's ``upgrade``/``downgrade`` hermetically on *engine*.

    ``fn`` is a migration module's ``upgrade``/``downgrade`` function; its ``op.*`` calls
    resolve the module-global name ``op`` (``from alembic import op``). We temporarily rebind
    that name in the function's own module namespace to a ctx-local ``Operations`` instance,
    so the DDL runs against ``engine``'s connection with zero dependence on the shared global
    proxy (see module docstring / issue #470). The original ``op`` is restored afterwards.
    """
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        local_op = Operations(ctx)
        module_globals = getattr(fn, "__globals__", None)
        if module_globals is None:
            # No module namespace to rebind (e.g. a lambda) — fall back to the global proxy.
            with Operations.context(ctx):
                fn()
            return
        sentinel = object()
        previous_op = module_globals.get("op", sentinel)
        module_globals["op"] = local_op
        try:
            fn()
        finally:
            if previous_op is sentinel:
                module_globals.pop("op", None)
            else:
                module_globals["op"] = previous_op
