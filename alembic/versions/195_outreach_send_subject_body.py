"""Resell outreach retry fidelity: persist the sent subject/body on excess_outreach.

What (DDL, reversible):
  - ADD excess_outreach.send_subject (Text, NULLABLE) — the exact subject the campaign
    was sent with.
  - ADD excess_outreach.send_body (Text, NULLABLE) — the exact body the campaign was sent
    with.

Why: the one-click Retry re-runs the Sent-folder double-send guard
(``email_service._find_sent_message``) which matches on an EXACT subject. Before this the
retry used a hard-coded default subject, so a campaign whose subject was customized never
matched its already-delivered message — the guard returned no match and the offer was
RE-SENT (a double-send). Persisting the actual subject/body lets the guard match what was
really sent and lets a legitimate resend reuse the original wording instead of the default.
NULL on manual-log rows (phone/teams/marketplace) and on legacy email rows (retry falls
back to the default text for those).

Downgrade: fully reversible — drops both columns.

Called by: alembic (upgrade/downgrade).
Depends on: excess_outreach (created in 133_resell_outreach_schema).

Revision ID: 195_outreach_send_subject_body
Revises: 194_excess_outreach_failed_states
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "195_outreach_send_subject_body"
down_revision = "194_excess_outreach_failed_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("excess_outreach", sa.Column("send_subject", sa.Text(), nullable=True))
    op.add_column("excess_outreach", sa.Column("send_body", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("excess_outreach", "send_body")
    op.drop_column("excess_outreach", "send_subject")
