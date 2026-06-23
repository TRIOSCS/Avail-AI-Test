"""Tests for migration 129 (cutover: drop bids + bid_solicitations).

What: revision metadata (id <= 32 vs PG VARCHAR(32), chains onto 128) plus an
      executable upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite
      engine. The upgrade drops both tables; the downgrade recreates them structure-
      only (schema-reversible, no data restore). Portable DDL runs on SQLite via the
      migration harness.
Called by: pytest
Depends on: alembic/versions/129_drop_bid_tables.py, tests/migration_harness.run_ops
"""

import importlib.util
import os

from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "129_drop_bid_tables.py")
_spec = importlib.util.spec_from_file_location("migration_129", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "129_drop_bid_tables"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision(self):
        assert _mod.down_revision == "128_bid_back_schema"


class TestExecution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        # Pre-create the two tables the upgrade drops (+ the FK-target tables the
        # downgrade recreates them against, so the round trip is self-contained).
        Table("users", md, Column("id", Integer, primary_key=True))
        Table("companies", md, Column("id", Integer, primary_key=True))
        Table("vendor_cards", md, Column("id", Integer, primary_key=True))
        Table("excess_line_items", md, Column("id", Integer, primary_key=True))
        Table(
            "bids",
            md,
            Column("id", Integer, primary_key=True),
            Column("excess_line_item_id", Integer),
            Column("unit_price", Numeric(12, 4)),
            Column("quantity_wanted", Integer),
            Column("created_by", Integer),
        )
        Table(
            "bid_solicitations",
            md,
            Column("id", Integer, primary_key=True),
            Column("excess_line_item_id", Integer),
            Column("contact_id", Integer),
            Column("sent_by", Integer),
            Column("status", String(20)),
        )
        md.create_all(engine)
        return engine

    def test_upgrade_downgrade_upgrade(self):
        engine = self._engine()

        # UPGRADE — both retired tables are dropped.
        run_ops(engine, _mod.upgrade)
        tables = set(inspect(engine).get_table_names())
        assert "bids" not in tables
        assert "bid_solicitations" not in tables

        # DOWNGRADE — recreates both structure-only (schema-reversible).
        run_ops(engine, _mod.downgrade)
        tables = set(inspect(engine).get_table_names())
        assert {"bids", "bid_solicitations"} <= tables
        bidsol_cols = {c["name"] for c in inspect(engine).get_columns("bid_solicitations")}
        # The full pre-cutover column set (001 baseline), not just the original create.
        assert {"recipient_email", "graph_message_id", "parsed_bid_id", "body_preview"} <= bidsol_cols
        bidsol_idx = {i["name"] for i in inspect(engine).get_indexes("bid_solicitations")}
        assert "ix_bidsol_status" in bidsol_idx

        # UPGRADE again — idempotent re-drop.
        run_ops(engine, _mod.upgrade)
        tables = set(inspect(engine).get_table_names())
        assert "bids" not in tables
        assert "bid_solicitations" not in tables
