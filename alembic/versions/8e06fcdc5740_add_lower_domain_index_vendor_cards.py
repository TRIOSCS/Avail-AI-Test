"""add_lower_domain_index_vendor_cards.

Adds a functional index on lower(domain) for vendor_cards so that
case-insensitive domain lookups in get_or_create_card (Tier 2) can
use an index instead of a sequential scan.

Revision ID: 8e06fcdc5740
Revises: ba090b14bf74
Create Date: 2026-03-20 21:00:12.867258
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e06fcdc5740"
down_revision: Union[str, None] = "ba090b14bf74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_vendor_cards_domain_lower ON vendor_cards (lower(domain))"))


def downgrade() -> None:
    op.drop_index("ix_vendor_cards_domain_lower", table_name="vendor_cards")
