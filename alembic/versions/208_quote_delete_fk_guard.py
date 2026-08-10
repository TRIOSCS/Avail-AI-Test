"""208_quote_delete_fk_guard — stop a quote delete from destroying an approved deal.

QC 2026-08-10 P1-2. Two FK ondelete changes close the data-model review's #1
finding (a quote delete CASCADEs through the buy plan -> quality plan ->
prepayments, including PAID wire records):

  - buy_plans_v3.quote_id  CASCADE -> SET NULL: deleting a quote ORPHANS the buy
    plan (quote_id NULL), it no longer destroys the whole deal. The quote-delete
    reaches no further, so the QP + prepayments are never touched via this path.
  - prepayments.buy_plan_id CASCADE -> RESTRICT: a buy plan carrying prepayments
    cannot be deleted out from under them — paid wire records are protected at
    the schema level, not just by app-layer route guards.

Reversible (downgrade restores both to CASCADE). Round-tripped
upgrade->downgrade->upgrade on a throwaway PG scratch DB. Chains onto
207_quote_customer_message.

Revision ID: 208_quote_delete_fk_guard
Revises: 207_quote_customer_message
"""

from alembic import op

revision = "208_quote_delete_fk_guard"
down_revision = "207_quote_customer_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("buy_plans_v3_quote_id_fkey", "buy_plans_v3", type_="foreignkey")
    op.create_foreign_key(
        "buy_plans_v3_quote_id_fkey", "buy_plans_v3", "quotes", ["quote_id"], ["id"], ondelete="SET NULL"
    )
    op.drop_constraint("prepayments_buy_plan_id_fkey", "prepayments", type_="foreignkey")
    op.create_foreign_key(
        "prepayments_buy_plan_id_fkey", "prepayments", "buy_plans_v3", ["buy_plan_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint("prepayments_buy_plan_id_fkey", "prepayments", type_="foreignkey")
    op.create_foreign_key(
        "prepayments_buy_plan_id_fkey", "prepayments", "buy_plans_v3", ["buy_plan_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_constraint("buy_plans_v3_quote_id_fkey", "buy_plans_v3", type_="foreignkey")
    op.create_foreign_key(
        "buy_plans_v3_quote_id_fkey", "buy_plans_v3", "quotes", ["quote_id"], ["id"], ondelete="CASCADE"
    )
