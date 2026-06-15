"""Add is_scratch flag to requisitions (scratch / quick-source reqs).

What: adds ``requisitions.is_scratch`` BOOLEAN NOT NULL DEFAULT FALSE plus a partial
      index on ``(created_by) WHERE is_scratch`` used by the quick-source service's
      ``get_or_create_scratch_req`` lookup. Scratch reqs are created by a one-off Search
      action (Send RFQ / Add Offer) and are hidden from the normal requisitions list +
      picker. The ``server_default`` keeps every existing row valid.

Downgrade: drops the index then the column (reversible).

Called by: alembic (upgrade/downgrade).
Depends on: requisitions table.

Revision ID: 107_is_scratch_requisitions
Revises: 106_brand_canonicalization
Create Date: 2026-06-15
"""

import sqlalchemy as sa

from alembic import op

revision = "107_is_scratch_requisitions"
down_revision = "106_brand_canonicalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requisitions",
        sa.Column("is_scratch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_requisitions_scratch_user",
        "requisitions",
        ["created_by"],
        postgresql_where=sa.text("is_scratch"),
    )


def downgrade() -> None:
    op.drop_index("ix_requisitions_scratch_user", table_name="requisitions")
    op.drop_column("requisitions", "is_scratch")
