-- Migration 007: Buyer Routing Assignments
-- AVAIL v1.3.0 — Phase 3
-- Depends on: 006_activity_routing_foundation.sql
--
-- Creates:  routing_assignments
-- Idempotent: all statements use IF NOT EXISTS guards

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════
--  1. ROUTING ASSIGNMENTS — 48-hour waterfall tracking
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS routing_assignments (
    id              SERIAL PRIMARY KEY,
    requirement_id  INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    vendor_card_id  INTEGER NOT NULL REFERENCES vendor_cards(id),

    -- Top-3 buyer slots
    buyer_1_id      INTEGER REFERENCES users(id),
    buyer_2_id      INTEGER REFERENCES users(id),
    buyer_3_id      INTEGER REFERENCES users(id),

    -- Scoring details (JSON for transparency)
    buyer_1_score   FLOAT,
    buyer_2_score   FLOAT,
    buyer_3_score   FLOAT,
    scoring_details JSON,                          -- full breakdown per buyer

    -- Waterfall state: simple timestamps, no state machine
    assigned_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP NOT NULL,            -- assigned_at + 48h
    claimed_by_id   INTEGER REFERENCES users(id),  -- first to enter offer
    claimed_at      TIMESTAMP,

    -- Status derived from timestamps:
    --   active:   NOW() < expires_at AND claimed_by_id IS NULL
    --   claimed:  claimed_by_id IS NOT NULL
    --   expired:  NOW() >= expires_at AND claimed_by_id IS NULL
    status          VARCHAR(20) DEFAULT 'active',

    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_routing_req       ON routing_assignments(requirement_id);
CREATE INDEX IF NOT EXISTS ix_routing_vendor    ON routing_assignments(vendor_card_id);
CREATE INDEX IF NOT EXISTS ix_routing_expires   ON routing_assignments(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_routing_buyer1    ON routing_assignments(buyer_1_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_routing_buyer2    ON routing_assignments(buyer_2_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_routing_buyer3    ON routing_assignments(buyer_3_id) WHERE status = 'active';

-- Unique: one active assignment per requirement+vendor at a time
CREATE UNIQUE INDEX IF NOT EXISTS ix_routing_active_unique
    ON routing_assignments(requirement_id, vendor_card_id)
    WHERE status = 'active';

COMMIT;
