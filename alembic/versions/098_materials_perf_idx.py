"""Post-ingest performance indexes for the materials faceted page.

material_cards grew 1,859 -> 743,125 rows (SP2 ingest). Every hot query shape of
app/services/faceted_search_service.py was EXPLAIN (ANALYZE, BUFFERS)-profiled against
the live volume; each index below is justified by a measured seq-scan plan (evidence in
PR body). Ten indexes, all on material_cards — eight new names owned by this revision
plus two historical names owned by ancestor eabe89205d07 (see below):

- ix_mc_order_live          btree (search_count DESC, created_at DESC) partial
                            WHERE deleted_at IS NULL — default page order/pagination
                            (705ms top-N parallel seq scan -> index scan) and the
                            unfiltered total count (index-only scan).
- ix_mc_cat_order_live      btree (lower(btrim(category)), search_count DESC,
                            created_at DESC) partial WHERE deleted_at IS NULL AND
                            lower(btrim(category)) IS NOT NULL — commodity-scoped page +
                            count (~190ms each -> prefix index scan; PG proves
                            lower(btrim(category))='x' implies the IS NOT NULL predicate)
                            and the commodity-counts tree GROUP BY (index-only scan,
                            paired with the count(*) change in get_commodity_counts).
- ix_material_cards_search_vector  GIN (search_vector) — owned by ancestor revision
                            eabe89205d07, which is still in the active chain and creates
                            it (IF NOT EXISTS) on every fresh replay. The LIVE database
                            nevertheless lacks it: the live instance was provisioned by
                            stamping the revision history past eabe89205d07 without
                            executing it (verified — eabe89205d07's trigger
                            trig_material_cards_search_vector and function
                            material_cards_search_vector_update are also absent on live;
                            FTS is maintained by startup.py's trg_mc_fts instead), so
                            its DDL never ran there. This revision re-creates the index
                            IF NOT EXISTS to repair that out-of-band gap on live, where
                            multi-word q= was an 833ms seq scan; on fresh-replayed DBs
                            the create is a harmless idempotent no-op.
- ix_material_cards_trgm_mpn       GIN (display_mpn gin_trgm_ops) — owned by
                            eabe89205d07 likewise; re-created IF NOT EXISTS for the
                            same live-only gap.
- ix_mc_trgm_norm_mpn       GIN (normalized_mpn gin_trgm_ops)
- ix_mc_trgm_manufacturer   GIN (manufacturer gin_trgm_ops)
- ix_mc_trgm_description    GIN (description gin_trgm_ops)
                            The four trgm indexes serve the OR'd ILIKE branches of the
                            single-token q= path (1,127ms seq scan): a BitmapOr needs
                            EVERY branch indexed, and the FTS path OR's two ILIKE
                            fallbacks. avg col widths are tiny (desc 13 / mpn 12 chars).
- ix_mc_has_datasheet       partial btree (id) WHERE datasheet_url IS NOT NULL —
                            has_datasheet filter + global facet count (119ms for 6 rows).
- ix_mc_has_crosses         partial btree (id) WHERE cross_references IS NOT NULL AND
                            cross_references::text NOT IN ('[]','null','') — exact
                            predicate of the has_crosses filter (271ms for 2 rows).
                            Paired with stx_mc_crosses_text extended statistics on the
                            (cross_references::text) expression: every ingested row holds
                            a non-NULL '[]' (null_frac=0), and without expression stats
                            the planner guesses ~98.5% selectivity for the NOT IN and
                            walks ix_mc_order_live instead (measured 486ms regression on
                            scratch); with the stats + ANALYZE it picks this index
                            (0.1ms). ANALYZE material_cards runs at the end of the
                            migration to populate them.
- ix_mc_last_searched       partial btree (last_searched_at) WHERE last_searched_at IS
                            NOT NULL — searched_within buckets (187ms for 13 rows).

The eight 098-owned names are created WITHOUT if_not_exists: nothing in the chain
creates them, so a pre-existing same-named index can only be out-of-band DDL (e.g. a
manual experiment from the EXPLAIN investigation) whose definition may silently differ —
a loud failure here is a free drift detector. The two eabe89205d07-owned names keep
if_not_exists=True because pre-existence is the EXPECTED state on fresh replays.

Lock expectation: the repo's alembic env runs transaction-per-migration (no autocommit
blocks; no prior migration uses CONCURRENTLY), so these are plain CREATE INDEX —
each takes ShareLock on material_cards (writes blocked, reads fine). The full migration
(10 indexes + statistics + ANALYZE) took ~25s on a scratch copy of the live 153MB heap;
the enrichment worker's writes simply queue behind it.

NOTE: 097 is skipped in the chain (reserved by a concurrent branch when this shipped);
this revision chains onto 096_spec_provenance as the single head.

Downgrade drops the eight 098-owned indexes and the statistics object. The two
eabe89205d07-owned names are deliberately NOT dropped: at 096 that ancestor is still
applied and its history says they exist — only eabe89205d07's own downgrade may remove
them. (Dropping them here would leave a downgraded-to-096 schema silently missing the
FTS/trgm indexes the recorded history guarantees.)

Revision ID: 098_materials_perf_idx
Revises: 096_spec_provenance
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "098_materials_perf_idx"
down_revision = "096_spec_provenance"
branch_labels = None
depends_on = None

# (name, columns, kwargs) — declarative so upgrade/downgrade stay in lockstep.
# Owned by THIS revision: created without if_not_exists (loud on out-of-band name
# collisions), dropped on downgrade.
_NEW_INDEXES = [
    (
        "ix_mc_order_live",
        [sa.text("search_count DESC"), sa.text("created_at DESC")],
        {"postgresql_where": sa.text("deleted_at IS NULL")},
    ),
    (
        "ix_mc_cat_order_live",
        [sa.text("lower(btrim(category))"), sa.text("search_count DESC"), sa.text("created_at DESC")],
        {"postgresql_where": sa.text("deleted_at IS NULL AND lower(btrim(category)) IS NOT NULL")},
    ),
    ("ix_mc_trgm_norm_mpn", [sa.text("normalized_mpn gin_trgm_ops")], {"postgresql_using": "gin"}),
    ("ix_mc_trgm_manufacturer", [sa.text("manufacturer gin_trgm_ops")], {"postgresql_using": "gin"}),
    ("ix_mc_trgm_description", [sa.text("description gin_trgm_ops")], {"postgresql_using": "gin"}),
    (
        "ix_mc_has_datasheet",
        ["id"],
        {"postgresql_where": sa.text("datasheet_url IS NOT NULL")},
    ),
    (
        "ix_mc_has_crosses",
        ["id"],
        {
            "postgresql_where": sa.text(
                "cross_references IS NOT NULL AND cross_references::text NOT IN ('[]','null','')"
            )
        },
    ),
    (
        "ix_mc_last_searched",
        ["last_searched_at"],
        {"postgresql_where": sa.text("last_searched_at IS NOT NULL")},
    ),
]

# Owned by ancestor eabe89205d07 (still in the chain — it creates both on every fresh
# replay; identical definitions). Created here ONLY if missing, to repair the live DB
# that was stamped past eabe89205d07 without executing it. NEVER dropped on downgrade —
# at 096 the ancestor is still applied and only its own downgrade may remove them.
_HISTORICAL_INDEXES = [
    ("ix_material_cards_search_vector", ["search_vector"], {"postgresql_using": "gin"}),
    ("ix_material_cards_trgm_mpn", [sa.text("display_mpn gin_trgm_ops")], {"postgresql_using": "gin"}),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # GIN / pg_trgm / partial expression indexes are PostgreSQL-only; the SQLite
        # test DB queries the same shapes without them (feedback_sqlite_masks_postgres).
        return
    # Idempotent — 001 already creates it; defensive for partial-schema replays.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, columns, kwargs in _NEW_INDEXES:
        # No if_not_exists: a same-named leftover means out-of-band DDL with a possibly
        # different definition — fail loudly instead of silently keeping it.
        op.create_index(name, "material_cards", columns, unique=False, **kwargs)
    for name, columns, kwargs in _HISTORICAL_INDEXES:
        # if_not_exists: pre-existence is the expected state on fresh replays, where
        # ancestor eabe89205d07 has already created these.
        op.create_index(name, "material_cards", columns, unique=False, if_not_exists=True, **kwargs)
    # Expression statistics for the has_crosses predicate: every ingested row holds a
    # non-NULL cross_references (almost always '[]'), and the planner has no stats on
    # the ::text cast, so it guesses ~98.5% selectivity for the NOT IN and refuses
    # ix_mc_has_crosses. Univariate expression stats give the cast an MCV list
    # ('[]' ~ 100%), collapsing the estimate to ~0 and flipping the plan to the index.
    op.execute("CREATE STATISTICS IF NOT EXISTS stx_mc_crosses_text ON (cross_references::text) FROM material_cards")
    # Populate the new statistics (and refresh the rest post-index-build). ~2s.
    op.execute("ANALYZE material_cards")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP STATISTICS IF EXISTS stx_mc_crosses_text")
    for name, _columns, _kwargs in reversed(_NEW_INDEXES):
        op.drop_index(name, table_name="material_cards", if_exists=True)
    # _HISTORICAL_INDEXES deliberately survive — eabe89205d07 owns them (see docstring).
