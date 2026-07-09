"""scripts/check_schema_matches_models.py — Schema-equivalence check.

Connects to DATABASE_URL, reflects the live schema, runs
alembic.autogenerate.compare_metadata against app.models.Base.metadata,
filters known false positives, prints any remaining drift, exits non-zero
on drift.

Called by: .github/workflows/ci.yml — invoked between ``alembic upgrade head``
and ``alembic downgrade base`` in the "Alembic upgrade/downgrade smoke test"
step, so a model-vs-migration drift fails CI. Also runnable by local devs.
Depends on: app.models.Base, alembic, sqlalchemy.

Usage:
    DATABASE_URL=postgresql://... python scripts/check_schema_matches_models.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

# ---------------------------------------------------------------------------
# Grandfathered pre-existing drift (pre-dates the schema-drift gate).
#
# These entries are NOT false positives — they are genuine model-vs-DB drift
# that already existed in `001`-era migrations / raw-DDL startup hooks before
# this gate landed (orphan legacy tables with no model, indexes created by raw
# DDL the model never declares, unique constraints the model declares but the
# baseline never created, a TypeDecorator reflection mismatch, and a
# column-comment-only diff). Reconcile them properly later via real migrations;
# see the schema-drift tracking issue. (The two dead columns + one redundant FK
# were reconciled by migration 154 / #464; ~35 raw-DDL indexes were declared on the
# models + the duplicate ix_requisitions_company_id dropped by migration 172 / #464.)
#
# Each set is keyed on a SPECIFIC NAME (table name, (table, column) pair,
# constraint name, or (table, sorted-columns) tuple) so the predicate matches
# ONLY these grandfathered objects. A NEW drift on a different name still fails
# the gate — this is intentional and must stay name-scoped, never blanket-True.
# ---------------------------------------------------------------------------

# Orphan legacy tables present in the DB with no corresponding model.
# 2026-07-02 (#464 finish): buy_plans (V1), notification_engagement and self_heal_log
# are dropped by migration 174 (IF EXISTS — live staging already lacked them), so their
# entries were removed; if a future DB somehow resurrects them, the gate SHOULD flag it.
# Still grandfathered:
#   - _sp1_desc_backup — 700 live rows; migration 091's downgrade restore path needs it.
#   - enrichment_credit_usage — exists but EMPTY and referenced only by migration 030;
#     the old "billing telemetry, never drop" rationale looks moot, but dropping needs
#     an explicit product decision — keep grandfathered until the user approves.
_GRANDFATHERED_REMOVE_TABLES = {
    "_sp1_desc_backup",
    "enrichment_credit_usage",
}

# Indexes that live in the DB (raw-DDL) but the model's metadata never declares, so
# autogenerate wants to drop them. Migration 172 / #464 reconciled the bulk of these:
# ~35 pg_trgm GIN, GIN-on-JSONB-tag-array, FTS-tsvector GIN, plain-btree-FK, and simple
# partial indexes were declared directly on the ORM models (so autogenerate now sees
# them), and the duplicate ``ix_requisitions_company_id`` was dropped. The two groups
# below are the ones that deliberately stay grandfathered:
#
#   1. DANGER / orphan-table indexes — they belong to tables held in
#      ``_GRANDFATHERED_REMOVE_TABLES`` (live-data ``buy_plans`` /
#      ``enrichment_credit_usage`` we must never drop, plus orphan
#      ``notification_engagement`` / ``self_heal_log``). The index can't be reconciled
#      without first reconciling its table, which is out of scope.
#   2. PostgreSQL-only expression / complex-partial indexes — their definitions use
#      PG-specific SQL (``lower(col::text)``, ``TRIM(BOTH FROM ...)``, ``DESC NULLS LAST``,
#      ``<> ALL (ARRAY[...])`` predicates) that can't be expressed on the model in a way
#      that stays valid for the SQLite test engine, so they remain intentional raw-DDL.
_GRANDFATHERED_REMOVE_INDEXES = {
    # 1. Orphan-table index (see _GRANDFATHERED_REMOVE_TABLES). The buy_plans /
    #    notification_engagement / self_heal_log index entries left with their
    #    tables (2026-07-02, #464 finish — tables no longer exist on the rebuilt DB).
    "ix_ecu_provider_month",
    # 2. PostgreSQL-only expression / complex-partial indexes (intentional raw-DDL).
    "ix_mc_cat_order_live",
    "ix_mc_category_lower",
    "ix_mc_demand_queue",
    "ix_mc_has_crosses",
    "ix_mc_has_datasheet",
    "ix_mc_order_live",
    "ix_vendor_cards_domain_lower",
    # 3. Startup-backfill partial indexes (migration 187, P2.7) — operational
    #    existence-check indexes on the deferred backfills' IS NULL predicates,
    #    same shape/rationale as ix_mc_has_datasheet above. Migration-only by
    #    design (dropped when a backfill's predicate set goes permanently empty).
    "ix_requirements_backfill_norm_mpn",
    "ix_material_cards_backfill_norm_mpn",
    "ix_sightings_backfill_norm_mpn",
    "ix_offers_backfill_norm_mpn",
    "ix_sightings_backfill_vendor_norm",
    "ix_offers_backfill_vendor_norm",
    "ix_trouble_tickets_backfill_defaults",
    "ix_prospect_accounts_backfill_cooldown",
}

# Index the model declares but the DB's baseline never created (autogenerate wants to
# add it). Reconciled by #464: the site_contacts ``reports_to_id`` column dropped its
# ``index=True`` (which autogenerated the never-created ``ix_site_contacts_reports_to_id``)
# in favour of an explicit ``Index("ix_sc_reports_to", ...)`` matching the DB.
_GRANDFATHERED_ADD_INDEXES: set[str] = set()

# Dead columns still in the DB but dropped from the models.
# Reconciled by migration 154 (#464): activity_log.source_url and
# vendor_responses.teams_alert_sent_at were dropped, so the gate enforces them for real now.
_GRANDFATHERED_REMOVE_COLUMNS: set[tuple[str, str]] = set()

# Stale FK present in the DB (named) the model no longer declares.
# Reconciled by migration 154 (#464): the redundant fk_activity_log_quote was dropped, so the
# gate enforces it for real now.
_GRANDFATHERED_REMOVE_FKS: set[str] = set()

# Unique constraints the model declares but the baseline DB never created.
# Reconciled by migration 174 (#464 finish): all 21 were duplicate-checked clean on
# the live PG and created with model-matching names/column order, so the gate
# enforces them for real now.
_GRANDFATHERED_ADD_CONSTRAINTS: set[tuple[str, tuple[str, ...]]] = set()

# (table, column) pairs where a TypeDecorator (``UTCDateTime`` over
# ``TIMESTAMP WITH TIME ZONE``) reflects as plain ``TIMESTAMP``, so autogenerate
# emits a no-op modify_type. The stored type is equivalent.
_GRANDFATHERED_MODIFY_TYPE = {
    ("fru_links", "created_at"),
    ("fru_links", "updated_at"),
    ("oem_crosswalk", "looked_up_at"),
    ("oem_crosswalk", "created_at"),
    ("oem_crosswalk", "updated_at"),
    # partsurfer_desc_negative (migration 152): same UTCDateTime → TIMESTAMP no-op.
    ("partsurfer_desc_negative", "looked_up_at"),
    ("partsurfer_desc_negative", "retry_after"),
    ("partsurfer_desc_negative", "created_at"),
    ("partsurfer_desc_negative", "updated_at"),
}

# (table, column) pairs with a column-COMMENT-only diff. Reconciled by migration 174
# (#464 finish): the comment now lives on BOTH the model (intelligence.py) and the
# migration-built DB, so the gate enforces comment parity for real now.
_GRANDFATHERED_MODIFY_COMMENT: set[tuple[str, str]] = set()


def _add_constraint_key(diff: tuple) -> tuple[str | None, tuple[str, ...]]:
    """Return (table_name, sorted-column-names) for an add_constraint diff."""
    constraint = diff[1]
    table = constraint.table.name if constraint.table is not None else None
    return table, tuple(sorted(col.name for col in constraint.columns))


# Each entry is a (diff_kind, predicate) tuple. The predicate gets the raw diff
# tuple and returns True if the entry should be dropped from the result. Keep
# every entry commented with the underlying alembic/sqlalchemy quirk or the
# grandfathering rationale. Predicates must stay NAME-SCOPED (match only the
# listed objects) so genuinely new drift still fails the gate.
_ALLOWLIST: list[tuple[str, Callable[..., bool]]] = [
    # Numeric(10, 2) reflected as NUMERIC(10, 2) — same type, different rendering.
    # alembic.autogenerate sometimes flags this as modify_type. The check uses
    # str() on both sides so it works whether the values are SQLAlchemy type
    # objects (real alembic output) or their string representations (tests).
    (
        "modify_type",
        lambda d: len(d) >= 7 and "NUMERIC" in str(d[5]).upper() and "numeric" in str(d[6]).lower(),
    ),
    # --- Grandfathered pre-existing drift (see the _GRANDFATHERED_* sets) ---
    ("remove_table", lambda d: d[1].name in _GRANDFATHERED_REMOVE_TABLES),
    ("remove_index", lambda d: d[1].name in _GRANDFATHERED_REMOVE_INDEXES),
    ("add_index", lambda d: d[1].name in _GRANDFATHERED_ADD_INDEXES),
    ("remove_column", lambda d: (d[2], d[3].name) in _GRANDFATHERED_REMOVE_COLUMNS),
    ("remove_fk", lambda d: d[1].name in _GRANDFATHERED_REMOVE_FKS),
    ("add_constraint", lambda d: _add_constraint_key(d) in _GRANDFATHERED_ADD_CONSTRAINTS),
    ("modify_type", lambda d: len(d) >= 4 and (d[2], d[3]) in _GRANDFATHERED_MODIFY_TYPE),
    ("modify_comment", lambda d: len(d) >= 4 and (d[2], d[3]) in _GRANDFATHERED_MODIFY_COMMENT),
]


def filter_allowlist(diffs: Iterable[tuple]) -> list[tuple]:
    """Drop diff entries that match a documented allowlist pattern.

    ``compare_metadata`` returns column-level diffs (modify_type, modify_comment)
    wrapped in a single-element list, e.g. ``[('modify_type', ...)]``; table- and
    constraint-level diffs come back as bare tuples. Unwrap the former so the
    predicates always see the inner ``(kind, ...)`` tuple.
    """
    out: list[tuple] = []
    for d in diffs:
        probe = d[0] if isinstance(d, list) and len(d) == 1 else d
        kind = probe[0] if probe else None
        allowed = any(kind == allow_kind and predicate(probe) for allow_kind, predicate in _ALLOWLIST)
        if not allowed:
            out.append(d)
    return out


def format_diffs(diffs: Iterable[tuple]) -> str:
    """Human-readable rendering of remaining diffs, one per line."""
    lines = ["  " + " | ".join(repr(part) for part in d) for d in diffs]
    return "\n".join(lines) if lines else "(no diffs)"


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2
    engine = create_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        raw_diffs = list(compare_metadata(ctx, Base.metadata))
    filtered = filter_allowlist(raw_diffs)
    if filtered:
        print("Schema drift detected vs app.models.Base.metadata:")
        print(format_diffs(filtered))
        print(f"\n{len(filtered)} drift entr{'y' if len(filtered) == 1 else 'ies'}.")
        return 1
    print(f"Schema matches Base.metadata. ({len(raw_diffs)} raw diff(s), all in allowlist.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
