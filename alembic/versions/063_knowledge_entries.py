"""knowledge_entries table for Knowledge Ledger Phase 1.

Revision ID: 063
Revises: 062
Create Date: 2026-03-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "063"
down_revision: Union[str, None] = "062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_to_ids", postgresql.JSON(), nullable=True, server_default="[]"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mpn", sa.String(255), nullable=True),
        sa.Column("vendor_card_id", sa.Integer(), sa.ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requisition_id", sa.Integer(), sa.ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_index("ix_ke_requisition", "knowledge_entries", ["requisition_id", "created_at"])
    op.create_index("ix_ke_mpn", "knowledge_entries", ["mpn"])
    op.create_index("ix_ke_company", "knowledge_entries", ["company_id", "created_at"])
    op.create_index("ix_ke_vendor", "knowledge_entries", ["vendor_card_id"])
    op.create_index("ix_ke_parent", "knowledge_entries", ["parent_id"])
    op.create_index(
        "ix_ke_expires",
        "knowledge_entries",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index("ix_knowledge_entries_id", "knowledge_entries", ["id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_entries_id", table_name="knowledge_entries")
    op.drop_index("ix_ke_expires", table_name="knowledge_entries")
    op.drop_index("ix_ke_parent", table_name="knowledge_entries")
    op.drop_index("ix_ke_vendor", table_name="knowledge_entries")
    op.drop_index("ix_ke_company", table_name="knowledge_entries")
    op.drop_index("ix_ke_mpn", table_name="knowledge_entries")
    op.drop_index("ix_ke_requisition", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
