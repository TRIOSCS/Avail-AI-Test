"""Resell Phase 6a hot indexes: three missing single-column btree indexes.

What (DDL, reversible):
  - ix_excess_outreach_message on excess_outreach.graph_message_id — the reply adapter's
    exact-message-id match fallback (graph_conversation_id is already indexed by
    ix_excess_outreach_conversation; the missing one is graph_message_id).
  - ix_excess_offers_vendor_card on excess_offers.offerer_vendor_card_id — the
    buyer-affinity last-bid / who-to-offer history queries and the award win-hook all
    filter/join on the canonical buyer card.
  - ix_vr_conversation on vendor_responses.graph_conversation_id — the resell reply viewer's
    single-conversation query (_conversation_replies) and record_response's whole-thread match.

Why: these three columns are filtered/joined on the resell hot paths (reply matching, the
who-to-offer buyer ranking, and the reply viewer that runs inside the tracker poll) but were
unindexed, forcing sequential scans that grow with the tables. Each index name matches the
model __table_args__ declaration so the fresh-DB schema-drift gate stays green.

Downgrade: fully reversible — drops the three indexes (reverse order).

Called by: alembic (upgrade/downgrade).
Depends on: excess_outreach (133_resell_outreach_schema), excess_offers (127/128 cutover),
            vendor_responses (existing).

Revision ID: 200_resell_hot_indexes
Revises: 199_sighting_excess_line_fk
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op

revision = "200_resell_hot_indexes"
down_revision = "199_sighting_excess_line_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_excess_outreach_message", "excess_outreach", ["graph_message_id"])
    op.create_index("ix_excess_offers_vendor_card", "excess_offers", ["offerer_vendor_card_id"])
    op.create_index("ix_vr_conversation", "vendor_responses", ["graph_conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_vr_conversation", table_name="vendor_responses")
    op.drop_index("ix_excess_offers_vendor_card", table_name="excess_offers")
    op.drop_index("ix_excess_outreach_message", table_name="excess_outreach")
