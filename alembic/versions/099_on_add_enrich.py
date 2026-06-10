"""On-add auto-enrichment — priority lane stamp + validation conflicts.

What: Adds the three material_cards columns the on-add enrichment feature needs:
        - enrich_requested_at (DateTime, nullable, indexed) — the worker priority-lane
          stamp. Set ONLY by the single-add endpoint (a user is actively waiting on the
          card); the worker's select_batch orders stamped cards first (FIFO) and
          run_one_batch clears the stamp on every batch card so terminal not_found
          cards cannot pin the lane.
        - validation_conflicts (JSONB, nullable) — list of conflict entries
          {"key", "manual": {"value", "updated_at"},
           "evidence": {"source", "tier", "confidence", "value", "observed_at"}}
          recorded by spec_tiers.record_validation_conflict when a tier>=80
          deterministic/authoritative source contradicts a manual (tier 100) value.
          De-duped per (key, evidence.source) — newest evidence replaces.
        - has_validation_conflict (Boolean NOT NULL DEFAULT false) + a PARTIAL index
          (WHERE has_validation_conflict) — the review-queue filter predicate
          (faceted_search_service needs_review branch). Partial: conflicted cards are
          a tiny minority, so a full index would be ~all-false dead weight.
      Rollback drops both indexes and all three columns.
Called by: alembic (upgrade/downgrade).
Depends on: 097_dual_brand (current main head — re-chained from 098_materials_perf_idx
      when #263 merged; before that from 096_spec_provenance when #262 merged
      mid-build).

Revision ID: 099_on_add_enrich
Revises: 097_dual_brand
Create Date: 2026-06-10
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "099_on_add_enrich"
down_revision = "097_dual_brand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_cards",
        sa.Column("enrich_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_material_cards_enrich_requested_at",
        "material_cards",
        ["enrich_requested_at"],
    )

    op.add_column("material_cards", sa.Column("validation_conflicts", JSONB, nullable=True))
    op.add_column(
        "material_cards",
        sa.Column(
            "has_validation_conflict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial index — the review-queue filter only ever asks WHERE has_validation_conflict,
    # and conflicted cards are a tiny minority. sqlite_where keeps the test engine honest.
    op.create_index(
        "ix_material_cards_needs_review",
        "material_cards",
        ["has_validation_conflict"],
        postgresql_where=sa.text("has_validation_conflict"),
        sqlite_where=sa.text("has_validation_conflict"),
    )


def downgrade() -> None:
    op.drop_index("ix_material_cards_needs_review", table_name="material_cards")
    op.drop_column("material_cards", "has_validation_conflict")
    op.drop_column("material_cards", "validation_conflicts")
    op.drop_index("ix_material_cards_enrich_requested_at", table_name="material_cards")
    op.drop_column("material_cards", "enrich_requested_at")
