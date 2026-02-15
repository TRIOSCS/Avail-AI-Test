-- Migration 004: Acctivate sync fields + inventory/sync log tables
-- Run BEFORE deploying Acctivate-enabled code

-- VendorCard: Acctivate behavioral data
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS acctivate_vendor_id VARCHAR(255);
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS cancellation_rate DOUBLE PRECISION;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS rma_rate DOUBLE PRECISION;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS acctivate_total_orders INTEGER;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS acctivate_total_units INTEGER;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS acctivate_last_order_date DATE;
ALTER TABLE vendor_cards ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_vc_acctivate_id ON vendor_cards(acctivate_vendor_id);

-- MaterialVendorHistory: Acctivate transaction truth
ALTER TABLE material_vendor_history ADD COLUMN IF NOT EXISTS acctivate_last_price DOUBLE PRECISION;
ALTER TABLE material_vendor_history ADD COLUMN IF NOT EXISTS acctivate_last_date DATE;
ALTER TABLE material_vendor_history ADD COLUMN IF NOT EXISTS acctivate_rma_rate DOUBLE PRECISION;
ALTER TABLE material_vendor_history ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'api_sighting';

-- Inventory snapshots — refreshed daily from Acctivate
CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(255) NOT NULL,
    warehouse_id VARCHAR(100),
    qty_on_hand INTEGER DEFAULT 0,
    synced_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_inv_product_warehouse
    ON inventory_snapshots(product_id, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_inv_product ON inventory_snapshots(product_id);

-- Sync log — tracks every sync run
CREATE TABLE IF NOT EXISTS sync_logs (
    id SERIAL PRIMARY KEY,
    source VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    duration_seconds DOUBLE PRECISION,
    row_counts JSONB,
    errors JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sync_source_time ON sync_logs(source, started_at);
