"""Make sightings.requirement_id nullable — supports requirement-less Sightings.

What (DDL, reversible):
  - ALTER sightings.requirement_id to NULLABLE (was NOT NULL). The FK constraint
    itself (ondelete="CASCADE") is unchanged — a NULL value is simply exempt
    from any cascade action, so requirement-scoped rows keep their existing
    delete-with-parent-requirement behavior.

Why: interactive/global "quick search" results (app.search_service.stream_search_mpn)
now persist as Sightings so vendor intelligence, material-card history, and scoring
accrue from every search a buyer runs — not only requisition-scoped ones. Those
searches have no Requirement to attach to, so requirement_id must accept NULL.
Requirement-scoped writers (search_requirement, worker sighting writers) are
unaffected — they still always pass a real requirement_id.

Downgrade: refuses to run if any NULL requirement_id rows exist — re-imposing NOT
NULL would either fail the constraint or (if forced via a data migration) silently
destroy those rows, and this migration will not do that implicitly. The operator
must first delete or backfill those rows, e.g.:
    DELETE FROM sightings WHERE requirement_id IS NULL;
before downgrading. Once no NULL rows remain, the column reverts to NOT NULL.

Called by: alembic (upgrade/downgrade).
Depends on: sightings (created in 001_initial_schema).

Revision ID: 197_sighting_req_id_nullable
Revises: 196_approvals_foundations
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "197_sighting_req_id_nullable"
down_revision = "196_approvals_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("sightings", "requirement_id", existing_type=sa.INTEGER(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    null_count = bind.execute(sa.text("SELECT COUNT(*) FROM sightings WHERE requirement_id IS NULL")).scalar()
    if null_count:
        raise RuntimeError(
            f"Cannot downgrade 197_sighting_req_id_nullable: {null_count} sighting "
            "row(s) have requirement_id IS NULL (requirement-less interactive/global search "
            "sightings). Re-imposing NOT NULL would violate the constraint, and this "
            "migration will not silently delete that data. Either remove them explicitly "
            "(DELETE FROM sightings WHERE requirement_id IS NULL;) or backfill a real "
            "requirement_id on each row, then re-run 'alembic downgrade'."
        )
    op.alter_column("sightings", "requirement_id", existing_type=sa.INTEGER(), nullable=False)
