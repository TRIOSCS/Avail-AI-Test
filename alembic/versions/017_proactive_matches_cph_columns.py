"""Add CPH-enriched columns to proactive_matches.

ORM and app code expect material_card_id, company_id, match_score, etc.
When TESTING=1 or DB is built only via Alembic, startup _add_missing_columns
does not run, so these columns must be added by a migration. Idempotent.

Revision ID: 017_proactive_matches_cph
Revises: 016_add_sightings_vendor_name_normalized
Create Date: 2026-02-26
"""

from sqlalchemy import text

from alembic import op

revision = "017_proactive_matches_cph"
down_revision = "016_add_sightings_vendor_name_normalized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    stmts = [
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS material_card_id INTEGER REFERENCES material_cards(id) ON DELETE SET NULL",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS match_score INTEGER DEFAULT 0",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS margin_pct FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_purchase_count INTEGER DEFAULT 0",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_last_price FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_last_purchased_at TIMESTAMP",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS our_cost FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS dismiss_reason VARCHAR(255)",
    ]
    for stmt in stmts:
        conn.execute(text(stmt))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pm_material_card ON proactive_matches (material_card_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pm_score ON proactive_matches (match_score)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pm_status_sales ON proactive_matches (status, salesperson_id)"))


def downgrade() -> None:
    op.drop_index("ix_pm_status_sales", table_name="proactive_matches", if_exists=True)
    op.drop_index("ix_pm_score", table_name="proactive_matches", if_exists=True)
    op.drop_index("ix_pm_material_card", table_name="proactive_matches", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS dismiss_reason")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS our_cost")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS customer_last_purchased_at")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS customer_last_price")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS customer_purchase_count")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS margin_pct")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS match_score")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS company_id")
    op.execute("ALTER TABLE IF EXISTS proactive_matches DROP COLUMN IF EXISTS material_card_id")
