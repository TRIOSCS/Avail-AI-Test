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
        op.add_column("activity_log", sa.Column("quality_score", sa.Float(), nullable=True))
    if not _column_exists("activity_log", "quality_classification"):
        op.add_column("activity_log", sa.Column("quality_classification", sa.String(30), nullable=True))
    if not _column_exists("activity_log", "quality_assessed_at"):
        op.add_column("activity_log", sa.Column("quality_assessed_at", sa.DateTime(), nullable=True))
    if not _column_exists("activity_log", "is_meaningful"):
        op.add_column("activity_log", sa.Column("is_meaningful", sa.Boolean(), nullable=True))

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
        )


def downgrade():
    op.drop_index("ix_activity_unscored", table_name="activity_log")
    op.drop_column("activity_log", "is_meaningful")
    op.drop_column("activity_log", "quality_assessed_at")
    op.drop_column("activity_log", "quality_classification")
    op.drop_column("activity_log", "quality_score")
