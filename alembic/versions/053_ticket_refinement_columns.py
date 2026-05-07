"""Add ticket refinement columns (similarity, agent testing context).

Columns: similarity_score, tested_area, dom_snapshot, network_errors,
performance_timings, reproduction_steps.

Revision ID: 053
Revises: 052
"""

import sqlalchemy as sa

from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("trouble_tickets", sa.Column("similarity_score", sa.Float(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("tested_area", sa.String(50), nullable=True))
    op.add_column("trouble_tickets", sa.Column("dom_snapshot", sa.Text(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("network_errors", sa.JSON(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("performance_timings", sa.JSON(), nullable=True))
    op.add_column("trouble_tickets", sa.Column("reproduction_steps", sa.JSON(), nullable=True))


def downgrade():
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS reproduction_steps")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS performance_timings")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS network_errors")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS dom_snapshot")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS tested_area")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS similarity_score")
