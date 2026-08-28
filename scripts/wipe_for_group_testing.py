"""wipe_for_group_testing.py — pre-group-testing data wipe (owner-run, rehearsed).

Wipes transactional/test data while KEEPING customer accounts, staff users, and
system configuration. Two corpus modes decide the fate of the part/vendor
intelligence corpus (materials, vendors, enrichment artifacts):

  --corpus keep   (RECOMMENDED) keep material/vendor intelligence — months of
                  enrichment work that contains no transactional test junk.
  --corpus wipe   literal "everything except customer accounts": the corpus goes
                  too (material_cards, vendor_cards, tags, facets, FRU links, ...).

Safety: refuses to run without an explicit --dsn (never reads DATABASE_URL, so it
can never default onto prod) AND --i-understand-this-deletes-data. Uses a single
TRUNCATE ... RESTART IDENTITY across the wipe set WITHOUT CASCADE — if a
referencing table was missed, PostgreSQL fails loudly instead of silently pulling
extra tables in. Prints per-table before/after counts.

Called by: the owner, manually, on wipe day (rehearsed on a throwaway copy first —
    see docs/RUNBOOK_GROUP_TESTING_WIPE.md)
Depends on: psycopg via SQLAlchemy engine only (stdlib + sqlalchemy)
"""

import argparse
import sys

from sqlalchemy import create_engine, text

# ── Classification (every table in the live schema appears in exactly one list;
#    verify_coverage() enforces this against the target DB at runtime). ──────────

KEEP_ALWAYS = [
    # identity + configuration + seeds
    "alembic_version",
    "users",
    "user_admin_audit",
    "system_config",
    "api_sources",
    "manufacturers",
    "tag_threshold_config",
    "commodity_spec_schemas",
    # customer accounts (the owner's explicit keep)
    "companies",
    "customer_sites",
    "site_contacts",
    "site_contact_attachments",
    "company_attachments",
    "account_collaborators",
    "crm_field_history",
    "prospect_accounts",
    "prospect_contacts",
    # per-user UI state (belongs to kept users)
    "saved_views",
    # prospecting provenance for kept prospect_accounts
    "discovery_batches",
]

# The part/vendor intelligence corpus — kept under --corpus keep, wiped under
# --corpus wipe.
CORPUS = [
    "material_cards",
    "material_tags",
    "material_spec_facets",
    "material_card_audit",
    "material_card_attachments",
    "material_card_datasheets",
    "material_price_snapshots",
    "material_vendor_history",
    "fru_links",
    "oem_crosswalk",
    "oem_spec_codes",
    "oem_spec_codes_pending",
    "oem_spec_codes_blacklist",
    "partsurfer_desc_negative",
    "knowledge_entries",
    "tags",
    "entity_tags",
    "part_equivalences",
    "_sp1_desc_backup",
    "vendor_cards",
    "vendor_contacts",
    "vendor_contact_attachments",
    "vendor_metrics_snapshot",
    "vendor_reviews",
    "strategic_vendors",
    "vendor_part_unavailability",
    "vendor_card_attachments",
]

# Transactional / test / derived data — always wiped.
WIPE_ALWAYS = [
    # sourcing pipeline
    "requisitions",
    "requirements",
    "offers",
    "offer_attachments",
    "sightings",
    "vendor_responses",
    "vendor_sighting_summary",
    "sourcing_leads",
    "lead_evidence",
    "customer_part_history",
    # quotes & deals
    "quotes",
    "quote_lines",
    "quote_requisitions",
    "buy_plans_v3",
    "buy_plan_lines",
    "buy_plan_attachments",
    "po_cancellations",
    "quality_plans",
    "prepayments",
    "verification_group_members",
    # approvals engine
    "approval_requests",
    "approval_steps",
    "approval_step_recipients",
    "approval_events",
    "approval_outbox",
    # resell
    "excess_lists",
    "excess_line_items",
    "excess_offers",
    "excess_offer_lines",
    "excess_outreach",
    "customer_bids",
    "customer_bid_lines",
    # proactive
    "proactive_matches",
    "proactive_digests",
    "proactive_outreach_lines",
    "proactive_throttle",
    "proactive_do_not_offer",
    # comms / activity / tasks
    "activity_log",
    "activity_digest",
    "notifications",
    "requisition_tasks",
    "trouble_tickets",
    "email_intelligence",
    "email_signature_extracts",
    "processed_messages",
    "graph_subscriptions",
    "change_log",
    # scores / analytics snapshots (recomputed)
    "avail_score_snapshot",
    "multiplier_score_snapshot",
    "unified_score_snapshot",
    "buyer_leaderboard_snapshot",
    "buyer_scores",
    "buyer_vendor_stats",
    "root_cause_groups",
    # ops / workers / caches / logs
    "api_usage_log",
    "pending_batches",
    "enrichment_jobs",
    "ics_search_queue",
    "ics_search_log",
    "ics_worker_status",
    "nc_search_queue",
    "nc_search_log",
    "nc_worker_status",
    "tbf_search_queue",
    "tbf_search_log",
    "tbf_worker_status",
    "enrichment_worker_status",
    "sync_state",
    "alert_seen",
    "intel_cache",
    "stock_list_hashes",
    "column_mapping_cache",
    "reconcile_runs",
    "enrichment_runs",
    "lead_feedback_events",
    "proactive_offers",
    # quality-plan ops tracking (per-TSO serial/FRU entries ride with the wiped deals)
    "qp_fru_lookups",
    "qp_serial_entries",
    "requirement_attachments",
    "requisition_attachments",
    # legacy contacts table (0 rows, superseded by site_contacts; CASCADE FK to requisitions)
    "contacts",
]


def _counts(conn, tables: list[str]) -> dict[str, int]:
    out = {}
    for t in tables:
        out[t] = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
    return out


def verify_coverage(conn) -> list[str]:
    """Every live table must be classified; return the unclassified ones."""
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars()
    classified = set(KEEP_ALWAYS) | set(CORPUS) | set(WIPE_ALWAYS)
    return sorted(set(rows) - classified)


def _guard_keep_to_wipe_cascades(conn, keep_set: list[str], wipe_set: list[str]) -> None:
    """Refuse if any KEPT table has an ON DELETE CASCADE FK into the wipe set.

    The wipe uses DELETE (not TRUNCATE), so keep→wipe SET NULL pointers null themselves
    automatically — but a CASCADE edge would silently delete KEPT rows. That is a
    classification error to fix, never to power through.
    """
    rows = conn.execute(
        text(
            """
            SELECT conrelid::regclass::text AS child,
                   confrelid::regclass::text AS parent,
                   confdeltype
            FROM pg_constraint WHERE contype = 'f'
            """
        )
    ).fetchall()
    keep, wipe = set(keep_set), set(wipe_set)
    bad = [(r.child, r.parent) for r in rows if r.child in keep and r.parent in wipe and r.confdeltype == "c"]
    if bad:
        raise SystemExit(f"Refusing: keep→wipe ON DELETE CASCADE edges (reclassify these): {bad}")


def _delete_all(conn, wipe_set: list[str]) -> None:
    """DELETE every wipe-set table empty, iterating so intra-set RESTRICT FKs (e.g.
    prepayments → buy_plans_v3) drain child-first without a topo sort; then restart each
    table's identity sequence."""
    remaining = list(wipe_set)
    for _pass in range(10):
        still = []
        for t_name in remaining:
            try:
                with conn.begin_nested():
                    conn.execute(text(f'DELETE FROM "{t_name}"'))
            except Exception:
                still.append(t_name)  # blocked by a RESTRICT child this pass
                continue
        if not still:
            break
        if len(still) == len(remaining):
            raise SystemExit(f"Refusing: could not drain (circular RESTRICT?): {still}")
        remaining = still
    for t_name in wipe_set:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_default LIKE 'nextval%'"
            ),
            {"t": t_name},
        ).scalars()
        for col in cols:
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": f'"{t_name}"', "c": col}).scalar()
            if seq:
                conn.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True, help="Target PostgreSQL DSN (explicit — no env fallback)")
    ap.add_argument("--corpus", choices=["keep", "wipe"], required=True)
    ap.add_argument("--i-understand-this-deletes-data", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Report the plan + counts, change nothing")
    args = ap.parse_args()

    if not args.i_understand_this_deletes_data and not args.dry_run:
        print("Refusing: pass --i-understand-this-deletes-data (or --dry-run).", file=sys.stderr)
        return 2

    wipe_set = WIPE_ALWAYS + (CORPUS if args.corpus == "wipe" else [])
    keep_set = KEEP_ALWAYS + (CORPUS if args.corpus == "keep" else [])

    engine = create_engine(args.dsn)
    with engine.begin() as conn:
        missing = verify_coverage(conn)
        if missing:
            print(f"Refusing: unclassified tables in target DB: {missing}", file=sys.stderr)
            print("Add each to KEEP_ALWAYS / CORPUS / WIPE_ALWAYS first.", file=sys.stderr)
            return 3

        before = _counts(conn, wipe_set)
        keep_before = _counts(conn, keep_set)
        print(f"Corpus mode: {args.corpus}")
        print(f"Wiping {len(wipe_set)} tables ({sum(before.values()):,} rows); keeping {len(keep_set)} tables.")
        for t in sorted(wipe_set):
            if before[t]:
                print(f"  wipe {t}: {before[t]:,}")
        if args.dry_run:
            print("DRY RUN — nothing changed.")
            return 0

        _guard_keep_to_wipe_cascades(conn, keep_set, wipe_set)
        # DELETE (not TRUNCATE): honors keep→wipe ON DELETE SET NULL provenance
        # pointers natively, and TRUNCATE's structural FK rule would force kept
        # referencing tables into the statement.
        _delete_all(conn, wipe_set)

        after = _counts(conn, wipe_set)
        keep_after = _counts(conn, keep_set)

    leftover = {t: n for t, n in after.items() if n}
    keep_diff = {t: (keep_before[t], keep_after[t]) for t in keep_set if keep_before[t] != keep_after[t]}
    print(f"Done. Wiped rows: {sum(before.values()):,} → {sum(after.values()):,}")
    if leftover:
        print(f"ERROR: rows left in wipe set: {leftover}", file=sys.stderr)
        return 4
    if keep_diff:
        print(f"ERROR: keep-set counts changed: {keep_diff}", file=sys.stderr)
        return 5
    print("Keep-set verified unchanged:")
    for t in sorted(keep_set):
        if keep_after[t]:
            print(f"  kept {t}: {keep_after[t]:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
