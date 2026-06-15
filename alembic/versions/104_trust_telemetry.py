"""Add reconcile_runs + facet_audits — durable trust-telemetry tables.

reconcile_runs persists one row per reconcile_decoded_facets execution (dry-run AND
apply) with the run's scope (sources/keys) and per-failure-class tallies — both prior
reconcile rounds' apply tallies were runtime-log-only and died with container rotation.
facet_audits stores per-row audit verdicts (correct/wrong/unverifiable) for the
volume-weighted facet-accuracy audits; it lands in THIS migration so the Phase-2.2
audit harness needs no second one. ck_facet_audits_verdict pins the closed verdict
vocabulary at the DB level (the model's @validates only guards ORM writers).

Downgrade drops the indexes + both tables (telemetry, not source data; acceptable
loss on rollback).

Revision ID: 104_trust_telemetry
Revises: 103_unavail_policy_columns
Create Date: 2026-06-12

NOTE: the plan reserved 102 for this branch, but feat/vendor-part-unavailability
claimed 102+103 and merged first (PR #270) — per the registry protocol this branch
took the next free number (104) and chained onto the live head; the chain runs
096 -> 098 -> 097 -> 099 -> 100 -> 101 -> 102 -> 103 -> 104.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "104_trust_telemetry"
down_revision = "103_unavail_policy_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconcile_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("sources", JSONB, nullable=False),
        sa.Column("keys", JSONB, nullable=False),
        sa.Column("by_class", JSONB, nullable=False),
        sa.Column("totals", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reconcile_runs_ran_at", "reconcile_runs", ["ran_at"], unique=False)

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


def downgrade() -> None:
    op.drop_index("ix_facet_audits_category_key", table_name="facet_audits")
    op.drop_index("ix_facet_audits_card_id", table_name="facet_audits")
    op.drop_index("ix_facet_audits_audited_at", table_name="facet_audits")
    op.drop_table("facet_audits")
    op.drop_index("ix_reconcile_runs_ran_at", table_name="reconcile_runs")
    op.drop_table("reconcile_runs")
