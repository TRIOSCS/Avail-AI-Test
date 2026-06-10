"""Add oem_crosswalk table — permanent OEM spare→canonical-MPN web-resolution cache.

One row per grounded resolution of an OEM/system-vendor spare PN (Phase A: HP/HPE via
PartSurfer) to the canonical manufacturer MPN it relabels, INCLUDING negative rows
(status='no_match' — a 90-day negative cache so an uncatalogued spare is not re-fetched
daily). no_match rows store source_domain='' (NOT NULL sentinel — NULLs are pairwise-
distinct in a UNIQUE constraint, so a nullable domain would never dedupe negatives),
making uq_oem_crosswalk_edge enforce one negative row per (spare_norm, vendor).
ck_oem_crosswalk_status_canonical pins the status×canonical nullability invariant.
Written by the enrichment worker's paced resolution pass (Pass A) and the
backfill_oem_crosswalk CLI; read by app/services/oem_crosswalk_enrich.py (the
deterministic tier-80 writer pass).

Downgrade drops the indexes + table (data is re-fetchable via the resolver; acceptable
loss on rollback).

Revision ID: 100_oem_crosswalk
Revises: 099_on_add_enrich
Create Date: 2026-06-10

NOTE: 092 is permanently retired (pre-registry reservation noted in 094's header) — the
chain runs 096 -> 098 -> 097 -> 099 -> 100 (re-chains kept the claimed numbers).
"""

import sqlalchemy as sa

from alembic import op

revision = "100_oem_crosswalk"
down_revision = "099_on_add_enrich"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oem_crosswalk",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("spare_raw", sa.String(length=64), nullable=False),
        sa.Column("spare_norm", sa.String(length=64), nullable=False),
        sa.Column("vendor", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("canonical_mpn_raw", sa.String(length=64), nullable=True),
        sa.Column("canonical_mpn_norm", sa.String(length=64), nullable=True),
        sa.Column("canonical_manufacturer", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("looked_up_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spare_norm", "vendor", "source_domain", name="uq_oem_crosswalk_edge"),
        sa.CheckConstraint(
            "(status = 'resolved') = (canonical_mpn_norm IS NOT NULL)",
            name="ck_oem_crosswalk_status_canonical",
        ),
    )
    op.create_index("ix_oem_crosswalk_spare_norm", "oem_crosswalk", ["spare_norm"], unique=False)
    op.create_index("ix_oem_crosswalk_canonical_norm", "oem_crosswalk", ["canonical_mpn_norm"], unique=False)
    op.create_index("ix_oem_crosswalk_status", "oem_crosswalk", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_oem_crosswalk_status", table_name="oem_crosswalk")
    op.drop_index("ix_oem_crosswalk_canonical_norm", table_name="oem_crosswalk")
    op.drop_index("ix_oem_crosswalk_spare_norm", table_name="oem_crosswalk")
    op.drop_table("oem_crosswalk")
