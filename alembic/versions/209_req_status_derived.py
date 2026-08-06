"""W3.3 derived requisition status — collapse the stored 9-state ladder to 6.

The mid-pipeline stages (rfqs_sent / offers / quoted) stop being stored: they are
derived from data (RFQ Contact rows / Offer rows / QuoteRequisition rows) by
app/services/requisition_state.derived_status. The stored column keeps only user
intent: draft / open / hotlist / won / lost / cancelled (spec §5.1/§10, W3.3).

DML remap (many-to-one) + CHECK narrowing. Live row counts from availai-simp-db-1
(fresh prod copy), 2026-08-06 — 33 rows:

    | old (rows)    | new  | rationale                                          |
    |---------------|------|----------------------------------------------------|
    | open (17)     | open | identity                                           |
    | offers (4)    | open | stage now derived from the reqs' Offer rows        |
    | quoted (7)    | open | stage now derived from QuoteRequisition membership |
    | rfqs_sent (0) | open | stage now derived from email Contact rows          |
    | draft (1), won (1), lost (2), cancelled (1) — identity (user intent)      |
    | hotlist (0)   — identity (monitor intent)                                 |

ck_requisitions_status (from migration 158) is narrowed to the 6 stored values so
any code path still trying to write a mid-pipeline value fails loudly.

Downgrade restores the 9-value CHECK; the data remap is many-to-one and NOT
recoverable (which row was at which stage is re-derivable from the same data the
app now derives from), documented no-op mirroring 093/100/189/193/205/206.
"""

import sqlalchemy as sa

from alembic import op

revision = "209_req_status_derived"
down_revision = "208_buyplan_drop_inbound"
branch_labels = None
depends_on = None

_STORED = "'draft', 'open', 'hotlist', 'won', 'lost', 'cancelled'"
_LADDER = "'draft', 'open', 'rfqs_sent', 'offers', 'quoted', 'won', 'lost', 'hotlist', 'cancelled'"


def upgrade() -> None:
    bind = op.get_bind()
    # Drop the 9-value CHECK first so the remap is unconstrained mid-flight.
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")
    bind.execute(sa.text("UPDATE requisitions SET status = 'open' WHERE status IN ('rfqs_sent', 'offers', 'quoted')"))
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        f"status IN ({_STORED})",
    )


def downgrade() -> None:
    # Structural reverse only: restore the pre-collapse 9-value CHECK. The data
    # remap is many-to-one (mid-pipeline rows became 'open') and is not reversed —
    # every collapsed value remains valid under the wider CHECK, and the stage
    # information lives in the Contact/Offer/QuoteRequisition rows it was derived
    # from all along.
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        f"status IN ({_LADDER})",
    )
