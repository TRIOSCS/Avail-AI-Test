"""Add quote_requisitions join table + backfill (OQ-02 combined cross-req quote).

A combined quote (OQ-02/REQ-04) spans line items from 2+ requisitions selected
together in the list "Build Quote" flow. ``Quote.requisition_id`` still records the
PRIMARY/anchor requisition (first selected), and this join table links a quote to
EVERY contributing requisition (the primary included) so requisition-scoped read
paths surface a combined quote on all of its requisitions — not just the anchor.

Backfill (same migration): every existing quote gets its own self-row
(``quote_id == id``, ``requisition_id == requisitions``) so the shared read helper
(services/quote_requisitions.py) works uniformly for old single-req quotes too.

Additive/reversible. Downgrade drops the table (and its self-rows).

Revision ID: 175_add_quote_requisitions
Revises: 174_reconcile_uq_drift
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "175_add_quote_requisitions"
down_revision = "174_reconcile_uq_drift"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_requisitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("requisition_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requisition_id"], ["requisitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_id", "requisition_id", name="uq_quote_requisition"),
    )
    op.create_index("ix_quote_requisitions_quote", "quote_requisitions", ["quote_id"], unique=False)
    op.create_index("ix_quote_requisitions_req", "quote_requisitions", ["requisition_id"], unique=False)

    # Backfill one self-row per existing quote so the read helper works for legacy
    # single-req quotes. COALESCE keeps the join row's created_at aligned with the
    # quote when a legacy row has a NULL created_at.
    op.get_bind().execute(
        text(
            "INSERT INTO quote_requisitions (quote_id, requisition_id, created_at) "
            "SELECT id, requisition_id, COALESCE(created_at, now()) FROM quotes"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_quote_requisitions_req", table_name="quote_requisitions")
    op.drop_index("ix_quote_requisitions_quote", table_name="quote_requisitions")
    op.drop_table("quote_requisitions")
