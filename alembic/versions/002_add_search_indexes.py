"""Add indexes on columns used by search.

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
    # NOTE: migration 001_initial uses Base.metadata.create_all(checkfirst=True),
    # which already creates these indexes from the SQLAlchemy model definitions.
    # On a fresh DB, 002 would otherwise raise DuplicateTable. Use
    # CREATE INDEX IF NOT EXISTS so this migration is idempotent regardless of
    # whether 001 is replayed (new DB) or stamped (existing prod DB pre-baseline).
    op.execute("CREATE INDEX IF NOT EXISTS ix_requisitions_name ON requisitions (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_requisitions_customer_name ON requisitions (customer_name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_req_primary_mpn ON requirements (primary_mpn)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_req_primary_mpn")
    op.execute("DROP INDEX IF EXISTS ix_requisitions_customer_name")
    op.execute("DROP INDEX IF EXISTS ix_requisitions_name")
