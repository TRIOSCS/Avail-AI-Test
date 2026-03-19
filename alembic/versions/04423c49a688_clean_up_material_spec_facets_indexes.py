"""Clean up material_spec_facets indexes.

What: Drop unused ix_msf_key_text_card and redundant ix_msf_card indexes.
      Recreate ix_msf_key_numeric with category as leading column.
Called by: Alembic migration chain.
Depends on: 8e2994d58df3 (drop material_spec_conflicts table).

Revision ID: 04423c49a688
Revises: 8e2994d58df3
Create Date: 2026-03-19 20:47:30.918055
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04423c49a688"
down_revision: Union[str, None] = "8e2994d58df3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop unused index (no query uses spec_key + value_text + material_card_id)
    op.drop_index("ix_msf_key_text_card", table_name="material_spec_facets")

    # Drop redundant index (covered by FK index and uq_msf_card_spec)
    op.drop_index("ix_msf_card", table_name="material_spec_facets")

    # Recreate numeric index with category as leading column to match query patterns
    op.drop_index("ix_msf_key_numeric", table_name="material_spec_facets")
    op.create_index(
        "ix_msf_key_numeric",
        "material_spec_facets",
        ["category", "spec_key", "value_numeric"],
        postgresql_where="value_numeric IS NOT NULL",
    )


def downgrade() -> None:
    # Restore original numeric index (without category)
    op.drop_index("ix_msf_key_numeric", table_name="material_spec_facets")
    op.create_index(
        "ix_msf_key_numeric",
        "material_spec_facets",
        ["spec_key", "value_numeric"],
        postgresql_where="value_numeric IS NOT NULL",
    )

    # Restore redundant card index
    op.create_index(
        "ix_msf_card",
        "material_spec_facets",
        ["material_card_id"],
    )

    # Restore key_text_card index
    op.create_index(
        "ix_msf_key_text_card",
        "material_spec_facets",
        ["spec_key", "value_text", "material_card_id"],
    )
