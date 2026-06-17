"""Add offer qualification capture columns + migrate legacy condition.

What: adds offers.qualification_status / qualification_note / qualification (JSON)
      and an index on qualification_status; migrates legacy condition 'used' -> 'pulls'.
Downgrade: drops the index + 3 columns. The 'used' -> 'pulls' data change is NOT
      reversed (legacy 'used' is unrecoverable post-merge) — documented one-way.
Called by: alembic (upgrade/downgrade).
Depends on: offers table.

Revision ID: 108_offer_qualification
Revises: 107_is_scratch_requisitions
Create Date: 2026-06-17
"""

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import text

from alembic import op

revision = "108_offer_qualification"
down_revision = "107_is_scratch_requisitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("qualification_status", sa.String(length=20), nullable=True))
    op.add_column("offers", sa.Column("qualification_note", sa.Text(), nullable=True))
    op.add_column("offers", sa.Column("qualification", sa.JSON(), nullable=True))
    op.create_index("ix_offers_qualification_status", "offers", ["qualification_status"])

    conn = op.get_bind()
    result = conn.execute(text("UPDATE offers SET condition = 'pulls' WHERE condition = 'used'"))
    logger.info("108_offer_qualification: migrated {} legacy 'used' offers -> 'pulls'", result.rowcount)


def downgrade() -> None:
    op.drop_index("ix_offers_qualification_status", table_name="offers")
    op.drop_column("offers", "qualification")
    op.drop_column("offers", "qualification_note")
    op.drop_column("offers", "qualification_status")
    # Note: 'pulls' -> 'used' is intentionally NOT reversed (legacy value unrecoverable).
