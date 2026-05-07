"""Add ticket refinement columns (similarity, agent testing context).

Columns: similarity_score, tested_area, dom_snapshot, network_errors,
performance_timings, reproduction_steps.

Revision ID: 053
Revises: 052
"""

from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS similarity_score DOUBLE PRECISION")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS tested_area VARCHAR(50)")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS dom_snapshot TEXT")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS network_errors JSON")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS performance_timings JSON")
    op.execute("ALTER TABLE trouble_tickets ADD COLUMN IF NOT EXISTS reproduction_steps JSON")


def downgrade():
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS reproduction_steps")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS performance_timings")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS network_errors")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS dom_snapshot")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS tested_area")
    op.execute("ALTER TABLE IF EXISTS trouble_tickets DROP COLUMN IF EXISTS similarity_score")
