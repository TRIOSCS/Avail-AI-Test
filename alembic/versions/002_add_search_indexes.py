"""Add indexes on columns used by search

Revision ID: 002_search_indexes
Revises: 001_initial
Create Date: 2026-02-21

Adds B-tree indexes on requisitions.name, requisitions.customer_name,
and requirements.primary_mpn to eliminate full table scans during search.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_search_indexes"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_requisitions_name", "requisitions", ["name"])
    op.create_index("ix_requisitions_customer_name", "requisitions", ["customer_name"])
    op.create_index("ix_req_primary_mpn", "requirements", ["primary_mpn"])


def downgrade() -> None:
    op.drop_index("ix_req_primary_mpn", table_name="requirements")
    op.drop_index("ix_requisitions_customer_name", table_name="requisitions")
    op.drop_index("ix_requisitions_name", table_name="requisitions")
