"""Rename material_card_datasheets OneDrive-era columns to library_* (SharePoint).

What: renames onedrive_item_id -> library_item_id and onedrive_url -> library_web_url on
      material_card_datasheets. After the datasheet company-library migration (#384) these
      columns hold the Graph driveItem id + webUrl of the copy in the shared SharePoint
      library (not per-user OneDrive), so the onedrive_* names were stale. Pure rename, no
      data change. Done now while the table is empty (feature merged but not yet deployed),
      so it never becomes a data-bearing migration.
Downgrade: renames the two columns back to onedrive_item_id / onedrive_url.

Revision ID: 120_datasheet_lib_col_rename
Revises: 119_alert_seen
Create Date: 2026-06-19
"""

from typing import Sequence, Union

from alembic import op

revision = "120_datasheet_lib_col_rename"
down_revision = "119_alert_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("material_card_datasheets", "onedrive_item_id", new_column_name="library_item_id")
    op.alter_column("material_card_datasheets", "onedrive_url", new_column_name="library_web_url")


def downgrade() -> None:
    op.alter_column("material_card_datasheets", "library_web_url", new_column_name="onedrive_url")
    op.alter_column("material_card_datasheets", "library_item_id", new_column_name="onedrive_item_id")
