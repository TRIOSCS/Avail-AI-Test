"""Schema-drift reconciliation (#464): drop the redundant ix_requisitions_company_id index.

What:
  - ``requisitions(company_id)`` carries TWO identical btree indexes: ``ix_requisitions_company``
    (created by the ``001`` baseline, which the ORM declares) and ``ix_requisitions_company_id``
    (added later by migration ``078``, which the ORM never declared). ``compare_metadata`` flags
    the un-declared duplicate as a phantom ``remove_index``, so it was grandfathered in
    ``scripts/check_schema_matches_models.py``.

Drop the duplicate; the model-declared ``ix_requisitions_company`` stays and keeps serving every
``company_id`` lookup, so this is a behavioral no-op that just sheds redundant write/maintenance
cost. With it gone the grandfathered allowlist entry is retired and the drift gate enforces the
real schema.

Downgrade: re-creates ``ix_requisitions_company_id`` so an upgrade->downgrade->upgrade round-trip
is symmetric (mirrors migration ``078``).

Called by: alembic (upgrade/downgrade).
Depends on: requisitions table (company_id column + ix_requisitions_company index).

Revision ID: 172_drop_dup_req_company_idx
Revises: 171_unavail_condition
Create Date: 2026-06-29
"""

from alembic import op

revision = "172_drop_dup_req_company_idx"
down_revision = "171_unavail_condition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_requisitions_company_id", table_name="requisitions", if_exists=True)


def downgrade() -> None:
    op.create_index("ix_requisitions_company_id", "requisitions", ["company_id"], if_not_exists=True)
