"""tests/test_migration_harness.py — Tests for the hermetic migration runner.

Called by: pytest
Depends on: tests.migration_harness.run_ops
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from alembic import op as alembic_op
from tests.migration_harness import run_ops


def _mem_engine():
    return sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _make_upgrade(table_name):
    """Build a migration-style upgrade() whose module global `op` is the alembic proxy,
    mirroring how real migrations reference `op` (``from alembic import op``)."""
    ns = {"sa": sa, "op": alembic_op}
    src = f"def upgrade():\n    op.create_table('{table_name}', sa.Column('id', sa.Integer, primary_key=True))\n"
    exec(src, ns)  # noqa: S102 — controlled source, test-only
    return ns["upgrade"], ns


def test_run_ops_creates_table_via_locally_bound_op():
    """run_ops must execute the DDL against the provided engine (the happy path)."""
    upgrade, _ns = _make_upgrade("demo_created")
    engine = _mem_engine()
    run_ops(engine, upgrade)
    assert "demo_created" in inspect(engine).get_table_names()


def test_run_ops_restores_module_op_after_running():
    """run_ops rebinds the migration module's `op` only for the call, then restores it —
    so it leaves no global/namespace state behind (issue #470 hardening)."""
    upgrade, ns = _make_upgrade("demo_restore")
    engine = _mem_engine()
    run_ops(engine, upgrade)
    assert ns["op"] is alembic_op  # original proxy restored


def test_run_ops_is_independent_of_a_clobbered_global_proxy():
    """The whole point of the rebind: even if alembic's PROCESS-GLOBAL op proxy is pointing
    at a different (wrong) context — as a concurrent xdist test could leave it — run_ops
    still runs the migration against the correct engine, because it binds `op` locally."""
    # Install a bogus global proxy bound to a DIFFERENT engine (the "clobbered" state).
    other_engine = _mem_engine()
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with other_engine.connect() as other_conn:
        bogus_ctx = MigrationContext.configure(other_conn)
        with Operations.context(bogus_ctx):  # global alembic.op now points at other_engine
            upgrade, _ns = _make_upgrade("demo_independent")
            engine = _mem_engine()
            run_ops(engine, upgrade)
            # table landed on the intended engine, NOT the clobbered global one
            assert "demo_independent" in inspect(engine).get_table_names()
            assert "demo_independent" not in inspect(other_engine).get_table_names()
