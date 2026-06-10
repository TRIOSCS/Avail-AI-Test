"""Add wechat_id to site_contacts — WeChat handle for click-to-message outreach.

Used by the CDM account workspace contact panel (click-to-WeChat deep link with
activity logging). Written by the site-contact create form; rendered in the
customer contact panels (app/templates/htmx/partials/customers/tabs/
contacts_tab.html and site_contacts.html).

Downgrade drops the column (data loss limited to manually entered WeChat IDs).

Revision ID: 095_wechat_id
Revises: 094_fru_links
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "095_wechat_id"
down_revision = "094_fru_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("site_contacts", sa.Column("wechat_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("site_contacts", "wechat_id")
