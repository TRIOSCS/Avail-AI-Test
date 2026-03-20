"""Add manufacturer and lifecycle status indexes.

Revision ID: fb863358a701
Revises: 8aad37e73b45
Create Date: 2026-03-20 19:24:25.210675
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb863358a701"
down_revision: Union[str, None] = "8aad37e73b45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(op.f("ix_material_cards_manufacturer"), "material_cards", ["manufacturer"], unique=False)
    op.create_index(op.f("ix_material_cards_lifecycle_status"), "material_cards", ["lifecycle_status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_material_cards_lifecycle_status"), table_name="material_cards")
    op.drop_index(op.f("ix_material_cards_manufacturer"), table_name="material_cards")
