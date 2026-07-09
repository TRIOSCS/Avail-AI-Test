"""Partial indexes on startup-backfill IS NULL predicates (P2.7).

app/startup.py's SLOW deferred backfills (now in run_deferred_startup_backfills,
see P2.7 in docs/CODE_AUDIT_AND_HARDENING_PLAN.md) each scan a table for a narrow
IS NULL predicate every boot to find the (hopefully shrinking, eventually empty)
set of legacy rows still needing normalization. On a fresh/small DB those scans are
free; on a prod-sized table they are a full seq scan repeated on EVERY app restart,
not just the first. A partial index scoped to the exact backfill predicate makes a
repeat-boot scan an index scan over an (ideally near-empty) row set instead —
O(remaining rows), not O(table).

One index per backfill helper's WHERE clause:
  - requirements:        _backfill_normalized_mpn (part 1)
  - material_cards:      _backfill_normalized_mpn (part 2)
  - sightings:            _backfill_sighting_offer_normalized_mpn (sightings half)
  - offers:                _backfill_sighting_offer_normalized_mpn (offers half)
  - sightings:            _backfill_sighting_vendor_normalized
  - offers:                _backfill_offer_vendor_normalized
  - trouble_tickets:      _backfill_ticket_defaults
  - prospect_accounts:    _backfill_sweep_cooldown

All are PostgreSQL partial indexes (postgresql_where) — no-ops on the SQLite test
DB, guarded by dialect like sibling migrations (e.g. 098_materials_perf_idx).

Revision ID: 187_startup_backfill_partial_idx
Revises: a431c202afa4
Create Date: 2026-07-09
"""

import sqlalchemy as sa

from alembic import op

revision = "187_startup_backfill_partial_idx"
# Serialized after P3.1's index migration (both landed on the same branch;
# linear history keeps `alembic downgrade -1` unambiguous — no merge point).
down_revision = "71d3fef96529"
branch_labels = None
depends_on = None

# (index_name, table, columns, postgresql_where) — declarative so upgrade/downgrade
# stay in lockstep. Each indexes the primary key only (existence-check shape,
# mirroring ix_mc_has_datasheet/ix_mc_has_crosses in 098_materials_perf_idx) since
# the backfills only need to find candidate rows, not order or aggregate them.
_PARTIAL_INDEXES = [
    (
        "ix_requirements_backfill_norm_mpn",
        "requirements",
        ["id"],
        "normalized_mpn IS NULL AND primary_mpn IS NOT NULL",
    ),
    (
        "ix_material_cards_backfill_norm_mpn",
        "material_cards",
        ["id"],
        "normalized_mpn IS NULL AND display_mpn IS NOT NULL",
    ),
    (
        "ix_sightings_backfill_norm_mpn",
        "sightings",
        ["id"],
        "normalized_mpn IS NULL AND mpn_matched IS NOT NULL",
    ),
    (
        "ix_offers_backfill_norm_mpn",
        "offers",
        ["id"],
        "normalized_mpn IS NULL AND mpn IS NOT NULL",
    ),
    (
        "ix_sightings_backfill_vendor_norm",
        "sightings",
        ["id"],
        "vendor_name_normalized IS NULL AND vendor_name IS NOT NULL",
    ),
    (
        "ix_offers_backfill_vendor_norm",
        "offers",
        ["id"],
        "vendor_name_normalized IS NULL AND vendor_name IS NOT NULL",
    ),
    (
        "ix_trouble_tickets_backfill_defaults",
        "trouble_tickets",
        ["id"],
        "risk_tier IS NULL AND category IS NULL",
    ),
    (
        "ix_prospect_accounts_backfill_cooldown",
        "prospect_accounts",
        ["id"],
        "swept_at IS NOT NULL AND reclaim_blocked_until IS NULL AND status != 'dismissed'",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Partial (postgresql_where) indexes are PostgreSQL-only; the SQLite test DB
        # runs the same backfill queries without them.
        return
    for name, table, columns, where in _PARTIAL_INDEXES:
        op.create_index(name, table, columns, unique=False, postgresql_where=sa.text(where))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, table, _columns, _where in reversed(_PARTIAL_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
