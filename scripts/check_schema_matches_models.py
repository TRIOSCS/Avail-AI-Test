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
# baseline never created, two dead columns, one stale FK render, a TypeDecorator
# reflection mismatch, and a column-comment-only diff). Reconcile them properly
# later via real migrations; see the schema-drift tracking issue.
#
# Each set is keyed on a SPECIFIC NAME (table name, (table, column) pair,
# constraint name, or (table, sorted-columns) tuple) so the predicate matches
# ONLY these grandfathered objects. A NEW drift on a different name still fails
# the gate — this is intentional and must stay name-scoped, never blanket-True.
# ---------------------------------------------------------------------------

# Orphan legacy tables present in the DB with no corresponding model.
_GRANDFATHERED_REMOVE_TABLES = {
    "_sp1_desc_backup",
    "buy_plans",
    "enrichment_credit_usage",
    "notification_engagement",
    "self_heal_log",
}

# Indexes that live in the DB (raw-DDL / trgm / gin / tsvector / partial) but the
# model's metadata never declares, so autogenerate wants to drop them.
_GRANDFATHERED_REMOVE_INDEXES = {
    "ix_activity_unscored",
    "ix_activity_user_channel_created",
    "ix_buyplans_token",
    "ix_companies_domain_trgm",
    "ix_companies_name_trgm",
    "ix_companies_normalized_name_trgm",
    "ix_companies_parent_company_id",
    "ix_companies_primary_contact_id",
    "ix_ecu_provider_month",
    "ix_material_cards_search_vector",
    "ix_material_cards_trgm_mpn",
    "ix_mc_cat_order_live",
    "ix_mc_category_lower",
    "ix_mc_demand_queue",
    "ix_mc_has_crosses",
    "ix_mc_has_datasheet",
    "ix_mc_last_searched",
    "ix_mc_order_live",
    "ix_mc_trgm_description",
    "ix_mc_trgm_manufacturer",
    "ix_mc_trgm_norm_mpn",
    "ix_notif_engage_created",
    "ix_notif_engage_user_action",
    "ix_notif_engage_user_event",
    "ix_offers_evidence_tier",
    "ix_offers_excess_line_item",
    "ix_offers_mpn_trgm",
    "ix_offers_vendor_name_trgm",
    "ix_requirements_normalized_mpn_trgm",
    "ix_requirements_primary_mpn_trgm",
    "ix_requisitions_company_id",
    "ix_requisitions_customer_name_trgm",
    "ix_requisitions_name_trgm",
    "ix_sc_reports_to",
    "ix_self_heal_log_created_at",
    "ix_self_heal_log_ticket_id",
    "ix_sighting_cache_lookup",
    "ix_sightings_evidence_tier",
    "ix_site_contacts_email_trgm",
    "ix_site_contacts_full_name_trgm",
    "ix_vendor_cards_brand_tags_gin",
    "ix_vendor_cards_broadcast",
    "ix_vendor_cards_commodity_tags_gin",
    "ix_vendor_cards_display_name_trgm",
    "ix_vendor_cards_domain_lower",
    "ix_vendor_cards_domain_trgm",
    "ix_vendor_cards_normalized_name_trgm",
    "ix_vendor_contacts_email_trgm",
    "ix_vendor_contacts_full_name_trgm",
}

# Index the model declares but the DB's baseline never created (autogenerate
# wants to add it). Grandfathered: reconcile via a real migration later.
_GRANDFATHERED_ADD_INDEXES = {
    "ix_site_contacts_reports_to_id",
}

# Dead columns still in the DB but dropped from the models.
_GRANDFATHERED_REMOVE_COLUMNS = {
    ("activity_log", "source_url"),
    ("vendor_responses", "teams_alert_sent_at"),
}

# Stale FK present in the DB (named) the model no longer declares.
_GRANDFATHERED_REMOVE_FKS = {
    "fk_activity_log_quote",
}

# Unique constraints the model declares but the baseline DB never created, so
# autogenerate wants to add them. Many are single-column ``unique=True`` columns
# whose UniqueConstraint has ``name=None``, so we key on (table, sorted columns)
# rather than the constraint name.
_GRANDFATHERED_ADD_CONSTRAINTS = {
    ("api_sources", ("name",)),
    ("commodity_spec_schemas", ("commodity", "spec_key")),
    ("customer_part_history", ("company_id", "material_card_id", "source")),
    ("discovery_batches", ("batch_id",)),
    ("email_signature_extracts", ("sender_email",)),
    ("enrichment_runs", ("run_id",)),
    ("entity_tags", ("entity_id", "entity_type", "tag_id")),
    ("graph_subscriptions", ("subscription_id",)),
    ("knowledge_config", ("key",)),
    ("material_spec_facets", ("material_card_id", "spec_key")),
    ("material_tags", ("material_card_id", "tag_id")),
    ("prospect_accounts", ("domain",)),
    ("quotes", ("quote_number",)),
    ("sourcing_leads", ("part_number_matched", "requirement_id", "vendor_name_normalized")),
    ("tag_threshold_config", ("entity_type", "tag_type")),
    ("tags", ("name", "tag_type")),
    ("trouble_tickets", ("ticket_number",)),
    ("users", ("azure_id",)),
    ("users", ("email",)),
    ("vendor_sighting_summary", ("requirement_id", "vendor_name")),
    ("verification_group_members", ("user_id",)),
}

# (table, column) pairs where a TypeDecorator (``UTCDateTime`` over
# ``TIMESTAMP WITH TIME ZONE``) reflects as plain ``TIMESTAMP``, so autogenerate
# emits a no-op modify_type. The stored type is equivalent.
_GRANDFATHERED_MODIFY_TYPE = {
    ("fru_links", "created_at"),
    ("fru_links", "updated_at"),
    ("oem_crosswalk", "looked_up_at"),
    ("oem_crosswalk", "created_at"),
    ("oem_crosswalk", "updated_at"),
}

# (table, column) pairs with a column-COMMENT-only diff (model sets a comment the
# baseline DB column lacks). Cosmetic; reconcile via migration later.
_GRANDFATHERED_MODIFY_COMMENT = {
    ("material_cards", "enrichment_status"),
}


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
