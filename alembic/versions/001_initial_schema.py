"""initial schema - baseline for all existing tables

Revision ID: 001_initial
Revises: None
Create Date: 2026-02-14

For EXISTING databases: run `alembic stamp 001_initial` (skip DDL, just mark as current).
For NEW databases: run `alembic upgrade head` (creates all tables from models).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all tables from SQLAlchemy models.

    Uses metadata.create_all with checkfirst=True so it's safe to run
    even if some tables already exist (idempotent).
    """
    from app.models import Base
    from app.database import engine

    Base.metadata.create_all(bind=engine, checkfirst=True)


def downgrade() -> None:
    """Drop all tables. ⚠️ DESTRUCTIVE — only for dev/test environments."""
    from app.models import Base
    from app.database import engine

    Base.metadata.drop_all(bind=engine)
