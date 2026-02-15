-- Migration 008: Offer Attribution Columns
-- AVAIL v1.3.0 — Phase 3 support
-- Depends on: 007_routing_assignments.sql
--
-- Adds: expires_at, reconfirmed_at, reconfirm_count, attribution_status to offers
-- Idempotent: all statements use IF NOT EXISTS guards

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════
--  1. OFFER ATTRIBUTION COLUMNS
-- ═══════════════════════════════════════════════════════════════════════

ALTER TABLE offers ADD COLUMN IF NOT EXISTS expires_at           TIMESTAMP;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS reconfirmed_at       TIMESTAMP;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS reconfirm_count      INTEGER DEFAULT 0;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS attribution_status   VARCHAR(20) DEFAULT 'active';

-- Index for expiration sweep: find active offers past their TTL
CREATE INDEX IF NOT EXISTS ix_offer_attribution
    ON offers(attribution_status, expires_at)
    WHERE attribution_status = 'active';

COMMIT;
