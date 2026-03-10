"""Document valid Contact and VendorResponse status values.

No schema change — status remains String(50), validated in application code.

Contact.status values: sent, failed, opened, responded, quoted, declined, ooo, bounced, retried
VendorResponse.status values: new, reviewed, rejected

Revision ID: 071
Revises: 070
Create Date: 2026-03-10
"""

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
