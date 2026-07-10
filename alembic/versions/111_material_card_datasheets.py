"""material_card_datasheets table + datasheet stamp columns on material_cards."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "111_material_card_datasheets"
down_revision = "110_crm_cadence_clocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("datasheet_captured_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("material_cards", sa.Column("datasheet_searched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "material_card_datasheets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_card_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("onedrive_item_id", sa.String(length=500), nullable=True),
        sa.Column("onedrive_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_card_id"], ["material_cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_material_card_datasheets_material_card_id",
        "material_card_datasheets",
        ["material_card_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_material_card_datasheets_material_card_id", table_name="material_card_datasheets")
    op.drop_table("material_card_datasheets")
    op.drop_column("material_cards", "datasheet_searched_at")
    op.drop_column("material_cards", "datasheet_captured_at")
