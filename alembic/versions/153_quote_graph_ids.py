"""Add graph_message_id + graph_conversation_id to quotes -- capture the outbound quote
email's Microsoft Graph identifiers at send time.

The canonical quote-send service (app/services/quote_send.py) records these so a customer
reply to a sent quote can be threaded back to the quote (mirrors how RFQ sends capture the
vendor-message ids). Both columns are nullable: drafts/legacy rows have no send, and a
transient Sent-Items propagation miss leaves them NULL without failing the send.

Downgrade drops both columns (the ids are re-derivable only from Graph and are non-critical
metadata; acceptable loss on rollback).

Revision ID: 153_quote_graph_ids
Revises: 152_partsurfer_desc_negative
Create Date: 2026-06-25
"""

import sqlalchemy as sa

from alembic import op

revision = "153_quote_graph_ids"
down_revision = "152_partsurfer_desc_negative"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("graph_message_id", sa.String(length=255), nullable=True))
    op.add_column("quotes", sa.Column("graph_conversation_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "graph_conversation_id")
    op.drop_column("quotes", "graph_message_id")
