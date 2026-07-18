"""Drop orphaned facet_audits + knowledge_config tables (dead ORM models).

What: DROP TABLE facet_audits (model FacetAudit, app/models/telemetry.py) and
knowledge_config (model KnowledgeConfig, app/models/knowledge.py). Both are
audit findings (docs/audit/2026-07-18-non-production-code-audit.md ss1):
zero readers/writers anywhere in app/ (grep-verified). Their documented
writers were never built — FacetAudit's docstring named
app/management/audit_facets.py ("future") which does not exist;
KnowledgeConfig's docstring named services/teams_qa_service.py which also
does not exist. facet_audits is empty in production by construction (no
writer ever ran); knowledge_config holds only the single seed row that
064_teams_qa_routing inserted ('daily_question_cap' = '10') — no reader
ever consumed it, so dropping it loses nothing observable.

ReconcileRun (reconcile_runs table, same telemetry.py module) and
KnowledgeEntry (knowledge_entries table, same knowledge.py module) are LIVE
and are NOT touched by this migration.

Downgrade recreates both tables exactly as the current (pre-removal) models
declared them:
  - facet_audits: created by 104_trust_telemetry — id/audited_at/card_id/
    category/spec_key/value/source/verdict/notes + ck_facet_audits_verdict
    CHECK + 3 indexes (audited_at, card_id, (category, spec_key)).
  - knowledge_config: created by 001_initial/064_teams_qa_routing, with
    uq_knowledge_config_key added later by 174_reconcile_uq_drift (the
    001-era baseline lacked it; 174 reconciled model-vs-DB drift). Recreated
    here WITH that unique constraint so downgrade matches the live schema
    immediately before this migration, not the pre-174 baseline.

Safety: no application writer ever existed for either table, so no
app-generated data is lost; downgrade fully reconstructs the schema and
re-inserts 064's seed row; prod additionally has 6-hourly pg_dump backups
(scripts/restore.sh) as a further safety net.

Revision ID: 196_drop_facet_audits_kconfig
Revises: 195_outreach_send_subject_body
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "196_drop_facet_audits_kconfig"
down_revision = "195_outreach_send_subject_body"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # facet_audits — drop indexes then table (mirrors 104_trust_telemetry's
    # own downgrade, which is exactly what undoing its creation looks like).
    op.drop_index("ix_facet_audits_category_key", table_name="facet_audits")
    op.drop_index("ix_facet_audits_card_id", table_name="facet_audits")
    op.drop_index("ix_facet_audits_audited_at", table_name="facet_audits")
    op.drop_table("facet_audits")

    # knowledge_config — drop the unique constraint 174 added, then the table.
    op.drop_constraint("uq_knowledge_config_key", "knowledge_config", type_="unique")
    op.drop_table("knowledge_config")


def downgrade() -> None:
    op.create_table(
        "knowledge_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_knowledge_config_key"),
    )
    # Restore the seed row 064_teams_qa_routing inserted, so downgrade
    # reproduces the exact pre-removal state, not just the schema.
    op.execute("INSERT INTO knowledge_config (key, value) VALUES ('daily_question_cap', '10')")

    op.create_table(
        "facet_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("spec_key", sa.String(length=64), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "verdict IN ('correct', 'wrong', 'unverifiable')",
            name="ck_facet_audits_verdict",
        ),
    )
    op.create_index("ix_facet_audits_audited_at", "facet_audits", ["audited_at"], unique=False)
    op.create_index("ix_facet_audits_card_id", "facet_audits", ["card_id"], unique=False)
    op.create_index("ix_facet_audits_category_key", "facet_audits", ["category", "spec_key"], unique=False)
