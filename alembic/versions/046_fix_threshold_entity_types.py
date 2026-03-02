"""Fix tag_threshold_config entity_type values to match propagate_tags_to_entity.

Old values 'vendor'/'customer' never matched actual entity types 'vendor_card'/
'customer_site', causing all EntityTags to silently get is_visible=False.

Revision ID: 046_fix_threshold_entity_types
Revises: 045
"""

from alembic import op
import sqlalchemy as sa

revision = "046_fix_threshold_entity_types"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fix existing rows
    op.execute("UPDATE tag_threshold_config SET entity_type = 'vendor_card' WHERE entity_type = 'vendor'")
    op.execute("UPDATE tag_threshold_config SET entity_type = 'customer_site' WHERE entity_type = 'customer'")

    # Add company thresholds
    config_table = sa.table(
        "tag_threshold_config",
        sa.column("entity_type", sa.String),
        sa.column("tag_type", sa.String),
        sa.column("min_count", sa.Integer),
        sa.column("min_percentage", sa.Float),
    )
    op.bulk_insert(
        config_table,
        [
            {"entity_type": "company", "tag_type": "brand", "min_count": 2, "min_percentage": 0.05},
            {"entity_type": "company", "tag_type": "commodity", "min_count": 3, "min_percentage": 0.05},
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM tag_threshold_config WHERE entity_type = 'company'")
    op.execute("UPDATE tag_threshold_config SET entity_type = 'vendor' WHERE entity_type = 'vendor_card'")
    op.execute("UPDATE tag_threshold_config SET entity_type = 'customer' WHERE entity_type = 'customer_site'")
