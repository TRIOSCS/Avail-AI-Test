"""Tests for migration 164 (SP-2 qp_sales column/gate rename + sales_so_number retire).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto deployed 163) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine
      that ASSERTS the two data-correctness ops, not just the schema shape:
        - Op A: users.can_approve_sales_orders -> can_approve_qp_sales (value preserved).
        - Op B: approval_requests.gate_type 'sales_order' -> 'qp_sales' (other gates untouched).
        - Op C: quality_plans.sales_so_number backfilled onto buy_plans_v3.sales_order_number
          ONLY where the buy plan's number is blank (divergent existing numbers preserved),
          then the column dropped.
      SQLite 3.45 supports RENAME COLUMN / DROP COLUMN / UPDATE-FROM, so the real upgrade()
      runs hermetically via the migration harness (no alembic CLI, no env.py).
Called by: pytest
Depends on: alembic/versions/164_sp2_qp_sales_rename.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "164_sp2_qp_sales_rename.py")
_spec = importlib.util.spec_from_file_location("migration_164", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "164_sp2_qp_sales_rename"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_deployed_163(self):
        assert _mod.down_revision == "163_sp2_sales_order_gate"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine, asserting data
    ops."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table(
            "users",
            md,
            Column("id", Integer, primary_key=True),
            Column("can_approve_sales_orders", Boolean),
        )
        Table(
            "approval_requests",
            md,
            Column("id", Integer, primary_key=True),
            Column("gate_type", String(50)),
        )
        Table(
            "quality_plans",
            md,
            Column("id", Integer, primary_key=True),
            Column("buy_plan_id", Integer),
            Column("sales_so_number", String(255)),
        )
        Table(
            "buy_plans_v3",
            md,
            Column("id", Integer, primary_key=True),
            Column("sales_order_number", String(100)),
        )
        md.create_all(engine)
        with engine.begin() as conn:
            # User holds the approval right — the rename must preserve the value.
            conn.exec_driver_sql("INSERT INTO users (id, can_approve_sales_orders) VALUES (1, 1)")
            # An in-flight QP-sales gate still persisted under the OLD value + a control row.
            conn.exec_driver_sql("INSERT INTO approval_requests (id, gate_type) VALUES (1, 'sales_order')")
            conn.exec_driver_sql("INSERT INTO approval_requests (id, gate_type) VALUES (2, 'buy_plan')")
            # Plan 1: blank SO# -> should receive the QP's value. Plan 2: divergent existing
            # value -> must be preserved (the backfill only fills blanks).
            conn.exec_driver_sql("INSERT INTO buy_plans_v3 (id, sales_order_number) VALUES (1, NULL)")
            conn.exec_driver_sql("INSERT INTO buy_plans_v3 (id, sales_order_number) VALUES (2, 'EXISTING-SO')")
            conn.exec_driver_sql(
                "INSERT INTO quality_plans (id, buy_plan_id, sales_so_number) VALUES (1, 1, 'SO-FROM-QP')"
            )
            conn.exec_driver_sql(
                "INSERT INTO quality_plans (id, buy_plan_id, sales_so_number) VALUES (2, 2, 'SO-DIVERGENT')"
            )
        return engine

    def test_upgrade_moves_data_then_downgrade_restores(self):
        engine = self._engine()

        # ── upgrade ──
        run_ops(engine, _mod.upgrade)

        user_cols = {c["name"] for c in inspect(engine).get_columns("users")}
        assert "can_approve_qp_sales" in user_cols and "can_approve_sales_orders" not in user_cols
        assert "sales_so_number" not in {c["name"] for c in inspect(engine).get_columns("quality_plans")}

        with engine.begin() as conn:
            # Op A: the True value survived the column rename.
            assert conn.exec_driver_sql("SELECT can_approve_qp_sales FROM users WHERE id=1").scalar() == 1
            # Op B: the stale gate value was rewritten; the unrelated gate was left alone.
            gates = dict(conn.exec_driver_sql("SELECT id, gate_type FROM approval_requests").all())
            assert gates == {1: "qp_sales", 2: "buy_plan"}
            # Op C2: blank buy plan got the QP SO#; the divergent existing value was preserved.
            nums = dict(conn.exec_driver_sql("SELECT id, sales_order_number FROM buy_plans_v3").all())
            assert nums == {1: "SO-FROM-QP", 2: "EXISTING-SO"}

        # ── downgrade ── (column re-added empty; gate value restored; SO# loss is accepted)
        run_ops(engine, _mod.downgrade)
        user_cols2 = {c["name"] for c in inspect(engine).get_columns("users")}
        assert "can_approve_sales_orders" in user_cols2 and "can_approve_qp_sales" not in user_cols2
        assert "sales_so_number" in {c["name"] for c in inspect(engine).get_columns("quality_plans")}
        with engine.begin() as conn:
            gates2 = dict(conn.exec_driver_sql("SELECT id, gate_type FROM approval_requests").all())
            assert gates2 == {1: "sales_order", 2: "buy_plan"}

        # ── re-upgrade ── (idempotent forward path)
        run_ops(engine, _mod.upgrade)
        assert "can_approve_qp_sales" in {c["name"] for c in inspect(engine).get_columns("users")}
        assert "sales_so_number" not in {c["name"] for c in inspect(engine).get_columns("quality_plans")}
