# Restore GIN trigram index on requirements.substitutes_text.
# Dropped in migration 049 (reconcile_schema_drift) but needed for
# ILIKE search performance on substitutes.
# Depends on: pg_trgm extension (already enabled).
"""Restore substitutes_text gin index.

Revision ID: 9c7e1ed1db3e
Revises: b7e2a1f3c4d5
Create Date: 2026-03-21
"""

from typing import Union

from alembic import op

revision: str = "9c7e1ed1db3e"
down_revision: Union[str, None] = "b7e2a1f3c4d5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_requirements_subs_trgm",
        "requirements",
        ["substitutes_text"],
        postgresql_using="gin",
        postgresql_ops={"substitutes_text": "gin_trgm_ops"},
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_requirements_subs_trgm", table_name="requirements", if_exists=True)
