"""Rework requisition pipeline + add is_archived; drop archive status.

Adds requisitions.is_archived (orthogonal hidden-but-retrievable flag) and remaps
the legacy status pipeline onto OPEN/RFQS_SENT/OFFERS/QUOTED/WON/LOST/HOTLIST:

    archived            -> is_archived=true, status='lost' (terminal placeholder)
    active / sourcing / reopened -> open  ("open automatically means sourcing")
    quoting             -> quoted
    NULL                -> open

Archive is no longer a status — see app/models/sourcing.py Requisition.is_archived
and app/services/requisition_state.set_archived(). The archived->lost+is_archived
mapping keeps those rows hidden-but-retrievable via the "Archived" filter regardless
of their terminal status value.

Called by: alembic. Depends on: 156_user_avatar.

Revision ID: 157_req_pipeline_hotlist
Revises: 156_user_avatar
Create Date: 2026-06-26
"""

import sqlalchemy as sa

from alembic import op

revision = "157_req_pipeline_hotlist"
down_revision = "156_user_avatar"
branch_labels = None
depends_on = None


# New status whitelist for the reworked pipeline (migration 157).
_NEW_STATUSES = "'draft','open','rfqs_sent','offers','quoted','won','lost','hotlist','cancelled'"
# Old whitelist from migration 8c22bd2f6837 (restored on downgrade).
_OLD_STATUSES = "'draft','active','sourcing','offers','quoting','quoted','reopened','won','lost','archived','cancelled'"


def upgrade() -> None:
    op.add_column(
        "requisitions",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_requisitions_is_archived", "requisitions", ["is_archived"])

    bind = op.get_bind()
    # Drop the old status CHECK first so the remap to new values is permitted
    # (ck_requisitions_status from migration 8c22bd2f6837 whitelists the old set).
    op.drop_constraint("ck_requisitions_status", "requisitions", type_="check")

    # Archived -> hidden boolean + terminal placeholder status (still retrievable).
    bind.execute(sa.text("UPDATE requisitions SET is_archived = true, status = 'lost' WHERE status = 'archived'"))
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
    # Best-effort reverse: archived rows recover their archived status; new-only
    # stages collapse to their nearest legacy equivalent so the old CHECK accepts them.
    bind.execute(sa.text("UPDATE requisitions SET status = 'archived' WHERE is_archived = true"))
    bind.execute(sa.text("UPDATE requisitions SET status = 'sourcing' WHERE status = 'rfqs_sent'"))
    bind.execute(sa.text("UPDATE requisitions SET status = 'active' WHERE status IN ('open', 'hotlist')"))
    op.create_check_constraint(
        "ck_requisitions_status",
        "requisitions",
        f"status IN ({_OLD_STATUSES})",
    )
    op.drop_index("ix_requisitions_is_archived", table_name="requisitions")
    op.drop_column("requisitions", "is_archived")
