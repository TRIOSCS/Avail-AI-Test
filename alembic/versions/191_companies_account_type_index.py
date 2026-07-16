"""Add a btree index on companies.account_type.

What: Creates ``ix_companies_account_type`` on ``companies.account_type``. The column
      is actively filtered in the CRM list path
      (``app/services/crm_service.py::list_companies`` — ``Company.account_type == account_type``
      when the type is one of the CDM account types) and by the inbound-customer alert
      source (``app/services/alerts/sources/inbound_customer.py`` —
      ``Company.account_type == "Customer"``), both of which had no supporting index and
      fell back to a sequential scan.

      Plain single-column btree, additive and PG-safe (no table rewrite, no lock beyond
      the brief ``CREATE INDEX`` share-lock). Registered in the Company model's
      ``__table_args__`` (app/models/crm.py) so the schema-drift gate
      (scripts/check_schema_matches_models.py) stays green.

Downgrade: drops the index (fully reversible).

Called by: alembic (upgrade/downgrade).
Depends on: companies table.

Revision ID: 191_companies_account_type_index
Revises: 189_category_residue_backfill
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "191_companies_account_type_index"
down_revision: str | None = "189_category_residue_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_companies_account_type",
        "companies",
        ["account_type"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_companies_account_type", table_name="companies", if_exists=True)
