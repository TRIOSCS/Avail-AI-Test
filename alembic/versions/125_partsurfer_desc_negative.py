"""Add partsurfer_desc_negative -- durable negative cache for PartSurfer DESCRIPTION
misses.

One row per normalized HP/HPE spare PN that produced NO usable PartSurfer description, so
the enrichment worker's _partsurfer_desc_pass stops re-fetching dead/ungrammatical spares
live from partsurfer.hpe.com every batch (the throughput win on the 145k not_found cards in
the 743k catalog). This is a DIFFERENT sub-resource from oem_crosswalk (which caches the
spare->canonical-MPN web resolution), so it is its OWN table -- reusing oem_crosswalk's
(spare_norm,'hpe','') no_match key would conflate "no description" with "no crosswalk".

spare_norm is UNIQUE (one row per spare). reason in ('no_result','ungrammatical') drives
the retry window stored on retry_after (looked_up_at + 90d for no_result, + 14d for the
ungrammatical/grammar-declined case -- a parse miss is a short retry, not a permanent
verdict). ix_partsurfer_neg_retry_after indexes the selector's freshness comparison.

Downgrade drops the index + table (the data is re-derivable by re-fetching; acceptable
loss on rollback).

Revision ID: 125_partsurfer_desc_negative
Revises: 124_offer_status_constraint
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "125_partsurfer_desc_negative"
down_revision = "124_offer_status_constraint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partsurfer_desc_negative",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("spare_norm", sa.String(length=64), nullable=False),
        sa.Column("spare_raw", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("looked_up_at", sa.DateTime(), nullable=False),
        sa.Column("retry_after", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spare_norm", name="uq_partsurfer_neg_spare_norm"),
        sa.CheckConstraint(
            "reason IN ('no_result', 'ungrammatical')",
            name="ck_partsurfer_neg_reason",
        ),
    )
    op.create_index("ix_partsurfer_neg_retry_after", "partsurfer_desc_negative", ["retry_after"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_partsurfer_neg_retry_after", table_name="partsurfer_desc_negative")
    op.drop_table("partsurfer_desc_negative")
