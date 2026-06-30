"""Tests for migration 173 (Phase D approvals workflow fold — data-only backfills).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto current head 172) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine
      asserting the TWO data backfills, not just that it runs:
        - R2 SO-fold: ACTIVE/INBOUND plans with so_status='pending' are stamped
          so_status='approved' + so_verified_at; DRAFT/already-approved rows are untouched.
        - R3 PO-rights: active verification_group_members gain can_approve_purchase_orders;
          inactive members / non-members / already-granted are untouched (idempotent).
      The migration's PG ``now()`` is registered as a SQLite scalar so the real upgrade()
      runs hermetically via the migration harness (no alembic CLI). ``IS DISTINCT FROM`` is
      native to SQLite 3.39+. Full PG round-trip is proven separately (migration-full-cycle).

Called by: pytest
Depends on: alembic/versions/173_approvals_workflow_fold.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
)
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "173_approvals_workflow_fold.py")
_spec = importlib.util.spec_from_file_location("migration_173", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "173_approvals_workflow_fold"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "172_drop_dup_req_company_idx"


class TestExecution:
    """Upgrade -> downgrade(no-op) -> upgrade on a scratch SQLite engine, asserting both
    backfills."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        # The migration uses PG ``now()`` — register it as a SQLite scalar so upgrade() runs.
        @event.listens_for(engine, "connect")
        def _register_now(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.create_function("now", 0, lambda: "2026-06-30T00:00:00+00:00")

        md = MetaData()
        Table(
            "buy_plans_v3",
            md,
            Column("id", Integer, primary_key=True),
            Column("status", String(30)),
            Column("so_status", String(30)),
            Column("so_verified_at", String(40)),
        )
        Table(
            "users",
            md,
            Column("id", Integer, primary_key=True),
            Column("can_approve_purchase_orders", Boolean),
        )
        Table(
            "verification_group_members",
            md,
            Column("id", Integer, primary_key=True),
            Column("user_id", Integer),
            Column("is_active", Boolean),
        )
        md.create_all(engine)
        with engine.begin() as conn:
            # ── R2 SO-fold fixtures ──
            # 1: ACTIVE + pending -> approved.  2: INBOUND + pending -> approved.
            # 3: DRAFT + pending -> untouched (not in-flight).  4: ACTIVE + already approved.
            conn.exec_driver_sql(
                "INSERT INTO buy_plans_v3 (id, status, so_status, so_verified_at) VALUES (1,'active','pending',NULL)"
            )
            conn.exec_driver_sql(
                "INSERT INTO buy_plans_v3 (id, status, so_status, so_verified_at) VALUES (2,'inbound','pending',NULL)"
            )
            conn.exec_driver_sql(
                "INSERT INTO buy_plans_v3 (id, status, so_status, so_verified_at) VALUES (3,'draft','pending',NULL)"
            )
            conn.exec_driver_sql(
                "INSERT INTO buy_plans_v3 (id, status, so_status, so_verified_at) VALUES (4,'active','approved',NULL)"
            )
            # ── R3 PO-rights fixtures ──
            # 10: active member, not granted -> granted.  11: already granted -> idempotent.
            # 12: INACTIVE member -> untouched.  13: not a member -> untouched.
            conn.exec_driver_sql("INSERT INTO users (id, can_approve_purchase_orders) VALUES (10, 0)")
            conn.exec_driver_sql("INSERT INTO users (id, can_approve_purchase_orders) VALUES (11, 1)")
            conn.exec_driver_sql("INSERT INTO users (id, can_approve_purchase_orders) VALUES (12, 0)")
            conn.exec_driver_sql("INSERT INTO users (id, can_approve_purchase_orders) VALUES (13, 0)")
            conn.exec_driver_sql("INSERT INTO verification_group_members (id, user_id, is_active) VALUES (1, 10, 1)")
            conn.exec_driver_sql("INSERT INTO verification_group_members (id, user_id, is_active) VALUES (2, 11, 1)")
            conn.exec_driver_sql("INSERT INTO verification_group_members (id, user_id, is_active) VALUES (3, 12, 0)")
        return engine

    @staticmethod
    def _state(engine):
        with engine.begin() as conn:
            plans = dict(conn.exec_driver_sql("SELECT id, so_status FROM buy_plans_v3").all())
            ts_set = {
                pid for (pid, ts) in conn.exec_driver_sql("SELECT id, so_verified_at FROM buy_plans_v3").all() if ts
            }
            po = dict(conn.exec_driver_sql("SELECT id, can_approve_purchase_orders FROM users").all())
        return plans, ts_set, po

    def _assert_backfilled(self, engine):
        plans, ts_set, po = self._state(engine)
        # R2: only the in-flight (active/inbound) pending plans flip to approved + stamp ts.
        assert plans == {1: "approved", 2: "approved", 3: "pending", 4: "approved"}
        assert ts_set == {1, 2}  # plan 4 was already approved → not re-stamped
        # R3: only the ACTIVE member without the flag gains it; everyone else unchanged.
        assert po == {10: 1, 11: 1, 12: 0, 13: 0}

    def test_upgrade_backfills_then_downgrade_is_noop_then_reupgrade(self):
        engine = self._engine()

        # ── upgrade ──
        run_ops(engine, _mod.upgrade)
        self._assert_backfilled(engine)

        # ── downgrade (documented no-op: the backfilled data persists) ──
        run_ops(engine, _mod.downgrade)
        self._assert_backfilled(engine)

        # ── re-upgrade (idempotent forward path) ──
        run_ops(engine, _mod.upgrade)
        self._assert_backfilled(engine)
