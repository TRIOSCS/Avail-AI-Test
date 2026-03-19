"""Add faceted search tables and specs_structured column.

Revision ID: 81fc4ce30028
Revises: 071_enrichment_runs
Create Date: 2026-03-19 14:43:28.848265
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "81fc4ce30028"
down_revision: Union[str, None] = "071_enrichment_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- New tables ---
    op.create_table(
        "commodity_spec_schemas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commodity", sa.String(length=100), nullable=False),
        sa.Column("spec_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("data_type", sa.String(length=20), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("canonical_unit", sa.String(length=20), nullable=True),
        sa.Column("enum_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("numeric_range", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("is_filterable", sa.Boolean(), server_default="true", nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commodity", "spec_key", name="uq_css_commodity_spec_key"),
    )
    op.create_table(
        "material_spec_conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("material_card_id", sa.Integer(), nullable=False),
        sa.Column("spec_key", sa.String(length=100), nullable=False),
        sa.Column("existing_value", sa.String(length=255), nullable=True),
        sa.Column("existing_source", sa.String(length=50), nullable=True),
        sa.Column("existing_confidence", sa.Float(), nullable=True),
        sa.Column("incoming_value", sa.String(length=255), nullable=True),
        sa.Column("incoming_source", sa.String(length=50), nullable=True),
        sa.Column("incoming_confidence", sa.Float(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("resolved_by", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_card_id"], ["material_cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "material_spec_facets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("material_card_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("spec_key", sa.String(length=100), nullable=False),
        sa.Column("value_text", sa.String(length=255), nullable=True),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_unit", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["material_card_id"], ["material_cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("material_card_id", "spec_key", name="uq_msf_card_spec"),
    )
    op.create_index("ix_msf_card", "material_spec_facets", ["material_card_id"], unique=False)
    op.create_index("ix_msf_category_key", "material_spec_facets", ["category", "spec_key"], unique=False)
    op.create_index(
        "ix_msf_category_key_text", "material_spec_facets", ["category", "spec_key", "value_text"], unique=False
    )
    op.create_index(
        "ix_msf_key_numeric",
        "material_spec_facets",
        ["spec_key", "value_numeric"],
        unique=False,
        postgresql_where=sa.text("value_numeric IS NOT NULL"),
    )
    op.create_index(
        "ix_msf_key_text_card", "material_spec_facets", ["spec_key", "value_text", "material_card_id"], unique=False
    )

    # --- New column on material_cards ---
    op.add_column(
        "material_cards", sa.Column("specs_structured", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )

    # NOTE: Trigram indexes (_trgm) are managed by startup.py, not Alembic.


def downgrade() -> None:
    # --- Drop column ---
    op.drop_column("material_cards", "specs_structured")

    # --- Drop indexes then tables (reverse order) ---
    op.drop_index("ix_msf_key_text_card", table_name="material_spec_facets")
    op.drop_index(
        "ix_msf_key_numeric", table_name="material_spec_facets", postgresql_where=sa.text("value_numeric IS NOT NULL")
    )
    op.drop_index("ix_msf_category_key_text", table_name="material_spec_facets")
    op.drop_index("ix_msf_category_key", table_name="material_spec_facets")
    op.drop_index("ix_msf_card", table_name="material_spec_facets")
    op.drop_table("material_spec_facets")
    op.drop_table("material_spec_conflicts")
    op.drop_table("commodity_spec_schemas")
