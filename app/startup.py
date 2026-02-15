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
        conn.execute(sqltext("""
            INSERT INTO site_contacts (customer_site_id, full_name, title, email, phone, is_primary)
            SELECT id, contact_name, contact_title, contact_email, contact_phone, TRUE
            FROM customer_sites
            WHERE contact_name IS NOT NULL AND contact_name != ''
        """))
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
    ]
    for stmt in stmts:
        _exec(conn, stmt)
