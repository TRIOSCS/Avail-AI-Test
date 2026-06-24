"""Add vendor_card_id and vendor_contact_id to requisition_tasks (migration 142).

Revision ID: 142_vendor_task_cols
Revises: 141_reclaim_cooldown
Create Date: 2026-06-24

Changes:
  - ADD vendor_card_id Integer FK → vendor_cards.id ondelete=CASCADE nullable
  - ADD vendor_contact_id Integer FK → vendor_contacts.id ondelete=CASCADE nullable
  - ADD index ix_rt_vendor_card_status on (vendor_card_id, status)
  - ADD index ix_rt_vendor_contact_status on (vendor_contact_id, status)
  - DROP + RECREATE CHECK constraint ck_task_has_parent to include vendor columns:
      requisition_id IS NOT NULL OR company_id IS NOT NULL
      OR site_contact_id IS NOT NULL
      OR vendor_card_id IS NOT NULL OR vendor_contact_id IS NOT NULL

Downgrade: drops the vendor columns + indexes, restores original 4-column CHECK.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "142_vendor_task_cols"
down_revision: Union[str, None] = "141_reclaim_cooldown"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop old CHECK constraint (must drop before re-creating with extended expression).
    op.drop_constraint("ck_task_has_parent", "requisition_tasks", type_="check")

    # 2. Add vendor_card_id FK column.
    op.add_column(
        "requisition_tasks",
        sa.Column("vendor_card_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rt_vendor_card",
        "requisition_tasks",
        "vendor_cards",
        ["vendor_card_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_rt_vendor_card_status", "requisition_tasks", ["vendor_card_id", "status"])

    # 3. Add vendor_contact_id FK column.
    op.add_column(
        "requisition_tasks",
        sa.Column("vendor_contact_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rt_vendor_contact",
        "requisition_tasks",
        "vendor_contacts",
        ["vendor_contact_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_rt_vendor_contact_status", "requisition_tasks", ["vendor_contact_id", "status"])

    # 4. Recreate CHECK constraint including the two new vendor columns.
    op.create_check_constraint(
        "ck_task_has_parent",
        "requisition_tasks",
        (
            "requisition_id IS NOT NULL OR company_id IS NOT NULL"
            " OR site_contact_id IS NOT NULL"
            " OR vendor_card_id IS NOT NULL OR vendor_contact_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    # Drop the extended CHECK constraint first.
    op.drop_constraint("ck_task_has_parent", "requisition_tasks", type_="check")

    # Drop vendor_contact_id index, FK, and column.
    op.drop_index("ix_rt_vendor_contact_status", table_name="requisition_tasks")
    op.drop_constraint("fk_rt_vendor_contact", "requisition_tasks", type_="foreignkey")
    op.drop_column("requisition_tasks", "vendor_contact_id")

    # Drop vendor_card_id index, FK, and column.
    op.drop_index("ix_rt_vendor_card_status", table_name="requisition_tasks")
    op.drop_constraint("fk_rt_vendor_card", "requisition_tasks", type_="foreignkey")
    op.drop_column("requisition_tasks", "vendor_card_id")

    # Restore original CHECK constraint (the 3-column version from migration 138).
    op.create_check_constraint(
        "ck_task_has_parent",
        "requisition_tasks",
        "requisition_id IS NOT NULL OR company_id IS NOT NULL OR site_contact_id IS NOT NULL",
    )
