"""Reconcile the offers status CHECK constraint with the OfferStatus enum.

What it does: drops the drifted ``chk_offer_status`` (migration 048) which allowed
``active,expired,won,lost,pending_review,rejected`` — it OMITTED the valid
``approved`` and ``sold`` states and carried a phantom ``lost`` — and (re)creates
``ck_offers_status`` enforcing EXACTLY the ``OfferStatus`` enum
(``pending_review,active,approved,rejected,sold,won,expired``), VALIDATED.

Why: real write paths set offers to APPROVED (offer approval) and SOLD (mark-sold).
The broken constraint made a fresh-DB rebuild / backup-restore reject those writes;
live worked only because the constraint had been manually dropped. The drop is
``IF EXISTS`` so this is idempotent across both states (absent on live, present on a
fresh rebuild). Postgres-only (raw ``DROP CONSTRAINT IF EXISTS`` + validated CHECK);
no-ops on other dialects so the SQLite test schema is unaffected.

Called by: alembic. Depends on: 123_sp4_park_provenance (current head).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "124_offer_status_constraint"
down_revision = "123_sp4_park_provenance"
branch_labels = None
depends_on = None

# The authoritative OfferStatus enum value set (app/constants.py:OfferStatus).
# Kept in lock-step by tests/test_offer_status_constraint.py.
_ENUM_SET = "status IN ('pending_review','active','approved','rejected','sold','won','expired')"
# The original drifted set created by migration 048 (restored on downgrade).
_DRIFTED_SET = "status IN ('active','expired','won','lost','pending_review','rejected')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Remove the drifted constraint (absent on live, present + broken on a fresh rebuild).
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS chk_offer_status")
    # Normalize then (re)create the correct, validated constraint. DROP IF EXISTS makes
    # this idempotent: live has neither constraint; a fresh rebuild already has a valid
    # ck_offers_status (migration 8c22bd2f6837) — drop+recreate converges both states.
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", _ENUM_SET)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Restore the pre-124 fresh-rebuild state: correct ck_offers_status (8c22bd2f6837)
    # plus the drifted chk_offer_status (048) as NOT VALID.
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", _ENUM_SET)
    op.execute(f"ALTER TABLE offers ADD CONSTRAINT chk_offer_status CHECK ({_DRIFTED_SET}) NOT VALID")
