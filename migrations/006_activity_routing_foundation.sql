-- Migration 006: Activity Logging, Buyer Routing & Customer Ownership Foundation
-- AVAIL v1.3.0 — Phase 1
-- Depends on: 005_email_pipeline_v2.sql
--
-- Creates:  activity_log, buyer_profiles, buyer_vendor_stats
-- Alters:   customers (via customer_sites parent → companies), offers
-- Idempotent: all statements use IF NOT EXISTS / IF EXISTS guards

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════
--  1. ACTIVITY LOG — zero-manual-logging, system-event-only truth
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS activity_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    activity_type   VARCHAR(20) NOT NULL,          -- email_sent, email_received, call_outbound, call_inbound
    channel         VARCHAR(20) NOT NULL,           -- email, phone

    -- Polymorphic link to customer or vendor (at most one set)
    company_id      INTEGER REFERENCES companies(id),
    vendor_card_id  INTEGER REFERENCES vendor_cards(id),

    -- Contact details captured at log time
    contact_email   VARCHAR(255),
    contact_phone   VARCHAR(100),
    contact_name    VARCHAR(255),

    -- Metadata
    subject         VARCHAR(500),                   -- email subject if applicable
    duration_seconds INTEGER,                       -- call duration if available
    external_id     VARCHAR(255),                   -- Graph message ID or 8x8 call ID for dedup

    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indexes for the three primary query patterns
CREATE INDEX IF NOT EXISTS ix_activity_company     ON activity_log(company_id, created_at)   WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_activity_vendor      ON activity_log(vendor_card_id, created_at) WHERE vendor_card_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_activity_user        ON activity_log(user_id, created_at);

-- Dedup: prevent double-logging the same Graph message or call record
CREATE UNIQUE INDEX IF NOT EXISTS ix_activity_external ON activity_log(external_id) WHERE external_id IS NOT NULL;


-- ═══════════════════════════════════════════════════════════════════════
--  2. BUYER PROFILES — commodity / geography / brand assignments
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS buyer_profiles (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) UNIQUE,

    -- Commodity assignments
    primary_commodity   VARCHAR(100),               -- semiconductors, pc_server_parts, etc.
    secondary_commodity VARCHAR(100),

    -- Geographic coverage
    primary_geography   VARCHAR(50),                -- apac, emea, americas

    -- Brand specialties (e.g. IBM specialist)
    brand_specialties   TEXT[],                      -- ['IBM']
    brand_material_types TEXT[],                     -- ['systems', 'parts', 'components']
    brand_usage_types   TEXT[],                      -- ['sourcing_to_buy', 'selling_trading', 'backup_buying']

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);


-- ═══════════════════════════════════════════════════════════════════════
--  3. CUSTOMER / COMPANY OWNERSHIP FIELDS
-- ═══════════════════════════════════════════════════════════════════════
-- The spec says "customers" but our schema uses companies → customer_sites.
-- Ownership lives on companies (the parent level).

ALTER TABLE companies ADD COLUMN IF NOT EXISTS is_strategic         BOOLEAN DEFAULT FALSE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS ownership_cleared_at TIMESTAMP;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_activity_at     TIMESTAMP;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS account_owner_id     INTEGER REFERENCES users(id);

CREATE INDEX IF NOT EXISTS ix_companies_last_activity ON companies(account_owner_id, last_activity_at)
    WHERE account_owner_id IS NOT NULL;


-- ═══════════════════════════════════════════════════════════════════════
--  4. OFFER ATTRIBUTION FIELDS — 14-day TTL with reconfirmation
-- ═══════════════════════════════════════════════════════════════════════

ALTER TABLE offers ADD COLUMN IF NOT EXISTS expires_at          TIMESTAMP;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS reconfirmed_at      TIMESTAMP;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS reconfirm_count     INTEGER DEFAULT 0;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS attribution_status  VARCHAR(20) DEFAULT 'active';

CREATE INDEX IF NOT EXISTS ix_offers_expiration ON offers(expires_at, attribution_status)
    WHERE attribution_status = 'active';


-- ═══════════════════════════════════════════════════════════════════════
--  5. BUYER–VENDOR STATS — per-buyer performance with each vendor
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS buyer_vendor_stats (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    vendor_card_id      INTEGER NOT NULL REFERENCES vendor_cards(id),

    rfqs_sent           INTEGER DEFAULT 0,
    responses_received  INTEGER DEFAULT 0,
    response_rate       FLOAT,                      -- computed: responses / rfqs
    offers_logged       INTEGER DEFAULT 0,
    offers_won          INTEGER DEFAULT 0,
    win_rate            FLOAT,                      -- computed: won / logged
    avg_response_hours  FLOAT,
    last_contact_at     TIMESTAMP,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE(user_id, vendor_card_id)
);

CREATE INDEX IF NOT EXISTS ix_bvs_vendor ON buyer_vendor_stats(vendor_card_id);
CREATE INDEX IF NOT EXISTS ix_bvs_user   ON buyer_vendor_stats(user_id);


-- ═══════════════════════════════════════════════════════════════════════
--  6. VENDOR CARD SCORECARD FIELDS
-- ═══════════════════════════════════════════════════════════════════════

ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS avg_response_hours  FLOAT;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS overall_win_rate    FLOAT;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS total_pos           INTEGER DEFAULT 0;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS total_revenue       FLOAT DEFAULT 0;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS last_activity_at    TIMESTAMP;


-- ═══════════════════════════════════════════════════════════════════════
--  7. GRAPH WEBHOOK STATE — subscription tracking for push notifications
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS graph_subscriptions (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    subscription_id     VARCHAR(255) NOT NULL UNIQUE,   -- Graph subscription ID
    resource            VARCHAR(255) NOT NULL,           -- e.g. /me/messages
    change_type         VARCHAR(100) NOT NULL,           -- created, updated, deleted
    expiration_dt       TIMESTAMP NOT NULL,              -- Graph subs expire every 3 days max for mail
    client_state        VARCHAR(255),                    -- secret for validation
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_graphsub_user ON graph_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS ix_graphsub_expiry ON graph_subscriptions(expiration_dt);

COMMIT;
