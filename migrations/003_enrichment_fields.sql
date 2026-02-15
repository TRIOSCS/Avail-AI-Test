-- AVAIL v1.2.0 — Enrichment schema migration
-- Run this on the production database before deploying the updated code

-- ── Company: add enrichment fields ─────────────────────────────────────
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS linkedin_url VARCHAR(500);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS legal_name VARCHAR(500);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS employee_size VARCHAR(50);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS hq_city VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS hq_state VARCHAR(100);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS hq_country VARCHAR(100);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_enriched_at TIMESTAMP;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS enrichment_source VARCHAR(50);

CREATE INDEX IF NOT EXISTS ix_companies_domain ON companies(domain);

-- ── VendorCard: add enrichment fields ──────────────────────────────────
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS linkedin_url VARCHAR(500);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS legal_name VARCHAR(500);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS employee_size VARCHAR(50);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS hq_city VARCHAR(255);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS hq_state VARCHAR(100);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS hq_country VARCHAR(100);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS industry VARCHAR(255);

-- ── VendorContact: add linkedin_url ────────────────────────────────────
ALTER TABLE vendor_contacts ADD COLUMN IF NOT EXISTS linkedin_url VARCHAR(500);

-- ── CustomerSite: add contact_linkedin ─────────────────────────────────
ALTER TABLE customer_sites ADD COLUMN IF NOT EXISTS contact_linkedin VARCHAR(500);
