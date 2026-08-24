"""214_manufacturer_aliases_pending — queue AI-proposed manufacturer aliases.

manufacturer strings that miss normalize_brand_name accumulate silently as variants
("Seagate"/"SEAGATE"/"Seagate Technology"). The nightly harvester maps each unmatched
variant to an existing canonical or 'new'/'unknown' via Claude and queues it here for
human approval; approve appends the variant to the canonical Manufacturer.aliases JSON
(spec_codes pending pattern). Never rewrites raw source-reported columns.

Additive + reversible (downgrade drops the table). Round-tripped on throwaway PG.
Chains onto 213_offer_lead_time_days.

Revision ID: 214_manufacturer_aliases_pending
Revises: 213_offer_lead_time_days
"""

import sqlalchemy as sa

from alembic import op

revision = "214_manufacturer_aliases_pending"
down_revision = "213_offer_lead_time_days"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manufacturer_aliases_pending",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("variant", sa.String(length=255), nullable=False),
        sa.Column("variant_normalized", sa.String(length=255), nullable=False),
        sa.Column("proposed_canonical", sa.String(length=255), nullable=True),
        sa.Column("proposed_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="ai"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("variant", name="uq_mfr_alias_pending_variant"),
    )
    # Name must match SQLAlchemy's auto-name for Column(..., index=True) on the model, or
    # the fresh-DB schema-drift gate sees a remove_index/add_index pair.
    op.create_index(
        "ix_manufacturer_aliases_pending_variant_normalized",
        "manufacturer_aliases_pending",
        ["variant_normalized"],
    )


def downgrade() -> None:
    op.drop_index("ix_manufacturer_aliases_pending_variant_normalized", table_name="manufacturer_aliases_pending")
    op.drop_table("manufacturer_aliases_pending")
