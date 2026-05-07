"""Add AI quality scoring columns to activity_log.

Revision ID: 081_quality
Revises: f2ee82c7b17d
"""

import sqlalchemy as sa

from alembic import op

revision = "081_quality"
down_revision = "f2ee82c7b17d"
branch_labels = None
depends_on = None


def _column_exists(table, column):
    from sqlalchemy import inspect

    bind = op.get_bind()
    insp = inspect(bind)
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade():
    if not _column_exists("activity_log", "quality_score"):
        op.execute("ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS quality_score DOUBLE PRECISION")
    if not _column_exists("activity_log", "quality_classification"):
        op.execute("ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS quality_classification VARCHAR(30)")
    if not _column_exists("activity_log", "quality_assessed_at"):
        op.execute("ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS quality_assessed_at TIMESTAMP WITHOUT TIME ZONE")
    if not _column_exists("activity_log", "is_meaningful"):
        op.execute("ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS is_meaningful BOOLEAN")

    # Guard index creation for idempotent re-runs
    from sqlalchemy import inspect

    bind = op.get_bind()
    insp = inspect(bind)
    existing_indexes = [idx["name"] for idx in insp.get_indexes("activity_log")]
    if "ix_activity_unscored" not in existing_indexes:
        op.create_index(
            "ix_activity_unscored",
            "activity_log",
            ["quality_assessed_at"],
            postgresql_where=sa.text("quality_assessed_at IS NULL"),
            if_not_exists=True,
        )


def downgrade():
    op.drop_index("ix_activity_unscored", table_name="activity_log", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS activity_log DROP COLUMN IF EXISTS is_meaningful")
    op.execute("ALTER TABLE IF EXISTS activity_log DROP COLUMN IF EXISTS quality_assessed_at")
    op.execute("ALTER TABLE IF EXISTS activity_log DROP COLUMN IF EXISTS quality_classification")
    op.execute("ALTER TABLE IF EXISTS activity_log DROP COLUMN IF EXISTS quality_score")
