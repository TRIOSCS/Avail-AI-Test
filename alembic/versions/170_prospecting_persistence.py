"""Prospecting persistence: buyer_ready_score cache column + warm-intro trgm indexes.

Wave 5 prospecting persistence — two purely additive items:

1. ``prospect_accounts.buyer_ready_score`` (Integer, nullable): a write-through CACHE of
   the composite buyer-ready score that ``app.services.prospect_priority.build_priority_snapshot``
   computes on the fly. The recompute stays the source of truth; a ProspectAccount
   before_insert/before_update mapper listener keeps the column in lockstep so the
   prospecting "buyer_ready_desc" sort can rank in SQL (order_by + offset/limit) instead of
   loading every row and snapshotting it O(N) per request. A plain btree index backs the
   sort. Backfilled here from the same snapshot helper (single-sourced).

2. pg_trgm GIN index for the warm-intro lookup (``app.services.prospect_warm_intros.
   detect_warm_intros``), which scans ``sightings.vendor_email`` with a leading-wildcard
   ILIKE (``%@<domain>``). A btree cannot serve a leading wildcard, so this is a sequential
   scan today; ``GIN (vendor_email gin_trgm_ops)`` lets Postgres use the index for the
   ILIKE. The lookup's other ILIKE column, ``site_contacts.email``, is already trgm-indexed
   (``ix_site_contacts_email_trgm`` from migration a513288799de), so only the sightings side
   is missing. Postgres-only — guarded on ``dialect.name == 'postgresql'`` (mirrors
   migrations 049/120); SQLite keeps the seq scan in tests.

Revision ID: 170_prospecting_persistence
Revises: 169_crm_field_history
Create Date: 2026-06-29
"""

import sqlalchemy as sa

from alembic import op

revision = "170_prospecting_persistence"
down_revision = "169_crm_field_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. buyer_ready_score cache column + btree index (all dialects).
    op.add_column("prospect_accounts", sa.Column("buyer_ready_score", sa.Integer(), nullable=True))
    op.create_index(
        "ix_prospect_accounts_buyer_ready_score",
        "prospect_accounts",
        ["buyer_ready_score"],
        unique=False,
    )

    # Backfill existing rows through the same recompute the app uses (single-sourced).
    from sqlalchemy.orm import Session

    from app.models.prospect_account import ProspectAccount
    from app.services.prospect_priority import build_priority_snapshot

    session = Session(bind=bind)
    try:
        for prospect in session.query(ProspectAccount).all():
            prospect.buyer_ready_score = build_priority_snapshot(prospect)["buyer_ready_score"]
        session.commit()
    finally:
        session.close()

    # 2. pg_trgm GIN index for the warm-intro sightings.vendor_email ILIKE scan
    # (Postgres only; site_contacts.email is already covered by a513288799de).
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.create_index(
            "ix_sightings_vendor_email_trgm",
            "sightings",
            [sa.text("vendor_email gin_trgm_ops")],
            unique=False,
            postgresql_using="gin",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_sightings_vendor_email_trgm", table_name="sightings", if_exists=True)
    op.drop_index("ix_prospect_accounts_buyer_ready_score", table_name="prospect_accounts")
    op.drop_column("prospect_accounts", "buyer_ready_score")
