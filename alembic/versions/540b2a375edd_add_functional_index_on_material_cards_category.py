"""Add functional index on material_cards category.

What: Creates a partial functional index on lower(trim(category)) for the
      faceted search service, which filters/groups by that expression on 743K rows.
Called by: Alembic migration chain.
Depends on: 04423c49a688 (clean up material_spec_facets indexes).

Revision ID: 540b2a375edd
Revises: 04423c49a688
Create Date: 2026-03-19 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "540b2a375edd"
down_revision: Union[str, None] = "04423c49a688"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_mc_category_lower",
        "material_cards",
        [sa.text("lower(trim(category))")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mc_category_lower", table_name="material_cards")
