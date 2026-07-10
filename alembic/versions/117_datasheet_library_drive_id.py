"""material_card_datasheets.library_drive_id — Graph drive id of the company library."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "117_datasheet_library_drive_id"
down_revision = "116_site_contact_dnc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("material_card_datasheets", sa.Column("library_drive_id", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("material_card_datasheets", "library_drive_id")
