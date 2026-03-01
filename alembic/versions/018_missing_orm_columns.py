"""Add ORM columns that may be missing when only Alembic runs (no startup _add_missing_columns).

list_quotes and other endpoints load Quote -> customer_site -> Company, site_contacts, User.
If companies/customer_sites/users are missing columns the ORM expects, the query fails.
Idempotent: ADD COLUMN IF NOT EXISTS.

Revision ID: 018_missing_orm_cols
Revises: 017_proactive_matches_cph
Create Date: 2026-02-26
"""

from sqlalchemy import text

from alembic import op

revision = "018_missing_orm_cols"
down_revision = "017_proactive_matches_cph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    stmts = [
        # companies (Quote -> customer_site -> company)
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS brand_tags JSON DEFAULT '[]'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS commodity_tags JSON DEFAULT '[]'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS material_tags_updated_at TIMESTAMP",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'manual'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS ownership_cleared_at TIMESTAMP",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP",
        # customer_sites
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP",
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS ownership_cleared_at TIMESTAMP",
        # site_contacts (loaded via customer_site.site_contacts)
        "ALTER TABLE site_contacts ADD COLUMN IF NOT EXISTS contact_status VARCHAR(20) DEFAULT 'new'",
        # users (Quote.created_by)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
        # buy_plans (token_expires_at used in buy plan flow)
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
    ]
    for stmt in stmts:
        op.execute(text(stmt))


def downgrade() -> None:
    # Optional: drop columns. Skip to avoid breaking DBs that had them from startup.
    pass
