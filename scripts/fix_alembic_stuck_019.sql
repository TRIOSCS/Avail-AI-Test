-- One-time fix when alembic_version points at a revision that no longer exists in code.
-- Use case: DB was at 019_site_contacts_contact_status_ensure (removed); repo has only 019_activity_req_channel.
--
-- Correct sequence: 018_missing_orm_cols -> 019_activity_req_channel -> 020 -> ... -> 047
-- 018 already adds site_contacts.contact_status (ADD COLUMN IF NOT EXISTS), so no separate migration needed.
--
-- Run from host (Docker):
--   docker compose -f docker-compose.yml -f docker-compose.local.yml exec db \
--     psql -U availai -d availai -c "UPDATE alembic_version SET version_num = '018_missing_orm_cols' WHERE version_num = '019_site_contacts_contact_status_ensure';"
--
-- Or pipe this file (from project root):
--   docker compose -f docker-compose.yml -f docker-compose.local.yml exec -T db psql -U availai -d availai < scripts/fix_alembic_stuck_019.sql

UPDATE alembic_version
SET version_num = '018_missing_orm_cols'
WHERE version_num = '019_site_contacts_contact_status_ensure';
