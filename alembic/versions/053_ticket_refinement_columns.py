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
    op.drop_column("trouble_tickets", "reproduction_steps")
    op.drop_column("trouble_tickets", "performance_timings")
    op.drop_column("trouble_tickets", "network_errors")
    op.drop_column("trouble_tickets", "dom_snapshot")
    op.drop_column("trouble_tickets", "tested_area")
    op.drop_column("trouble_tickets", "similarity_score")
