"""Schema-drift reconciliation (#464): retire 3 grandfathered model-vs-DB drift entries.

What:
  - ``activity_log.source_url`` (character varying) — only ever read via
    ``getattr(a, "source_url", None)`` in the activity routers, so removing the column is a
    behavioral no-op; the ORM stopped declaring it.
  - ``vendor_responses.teams_alert_sent_at`` (timestamp with time zone) — fully unreferenced
    in code; the ORM stopped declaring it.
  - The redundant ``fk_activity_log_quote`` foreign key on ``activity_log.quote_id``. Migration
    049 added this second, ondelete-less FK alongside the model-correct
    ``activity_log_quote_id_fkey`` (ON DELETE SET NULL). The model declares exactly one FK on
    ``quote_id``, so ``compare_metadata`` flags the duplicate as a phantom ``remove_fk``. Drop
    the duplicate; the SET NULL FK (which matches the model) stays.

These were the three remaining grandfathered entries in
``scripts/check_schema_matches_models.py`` (``_GRANDFATHERED_REMOVE_COLUMNS`` and
``_GRANDFATHERED_REMOVE_FKS``); this migration makes the live schema match the models so those
allowlist entries can be retired and the drift gate enforces them for real.

Downgrade: re-adds both columns (nullable, no backfill — the data was dead) and re-creates the
redundant ``fk_activity_log_quote`` FK so an upgrade→downgrade→upgrade round-trip is symmetric.

Called by: alembic (upgrade/downgrade).
Depends on: activity_log, vendor_responses tables.

Revision ID: 154_drop_dead_cols
Revises: 153_quote_graph_ids
Create Date: 2026-06-25
"""

import sqlalchemy as sa

from alembic import op

revision = "154_drop_dead_cols"
down_revision = "153_quote_graph_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Redundant ondelete-less FK from migration 049; the model-correct
    # activity_log_quote_id_fkey (ON DELETE SET NULL) is left in place.
    op.drop_constraint("fk_activity_log_quote", "activity_log", type_="foreignkey")

    # batch_alter_table keeps the column ops SQLite-portable (table-rebuild emulation).
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_column("source_url")

    with op.batch_alter_table("vendor_responses") as batch_op:
        batch_op.drop_column("teams_alert_sent_at")


def downgrade() -> None:
    with op.batch_alter_table("vendor_responses") as batch_op:
        batch_op.add_column(sa.Column("teams_alert_sent_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.add_column(sa.Column("source_url", sa.String(), nullable=True))

    op.create_foreign_key("fk_activity_log_quote", "activity_log", "quotes", ["quote_id"], ["id"])
