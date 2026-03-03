"""
startup.py — Database Startup Migrations (Idempotent)

Tables, columns, and indexes are defined in the ORM models (models.py) and created
via Base.metadata.create_all(checkfirst=True). This file only handles PostgreSQL-
specific operations that can't be expressed in the ORM: triggers, seeds, backfills,
and CHECK constraints.

Called by: main.py lifespan
Depends on: database.py (engine), models.py (Base)
"""

from loguru import logger
from sqlalchemy import text as sqltext

from .database import SessionLocal, engine


def run_startup_migrations() -> None:
    """Execute all idempotent startup operations. Safe to call on every app boot."""
    import os

    if os.environ.get("TESTING"):
        logger.info("TESTING mode — skipping startup migrations")
        return

    # Create all tables/columns/indexes defined in ORM models (no-op if they exist)
    from .models import Base

    Base.metadata.create_all(bind=engine, checkfirst=True)
    logger.info("ORM schema sync complete (create_all checkfirst=True)")

    with engine.connect() as conn:
        _add_missing_columns(conn)
        _enable_pg_stat_statements(conn)
        _create_fts_triggers(conn)
        _backfill_fts(conn)
        _seed_system_config(conn)
        _seed_site_contacts(conn)
        _add_check_constraints(conn)
        _create_perf_indexes(conn)
        _create_count_triggers(conn)
        _backfill_company_counts(conn)
        _exec(conn, "UPDATE api_sources SET is_active = false WHERE status = 'disabled' AND is_active = true")
        _exec(
            conn,
            "UPDATE trouble_tickets SET resolved_at = COALESCE(diagnosed_at, created_at) + INTERVAL '1 hour' "
            "WHERE status = 'resolved' AND resolved_at IS NULL",
        )
        _analyze_hot_tables(conn)

    _backfill_normalized_mpn()
    if os.environ.get("TESTING") == "1" or os.environ.get("ENABLE_PASSWORD_LOGIN", "false").lower() == "true":
        _create_default_user_if_env_set()
    _backfill_sighting_offer_normalized_mpn()
    _backfill_sighting_vendor_normalized()
    _backfill_proactive_offer_qty()
    logger.info("Startup migrations complete")


def _create_default_user_if_env_set() -> None:
    """Create a default user from env vars if provided:
    DEFAULT_USER_EMAIL, DEFAULT_USER_PASSWORD, DEFAULT_USER_ROLE (optional).
    If the vars are missing, do nothing.
    """
    import base64
    import hashlib
    import os

    email = os.environ.get("DEFAULT_USER_EMAIL")
    password = os.environ.get("DEFAULT_USER_PASSWORD")
    role = os.environ.get("DEFAULT_USER_ROLE", "admin")
    if not email or not password:
        return

    # Avoid importing heavy ORM until we need it
    from .models.auth import User

    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email=email).first()
        if existing:
            logger.info("Default user %s already exists, skipping creation", email)
            return

        # PBKDF2-HMAC-SHA256 with random salt
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        store = base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()

        user = User(email=email.lower(), name=email.split("@")[0], role=role, password_hash=store)
        db.add(user)
        db.commit()
        logger.info("Created default user %s with role %s", email, role)
    except Exception:
        logger.exception("Failed creating default user")
    finally:
        db.close()


def _add_missing_columns(conn) -> None:
    """Add columns that exist in ORM models but not yet in the DB.

    create_all(checkfirst=True) only creates missing tables, not missing
    columns on existing tables.  This bridges the gap without a full Alembic
    migration.
    """
    stmts = [
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
        # Unified vendor score columns
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS vendor_score FLOAT",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS advancement_score FLOAT",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS is_new_vendor BOOLEAN DEFAULT TRUE",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS vendor_score_computed_at TIMESTAMP",
        # Add password_hash to users for optional password auth (encrypted at rest)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
        # Contact intelligence columns on vendor_contacts
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS first_name VARCHAR(100)",
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS last_name VARCHAR(100)",
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS phone_mobile VARCHAR(100)",
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS relationship_score FLOAT",
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS activity_trend VARCHAR(20)",
        "ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS score_computed_at TIMESTAMP",
        # Activity log additions
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS quote_id INTEGER",
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS auto_logged BOOLEAN DEFAULT FALSE",
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMP",
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS customer_site_id INTEGER REFERENCES customer_sites(id)",
        # Prospecting pool: site-level ownership columns
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP",
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS ownership_cleared_at TIMESTAMP",
        # Contact archive + note log columns
        "ALTER TABLE site_contacts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS site_contact_id INTEGER REFERENCES site_contacts(id)",
        # Customer AI material tags (mirrors vendor_cards pattern)
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS brand_tags JSON DEFAULT '[]'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS commodity_tags JSON DEFAULT '[]'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS material_tags_updated_at TIMESTAMP",
        # API health: active/planned classification
        "ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT FALSE",
        # Prospecting module: company record origin
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'manual'",
        # Proactive matching: CPH-enriched columns on proactive_matches
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS material_card_id INTEGER REFERENCES material_cards(id) ON DELETE SET NULL",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS match_score INTEGER DEFAULT 0",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS margin_pct FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_purchase_count INTEGER DEFAULT 0",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_last_price FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS customer_last_purchased_at TIMESTAMP",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS our_cost FLOAT",
        "ALTER TABLE proactive_matches ADD COLUMN IF NOT EXISTS dismiss_reason VARCHAR(255)",
        # Vendor name normalization on sightings
        "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS vendor_name_normalized VARCHAR(255)",
    ]
    for stmt in stmts:
        _exec(conn, stmt)

    # FK constraint migration: SET NULL on offers.vendor_card_id
    _exec(
        conn,
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name='offers_vendor_card_id_fkey' AND table_name='offers')
          THEN
            ALTER TABLE offers DROP CONSTRAINT offers_vendor_card_id_fkey;
            ALTER TABLE offers ADD CONSTRAINT offers_vendor_card_id_fkey
              FOREIGN KEY (vendor_card_id) REFERENCES vendor_cards(id) ON DELETE SET NULL;
          END IF;
        END $$
    """,
    )
    # FK constraint migration: SET NULL on offers.vendor_response_id
    _exec(
        conn,
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name='offers_vendor_response_id_fkey' AND table_name='offers')
          THEN
            ALTER TABLE offers DROP CONSTRAINT offers_vendor_response_id_fkey;
            ALTER TABLE offers ADD CONSTRAINT offers_vendor_response_id_fkey
              FOREIGN KEY (vendor_response_id) REFERENCES vendor_responses(id) ON DELETE SET NULL;
          END IF;
        END $$
    """,
    )

    # Backfill vendor_score from engagement_score as initial data
    _exec(
        conn,
        """
        UPDATE vendor_cards
        SET vendor_score = engagement_score,
            advancement_score = engagement_score,
            is_new_vendor = CASE WHEN engagement_score IS NULL THEN TRUE ELSE FALSE END
        WHERE vendor_score IS NULL AND engagement_score IS NOT NULL
    """,
    )

    # Backfill first_name/last_name from full_name
    _exec(
        conn,
        """
        UPDATE vendor_contacts
        SET first_name = SPLIT_PART(full_name, ' ', 1),
            last_name = CASE
                WHEN POSITION(' ' IN full_name) > 0
                THEN SUBSTRING(full_name FROM POSITION(' ' IN full_name) + 1)
                ELSE NULL
            END
        WHERE full_name IS NOT NULL AND first_name IS NULL
    """,
    )

    # Backfill occurred_at from created_at
    _exec(
        conn,
        """
        UPDATE activity_log SET occurred_at = created_at WHERE occurred_at IS NULL
    """,
    )

    # Backfill customer_sites.last_activity_at from parent company
    _exec(
        conn,
        """
        UPDATE customer_sites cs
        SET last_activity_at = c.last_activity_at
        FROM companies c
        WHERE cs.company_id = c.id
          AND cs.last_activity_at IS NULL
          AND c.last_activity_at IS NOT NULL
    """,
    )

    # Backfill site_contacts.is_active
    _exec(conn, "UPDATE site_contacts SET is_active = TRUE WHERE is_active IS NULL")

    # Backfill api_sources.is_active from status
    _exec(conn, "UPDATE api_sources SET is_active = TRUE WHERE status = 'live' AND is_active = FALSE")


def _exec(conn, stmt: str, params: dict | None = None) -> None:
    """Execute a single DDL statement with rollback on failure."""
    try:
        conn.execute(sqltext(stmt), params or {})
        conn.commit()
    except Exception as e:
        logger.warning("DDL failed: %s", e)
        conn.rollback()


# ── pg_stat_statements extension ─────────────────────────────────────


def _enable_pg_stat_statements(conn) -> None:
    """Enable pg_stat_statements extension for query performance monitoring."""
    _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


# ── Full-text search triggers (PostgreSQL-specific) ─────────────────


def _create_fts_triggers(conn) -> None:
    """Create trigger functions and triggers for FTS on vendor_cards and material_cards."""
    _exec(
        conn,
        """
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
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vc_fts') THEN
                CREATE TRIGGER trg_vc_fts BEFORE INSERT OR UPDATE ON vendor_cards
                FOR EACH ROW EXECUTE FUNCTION vendor_cards_fts_update();
            END IF;
        END $$;
    """,
    )

    _exec(
        conn,
        """
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
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mc_fts') THEN
                CREATE TRIGGER trg_mc_fts BEFORE INSERT OR UPDATE ON material_cards
                FOR EACH ROW EXECUTE FUNCTION material_cards_fts_update();
            END IF;
        END $$;
    """,
    )


# ── One-time FTS backfill ────────────────────────────────────────────


def _backfill_fts(conn) -> None:
    """Backfill search_vector on existing rows where NULL."""
    _exec(
        conn,
        """
        UPDATE vendor_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(domain, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(industry, '')), 'C')
        WHERE search_vector IS NULL
    """,
    )

    _exec(
        conn,
        """
        UPDATE material_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(manufacturer, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'C')
        WHERE search_vector IS NULL
    """,
    )


# ── Seed data ────────────────────────────────────────────────────────


def _seed_system_config(conn) -> None:
    """Seed default feature flags (INSERT ON CONFLICT DO NOTHING)."""
    seeds = [
        ("inbox_scan_interval_min", "30", "Minutes between inbox scan cycles"),
        ("email_mining_enabled", "false", "Enable email mining background job"),
        ("proactive_matching_enabled", "true", "Enable proactive offer matching"),
        ("activity_tracking_enabled", "true", "Enable CRM activity tracking"),
    ]
    for key, value, desc in seeds:
        _exec(
            conn,
            """INSERT INTO system_config (key, value, description)
            VALUES (:key, :value, :desc)
            ON CONFLICT (key) DO NOTHING""",
            {"key": key, "value": value, "desc": desc},
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
        logger.info("Seeded site_contacts from existing customer_sites data")
    except Exception as e:
        logger.warning("Seed site_contacts failed: %s", e)
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
                sqltext(
                    "SELECT id, primary_mpn FROM requirements WHERE normalized_mpn IS NULL AND primary_mpn IS NOT NULL"
                )
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
                logger.info("Backfilled normalized_mpn on %d requirements", len(rows))
        except Exception as e:
            logger.warning("Backfill requirements.normalized_mpn failed: %s", e)
            conn.rollback()

        # 2. Backfill material_cards.normalized_mpn where NULL only (skip full re-scan)
        try:
            cards = conn.execute(
                sqltext(
                    "SELECT id, display_mpn FROM material_cards WHERE normalized_mpn IS NULL AND display_mpn IS NOT NULL"
                )
            ).fetchall()
            updated = 0
            for c in cards:
                new_norm = _key(c[1])
                if new_norm:
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
                logger.info("Backfilled normalized_mpn on %d material_cards", updated)
        except Exception as e:
            logger.warning("Backfill material_cards.normalized_mpn failed: %s", e)
            conn.rollback()


# ── CHECK constraints (PostgreSQL NOT VALID) ─────────────────────────


def _add_check_constraints(conn) -> None:
    """Add CHECK constraints (NOT VALID) — only new inserts/updates are checked."""
    constraints = [
        # ── requirements ──
        ("requirements", "chk_req_target_qty", "target_qty IS NULL OR target_qty >= 1"),
        ("requirements", "chk_req_target_price", "target_price IS NULL OR target_price >= 0"),
        ("requirements", "chk_req_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
        (
            "requirements",
            "chk_req_packaging",
            "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')",
        ),
        # ── sightings ──
        ("sightings", "chk_sight_qty", "qty_available IS NULL OR qty_available > 0"),
        ("sightings", "chk_sight_price", "unit_price IS NULL OR unit_price > 0"),
        ("sightings", "chk_sight_moq", "moq IS NULL OR moq > 0"),
        ("sightings", "chk_sight_confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
        ("sightings", "chk_sight_score", "score IS NULL OR score >= 0"),
        ("sightings", "chk_sight_lead_time", "lead_time_days IS NULL OR lead_time_days >= 0"),
        ("sightings", "chk_sight_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
        (
            "sightings",
            "chk_sight_packaging",
            "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
        ),
        # ── offers ──
        ("offers", "chk_offer_qty", "qty_available IS NULL OR qty_available > 0"),
        ("offers", "chk_offer_price", "unit_price IS NULL OR unit_price > 0"),
        ("offers", "chk_offer_moq", "moq IS NULL OR moq > 0"),
        ("offers", "chk_offer_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
        (
            "offers",
            "chk_offer_packaging",
            "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
        ),
        ("offers", "chk_offer_status", "status IN ('active','expired','won','lost','pending_review','rejected')"),
    ]
    # NOTE: table/constraint names are hardcoded literals above — not user input.
    # DDL identifiers cannot use bind params. This is safe as-is.
    for table, name, check in constraints:
        _exec(
            conn,
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID;
                END IF;
            END $$;
        """,
        )


# ── Performance indexes (idempotent) ─────────────────────────────────


def _create_perf_indexes(conn) -> None:
    """Create functional/composite indexes for hot query paths."""
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_sightings_vendor_norm
        ON sightings (vendor_name_normalized)
    """,
    )
    # Drop legacy expression index — vendor_name_normalized index handles all queries now
    _exec(conn, "DROP INDEX IF EXISTS ix_sightings_vendor_lower")
    # pg_trgm for fast ILIKE search on requisitions + requirements
    _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_requisitions_name_trgm
        ON requisitions USING gin (name gin_trgm_ops)
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_requisitions_customer_name_trgm
        ON requisitions USING gin (customer_name gin_trgm_ops)
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_requirements_mpn_trgm
        ON requirements USING gin (primary_mpn gin_trgm_ops)
    """,
    )
    # Generated column + trigram index for substitutes search
    _exec(
        conn,
        """
        ALTER TABLE requirements
        ADD COLUMN IF NOT EXISTS substitutes_text TEXT
        GENERATED ALWAYS AS (substitutes::text) STORED
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_requirements_subs_trgm
        ON requirements USING gin (substitutes_text gin_trgm_ops)
    """,
    )
    # Phase 1: Additional performance indexes for hot query paths
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_vendor_cards_blacklisted
        ON vendor_cards (is_blacklisted) WHERE is_blacklisted = TRUE
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_requirements_primary_mpn
        ON requirements (LOWER(primary_mpn))
    """,
    )
    # ix_sightings_mpn_matched removed — 0 scans, not worth the write overhead
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_offers_vendor_card
        ON offers (vendor_card_id) WHERE vendor_card_id IS NOT NULL
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_contacts_vendor_name
        ON contacts (vendor_name) WHERE vendor_name IS NOT NULL
    """,
    )
    _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_vendor_responses_vendor_name
        ON vendor_responses (vendor_name) WHERE vendor_name IS NOT NULL
    """,
    )
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_offers_vendor_name ON offers (vendor_name)")
    # ix_vendor_cards_created_at removed — 0 scans
    _exec(
        conn, "CREATE INDEX IF NOT EXISTS ix_vendor_cards_score_computed_at ON vendor_cards (vendor_score_computed_at)"
    )

    # GIN FTS indexes removed — 0 scans, 146 MB combined. Re-add if FTS search is implemented.

    # ix_vendor_contacts_phone removed — 0 scans

    # FK indexes — prevent slow JOINs / cascade deletes
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_activity_log_customer_site_id ON activity_log (customer_site_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_activity_log_site_contact_id ON activity_log (site_contact_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_requisitions_updated_by_id ON requisitions (updated_by_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_offers_approved_by_id ON offers (approved_by_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_offers_updated_by_id ON offers (updated_by_id)")

    # Proactive matching indexes
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_pm_material_card ON proactive_matches (material_card_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_pm_score ON proactive_matches (match_score)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS ix_pm_status_sales ON proactive_matches (status, salesperson_id)")

    # Phase 3: soft-delete column for material cards
    _exec(conn, "ALTER TABLE material_cards ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP")


def _backfill_sighting_offer_normalized_mpn() -> None:
    """One-time backfill: populate sightings.normalized_mpn and offers.normalized_mpn."""
    import re

    _nonalnum = re.compile(r"[^a-z0-9]")

    def _key(raw):
        if not raw:
            return ""
        return _nonalnum.sub("", str(raw).strip().lower())

    with engine.connect() as conn:
        # Sightings: compute from mpn_matched
        try:
            rows = conn.execute(
                sqltext(
                    "SELECT id, mpn_matched FROM sightings WHERE normalized_mpn IS NULL AND mpn_matched IS NOT NULL"
                )
            ).fetchall()
            if rows:
                for r in rows:
                    nk = _key(r[1])
                    if nk:
                        conn.execute(
                            sqltext("UPDATE sightings SET normalized_mpn = :nk WHERE id = :id"),
                            {"nk": nk, "id": r[0]},
                        )
                conn.commit()
                logger.info("Backfilled normalized_mpn on %d sightings", len(rows))
        except Exception as e:
            logger.warning("Backfill sightings.normalized_mpn failed: %s", e)
            conn.rollback()

        # Offers: compute from mpn
        try:
            rows = conn.execute(
                sqltext("SELECT id, mpn FROM offers WHERE normalized_mpn IS NULL AND mpn IS NOT NULL")
            ).fetchall()
            if rows:
                for r in rows:
                    nk = _key(r[1])
                    if nk:
                        conn.execute(
                            sqltext("UPDATE offers SET normalized_mpn = :nk WHERE id = :id"),
                            {"nk": nk, "id": r[0]},
                        )
                conn.commit()
                logger.info("Backfilled normalized_mpn on %d offers", len(rows))
        except Exception as e:
            logger.warning("Backfill offers.normalized_mpn failed: %s", e)
            conn.rollback()


def _backfill_sighting_vendor_normalized() -> None:
    """Backfill sightings.vendor_name_normalized from vendor_name until none remain."""
    from .vendor_utils import normalize_vendor_name

    with engine.connect() as conn:
        # Check column exists first
        try:
            conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings LIMIT 0"))
        except Exception:
            conn.rollback()
            return  # Column not yet created

        total = 0
        while True:
            try:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, vendor_name FROM sightings "
                        "WHERE vendor_name_normalized IS NULL AND vendor_name IS NOT NULL "
                        "LIMIT 10000"
                    )
                ).fetchall()
                if not rows:
                    break
                batch = []
                for r in rows:
                    nv = normalize_vendor_name(r[1])
                    if nv:
                        batch.append({"nv": nv, "id": r[0]})
                if batch:
                    for b in batch:
                        conn.execute(
                            sqltext("UPDATE sightings SET vendor_name_normalized = :nv WHERE id = :id"),
                            b,
                        )
                    conn.commit()
                total += len(batch)
            except Exception as e:
                logger.warning("Backfill sightings.vendor_name_normalized failed: %s", e)
                conn.rollback()
                break
        if total:
            logger.info("Backfilled vendor_name_normalized on %d sightings", total)


# ── Denormalized company count triggers ──────────────────────────────


def _create_count_triggers(conn) -> None:
    """Create triggers to keep companies.site_count and open_req_count in sync."""
    # Trigger function: update site_count when customer_sites change
    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION trg_update_company_site_count() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                UPDATE companies SET site_count = (
                    SELECT COUNT(*) FROM customer_sites
                    WHERE company_id = OLD.company_id AND is_active = TRUE
                ) WHERE id = OLD.company_id;
            END IF;
            IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
                UPDATE companies SET site_count = (
                    SELECT COUNT(*) FROM customer_sites
                    WHERE company_id = NEW.company_id AND is_active = TRUE
                ) WHERE id = NEW.company_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cs_site_count') THEN
                CREATE TRIGGER trg_cs_site_count AFTER INSERT OR UPDATE OR DELETE ON customer_sites
                FOR EACH ROW EXECUTE FUNCTION trg_update_company_site_count();
            END IF;
        END $$;
    """,
    )

    # Trigger function: update open_req_count when requisitions change
    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION trg_update_company_req_count() RETURNS trigger AS $$
        DECLARE
            v_company_id INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                SELECT cs.company_id INTO v_company_id
                FROM customer_sites cs WHERE cs.id = OLD.customer_site_id;
                IF v_company_id IS NOT NULL THEN
                    UPDATE companies SET open_req_count = (
                        SELECT COUNT(*) FROM requisitions r
                        JOIN customer_sites cs2 ON r.customer_site_id = cs2.id
                        WHERE cs2.company_id = v_company_id
                          AND r.status NOT IN ('archived', 'won', 'lost')
                    ) WHERE id = v_company_id;
                END IF;
            END IF;
            IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
                SELECT cs.company_id INTO v_company_id
                FROM customer_sites cs WHERE cs.id = NEW.customer_site_id;
                IF v_company_id IS NOT NULL THEN
                    UPDATE companies SET open_req_count = (
                        SELECT COUNT(*) FROM requisitions r
                        JOIN customer_sites cs2 ON r.customer_site_id = cs2.id
                        WHERE cs2.company_id = v_company_id
                          AND r.status NOT IN ('archived', 'won', 'lost')
                    ) WHERE id = v_company_id;
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_req_count') THEN
                CREATE TRIGGER trg_req_count AFTER INSERT OR UPDATE OR DELETE ON requisitions
                FOR EACH ROW EXECUTE FUNCTION trg_update_company_req_count();
            END IF;
        END $$;
    """,
    )


def _backfill_company_counts(conn) -> None:
    """Idempotent backfill of site_count and open_req_count on companies."""
    _exec(
        conn,
        """
        UPDATE companies c SET site_count = (
            SELECT COUNT(*) FROM customer_sites cs
            WHERE cs.company_id = c.id AND cs.is_active = TRUE
        )
    """,
    )
    _exec(
        conn,
        """
        UPDATE companies c SET open_req_count = (
            SELECT COUNT(*) FROM requisitions r
            JOIN customer_sites cs ON r.customer_site_id = cs.id
            WHERE cs.company_id = c.id
              AND r.status NOT IN ('archived', 'won', 'lost')
        )
    """,
    )


def _analyze_hot_tables(conn) -> None:
    """Run ANALYZE on hot tables to keep pg_stat estimates fresh."""
    for tbl in ("companies", "customer_sites", "requisitions"):
        _exec(conn, "ANALYZE " + tbl)


def _backfill_proactive_offer_qty() -> None:
    """Fix proactive offer totals: use customer target_qty instead of vendor qty_available.

    Bug: send_proactive_offer() was using offer.qty_available (vendor's entire stock)
    to calculate total_sell/total_cost. The correct qty is min(qty_available, target_qty).
    This backfill recalculates totals and fixes line_items[].qty for all affected offers.

    Called by: run_startup_migrations
    Depends on: proactive_offers, proactive_matches, requirements tables
    """
    import json
    from decimal import Decimal

    with engine.connect() as conn:
        try:
            # Pre-load match_id -> target_qty map
            match_rows = conn.execute(
                sqltext("""
                    SELECT pm.id, r.target_qty
                    FROM proactive_matches pm
                    JOIN requirements r ON r.id = pm.requirement_id
                    WHERE r.target_qty IS NOT NULL AND r.target_qty > 0
                """)
            ).fetchall()
            target_map = {r[0]: r[1] for r in match_rows}

            if not target_map:
                return

            offers = conn.execute(
                sqltext("SELECT id, line_items FROM proactive_offers WHERE line_items IS NOT NULL")
            ).fetchall()

            fixed = 0
            for offer_id, raw_items in offers:
                if not raw_items:
                    continue
                items = raw_items if isinstance(raw_items, list) else json.loads(raw_items)

                new_items = []
                total_sell = Decimal("0")
                total_cost = Decimal("0")
                changed = False

                for item in items:
                    match_id = item.get("match_id")
                    old_qty = item.get("qty", 0)
                    target_qty = target_map.get(match_id)

                    new_qty = min(old_qty, target_qty) if target_qty else old_qty
                    if new_qty != old_qty:
                        changed = True

                    item["qty"] = new_qty
                    new_items.append(item)

                    sell_price = float(item.get("sell_price") or item.get("unit_price", 0))
                    cost_price = float(item.get("unit_price", 0))
                    total_sell += Decimal(str(sell_price)) * new_qty
                    total_cost += Decimal(str(cost_price)) * new_qty

                if changed:
                    conn.execute(
                        sqltext(
                            "UPDATE proactive_offers "
                            "SET line_items = :items, total_sell = :sell, total_cost = :cost "
                            "WHERE id = :id"
                        ),
                        {
                            "items": json.dumps(new_items),
                            "sell": float(total_sell),
                            "cost": float(total_cost),
                            "id": offer_id,
                        },
                    )
                    fixed += 1

            if fixed:
                conn.commit()
                logger.info("Fixed proactive offer quantities on %d offers", fixed)
        except Exception as e:
            logger.warning("Backfill proactive offer qty failed: %s", e)
            conn.rollback()
