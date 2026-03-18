"""Add vendor_sighting_summary table.

Revision ID: 1e277d3b08a6
Revises: 080
Create Date: 2026-03-18 18:32:00.411714

Creates the vendor_sighting_summary table — a materialized aggregation of
sightings grouped by (requirement_id, vendor_name). Populated by the
sighting_aggregation service; queried by the sourcing tab for instant display.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1e277d3b08a6"
down_revision: Union[str, None] = "080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_sighting_summary",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("requirement_id", sa.Integer(), nullable=False),
        sa.Column("vendor_name", sa.String(), nullable=False),
        sa.Column("vendor_phone", sa.String(), nullable=True),
        sa.Column("estimated_qty", sa.Integer(), nullable=True),
        sa.Column("avg_price", sa.Float(), nullable=True),
        sa.Column("best_price", sa.Float(), nullable=True),
        sa.Column("listing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_types", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("tier", sa.String(length=20), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("requirement_id", "vendor_name", name="uq_vss_req_vendor"),
    )
    op.create_index("ix_vss_requirement", "vendor_sighting_summary", ["requirement_id"])
    op.create_index("ix_vss_vendor", "vendor_sighting_summary", ["vendor_name"])
    op.create_index("ix_vss_score", "vendor_sighting_summary", ["score"])


def downgrade() -> None:
    op.drop_index("ix_vss_score", table_name="vendor_sighting_summary")
    op.drop_index("ix_vss_vendor", table_name="vendor_sighting_summary")
    op.drop_index("ix_vss_requirement", table_name="vendor_sighting_summary")
    op.drop_table("vendor_sighting_summary")
