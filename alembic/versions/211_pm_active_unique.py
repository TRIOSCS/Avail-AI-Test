"""211 — one active proactive match per (mpn, company): partial unique index.

QC finding pa-match-no-unique-index: "one active row per (part, company)" was
enforced only in Python (_existing_match_company_ids), so an overlapping
scheduler scan + manual Refresh could both pass the read-side dedup and insert
duplicate matches. This adds the DB-level guarantee:

1. Backfill: among rows with status IN ('new','sent') and a non-NULL
   company_id, any row that is not the newest (MAX(id)) of its
   (mpn, company_id) group is flipped to status='expired' — history is
   preserved, the row just leaves the active predicate (DELETE would erase
   match/outreach history).
2. ``uq_pm_active_mpn_company`` — UNIQUE (mpn, company_id) partial index
   WHERE status IN ('new','sent'). Matches the model __table_args__ entry
   (drift gate green). company_id IS NULL (back-order lines) is naturally
   unconstrained — NULLs are distinct in a unique index; those rows are
   deduped per owner in Python as before.

The scan writers already wrap their commits in try/rollback, so a race loser
now yields cleanly instead of persisting a duplicate. Cross-SPELLING dedup
(equivalence classes) remains Python-side — the index pins the exact-spelling
core, which is what the identical-scan race produces.

Reversible-with-documented-loss: downgrade drops the index; rows expired by
the backfill stay expired (they were duplicates — mirrors 210's wording).

Chains onto 210_part_outcome_hotlist.
"""

import sqlalchemy as sa

from alembic import op

revision = "211_pm_active_unique"
down_revision = "210_part_outcome_hotlist"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('new', 'sent')"


def upgrade():
    # 1) Expire the older duplicates so the unique index can build. Groups of one
    #    keep their row (its id IS the group max).
    op.execute(
        """
        UPDATE proactive_matches
        SET status = 'expired'
        WHERE status IN ('new', 'sent')
          AND company_id IS NOT NULL
          AND id NOT IN (
              SELECT MAX(id)
              FROM proactive_matches
              WHERE status IN ('new', 'sent')
                AND company_id IS NOT NULL
              GROUP BY mpn, company_id
          )
        """
    )

    # 2) The partial unique index (name + predicate must match the model).
    op.create_index(
        "uq_pm_active_mpn_company",
        "proactive_matches",
        ["mpn", "company_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE),
        sqlite_where=sa.text(_ACTIVE),
    )


def downgrade():
    op.drop_index("uq_pm_active_mpn_company", table_name="proactive_matches")
