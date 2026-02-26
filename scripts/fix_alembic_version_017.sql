-- Fix alembic_version if you manually set it to the wrong revision ID.
-- The 017 migration's revision is "017_proactive_matches_cph" (not 017_proactive_matches_cph_columns).
--
-- From host (Docker):
--   docker compose exec db psql -U availai -d availai -c "UPDATE alembic_version SET version_num = '017_proactive_matches_cph' WHERE version_num = '017_proactive_matches_cph_columns';"

UPDATE alembic_version SET version_num = '017_proactive_matches_cph' WHERE version_num = '017_proactive_matches_cph_columns';
