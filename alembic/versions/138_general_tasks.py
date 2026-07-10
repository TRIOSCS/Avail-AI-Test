"""Extend requisition_tasks for general CRM tasks (company + contact scope).

Revision ID: 138_general_tasks
Revises: 137_contact_fields
Create Date: 2026-06-23

Changes:
  - requisition_tasks.requisition_id: NOT NULL → NULL (tasks can now be scoped to a
    company or contact instead of a requisition)
  - ADD company_id Integer FK → companies.id ondelete=CASCADE nullable
  - ADD site_contact_id Integer FK → site_contacts.id ondelete=CASCADE nullable
  - ADD index ix_rt_company_status on (company_id, status)
  - ADD index ix_rt_contact_status on (site_contact_id, status)
  - ADD CHECK constraint ck_task_has_parent: at least one parent FK must be non-NULL

Downgrade note:
  The downgrade restores requisition_id to NOT NULL. Before doing so it DELETES all
  tasks whose requisition_id is NULL (i.e. company-scoped and contact-scoped tasks).
  This is an intentional data-loss step: those task types did not exist before this
  migration and cannot survive a rollback.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "138_general_tasks"
down_revision: str | None = "137_contact_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Make requisition_id nullable — tasks can now be scoped to company/contact.
    op.alter_column(
        "requisition_tasks",
        "requisition_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # 2. Add company_id FK column.
    op.add_column(
        "requisition_tasks",
        sa.Column("company_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rt_company",
        "requisition_tasks",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_rt_company_status", "requisition_tasks", ["company_id", "status"])

    # 3. Add site_contact_id FK column.
    op.add_column(
        "requisition_tasks",
        sa.Column("site_contact_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rt_site_contact",
        "requisition_tasks",
        "site_contacts",
        ["site_contact_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_rt_contact_status", "requisition_tasks", ["site_contact_id", "status"])

    # 4. CHECK constraint: at least one of the three parent FKs must be non-NULL.
    #    SQLite silently ignores CHECK constraints on ALTER TABLE; PG enforces them.
    op.create_check_constraint(
        "ck_task_has_parent",
        "requisition_tasks",
        "(requisition_id IS NOT NULL OR company_id IS NOT NULL OR site_contact_id IS NOT NULL)",
    )


def downgrade() -> None:
    # Drop the CHECK constraint first (PG requires it before column changes).
    op.drop_constraint("ck_task_has_parent", "requisition_tasks", type_="check")

    # Drop site_contact_id index, FK, and column.
    op.drop_index("ix_rt_contact_status", table_name="requisition_tasks")
    op.drop_constraint("fk_rt_site_contact", "requisition_tasks", type_="foreignkey")
    op.drop_column("requisition_tasks", "site_contact_id")

    # Drop company_id index, FK, and column.
    op.drop_index("ix_rt_company_status", table_name="requisition_tasks")
    op.drop_constraint("fk_rt_company", "requisition_tasks", type_="foreignkey")
    op.drop_column("requisition_tasks", "company_id")

    # Purge company/contact-scoped tasks before restoring NOT NULL — these task types
    # did not exist before this migration and cannot survive the rollback (data loss is
    # documented in the module docstring).
    op.execute("DELETE FROM requisition_tasks WHERE requisition_id IS NULL")

    # Restore requisition_id to NOT NULL.
    op.alter_column(
        "requisition_tasks",
        "requisition_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
