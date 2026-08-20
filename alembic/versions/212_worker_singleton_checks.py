"""212 — converge the modern CHECK-constraint set (models ↔ migrations ↔ prod).

Follow-up to the alembic-1.19 drift-gate fix (#894): CHECK constraints are back in
the drift gate's scope, every modern constraint is now DECLARED on its model, and
this migration makes real databases match. Three convergence cases:

1. nc/ics_worker_status singleton CHECKs: the original migrations (023, 031) had
   them, the squashed 001_initial_schema recreated both tables WITHOUT them —
   restored here.
2. Two enum-lagging constraints, recreated from their enums:
   - ck_buy_plans_status was created WITHOUT 'inbound' (BuyPlanStatus has had it
     for months) — a fresh deployment would reject the first inbound plan
     (production never noticed only because, being schema-stamped pre-squash, it
     never had that one at all).
   - ck_offers_status lacked 'reference' — requisition_service clones write
     reference copies of active offers, so the FIRST clone on any
     constraint-bearing DB (production included) would have 500'd; the model
     validator had been warn-logging "Unexpected offer status" on every clone.
3. Production (schema-stamped before the squash) lacks several modern constraints
   fresh DBs have (ck_quotes_status, ck_offers_parse_confidence_range,
   ck_sightings_confidence_range, ck_requirements_target_qty_nonneg) — created
   where missing.

Everything is idempotent (create only when absent, by name) and VALIDATED — the
production data was verified in-range for every expression on 2026-08-20 (all
status columns within their enums; both confidence ranges within 0..1; no negative
target_qty; both singleton tables hold only id=1). Downgrade restores the exact
pre-212 shape per case. The deliberately-untouched legacy NOT-VALID chk_* family
(001-era, superseded/conflicting — e.g. chk_offer_price demands unit_price > 0
while the app supports zero-price free-sample offers) stays as-is, enumerated in
the drift gate's allowlist pending an owner decision to drop it.

Chains onto 211_pm_active_unique.
"""

import sqlalchemy as sa

from alembic import op
from app.constants import BuyPlanStatus, OfferStatus

revision = "212_worker_singleton_checks"
down_revision = "211_pm_active_unique"
branch_labels = None
depends_on = None

_BUY_PLAN_STATUSES = ", ".join(f"'{s.value}'" for s in BuyPlanStatus)
_OFFER_STATUSES = ", ".join(f"'{s.value}'" for s in OfferStatus)

# (table, name, expression) — expressions mirror the model declarations verbatim.
_ENSURE = (
    ("nc_worker_status", "ck_nc_worker_status_singleton", "id = 1"),
    ("ics_worker_status", "ck_ics_worker_status_singleton", "id = 1"),
    ("quotes", "ck_quotes_status", "status IN ('draft', 'sent', 'won', 'lost', 'revised')"),
    (
        "offers",
        "ck_offers_parse_confidence_range",
        "parse_confidence IS NULL OR (parse_confidence >= 0.0 AND parse_confidence <= 1.0)",
    ),
    (
        "sightings",
        "ck_sightings_confidence_range",
        "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
    ),
    ("requirements", "ck_requirements_target_qty_nonneg", "target_qty >= 0"),
)


def _existing_names(bind) -> set[str]:
    return {
        r[0]
        for r in bind.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND connamespace = 'public'::regnamespace")
        )
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":  # pg_constraint probe is PG-only
        for table, name, expr in _ENSURE:
            op.create_check_constraint(name, table, expr)
        op.create_check_constraint("ck_buy_plans_status", "buy_plans_v3", f"status IN ({_BUY_PLAN_STATUSES})")
        op.create_check_constraint("ck_offers_status", "offers", f"status IN ({_OFFER_STATUSES})")
        return

    have = _existing_names(bind)
    for table, name, expr in _ENSURE:
        if name not in have:
            op.create_check_constraint(name, table, expr)

    # Enum-lagging constraints: recreate from the FULL enums.
    for table, name, statuses in (
        ("buy_plans_v3", "ck_buy_plans_status", _BUY_PLAN_STATUSES),
        ("offers", "ck_offers_status", _OFFER_STATUSES),
    ):
        if name in have:
            op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"status IN ({statuses})")


def downgrade() -> None:
    for table, name, _ in _ENSURE:
        op.drop_constraint(name, table, type_="check")
    op.drop_constraint("ck_buy_plans_status", "buy_plans_v3", type_="check")
    op.drop_constraint("ck_offers_status", "offers", type_="check")
    # Restore the pre-212 shapes so a fresh-DB downgrade round-trips exactly.
    op.create_check_constraint(
        "ck_buy_plans_status",
        "buy_plans_v3",
        "status IN ('draft', 'pending', 'active', 'halted', 'completed', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_offers_status",
        "offers",
        "status IN ('pending_review', 'active', 'approved', 'rejected', 'sold', 'won', 'expired')",
    )
