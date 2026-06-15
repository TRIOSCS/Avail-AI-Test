"""Demand-telemetry columns + worker-queue ordering index on material_cards.

Two nullable columns carrying TRIO's own demand signal from the SFDC Weekly Export
(LSC1__Material__c — Sourced_Qty_Last_90_Days__c 92.2% filled, Most_Recent_Source_TS__c
87.9%), backfilled ONE-SHOT by app/management/import_demand_telemetry.py (dry-run by
default; NO recurring refresh — the export is a static snapshot, re-import is an
explicit operator step whenever a new export lands):

- sourced_qty_90d INT NULL — units TRIO sourced for this MPN in the export's trailing
  90 days. Prioritization signal only, never displayed as a fact.
- last_sourced_at TIMESTAMPTZ NULL — most recent sourcing event timestamp.
  sa.DateTime(timezone=True) matches UTCDateTime's dialect impl so future
  autogenerate runs see no type diff.

Plus ix_mc_demand_queue (PostgreSQL only): a partial expression index whose key order
is the EXACT new ORDER BY of the worker's select_batch — (enrich_requested_at ASC
[NULLS LAST is the ASC default], (enrichment_status = 'unenriched') DESC,
sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS LAST, id) — partial on the
batch query's always-present filter terms (deleted_at IS NULL AND is_internal_part IS
false). Verified against a scratch PG 16 at live volume (743,125 rows, live status/
telemetry distribution): the planner takes an ordered Index Scan + LIMIT 5 on both the
literal and the bind-param (PREPARE/EXECUTE custom-plan) shapes — 8 buffers, ~0.1 ms,
vs a ~740k-row top-N heapsort every 30 s loop tick without it — and the index-scan row
order is identical to a forced explicit sort. The status OR-condition stays OUT of the
index predicate (its retry cutoffs are time-varying) and is applied as a scan filter,
which the plan shows costs nothing at LIMIT 5 because ~99.6% of live rows are
'unenriched' (eligible).

SQLite (test engine) gets the two columns but NOT the index — DESC NULLS LAST index
keys are not valid SQLite index DDL, and SQLite queries the same shapes unindexed
(feedback_sqlite_masks_postgres: the PG behavior is what the scratch-PG EXPLAIN above
verifies). For the same reason the index is deliberately NOT declared on the model
(migration-owned, like the 098 perf indexes).

Downgrade drops the index (PG) and both columns (both engines).

Revision ID: 105_demand_telemetry
Revises: 104_trust_telemetry
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "105_demand_telemetry"
down_revision = "104_trust_telemetry"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_mc_demand_queue"


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("sourced_qty_90d", sa.Integer(), nullable=True))
    op.add_column("material_cards", sa.Column("last_sourced_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # DESC NULLS LAST index keys are PostgreSQL-only syntax; the SQLite test DB
        # runs the same query shape without the index (feedback_sqlite_masks_postgres).
        return
    # No if_not_exists: nothing else creates this name, so a pre-existing index can
    # only be out-of-band DDL with a possibly different definition — fail loudly.
    op.create_index(
        _INDEX_NAME,
        "material_cards",
        [
            sa.text("enrich_requested_at ASC"),
            sa.text("(enrichment_status = 'unenriched') DESC"),
            sa.text("sourced_qty_90d DESC NULLS LAST"),
            sa.text("last_sourced_at DESC NULLS LAST"),
            sa.text("id"),
        ],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL AND is_internal_part IS false"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index(_INDEX_NAME, table_name="material_cards")
    op.drop_column("material_cards", "last_sourced_at")
    op.drop_column("material_cards", "sourced_qty_90d")
