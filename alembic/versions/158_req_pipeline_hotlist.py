"""Rework requisition pipeline + add outcome_reason; drop archive status.

Remaps the legacy status pipeline onto OPEN/RFQS_SENT/OFFERS/QUOTED/WON/LOST/HOTLIST
and adds requisitions.outcome_reason (the required Won/Lost close reason — enforced
app-side, nullable at the DB level so existing rows and non-closed reqs stay valid):

    archived            -> lost   (terminal; the old archive status collapses to lost)
    active / sourcing / reopened -> open  ("open automatically means sourcing")
    quoting             -> quoted
    NULL                -> open

There is NO requisition archive/hide capability — a requisition ends in Won or Lost
(each carrying a required outcome_reason). The unrelated CRM/contact is_archived
(archive-DNC, migration 148) is a separate feature and is untouched here.

Called by: alembic. Depends on: 157_qp_approvals.

Revision ID: 158_req_pipeline_hotlist
Revises: 157_qp_approvals
Create Date: 2026-06-26
"""

import sqlalchemy as sa

from alembic import op

revision = "158_req_pipeline_hotlist"
down_revision = "157_qp_approvals"
branch_labels = None
depends_on = None


# New status whitelist for the reworked pipeline (migration 158).
_NEW_STATUSES = "'draft','open','rfqs_sent','offers','quoted','won','lost','hotlist','cancelled'"
# Old whitelist from migration 8c22bd2f6837 (restored on downgrade).
_OLD_STATUSES = "'draft','active','sourcing','offers','quoting','quoted','reopened','won','lost','archived','cancelled'"


def upgrade() -> None:
    # Required Won/Lost close reason — nullable at the DB level (enforcement is
    # app-side, so existing rows and non-closed requisitions remain valid).
    op.add_column("requisitions", sa.Column("outcome_reason", sa.Text(), nullable=True))

    bind = op.get_bind()
    # Drop the old status CHECK first so the remap to new values is permitted
    # (ck_requisitions_status from migration 8c22bd2f6837 whitelists the old set).
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")

    # The old archive status collapses to the terminal lost stage (no hide flag).
    bind.execute(sa.text("UPDATE requisitions SET status = 'lost' WHERE status = 'archived'"))
    # Merge legacy active stages into the new single entry stage.
    bind.execute(sa.text("UPDATE requisitions SET status = 'open' WHERE status IN ('active', 'sourcing', 'reopened')"))
    bind.execute(sa.text("UPDATE requisitions SET status = 'quoted' WHERE status = 'quoting'"))
    # Any null/blank status defaults to the open entry stage.
    bind.execute(sa.text("UPDATE requisitions SET status = 'open' WHERE status IS NULL OR status = ''"))

    # Re-add the CHECK with the new pipeline whitelist.
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        f"status IN ({_NEW_STATUSES})",
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")
    # Best-effort reverse: new-only stages collapse to their nearest legacy
    # equivalent so the old CHECK accepts them. (The archived->lost remap is
    # lossy and not reversed — those rows stay lost.)
    bind.execute(sa.text("UPDATE requisitions SET status = 'sourcing' WHERE status = 'rfqs_sent'"))
    bind.execute(sa.text("UPDATE requisitions SET status = 'active' WHERE status IN ('open', 'hotlist')"))
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        f"status IN ({_OLD_STATUSES})",
    )
    op.drop_column("requisitions", "outcome_reason")
