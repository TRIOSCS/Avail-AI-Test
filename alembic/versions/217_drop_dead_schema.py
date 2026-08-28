"""217_drop_dead_schema — drop dead schema (Decisions I/J/K/L).

Four independently-scoped drops, all verified dead on prod (2026-08-29 scoping pass;
anchors re-verified at build time):

- **Decision I** — ``enrichment_queue`` table + the ``EnrichmentQueue`` ORM model.
  Superseded by ``material_cards.enrichment_status`` as the single source of
  enrichment state (see the NOTE in ``app/services/enrichment_worker/worker.py``); the
  merge services' repoint tuples (company/vendor) that reassigned/detached
  ``enrichment_queue`` rows on merge are removed same-commit since the table is gone.
- **Decision J** — ``excess_lists.total_line_items``. A denormalized write-side counter
  that nothing ever reads: the UI computes line counts live via ``len(items)``
  (app/routers/resell.py); only the writers (import/add_line/delete_line) and tests
  touched it. All writers + seeders + the ~23 test call sites are stripped same-commit.
- **Decision K** — ``sync_logs`` + ``enrichment_credit_usage`` tables (prod-lineage-only:
  created by raw startup DDL / migration 030, zero application code references left
  after #751 removed the ``SyncLog`` model and the sync-logs admin endpoint) and
  ``excess_line_items.market_price`` / ``demand_score`` (prod-lineage-only columns from
  the original excess-inventory migration; no ORM model ever declared them).
- **Decision L** — the 17 legacy NOT-VALID ``chk_*`` constraints from the 001-era squash
  (migration 048) that are superseded by the modern named ``ck_*`` constraints the
  models now declare (migration 212) — e.g. ``chk_offer_price`` demands
  ``unit_price > 0`` while the app deliberately supports zero-price free-sample offers.
  ``chk_offer_status`` is EXCLUDED — already handled by migration 124, untouched here.

Reversibility: everything is either 0 rows (enrichment_queue, sync_logs — #751 already
zeroed sync_logs; enrichment_credit_usage is empty) or exactly reconstructible EXCEPT
the ``market_price`` / ``demand_score`` cell values on the 2026-03-25 demo seed lists
(15 + 15 cells) — the ONLY documented data loss. ``excess_lists.total_line_items`` is
re-backfilled from ``count(*)`` on downgrade (exact, since it was always kept in lock-step
with the real row count). The 17 chk_* constraints recreate NOT VALID from migration
048's original predicates. ``enrichment_queue`` recreates in its final post-085 (UTC
timestamptz) / 049 (index reconciliation) / d2bea118f720 (FK ondelete fixups) shape,
matching the removed ORM model exactly.

NEVER TOUCHED: intel_cache (load-bearing raw-SQL cache), _sp1_desc_backup (migration
091's downgrade restore path), the modern ck_* constraints, chk_offer_status (124).

Round-tripped upgrade -> downgrade -> upgrade on a THROWAWAY PostgreSQL 16 instance.

Revision ID: 217_drop_dead_schema
Revises: 216_resell_erp_refs
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "217_drop_dead_schema"
down_revision = "216_resell_erp_refs"
branch_labels = None
depends_on = None

# Decision L: the 17 legacy NOT-VALID chk_* constraints (migration 048's predicates,
# verbatim). chk_offer_status is deliberately absent — migration 124 already reconciled
# it into ck_offers_status and owns its drop/recreate.
_LEGACY_CHECKS: list[tuple[str, str, str]] = [
    ("requirements", "chk_req_target_qty", "target_qty IS NULL OR target_qty >= 1"),
    ("requirements", "chk_req_target_price", "target_price IS NULL OR target_price >= 0"),
    ("requirements", "chk_req_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
    (
        "requirements",
        "chk_req_packaging",
        "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')",
    ),
    ("sightings", "chk_sight_qty", "qty_available IS NULL OR qty_available > 0"),
    ("sightings", "chk_sight_price", "unit_price IS NULL OR unit_price > 0"),
    ("sightings", "chk_sight_moq", "moq IS NULL OR moq > 0"),
    ("sightings", "chk_sight_confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
    ("sightings", "chk_sight_score", "score IS NULL OR score >= 0"),
    ("sightings", "chk_sight_lead_time", "lead_time_days IS NULL OR lead_time_days >= 0"),
    ("sightings", "chk_sight_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
    (
        "sightings",
        "chk_sight_packaging",
        "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
    ),
    ("offers", "chk_offer_qty", "qty_available IS NULL OR qty_available > 0"),
    ("offers", "chk_offer_price", "unit_price IS NULL OR unit_price > 0"),
    ("offers", "chk_offer_moq", "moq IS NULL OR moq > 0"),
    ("offers", "chk_offer_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
    (
        "offers",
        "chk_offer_packaging",
        "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
    ),
]


def upgrade() -> None:
    # ── Decision I: enrichment_queue ────────────────────────────────────────────
    op.drop_table("enrichment_queue", if_exists=True)

    # ── Decision J: excess_lists.total_line_items ───────────────────────────────
    op.drop_column("excess_lists", "total_line_items", if_exists=True)

    # ── Decision K: prod-lineage-only objects ───────────────────────────────────
    op.drop_table("sync_logs", if_exists=True)
    op.drop_table("enrichment_credit_usage", if_exists=True)
    op.drop_column("excess_line_items", "market_price", if_exists=True)
    op.drop_column("excess_line_items", "demand_score", if_exists=True)

    # ── Decision L: 17 legacy NOT-VALID chk_* constraints (Postgres-only DDL — the
    #    original 048 CHECKs were raw sa.text with no dialect guard; this mirrors that
    #    plus migration 124's guard convention for the same constraint family). ──────
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, name, _predicate in _LEGACY_CHECKS:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def downgrade() -> None:
    # ── Decision L: recreate NOT VALID from migration 048's predicates ─────────
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, name, predicate in _LEGACY_CHECKS:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({predicate}) NOT VALID")

    # ── Decision K: recreate prod-lineage-only objects in their historical shape ──
    op.add_column("excess_line_items", sa.Column("market_price", sa.Numeric(precision=12, scale=4), nullable=True))
    op.add_column("excess_line_items", sa.Column("demand_score", sa.Integer(), nullable=True))

    op.create_table(
        "enrichment_credit_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_ecu_provider_month",
        "enrichment_credit_usage",
        ["provider", "month"],
        unique=True,
        if_not_exists=True,
    )

    op.create_table(
        "sync_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("row_counts", sa.JSON(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_sync_source_time", "sync_logs", ["source", "started_at"], unique=False, if_not_exists=True)

    # ── Decision J: total_line_items, backfilled from the real row count (exact —
    #    the column was always kept in lock-step with actual ExcessLineItem rows) ──
    op.add_column("excess_lists", sa.Column("total_line_items", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE excess_lists SET total_line_items = ("
        "SELECT count(*) FROM excess_line_items WHERE excess_line_items.excess_list_id = excess_lists.id)"
    )

    # ── Decision I: enrichment_queue, final post-085/049/d2bea118f720 shape (matches
    #    the removed ORM model's columns/FKs/indexes exactly) ──────────────────────
    op.create_table(
        "enrichment_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_card_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("vendor_contact_id", sa.Integer(), nullable=True),
        sa.Column("enrichment_type", sa.String(length=50), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=False),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("proposed_value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("batch_job_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_job_id"], ["enrichment_jobs.id"], name="enrichment_queue_batch_job_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="enrichment_queue_company_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"], ["users.id"], name="enrichment_queue_reviewed_by_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["vendor_card_id"],
            ["vendor_cards.id"],
            name="enrichment_queue_vendor_card_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_contact_id"],
            ["vendor_contacts.id"],
            name="enrichment_queue_vendor_contact_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_eq_status", "enrichment_queue", ["status"], unique=False, if_not_exists=True)
    op.create_index("ix_eq_vendor", "enrichment_queue", ["vendor_card_id"], unique=False, if_not_exists=True)
    op.create_index("ix_eq_company", "enrichment_queue", ["company_id"], unique=False, if_not_exists=True)
    op.create_index("ix_eq_batch", "enrichment_queue", ["batch_job_id"], unique=False, if_not_exists=True)
    op.create_index(
        "ix_eq_status_created", "enrichment_queue", ["status", "created_at"], unique=False, if_not_exists=True
    )
    op.create_index("ix_eq_status_source", "enrichment_queue", ["status", "source"], unique=False, if_not_exists=True)
    op.create_index("ix_eq_reviewed_by", "enrichment_queue", ["reviewed_by_id"], unique=False, if_not_exists=True)
