"""Add check constraints on status and numeric columns.

Revision ID: 8c22bd2f6837
Revises: a23a31ac0a02
Create Date: 2026-03-27
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "8c22bd2f6837"
down_revision = "a23a31ac0a02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- Requisition status
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        "status IN ('draft','active','sourcing','offers','quoting','quoted','reopened','won','lost','archived','cancelled')",
    )

    # -- Requirement sourcing_status + target_qty
    op.create_check_constraint(
        "ck_requirements_sourcing_status",
        "requirements",
        "sourcing_status IN ('open','sourcing','offered','quoted','won','lost','archived')",
    )
    op.create_check_constraint(
        "ck_requirements_target_qty_nonneg",
        "requirements",
        "target_qty >= 0",
    )

    # -- Sighting confidence + unit_price
    op.create_check_constraint(
        "ck_sightings_confidence_range",
        "sightings",
        "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
    )
    op.create_check_constraint(
        "ck_sightings_unit_price_nonneg",
        "sightings",
        "unit_price IS NULL OR unit_price >= 0",
    )

    # -- Offer status + unit_price + parse_confidence
    op.create_check_constraint(
        "ck_offers_status",
        "offers",
        "status IN ('pending_review','active','approved','rejected','sold','won','expired')",
    )
    op.create_check_constraint(
        "ck_offers_unit_price_nonneg",
        "offers",
        "unit_price IS NULL OR unit_price >= 0",
    )
    op.create_check_constraint(
        "ck_offers_parse_confidence_range",
        "offers",
        "parse_confidence IS NULL OR (parse_confidence >= 0.0 AND parse_confidence <= 1.0)",
    )

    # -- Quote status
    op.create_check_constraint(
        "ck_quotes_status",
        "quotes",
        "status IN ('draft','sent','won','lost','revised')",
    )

    # -- BuyPlan status
    op.create_check_constraint(
        "ck_buy_plans_status",
        "buy_plans_v3",
        "status IN ('draft','pending','active','halted','completed','cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_buy_plans_status", "buy_plans_v3", type_="check")
    op.drop_constraint("ck_quotes_status", "quotes", type_="check")
    op.drop_constraint("ck_offers_parse_confidence_range", "offers", type_="check")
    op.drop_constraint("ck_offers_unit_price_nonneg", "offers", type_="check")
    op.drop_constraint("ck_offers_status", "offers", type_="check")
    op.drop_constraint("ck_sightings_unit_price_nonneg", "sightings", type_="check")
    op.drop_constraint("ck_sightings_confidence_range", "sightings", type_="check")
    op.drop_constraint("ck_requirements_target_qty_nonneg", "requirements", type_="check")
    op.drop_constraint("ck_requirements_sourcing_status", "requirements", type_="check")
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")
