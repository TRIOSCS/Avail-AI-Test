"""scripts/check_schema_matches_models.py — Schema-equivalence check.

Connects to DATABASE_URL, reflects the live schema, runs
alembic.autogenerate.compare_metadata against app.models.Base.metadata,
filters known false positives, prints any remaining drift, exits non-zero
on drift.

Called by: .github/workflows/ci.yml — invoked between ``alembic upgrade head``
and ``alembic downgrade base`` in the "Alembic upgrade/downgrade smoke test"
step, so a model-vs-migration drift fails CI. Also runnable by local devs.
Depends on: app.models.Base, alembic, sqlalchemy.

Two filtering mechanisms, deliberately separate:

* ``_ALLOWLIST`` — genuine *false positives*: cosmetic type-rendering quirks where
  the model and the migration produce identical DDL but ``compare_metadata`` reports a
  difference (e.g. ``Numeric`` reflected as ``NUMERIC``; the ``UTCDateTime`` TypeDecorator
  vs its ``TIMESTAMP`` impl). These are not drift.
* ``_GRANDFATHERED_DIFFS`` — *real, pre-existing drift* accepted on an INTERIM basis while
  it is reconciled bucket-by-bucket via real migrations. Each entry is a name-scoped
  signature (see ``_diff_signature``): a NEW drift on a different table/column/index/
  constraint produces a signature that is NOT in the set and therefore still fails CI.
  Exit path tracked in ``docs/BRANCH_AND_CI_WORKFLOW.md`` (grandfather-reconciliation issue).

Usage:
    DATABASE_URL=postgresql://... python scripts/check_schema_matches_models.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

# Each entry is a (diff_kind, predicate) tuple. The predicate gets the raw diff
# tuple and returns True if the entry is a known false positive that should be
# dropped from the result. Keep this list short; every entry needs a comment
# explaining the underlying alembic/sqlalchemy quirk.
_ALLOWLIST: list[tuple[str, Callable[..., bool]]] = [
    # Numeric(10, 2) reflected as NUMERIC(10, 2) — same type, different rendering.
    # alembic.autogenerate sometimes flags this as modify_type. The check uses
    # str() on both sides so it works whether the values are SQLAlchemy type
    # objects (real alembic output) or their string representations (tests).
    (
        "modify_type",
        lambda d: len(d) >= 7 and "NUMERIC" in str(d[5]).upper() and "numeric" in str(d[6]).lower(),
    ),
    # UTCDateTime is a TypeDecorator whose impl is TIMESTAMP (see app/database.py).
    # The migrations build the column as TIMESTAMP; the model declares UTCDateTime, and
    # alembic reports a modify_type even though the emitted DDL is identical. repr() is used
    # (not str()) because str(UTCDateTime()) renders as its impl "TIMESTAMP" — only repr keeps
    # the decorator name. False positive.
    (
        "modify_type",
        lambda d: len(d) >= 7 and "TIMESTAMP" in repr(d[5]).upper() and "UTCDATETIME" in repr(d[6]).upper(),
    ),
]

# Pre-existing drift between the migration-built schema and app.models, accepted on an
# INTERIM basis. NAME-SCOPED via _diff_signature: a new drift of any other name still fails.
# DANGER — these model-less tables are NEVER auto-dropped: buy_plans, enrichment_credit_usage,
# and notification_engagement hold live production data; dropping _sp1_desc_backup breaks the
# migration-091 downgrade. They are grandfathered (kept), and reconciled via real migrations
# per the tracking issue (#465) — see docs/BRANCH_AND_CI_WORKFLOW.md.
_GRANDFATHERED_DIFFS: frozenset[tuple] = frozenset(
    {
        # --- Model-less tables retained in the live DB (data / downgrade safety) ---
        ("remove_table", "_sp1_desc_backup"),
        ("remove_table", "buy_plans"),
        ("remove_table", "enrichment_credit_usage"),
        ("remove_table", "notification_engagement"),
        ("remove_table", "self_heal_log"),
        # --- Indexes belonging to those model-less tables ---
        ("remove_index", "buy_plans", "ix_buyplans_token"),
        ("remove_index", "enrichment_credit_usage", "ix_ecu_provider_month"),
        ("remove_index", "notification_engagement", "ix_notif_engage_created"),
        ("remove_index", "notification_engagement", "ix_notif_engage_user_action"),
        ("remove_index", "notification_engagement", "ix_notif_engage_user_event"),
        ("remove_index", "self_heal_log", "ix_self_heal_log_created_at"),
        ("remove_index", "self_heal_log", "ix_self_heal_log_ticket_id"),
        # --- Raw-SQL trigram / GIN / functional-partial indexes created in app/startup.py.
        #     These are PostgreSQL-specific and intentionally not declared on the ORM models. ---
        ("remove_index", "activity_log", "ix_activity_unscored"),
        ("remove_index", "activity_log", "ix_activity_user_channel_created"),
        ("remove_index", "companies", "ix_companies_domain_trgm"),
        ("remove_index", "companies", "ix_companies_name_trgm"),
        ("remove_index", "companies", "ix_companies_normalized_name_trgm"),
        ("remove_index", "companies", "ix_companies_parent_company_id"),
        ("remove_index", "companies", "ix_companies_primary_contact_id"),
        ("remove_index", "material_cards", "ix_material_cards_search_vector"),
        ("remove_index", "material_cards", "ix_material_cards_trgm_mpn"),
        ("remove_index", "material_cards", "ix_mc_cat_order_live"),
        ("remove_index", "material_cards", "ix_mc_category_lower"),
        ("remove_index", "material_cards", "ix_mc_demand_queue"),
        ("remove_index", "material_cards", "ix_mc_has_crosses"),
        ("remove_index", "material_cards", "ix_mc_has_datasheet"),
        ("remove_index", "material_cards", "ix_mc_last_searched"),
        ("remove_index", "material_cards", "ix_mc_order_live"),
        ("remove_index", "material_cards", "ix_mc_trgm_description"),
        ("remove_index", "material_cards", "ix_mc_trgm_manufacturer"),
        ("remove_index", "material_cards", "ix_mc_trgm_norm_mpn"),
        ("remove_index", "offers", "ix_offers_evidence_tier"),
        ("remove_index", "offers", "ix_offers_excess_line_item"),
        ("remove_index", "offers", "ix_offers_mpn_trgm"),
        ("remove_index", "offers", "ix_offers_vendor_name_trgm"),
        ("remove_index", "requirements", "ix_requirements_normalized_mpn_trgm"),
        ("remove_index", "requirements", "ix_requirements_primary_mpn_trgm"),
        ("remove_index", "requisitions", "ix_requisitions_company_id"),
        ("remove_index", "requisitions", "ix_requisitions_customer_name_trgm"),
        ("remove_index", "requisitions", "ix_requisitions_name_trgm"),
        ("remove_index", "sightings", "ix_sighting_cache_lookup"),
        ("remove_index", "sightings", "ix_sightings_evidence_tier"),
        ("remove_index", "site_contacts", "ix_sc_reports_to"),
        ("remove_index", "site_contacts", "ix_site_contacts_email_trgm"),
        ("remove_index", "site_contacts", "ix_site_contacts_full_name_trgm"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_brand_tags_gin"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_broadcast"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_commodity_tags_gin"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_display_name_trgm"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_domain_lower"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_domain_trgm"),
        ("remove_index", "vendor_cards", "ix_vendor_cards_normalized_name_trgm"),
        ("remove_index", "vendor_contacts", "ix_vendor_contacts_email_trgm"),
        ("remove_index", "vendor_contacts", "ix_vendor_contacts_full_name_trgm"),
        # --- Model declares an index the migrations have not yet created ---
        ("add_index", "site_contacts", "ix_site_contacts_reports_to_id"),
        # --- Dead columns pending a drop migration ---
        ("remove_column", "activity_log", "source_url"),
        ("remove_column", "vendor_responses", "teams_alert_sent_at"),
        # --- Stale FK pending cleanup ---
        ("remove_fk", "activity_log", "fk_activity_log_quote"),
        # --- Model-declared UniqueConstraints not present (by name) in the migration-built
        #     schema. Reconciled by naming them in real migrations. (table, sorted-cols) ---
        ("add_constraint", "api_sources", ("name",), "UniqueConstraint"),
        ("add_constraint", "commodity_spec_schemas", ("commodity", "spec_key"), "UniqueConstraint"),
        ("add_constraint", "customer_part_history", ("company_id", "material_card_id", "source"), "UniqueConstraint"),
        ("add_constraint", "discovery_batches", ("batch_id",), "UniqueConstraint"),
        ("add_constraint", "email_signature_extracts", ("sender_email",), "UniqueConstraint"),
        ("add_constraint", "enrichment_runs", ("run_id",), "UniqueConstraint"),
        ("add_constraint", "entity_tags", ("entity_id", "entity_type", "tag_id"), "UniqueConstraint"),
        ("add_constraint", "graph_subscriptions", ("subscription_id",), "UniqueConstraint"),
        ("add_constraint", "knowledge_config", ("key",), "UniqueConstraint"),
        ("add_constraint", "material_spec_facets", ("material_card_id", "spec_key"), "UniqueConstraint"),
        ("add_constraint", "material_tags", ("material_card_id", "tag_id"), "UniqueConstraint"),
        ("add_constraint", "prospect_accounts", ("domain",), "UniqueConstraint"),
        ("add_constraint", "quotes", ("quote_number",), "UniqueConstraint"),
        (
            "add_constraint",
            "sourcing_leads",
            ("part_number_matched", "requirement_id", "vendor_name_normalized"),
            "UniqueConstraint",
        ),
        ("add_constraint", "tag_threshold_config", ("entity_type", "tag_type"), "UniqueConstraint"),
        ("add_constraint", "tags", ("name", "tag_type"), "UniqueConstraint"),
        ("add_constraint", "trouble_tickets", ("ticket_number",), "UniqueConstraint"),
        ("add_constraint", "users", ("azure_id",), "UniqueConstraint"),
        ("add_constraint", "users", ("email",), "UniqueConstraint"),
        ("add_constraint", "vendor_sighting_summary", ("requirement_id", "vendor_name"), "UniqueConstraint"),
        ("add_constraint", "verification_group_members", ("user_id",), "UniqueConstraint"),
        # --- Model-declared column comment not emitted by the migration ---
        ("modify_comment", "material_cards", "enrichment_status"),
    }
)


def _obj_name(obj: Any) -> Any:
    """Return obj.name for a SQLAlchemy element, else the value itself.

    Defensive so signatures work for both real alembic objects (production) and the
    plain-tuple/string diffs used in unit tests.
    """
    return getattr(obj, "name", obj)


def _diff_signature(d: tuple) -> tuple | None:
    """Normalize an alembic diff to a stable, name-scoped signature.

    Returns None for diff shapes that are never grandfathered (so they always surface).
    Avoids object repr() — which embeds memory addresses for functional indexes — and
    keys only on durable names so the signature is identical across runs and environments.
    """
    kind = d[0] if d else None
    if kind in ("add_table", "remove_table"):
        return (kind, _obj_name(d[1]))
    if kind in ("add_index", "remove_index"):
        idx = d[1]
        return (kind, _obj_name(getattr(idx, "table", None)), _obj_name(idx))
    if kind in ("add_column", "remove_column"):
        return (kind, d[2], _obj_name(d[3]))
    if kind in ("add_fk", "remove_fk"):
        fk = d[1]
        return (kind, _obj_name(getattr(fk, "table", None)), getattr(fk, "name", None))
    if kind in ("add_constraint", "remove_constraint"):
        con = d[1]
        cols = tuple(sorted(_obj_name(c) for c in getattr(con, "columns", [])))
        return (kind, _obj_name(getattr(con, "table", None)), cols, type(con).__name__)
    if kind == "modify_comment":
        return (kind, d[2], d[3])
    return None


def _is_dropped(entry: tuple) -> bool:
    """True if a single diff tuple is an allowlisted false positive or grandfathered drift."""
    kind = entry[0] if entry else None
    if any(kind == allow_kind and predicate(entry) for allow_kind, predicate in _ALLOWLIST):
        return True
    return _diff_signature(entry) in _GRANDFATHERED_DIFFS


def filter_allowlist(diffs: Iterable[tuple]) -> list[tuple]:
    """Drop false-positive diffs (``_ALLOWLIST``) and grandfathered drift (``_GRANDFATHERED_DIFFS``).

    alembic.compare_metadata yields column-level modifications (modify_type / modify_nullable /
    modify_comment / ...) as a *list of tuples* grouped per column, and everything else as a bare
    tuple. The previous implementation tested the predicate against the outer list, so ``d[0]`` was
    the inner tuple rather than the diff kind and the modify_* allowlist never fired (dead code).
    Unwrap list-form diffs so each inner tuple is evaluated, and keep only the non-dropped ones.
    """
    out: list[tuple] = []
    for d in diffs:
        if isinstance(d, list):
            kept = [entry for entry in d if not _is_dropped(entry)]
            if kept:
                out.append(kept)
        elif not _is_dropped(d):
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
    print(f"Schema matches Base.metadata. ({len(raw_diffs)} raw diff(s), all allowlisted/grandfathered.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
