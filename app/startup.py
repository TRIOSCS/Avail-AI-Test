"""
startup.py — Database Startup Migrations (Idempotent)

All inline DDL statements that were previously in main.py at module load time.
These run once at app startup to ensure the schema is up to date. Every statement
is idempotent (IF NOT EXISTS / IF NOT EXISTS) so re-running is safe.

Business Rules:
- All statements must be idempotent — safe to run on every startup.
- Failures on individual statements are caught and logged, never crash the app.
- This file replaces 51 inline DDL statements that lived in main.py lines 28-224.

Called by: main.py (at module load)
Depends on: database.py (engine)
"""

import logging
from sqlalchemy import text as sqltext
from .database import engine

log = logging.getLogger(__name__)


def run_startup_migrations() -> None:
    """Execute all idempotent DDL statements. Safe to call on every app boot."""
    import os

    if os.environ.get("TESTING"):
        log.info("TESTING mode — skipping startup migrations")
        return
    with engine.connect() as conn:
        _add_columns(conn)
        _create_indexes(conn)
        _create_crm_tables(conn)
        _add_crm_columns(conn)
        _create_crm_indexes(conn)
        _create_proactive_tables(conn)
        _create_performance_tables(conn)
        _create_admin_settings_tables(conn)
    log.info("Startup migrations complete")


def _exec(conn, stmt: str) -> None:
    """Execute a single DDL statement with rollback on failure."""
    try:
        conn.execute(sqltext(stmt))
        conn.commit()
    except Exception:
        conn.rollback()


# ── Column additions on existing tables ──────────────────────────────


def _add_columns(conn) -> None:
    stmts = [
        "ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS last_searched_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS refresh_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_email_scan TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_signature TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_inbox_scan TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_contacts_sync TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS m365_connected BOOLEAN DEFAULT FALSE",
        "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS is_unavailable BOOLEAN DEFAULT FALSE",
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS message_id VARCHAR(255)",
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS graph_conversation_id VARCHAR(500)",
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS scanned_by_user_id INTEGER",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS graph_message_id VARCHAR(500)",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS graph_conversation_id VARCHAR(500)",
        # v1.2.0 — Roles
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'buyer'",
        # v1.2.0 — M365 health
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS m365_error_reason VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS m365_last_healthy TIMESTAMP",
        # v1.2.0 — Contact Enrichment
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS domain_aliases JSON DEFAULT '[]'",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS last_enriched_at TIMESTAMP",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS enrichment_source VARCHAR(50)",
        # v1.2.0 — Email Intelligence: Contact status tracking
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'sent'",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMP",
        # v1.2.0 — Email Intelligence: Reply classification
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS classification VARCHAR(50)",
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS needs_action BOOLEAN DEFAULT false",
        "ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS action_hint VARCHAR(255)",
        # AI material intelligence
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS brand_tags JSON DEFAULT '[]'",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS commodity_tags JSON DEFAULT '[]'",
        "ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS material_tags_updated_at TIMESTAMP",
        # New offers indicator
        "ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS offers_viewed_at TIMESTAMP",
        # v1.6.x — Vendor contact activity tracking
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS vendor_contact_id INTEGER REFERENCES vendor_contacts(id)",
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS notes TEXT",
        # v1.7.x — Scope activities to requisition
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS requisition_id INTEGER REFERENCES requisitions(id)",
    ]
    for stmt in stmts:
        _exec(conn, stmt)


# ── Indexes on existing tables ───────────────────────────────────────


def _create_indexes(conn) -> None:
    stmts = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_vr_message_id ON vendor_responses(message_id)",
        "CREATE INDEX IF NOT EXISTS ix_vr_conv_id ON vendor_responses(graph_conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_contact_conv_id ON contacts(graph_conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_sight_vendor ON sightings(vendor_name)",
        # v1.2.0 indexes
        "CREATE INDEX IF NOT EXISTS ix_contact_status ON contacts(status)",
        "CREATE INDEX IF NOT EXISTS ix_contact_user_status ON contacts(user_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_vr_classification ON vendor_responses(classification)",
        "CREATE INDEX IF NOT EXISTS ix_vc_domain ON vendor_cards(domain)",
        "CREATE INDEX IF NOT EXISTS ix_activity_vendor_contact ON activity_log(vendor_contact_id, created_at) WHERE vendor_contact_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_activity_requisition ON activity_log(requisition_id, vendor_card_id, created_at) WHERE requisition_id IS NOT NULL",
    ]
    for stmt in stmts:
        _exec(conn, stmt)


# ── CRM tables (v1.2.0) ─────────────────────────────────────────────


def _create_crm_tables(conn) -> None:
    crm_tables = [
        """CREATE TABLE IF NOT EXISTS companies (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            website VARCHAR(500),
            industry VARCHAR(255),
            notes TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS customer_sites (
            id SERIAL PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            site_name VARCHAR(255) NOT NULL,
            owner_id INTEGER REFERENCES users(id),
            contact_name VARCHAR(255),
            contact_email VARCHAR(255),
            contact_phone VARCHAR(100),
            contact_title VARCHAR(255),
            address_line1 VARCHAR(500),
            address_line2 VARCHAR(255),
            city VARCHAR(255),
            state VARCHAR(100),
            zip VARCHAR(20),
            country VARCHAR(100) DEFAULT 'US',
            payment_terms VARCHAR(100),
            shipping_terms VARCHAR(100),
            notes TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS offers (
            id SERIAL PRIMARY KEY,
            requisition_id INTEGER NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
            requirement_id INTEGER REFERENCES requirements(id),
            vendor_card_id INTEGER REFERENCES vendor_cards(id),
            vendor_name VARCHAR(255) NOT NULL,
            mpn VARCHAR(255) NOT NULL,
            manufacturer VARCHAR(255),
            qty_available INTEGER,
            unit_price NUMERIC(12,4),
            currency VARCHAR(10) DEFAULT 'USD',
            lead_time VARCHAR(100),
            date_code VARCHAR(100),
            condition VARCHAR(50),
            packaging VARCHAR(100),
            moq INTEGER,
            valid_until DATE,
            source VARCHAR(50) DEFAULT 'manual',
            vendor_response_id INTEGER REFERENCES vendor_responses(id),
            entered_by_id INTEGER REFERENCES users(id),
            notes TEXT,
            status VARCHAR(20) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS quotes (
            id SERIAL PRIMARY KEY,
            requisition_id INTEGER NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
            customer_site_id INTEGER NOT NULL REFERENCES customer_sites(id),
            quote_number VARCHAR(50) NOT NULL UNIQUE,
            revision INTEGER DEFAULT 1,
            line_items JSON NOT NULL DEFAULT '[]',
            subtotal NUMERIC(12,2),
            total_cost NUMERIC(12,2),
            total_margin_pct NUMERIC(5,2),
            payment_terms VARCHAR(100),
            shipping_terms VARCHAR(100),
            validity_days INTEGER DEFAULT 7,
            notes TEXT,
            status VARCHAR(20) DEFAULT 'draft',
            sent_at TIMESTAMP,
            result VARCHAR(20),
            result_reason VARCHAR(255),
            result_notes TEXT,
            result_at TIMESTAMP,
            won_revenue NUMERIC(12,2),
            created_by_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS site_contacts (
            id SERIAL PRIMARY KEY,
            customer_site_id INTEGER NOT NULL REFERENCES customer_sites(id) ON DELETE CASCADE,
            full_name VARCHAR(255) NOT NULL,
            title VARCHAR(255),
            email VARCHAR(255),
            phone VARCHAR(100),
            notes TEXT,
            is_primary BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS offer_attachments (
            id SERIAL PRIMARY KEY,
            offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            file_name VARCHAR(500) NOT NULL,
            onedrive_item_id VARCHAR(500),
            onedrive_url TEXT,
            thumbnail_url TEXT,
            content_type VARCHAR(100),
            size_bytes INTEGER,
            uploaded_by_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS buy_plans (
            id SERIAL PRIMARY KEY,
            requisition_id INTEGER NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
            quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            status VARCHAR(30) DEFAULT 'pending_approval',
            line_items JSON NOT NULL DEFAULT '[]',
            manager_notes TEXT,
            rejection_reason TEXT,
            submitted_by_id INTEGER REFERENCES users(id),
            approved_by_id INTEGER REFERENCES users(id),
            submitted_at TIMESTAMP DEFAULT NOW(),
            approved_at TIMESTAMP,
            rejected_at TIMESTAMP,
            approval_token VARCHAR(100) UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
    ]
    for stmt in crm_tables:
        _exec(conn, stmt)
    # One-time seed: copy existing inline contacts from customer_sites into site_contacts
    _seed_site_contacts(conn)


def _seed_site_contacts(conn) -> None:
    """Idempotent: copy contact_name/email/phone/title from customer_sites into site_contacts."""
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
    except Exception:
        conn.rollback()


# ── CRM column additions ────────────────────────────────────────────


def _add_crm_columns(conn) -> None:
    stmts = [
        "ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS customer_site_id INTEGER REFERENCES customer_sites(id)",
        "ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS cloned_from_id INTEGER REFERENCES requisitions(id)",
        "ALTER TABLE requirements ADD COLUMN IF NOT EXISTS target_price NUMERIC(12,4)",
        # v1.4.0: Company account management fields
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS account_type VARCHAR(50)",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS phone VARCHAR(100)",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS credit_terms VARCHAR(100)",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS tax_id VARCHAR(100)",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'USD'",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS preferred_carrier VARCHAR(100)",
        # v1.4.0: Site operations fields
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS site_type VARCHAR(50)",
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS timezone VARCHAR(50)",
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS receiving_hours VARCHAR(100)",
        "ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS carrier_account VARCHAR(100)",
        # v1.4.1: Buy plan remediation
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS sales_order_number VARCHAR(100)",
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS salesperson_notes TEXT",
        # v1.4.2: Buy plan completion, cancellation, audit
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS completed_by_id INTEGER REFERENCES users(id)",
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP",
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS cancelled_by_id INTEGER REFERENCES users(id)",
        "ALTER TABLE buy_plans ADD COLUMN IF NOT EXISTS cancellation_reason TEXT",
    ]
    for stmt in stmts:
        _exec(conn, stmt)


# ── CRM indexes ──────────────────────────────────────────────────────


def _create_crm_indexes(conn) -> None:
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_companies_name ON companies(name)",
        "CREATE INDEX IF NOT EXISTS ix_cs_company ON customer_sites(company_id)",
        "CREATE INDEX IF NOT EXISTS ix_cs_owner ON customer_sites(owner_id)",
        "CREATE INDEX IF NOT EXISTS ix_offers_req ON offers(requisition_id)",
        "CREATE INDEX IF NOT EXISTS ix_offers_requirement ON offers(requirement_id)",
        "CREATE INDEX IF NOT EXISTS ix_offers_vendor ON offers(vendor_card_id)",
        "CREATE INDEX IF NOT EXISTS ix_offers_mpn ON offers(mpn)",
        "CREATE INDEX IF NOT EXISTS ix_quotes_req ON quotes(requisition_id)",
        "CREATE INDEX IF NOT EXISTS ix_quotes_site ON quotes(customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_quotes_status ON quotes(status)",
        "CREATE INDEX IF NOT EXISTS ix_site_contacts_site ON site_contacts(customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_site_contacts_email ON site_contacts(email)",
        "CREATE INDEX IF NOT EXISTS ix_offer_attachments_offer ON offer_attachments(offer_id)",
        "CREATE INDEX IF NOT EXISTS ix_buyplans_req ON buy_plans(requisition_id)",
        "CREATE INDEX IF NOT EXISTS ix_buyplans_quote ON buy_plans(quote_id)",
        "CREATE INDEX IF NOT EXISTS ix_buyplans_status ON buy_plans(status)",
        "CREATE INDEX IF NOT EXISTS ix_buyplans_token ON buy_plans(approval_token)",
    ]
    for stmt in stmts:
        _exec(conn, stmt)


def _create_proactive_tables(conn) -> None:
    """Proactive offers — matching, sending, throttle."""
    tables = [
        """CREATE TABLE IF NOT EXISTS proactive_matches (
            id SERIAL PRIMARY KEY,
            offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            requirement_id INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
            requisition_id INTEGER NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
            customer_site_id INTEGER NOT NULL REFERENCES customer_sites(id),
            salesperson_id INTEGER NOT NULL REFERENCES users(id),
            mpn VARCHAR(255) NOT NULL,
            status VARCHAR(20) DEFAULT 'new',
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS proactive_offers (
            id SERIAL PRIMARY KEY,
            customer_site_id INTEGER NOT NULL REFERENCES customer_sites(id),
            salesperson_id INTEGER NOT NULL REFERENCES users(id),
            line_items JSON NOT NULL DEFAULT '[]',
            recipient_contact_ids JSON DEFAULT '[]',
            recipient_emails JSON DEFAULT '[]',
            subject VARCHAR(500),
            email_body_html TEXT,
            graph_message_id VARCHAR(500),
            status VARCHAR(20) DEFAULT 'sent',
            sent_at TIMESTAMP DEFAULT NOW(),
            converted_requisition_id INTEGER REFERENCES requisitions(id),
            converted_quote_id INTEGER REFERENCES quotes(id),
            converted_at TIMESTAMP,
            total_sell NUMERIC(12,2),
            total_cost NUMERIC(12,2),
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS proactive_throttle (
            id SERIAL PRIMARY KEY,
            mpn VARCHAR(255) NOT NULL,
            customer_site_id INTEGER NOT NULL REFERENCES customer_sites(id) ON DELETE CASCADE,
            last_offered_at TIMESTAMP NOT NULL,
            proactive_offer_id INTEGER REFERENCES proactive_offers(id)
        )""",
    ]
    for stmt in tables:
        _exec(conn, stmt)
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_pm_offer ON proactive_matches(offer_id)",
        "CREATE INDEX IF NOT EXISTS ix_pm_req ON proactive_matches(requisition_id)",
        "CREATE INDEX IF NOT EXISTS ix_pm_site ON proactive_matches(customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_pm_sales ON proactive_matches(salesperson_id)",
        "CREATE INDEX IF NOT EXISTS ix_pm_status ON proactive_matches(status)",
        "CREATE INDEX IF NOT EXISTS ix_pm_mpn_site ON proactive_matches(mpn, customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_poff_site ON proactive_offers(customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_poff_sales ON proactive_offers(salesperson_id)",
        "CREATE INDEX IF NOT EXISTS ix_poff_status ON proactive_offers(status)",
        "CREATE INDEX IF NOT EXISTS ix_poff_sent ON proactive_offers(sent_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_pt_mpn_site ON proactive_throttle(mpn, customer_site_id)",
        "CREATE INDEX IF NOT EXISTS ix_pt_last_offered ON proactive_throttle(last_offered_at)",
    ]
    for stmt in indexes:
        _exec(conn, stmt)
    conn.commit()


def _create_performance_tables(conn) -> None:
    """Performance tracking — vendor scorecards, buyer leaderboard, stock list dedup."""
    tables = [
        """CREATE TABLE IF NOT EXISTS vendor_metrics_snapshot (
            id SERIAL PRIMARY KEY,
            vendor_card_id INTEGER NOT NULL REFERENCES vendor_cards(id) ON DELETE CASCADE,
            snapshot_date DATE NOT NULL,
            response_rate FLOAT,
            quote_accuracy FLOAT,
            on_time_delivery FLOAT,
            cancellation_rate FLOAT,
            rma_rate FLOAT,
            lead_time_accuracy FLOAT,
            quote_conversion FLOAT,
            po_conversion FLOAT,
            avg_review_rating FLOAT,
            composite_score FLOAT,
            interaction_count INTEGER DEFAULT 0,
            is_sufficient_data BOOLEAN DEFAULT FALSE,
            rfqs_sent INTEGER DEFAULT 0,
            rfqs_answered INTEGER DEFAULT 0,
            pos_in_window INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS buyer_leaderboard_snapshot (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month DATE NOT NULL,
            offers_logged INTEGER DEFAULT 0,
            offers_quoted INTEGER DEFAULT 0,
            offers_in_buyplan INTEGER DEFAULT 0,
            offers_po_confirmed INTEGER DEFAULT 0,
            points_offers INTEGER DEFAULT 0,
            points_quoted INTEGER DEFAULT 0,
            points_buyplan INTEGER DEFAULT 0,
            points_po INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0,
            rank INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS stock_list_hashes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            content_hash VARCHAR(64) NOT NULL,
            vendor_card_id INTEGER REFERENCES vendor_cards(id),
            file_name VARCHAR(500),
            row_count INTEGER,
            first_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
            upload_count INTEGER DEFAULT 1
        )""",
    ]
    for stmt in tables:
        _exec(conn, stmt)
    # Add new columns to existing tables
    for col_stmt in [
        "ALTER TABLE vendor_metrics_snapshot ADD COLUMN IF NOT EXISTS quote_conversion FLOAT",
        "ALTER TABLE vendor_metrics_snapshot ADD COLUMN IF NOT EXISTS po_conversion FLOAT",
        "ALTER TABLE vendor_metrics_snapshot ADD COLUMN IF NOT EXISTS avg_review_rating FLOAT",
        # v1.5.2 — Stock list credit in buyer scorecard
        "ALTER TABLE buyer_leaderboard_snapshot ADD COLUMN IF NOT EXISTS stock_lists_uploaded INTEGER DEFAULT 0",
        "ALTER TABLE buyer_leaderboard_snapshot ADD COLUMN IF NOT EXISTS points_stock INTEGER DEFAULT 0",
    ]:
        _exec(conn, col_stmt)
    indexes = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_vms_vendor_date ON vendor_metrics_snapshot(vendor_card_id, snapshot_date)",
        "CREATE INDEX IF NOT EXISTS ix_vms_date ON vendor_metrics_snapshot(snapshot_date)",
        "CREATE INDEX IF NOT EXISTS ix_vms_composite ON vendor_metrics_snapshot(composite_score)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_bls_user_month ON buyer_leaderboard_snapshot(user_id, month)",
        "CREATE INDEX IF NOT EXISTS ix_bls_month_rank ON buyer_leaderboard_snapshot(month, rank)",
        "CREATE INDEX IF NOT EXISTS ix_bls_month_points ON buyer_leaderboard_snapshot(month, total_points)",
        "CREATE INDEX IF NOT EXISTS ix_slh_hash ON stock_list_hashes(content_hash)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_slh_user_hash ON stock_list_hashes(user_id, content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_slh_vendor ON stock_list_hashes(vendor_card_id)",
    ]
    for stmt in indexes:
        _exec(conn, stmt)
    conn.commit()


def _create_admin_settings_tables(conn) -> None:
    """Admin settings — system_config table, user.is_active column."""
    # Add is_active column to users
    _exec(
        conn,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    )
    # Create system_config table
    _exec(
        conn,
        """CREATE TABLE IF NOT EXISTS system_config (
        id SERIAL PRIMARY KEY,
        key VARCHAR(100) NOT NULL UNIQUE,
        value TEXT NOT NULL,
        description VARCHAR(500),
        updated_by VARCHAR(255),
        updated_at TIMESTAMP DEFAULT NOW()
    )""",
    )
    _exec(
        conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_sysconfig_key ON system_config(key)"
    )
    # Add credentials column to api_sources
    _exec(
        conn,
        "ALTER TABLE api_sources ADD COLUMN IF NOT EXISTS credentials JSONB DEFAULT '{}'::jsonb",
    )
    # Create pending_batches table for Anthropic Batch API tracking
    _exec(
        conn,
        """CREATE TABLE IF NOT EXISTS pending_batches (
        id SERIAL PRIMARY KEY,
        batch_id VARCHAR(255) NOT NULL,
        batch_type VARCHAR(50) DEFAULT 'inbox_parse',
        request_map JSONB,
        status VARCHAR(20) DEFAULT 'processing',
        submitted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        completed_at TIMESTAMP WITH TIME ZONE,
        result_count INTEGER,
        error_message TEXT
    )""",
    )
    _exec(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_pending_batches_batch_id ON pending_batches(batch_id)",
    )
    _exec(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_pending_batches_status ON pending_batches(status)",
    )
    # Seed default scoring weights (idempotent — INSERT ON CONFLICT DO NOTHING)
    seeds = [
        ("weight_recency", "30", "Scoring weight for data recency (0-100)"),
        ("weight_quantity", "20", "Scoring weight for quantity match (0-100)"),
        (
            "weight_vendor_reliability",
            "20",
            "Scoring weight for vendor reliability (0-100)",
        ),
        (
            "weight_data_completeness",
            "10",
            "Scoring weight for data completeness (0-100)",
        ),
        (
            "weight_source_credibility",
            "10",
            "Scoring weight for source credibility (0-100)",
        ),
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
