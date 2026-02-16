-- 009: Scope vendor activities (calls, notes) to a specific requisition.
-- Adds requisition_id FK to activity_log so manual activities can be tied
-- to the RFQ they relate to and shown in the thread view.

ALTER TABLE activity_log
    ADD COLUMN IF NOT EXISTS requisition_id INTEGER REFERENCES requisitions(id);

CREATE INDEX IF NOT EXISTS ix_activity_requisition
    ON activity_log(requisition_id, vendor_card_id, created_at)
    WHERE requisition_id IS NOT NULL;
