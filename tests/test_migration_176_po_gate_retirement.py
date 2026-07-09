"""Tests for migration 176 (deal-level PO gate retirement — data-only updates).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto current head 175) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine
      asserting the TWO data updates, not just that they run:
        - (a) stale-request cancel: approval_requests with gate_type='purchase_order' AND
          subject_type='buy_plan' AND status='requested' flip to 'cancelled' with
          resolved_at + resolution_note stamped; qp_purchasing rows, non-buy_plan subjects
          and already-resolved rows are untouched.
        - (b) plan release: buy_plans_v3 status='inbound' flips to 'active'; every other
          status is untouched.
      The migration's PG ``now()`` is registered as a SQLite scalar so the real upgrade()
      runs hermetically via the migration harness (no alembic CLI). Full PG round-trip is
      proven separately on a throwaway PostgreSQL 16.

Called by: pytest
Depends on: alembic/versions/176_retire_deal_po_gate.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "176_retire_deal_po_gate.py")
_spec = importlib.util.spec_from_file_location("migration_176", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "176_retire_deal_po_gate"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "175_add_quote_requisitions"


class TestExecution:
    """Upgrade -> downgrade(no-op) -> upgrade on a scratch SQLite engine, asserting both
    updates."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        # The migration uses PG ``now()`` — register it as a SQLite scalar so upgrade() runs.
        @event.listens_for(engine, "connect")
        def _register_now(dbapi_conn, _record):
            dbapi_conn.create_function("now", 0, lambda: "2026-07-03T00:00:00+00:00")

        md = MetaData()
        Table(
            "approval_requests",
            md,
            Column("id", Integer, primary_key=True),
            Column("gate_type", String(50)),
            Column("subject_type", String(50)),
            Column("status", String(50)),
            Column("resolved_at", String(40)),
            Column("resolution_note", Text),
        )
        Table(
            "buy_plans_v3",
            md,
            Column("id", Integer, primary_key=True),
            Column("status", String(30)),
        )
        md.create_all(engine)
        with engine.begin() as conn:
            # ── (a) stale-request fixtures ──
            # 1: deal-level PO + buy_plan + requested -> cancelled (the retirement target).
            # 2: same gate/subject but already approved -> untouched (resolved history kept).
            # 3: same gate but subject quality_plan -> untouched (predicate is buy_plan-scoped).
            # 4: qp_purchasing gate (166 rename) + requested -> untouched (different gate).
            # 5: same gate/subject but user-cancelled -> untouched (note NOT overwritten).
            rows = [
                (1, "purchase_order", "buy_plan", "requested", None, None),
                (2, "purchase_order", "buy_plan", "approved", "2026-06-01T00:00:00+00:00", None),
                (3, "purchase_order", "quality_plan", "requested", None, None),
                (4, "qp_purchasing", "quality_plan", "requested", None, None),
                (5, "purchase_order", "buy_plan", "cancelled", "2026-06-02T00:00:00+00:00", "user withdrew"),
            ]
            for row in rows:
                conn.exec_driver_sql(
                    "INSERT INTO approval_requests (id, gate_type, subject_type, status, resolved_at, "
                    "resolution_note) VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
            # ── (b) plan-release fixtures ──
            # 10: inbound -> active (the release target). 11-13: every other status untouched.
            for pid, status in [(10, "inbound"), (11, "active"), (12, "draft"), (13, "completed")]:
                conn.exec_driver_sql("INSERT INTO buy_plans_v3 (id, status) VALUES (?, ?)", (pid, status))
        return engine

    @staticmethod
    def _state(engine):
        with engine.begin() as conn:
            reqs = {
                rid: (status, resolved_at, note)
                for rid, status, resolved_at, note in conn.exec_driver_sql(
                    "SELECT id, status, resolved_at, resolution_note FROM approval_requests"
                ).all()
            }
            plans = dict(conn.exec_driver_sql("SELECT id, status FROM buy_plans_v3").all())
        return reqs, plans

    def _assert_retired(self, engine):
        reqs, plans = self._state(engine)
        # (a) only row 1 (purchase_order + buy_plan + requested) is cancelled + stamped.
        assert {rid: status for rid, (status, _ts, _note) in reqs.items()} == {
            1: "cancelled",
            2: "approved",
            3: "requested",
            4: "requested",
            5: "cancelled",
        }
        assert reqs[1][1] is not None  # resolved_at stamped
        assert reqs[1][2] == (
            "retired: deal-level PO gate removed 2026-07 — see per-PO PENDING_VERIFY sign-off instead"
        )
        assert reqs[2][2] is None and reqs[3][2] is None and reqs[4][2] is None
        assert reqs[5] == ("cancelled", "2026-06-02T00:00:00+00:00", "user withdrew")  # untouched
        # (b) only the inbound plan flips to active; everything else unchanged.
        assert plans == {10: "active", 11: "active", 12: "draft", 13: "completed"}

    def test_upgrade_retires_then_downgrade_is_noop_then_reupgrade(self):
        engine = self._engine()

        # ── upgrade ──
        run_ops(engine, _mod.upgrade)
        self._assert_retired(engine)

        # ── downgrade (documented no-op: the updated data persists) ──
        run_ops(engine, _mod.downgrade)
        self._assert_retired(engine)

        # ── re-upgrade (idempotent forward path: row 1 no longer matches 'requested') ──
        run_ops(engine, _mod.upgrade)
        self._assert_retired(engine)
