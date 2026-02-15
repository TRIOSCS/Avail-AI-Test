-- Migration 005: Email Pipeline v2 + Intelligence Layer
-- Covers: Email Mining v2 hardening, response parser upgrade, attachment parsing,
--         outbound mining, engagement scoring, contact enrichment, intel cache

-- ══════════════════════════════════════════════════════════════════════
-- H2: Message Deduplication Table
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT NOT NULL,
    processing_type TEXT NOT NULL,   -- 'mining', 'response', 'attachment', 'sent'
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, processing_type)
);

-- ══════════════════════════════════════════════════════════════════════
-- H8: Delta Query Infrastructure
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sync_state (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder TEXT NOT NULL,            -- 'Inbox', 'SentItems'
    delta_token TEXT,
    last_sync_at TIMESTAMPTZ,
    UNIQUE(user_id, folder)
);

-- ══════════════════════════════════════════════════════════════════════
-- Upgrade 2: Column Mapping Cache for Attachment Parsing
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS column_mapping_cache (
    id SERIAL PRIMARY KEY,
    vendor_domain TEXT NOT NULL,
    file_fingerprint TEXT NOT NULL,  -- Hash of first 10 rows
    mapping JSONB NOT NULL,
    confidence FLOAT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vendor_domain, file_fingerprint)
);

-- ══════════════════════════════════════════════════════════════════════
-- Definitive Spec: Prospect Contacts (Contact Enrichment Engine)
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS prospect_contacts (
    id SERIAL PRIMARY KEY,
    customer_site_id INTEGER REFERENCES customer_sites(id) ON DELETE SET NULL,
    vendor_card_id INTEGER REFERENCES vendor_cards(id) ON DELETE SET NULL,

    full_name VARCHAR(255) NOT NULL,
    title VARCHAR(255),
    email VARCHAR(255),
    email_status VARCHAR(20),           -- verified, guessed, unavailable, bounced
    phone VARCHAR(100),
    linkedin_url VARCHAR(500),

    source VARCHAR(50) NOT NULL,        -- apollo, web_search, email_reply, manual, import
    confidence VARCHAR(10) NOT NULL,    -- high, medium, low
    found_at TIMESTAMPTZ DEFAULT NOW(),
    verified_at TIMESTAMPTZ,

    is_saved BOOLEAN DEFAULT FALSE,
    saved_by_id INTEGER REFERENCES users(id),
    notes TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_prospect_contacts_site ON prospect_contacts(customer_site_id);
CREATE INDEX IF NOT EXISTS ix_prospect_contacts_vendor ON prospect_contacts(vendor_card_id);
CREATE INDEX IF NOT EXISTS ix_prospect_contacts_email ON prospect_contacts(email);

-- ══════════════════════════════════════════════════════════════════════
-- Definitive Spec: Intel Cache (Company Intelligence Cards)
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS intel_cache (
    id SERIAL PRIMARY KEY,
    cache_key VARCHAR(500) NOT NULL UNIQUE,
    data JSONB NOT NULL,
    ttl_days INTEGER NOT NULL DEFAULT 7,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_intel_cache_key ON intel_cache(cache_key);
CREATE INDEX IF NOT EXISTS ix_intel_cache_expires ON intel_cache(expires_at);

-- ══════════════════════════════════════════════════════════════════════
-- Upgrade 1: Contact Model Additions (Response Parser Hardening)
-- ══════════════════════════════════════════════════════════════════════
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS needs_review BOOLEAN DEFAULT FALSE;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS parse_result_json JSONB;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS parse_confidence FLOAT;

-- ══════════════════════════════════════════════════════════════════════
-- Upgrade 2: Sighting Model Additions (Richer Attachment Parsing)
-- ══════════════════════════════════════════════════════════════════════
ALTER TABLE sightings ADD COLUMN IF NOT EXISTS date_code VARCHAR(50);
ALTER TABLE sightings ADD COLUMN IF NOT EXISTS packaging VARCHAR(50);
ALTER TABLE sightings ADD COLUMN IF NOT EXISTS condition VARCHAR(50);
ALTER TABLE sightings ADD COLUMN IF NOT EXISTS lead_time_days INTEGER;
ALTER TABLE sightings ADD COLUMN IF NOT EXISTS lead_time VARCHAR(100);

-- ══════════════════════════════════════════════════════════════════════
-- Upgrade 3 & 4: VendorCard Engagement Fields
-- ══════════════════════════════════════════════════════════════════════
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS total_outreach INTEGER DEFAULT 0;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS total_responses INTEGER DEFAULT 0;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS total_wins INTEGER DEFAULT 0;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS ghost_rate FLOAT;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS response_velocity_hours FLOAT;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS last_contact_at TIMESTAMPTZ;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS relationship_months INTEGER;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS engagement_score FLOAT;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS engagement_computed_at TIMESTAMPTZ;

-- ══════════════════════════════════════════════════════════════════════
-- Vendor Response: add parse fields if missing
-- ══════════════════════════════════════════════════════════════════════
ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS match_method VARCHAR(50);
