"""Phase 1: Per-part sourcing status, buyer claim, and sales context fields.

Adds sourcing_status to requirements (per-part tracking),
claimed_by_id/claimed_at to requisitions (buyer ownership),
urgency/opportunity_value to requisitions (sales context for buyers).

Revision ID: 068
Revises: 067
Create Date: 2026-03-10
"""

from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- Requisition: buyer claim + sales context --
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS claimed_by_id INTEGER")
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS urgency VARCHAR(20) DEFAULT 'normal'")
    op.execute("ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS opportunity_value NUMERIC(12, 2)")

    op.create_foreign_key(
        "fk_requisitions_claimed_by",
        "requisitions",
        "users",
        ["claimed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_requisitions_claimed_by", "requisitions", ["claimed_by_id"], if_not_exists=True)
    op.create_index("ix_requisitions_urgency", "requisitions", ["urgency"], if_not_exists=True)

    # -- Requirement: per-part sourcing status --
    op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS sourcing_status VARCHAR(20) DEFAULT 'open'")
    op.create_index("ix_requirements_sourcing_status", "requirements", ["sourcing_status"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_requirements_sourcing_status", table_name="requirements", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS requirements DROP COLUMN IF EXISTS sourcing_status")

    op.drop_index("ix_requisitions_urgency", table_name="requisitions", if_exists=True)
    op.drop_index("ix_requisitions_claimed_by", table_name="requisitions", if_exists=True)
    op.drop_constraint("fk_requisitions_claimed_by", "requisitions", type_="foreignkey")
    op.execute("ALTER TABLE IF EXISTS requisitions DROP COLUMN IF EXISTS opportunity_value")
    op.execute("ALTER TABLE IF EXISTS requisitions DROP COLUMN IF EXISTS urgency")
    op.execute("ALTER TABLE IF EXISTS requisitions DROP COLUMN IF EXISTS claimed_at")
    op.execute("ALTER TABLE IF EXISTS requisitions DROP COLUMN IF EXISTS claimed_by_id")
