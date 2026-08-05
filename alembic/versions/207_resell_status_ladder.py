"""Resell W3 status collapse: the 5-state ExcessList ladder + the outcome column.

Spec: SIMPLIFICATION_SPEC §5.3 / §10-W3 — "resell status collapse 34→5; the Wave 3
migration ships the remap table". The ladder DRAFT → POSTED → BIDDING → AWARDED →
CLOSED applies to the LIST lifecycle; the sub-entity enums are trimmed per-enum
(ExcessLineItemStatus drops writer-less BIDDING; ExcessOutreachStatus drops
writer-less NO_RESPONSE; ExcessOfferStatus / OfferLineMatchStatus / CustomerBidStatus
keep every member — each has a live writer in the kept flow).

DDL (the packet's ONE schema addition, tables never dropped — columns only ADDED):
  - ADD excess_lists.outcome (String(20), NULLABLE, no default) — terminal outcome
    recorded on close: sold / scrapped / withdrawn / no_bids
    (constants.ExcessListOutcome). NULL until a list reaches CLOSED.

DML remap tables (all status columns are plain String(20), NO check constraints —
pure data UPDATEs on LOWER(TRIM(status)), mirroring 189/193/205). Live row counts
from availai-simp-db-1 (fresh prod copy), 2026-08-04:

  excess_lists.status (15 rows):
    | old (rows)     | new     | rationale                                        |
    |----------------|---------|--------------------------------------------------|
    | draft (2)      | draft   | identity                                         |
    | open (2)       | posted  | "open" = published/anonymized posting live       |
    | collecting (2) | bidding | collecting inbound broker bids                   |
    | bid_out (0)    | bidding | the list stays BIDDING while the bid-back is out |
    |                |         | (CustomerBid.status carries the sent/accepted    |
    |                |         | sub-state; the ended window lives in close_at,   |
    |                |         | which close_list always stamps)                  |
    | awarded (5)    | awarded | identity (outcome stays NULL until closed)       |
    | closed (1)     | closed  | terminal                                         |
    | expired (3)    | closed  | EXPIRED's only writer (expire_resell_lists       |
    |                |         | nightly job) was deleted in W1; closing is a     |
    |                |         | manual one-click act — a separate "expired"      |
    |                |         | terminal is dead vocabulary                      |

  excess_line_items.status (82 rows: available 61, awarded 14, bidding 4, withdrawn 3):
    | bidding (4) | available | BIDDING has NO writer anywhere; its lone read      |
    |             |           | treated it identically to AVAILABLE                |

  excess_outreach.status (no_response 1):
    | no_response (1) | sent | NO writer (deep-review B45); the "future aging job"  |
    |                 |      | W1.9 reserved it for is not on the kernel job list.  |
    |                 |      | Remapped, the row reads like every other silent      |
    |                 |      | buyer today                                          |

Outcome backfill (data-driven, applied to rows CLOSED after the remap, outcome NULL):
  1. accepted customer bid OR won offer exists            -> sold
  2. else >=1 inbound offer OR >=1 awarded line           -> withdrawn
  3. else (zero offers, nothing awarded)                  -> no_bids
  'scrapped' is never inferable — manual-entry only going forward.
  Live evidence for the 4 pre-W3 terminal rows (queried 2026-08-04):
    | list id | old status | offers | won | awarded lines | accepted bids | outcome   |
    | 4       | closed     | 0      | 0   | 0             | 0             | no_bids   |
    | 5       | expired    | 0      | 0   | 1             | 0             | withdrawn |
    | 6       | expired    | 3      | 0   | 0             | 0             | withdrawn |
    | 7       | expired    | 2      | 0   | 0             | 0             | withdrawn |
  (List 5 carries award history but never closed as sold — flagged in the owner
  packet; 'withdrawn' drafted per the recon call.)

The enum-member removals + @validates cleanup + every code/template reference ride
the SAME commit as this remap (mirrors 193/205). SQL is factored into
remap_statuses(connection) / backfill_outcomes(connection) so tests drive the exact
statements (pattern of 193's remap_legacy_statuses).

Downgrade: drops the outcome column; the status remaps are many-to-one
(bid_out/expired provenance not recoverable) -> documented no-op, mirrors
093/100/189/193/205.

Called by: alembic (upgrade/downgrade).
Depends on: excess_lists / excess_line_items / excess_outreach / excess_offers /
    customer_bids (127+ resell chain).

Revision ID: 207_resell_status_ladder
Revises: 206_requirement_norm_remap
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "207_resell_status_ladder"
down_revision = "206_requirement_norm_remap"
branch_labels = None
depends_on = None


def remap_statuses(connection) -> None:
    """The W3 ladder data UPDATEs (dialect-neutral; driven directly by tests)."""
    # ExcessList: old vocabulary -> the 5-state ladder (case-insensitive, mirrors 193).
    connection.execute(sa.text("UPDATE excess_lists SET status = 'posted' WHERE LOWER(TRIM(status)) = 'open'"))
    connection.execute(
        sa.text("UPDATE excess_lists SET status = 'bidding' WHERE LOWER(TRIM(status)) IN ('collecting', 'bid_out')")
    )
    connection.execute(
        sa.text("UPDATE excess_lists SET status = 'closed' WHERE LOWER(TRIM(status)) IN ('closed', 'expired')")
    )
    # ExcessLineItem: writer-less BIDDING folds into AVAILABLE.
    connection.execute(
        sa.text("UPDATE excess_line_items SET status = 'available' WHERE LOWER(TRIM(status)) = 'bidding'")
    )
    # ExcessOutreach: writer-less NO_RESPONSE reads like any other silent buyer.
    connection.execute(sa.text("UPDATE excess_outreach SET status = 'sent' WHERE LOWER(TRIM(status)) = 'no_response'"))


def backfill_outcomes(connection) -> None:
    """Outcome backfill for terminal (closed) lists — run AFTER remap_statuses.

    Rule (recon-verified against every pre-W3 terminal row): accepted customer bid or
    won offer -> sold; any inbound offer or awarded line without a win -> withdrawn;
    nothing at all -> no_bids. Non-terminal rows keep outcome NULL; 'scrapped' is
    manual-entry only.
    """
    connection.execute(
        sa.text(
            "UPDATE excess_lists SET outcome = 'sold' "
            "WHERE LOWER(TRIM(status)) = 'closed' AND outcome IS NULL AND ("
            "  EXISTS (SELECT 1 FROM customer_bids cb WHERE cb.excess_list_id = excess_lists.id"
            "          AND LOWER(TRIM(cb.status)) = 'accepted')"
            "  OR EXISTS (SELECT 1 FROM excess_offers eo WHERE eo.excess_list_id = excess_lists.id"
            "          AND LOWER(TRIM(eo.status)) = 'won')"
            ")"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE excess_lists SET outcome = 'withdrawn' "
            "WHERE LOWER(TRIM(status)) = 'closed' AND outcome IS NULL AND ("
            "  EXISTS (SELECT 1 FROM excess_offers eo WHERE eo.excess_list_id = excess_lists.id)"
            "  OR EXISTS (SELECT 1 FROM excess_line_items eli WHERE eli.excess_list_id = excess_lists.id"
            "          AND LOWER(TRIM(eli.status)) = 'awarded')"
            ")"
        )
    )
    connection.execute(
        sa.text("UPDATE excess_lists SET outcome = 'no_bids' WHERE LOWER(TRIM(status)) = 'closed' AND outcome IS NULL")
    )


def upgrade() -> None:
    op.add_column("excess_lists", sa.Column("outcome", sa.String(20), nullable=True))
    connection = op.get_bind()
    remap_statuses(connection)
    backfill_outcomes(connection)


def downgrade() -> None:
    # Status remaps are many-to-one (bid_out/expired/no_response/line-bidding provenance
    # is not recoverable) -> documented no-op, mirroring 093/100/189/193/205. Only the
    # additive column is reversed.
    op.drop_column("excess_lists", "outcome")
