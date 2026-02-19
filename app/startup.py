"""
startup.py — Database Startup Migrations (Idempotent)

Tables, columns, and indexes are defined in the ORM models (models.py) and created
via Base.metadata.create_all(checkfirst=True). This file only handles PostgreSQL-
specific operations that can't be expressed in the ORM: triggers, seeds, backfills,
and CHECK constraints.

Called by: main.py lifespan
Depends on: database.py (engine), models.py (Base)
"""

import logging
from sqlalchemy import text as sqltext
from .database import engine

log = logging.getLogger(__name__)


def run_startup_migrations() -> None:
    """Execute all idempotent startup operations. Safe to call on every app boot."""
    import os

    if os.environ.get("TESTING"):
        log.info("TESTING mode — skipping startup migrations")
        return

    # Create all tables/columns/indexes defined in ORM models (no-op if they exist)
    from .models import Base
    Base.metadata.create_all(bind=engine, checkfirst=True)
    log.info("ORM schema sync complete (create_all checkfirst=True)")

    with engine.connect() as conn:
        _create_fts_triggers(conn)
        _backfill_fts(conn)
        _seed_system_config(conn)
        _seed_site_contacts(conn)
        _add_check_constraints(conn)

    _backfill_normalized_mpn()
    log.info("Startup migrations complete")


def _exec(conn, stmt: str) -> None:
    """Execute a single DDL statement with rollback on failure."""
    try:
        conn.execute(sqltext(stmt))
        conn.commit()
    except Exception as e:
        log.warning("DDL failed: %s", e)
        conn.rollback()


# ── Full-text search triggers (PostgreSQL-specific) ─────────────────


def _create_fts_triggers(conn) -> None:
    """Create trigger functions and triggers for FTS on vendor_cards and material_cards."""
    _exec(conn, """
        CREATE OR REPLACE FUNCTION vendor_cards_fts_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', COALESCE(NEW.display_name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.normalized_name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.domain, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.industry, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    _exec(conn, """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vc_fts') THEN
                CREATE TRIGGER trg_vc_fts BEFORE INSERT OR UPDATE ON vendor_cards
                FOR EACH ROW EXECUTE FUNCTION vendor_cards_fts_update();
            END IF;
        END $$;
    """)

    _exec(conn, """
        CREATE OR REPLACE FUNCTION material_cards_fts_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', COALESCE(NEW.display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    _exec(conn, """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mc_fts') THEN
                CREATE TRIGGER trg_mc_fts BEFORE INSERT OR UPDATE ON material_cards
                FOR EACH ROW EXECUTE FUNCTION material_cards_fts_update();
            END IF;
        END $$;
    """)


# ── One-time FTS backfill ────────────────────────────────────────────


def _backfill_fts(conn) -> None:
    """Backfill search_vector on existing rows where NULL."""
    _exec(conn, """
        UPDATE vendor_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(domain, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(industry, '')), 'C')
        WHERE search_vector IS NULL
    """)

    _exec(conn, """
        UPDATE material_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(manufacturer, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'C')
        WHERE search_vector IS NULL
    """)


# ── Seed data ────────────────────────────────────────────────────────


def _seed_system_config(conn) -> None:
    """Seed default scoring weights and feature flags (INSERT ON CONFLICT DO NOTHING)."""
    seeds = [
        ("weight_recency", "30", "Scoring weight for data recency (0-100)"),
        ("weight_quantity", "20", "Scoring weight for quantity match (0-100)"),
        ("weight_vendor_reliability", "20", "Scoring weight for vendor reliability (0-100)"),
        ("weight_data_completeness", "10", "Scoring weight for data completeness (0-100)"),
        ("weight_source_credibility", "10", "Scoring weight for source credibility (0-100)"),
        ("weight_price", "10", "Scoring weight for price competitiveness (0-100)"),
        ("inbox_scan_interval_min", "30", "Minutes between inbox scan cycles"),
        ("email_mining_enabled", "false", "Enable email mining background job"),
        ("proactive_matching_enabled", "true", "Enable proactive offer matching"),
        ("activity_tracking_enabled", "true", "Enable CRM activity tracking"),
    ]
    for key, value, desc in seeds:
        _exec(
            conn,
            f"""INSERT INTO system_config (key, value, description)
            VALUES ('{key}', '{value}', '{desc}')
            ON CONFLICT (key) DO NOTHING""",
        )


def _seed_site_contacts(conn) -> None:
    """One-time: copy contact_name/email/phone/title from customer_sites into site_contacts."""
    try:
        row = conn.execute(sqltext("SELECT COUNT(*) FROM site_contacts")).scalar()
        if row and row > 0:
            return  # already seeded
        conn.execute(
            sqltext("""
            INSERT INTO site_contacts (customer_site_id, full_name, title, email, phone, is_primary)
            SELECT id, contact_name, contact_title, contact_email, contact_phone, TRUE
            FROM customer_sites
            WHERE contact_name IS NOT NULL AND contact_name != ''
        """)
        )
        conn.commit()
        log.info("Seeded site_contacts from existing customer_sites data")
    except Exception as e:
        log.warning("Seed site_contacts failed: %s", e)
        conn.rollback()


# ── One-time backfills ───────────────────────────────────────────────


def _backfill_normalized_mpn() -> None:
    """One-time backfill: populate requirements.normalized_mpn and re-normalize material_cards."""
    import re
    _nonalnum = re.compile(r"[^a-z0-9]")

    def _key(raw):
        if not raw:
            return ""
        return _nonalnum.sub("", str(raw).strip().lower())

    with engine.connect() as conn:
        # 1. Backfill requirements.normalized_mpn where NULL
        try:
            rows = conn.execute(
                sqltext("SELECT id, primary_mpn FROM requirements WHERE normalized_mpn IS NULL AND primary_mpn IS NOT NULL")
            ).fetchall()
            if rows:
                for r in rows:
                    nk = _key(r[1])
                    if nk:
                        conn.execute(
                            sqltext("UPDATE requirements SET normalized_mpn = :nk WHERE id = :id"),
                            {"nk": nk, "id": r[0]},
                        )
                conn.commit()
                log.info("Backfilled normalized_mpn on %d requirements", len(rows))
        except Exception as e:
            log.warning("Backfill requirements.normalized_mpn failed: %s", e)
            conn.rollback()

        # 2. Re-normalize material_cards.normalized_mpn (strip non-alnum, lowercase)
        try:
            cards = conn.execute(
                sqltext("SELECT id, normalized_mpn, display_mpn FROM material_cards")
            ).fetchall()
            updated = 0
            for c in cards:
                old_norm = c[1]
                new_norm = _key(c[2] or c[1])
                if new_norm and new_norm != old_norm:
                    existing = conn.execute(
                        sqltext("SELECT id FROM material_cards WHERE normalized_mpn = :n AND id != :id"),
                        {"n": new_norm, "id": c[0]},
                    ).fetchone()
                    if not existing:
                        conn.execute(
                            sqltext("UPDATE material_cards SET normalized_mpn = :n WHERE id = :id"),
                            {"n": new_norm, "id": c[0]},
                        )
                        updated += 1
            if updated:
                conn.commit()
                log.info("Re-normalized %d material_cards.normalized_mpn values", updated)
        except Exception as e:
            log.warning("Re-normalize material_cards failed: %s", e)
            conn.rollback()


# ── CHECK constraints (PostgreSQL NOT VALID) ─────────────────────────


def _add_check_constraints(conn) -> None:
    """Add CHECK constraints (NOT VALID) — only new inserts/updates are checked."""
    constraints = [
        # ── requirements ──
        ("requirements", "chk_req_target_qty", "target_qty IS NULL OR target_qty >= 1"),
        ("requirements", "chk_req_target_price", "target_price IS NULL OR target_price >= 0"),
        ("requirements", "chk_req_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
        ("requirements", "chk_req_packaging", "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')"),
        # ── sightings ──
        ("sightings", "chk_sight_qty", "qty_available IS NULL OR qty_available > 0"),
        ("sightings", "chk_sight_price", "unit_price IS NULL OR unit_price > 0"),
        ("sightings", "chk_sight_moq", "moq IS NULL OR moq > 0"),
        ("sightings", "chk_sight_confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
        ("sightings", "chk_sight_score", "score IS NULL OR score >= 0"),
        ("sightings", "chk_sight_lead_time", "lead_time_days IS NULL OR lead_time_days >= 0"),
        ("sightings", "chk_sight_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
        ("sightings", "chk_sight_packaging", "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')"),
        # ── offers ──
        ("offers", "chk_offer_qty", "qty_available IS NULL OR qty_available > 0"),
        ("offers", "chk_offer_price", "unit_price IS NULL OR unit_price > 0"),
        ("offers", "chk_offer_moq", "moq IS NULL OR moq > 0"),
        ("offers", "chk_offer_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
        ("offers", "chk_offer_packaging", "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')"),
        ("offers", "chk_offer_status", "status IN ('active','expired','won','lost','pending_review')"),
    ]
    for table, name, check in constraints:
        _exec(conn, f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID;
                END IF;
            END $$;
        """)
