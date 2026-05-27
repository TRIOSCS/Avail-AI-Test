"""add_oem_spec_code_resolver.

Adds tables for the IBM spec code resolver (approved, pending,
blacklist) and lineage columns on requirements/sightings/offers.

Revision ID: 9868c839df65
Revises: 084_description
Create Date: 2026-05-27 21:25:21.937771
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "9868c839df65"
down_revision = "084_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oem_spec_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("avl", postgresql.JSONB, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "approved_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("oem", "spec_code", name="uq_oem_spec_code"),
    )
    op.create_index("ix_oem_spec_codes_oem", "oem_spec_codes", ["oem"])
    op.create_index("ix_oem_spec_codes_spec_code", "oem_spec_codes", ["spec_code"])

    op.create_table(
        "oem_spec_codes_pending",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("proposed_avl", postgresql.JSONB, nullable=False),
        sa.Column("llm_confidence", sa.Float, nullable=False),
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "first_requirement_id",
            sa.Integer,
            sa.ForeignKey("requirements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "used_in_requirement_ids",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.UniqueConstraint("oem", "spec_code", name="uq_pending_oem_spec_code"),
    )
    op.create_index("ix_oem_spec_codes_pending_oem", "oem_spec_codes_pending", ["oem"])
    op.create_index(
        "ix_oem_spec_codes_pending_spec_code",
        "oem_spec_codes_pending",
        ["spec_code"],
    )

    op.create_table(
        "oem_spec_codes_blacklist",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("rejected_mpns", postgresql.JSONB, nullable=False),
        sa.Column(
            "rejected_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_index("ix_oem_spec_codes_blacklist_oem", "oem_spec_codes_blacklist", ["oem"])

    # Lineage columns — all nullable, no schema break for existing rows
    op.add_column(
        "requirements",
        sa.Column("oem_hint", sa.String(64), nullable=True),
    )
    op.add_column(
        "sightings",
        sa.Column("resolved_via_spec_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "sightings",
        sa.Column("source_mpn", sa.String(255), nullable=True),
    )
    op.add_column(
        "offers",
        sa.Column("resolved_via_spec_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "offers",
        sa.Column("source_mpn", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("offers", "source_mpn")
    op.drop_column("offers", "resolved_via_spec_code")
    op.drop_column("sightings", "source_mpn")
    op.drop_column("sightings", "resolved_via_spec_code")
    op.drop_column("requirements", "oem_hint")

    op.drop_index("ix_oem_spec_codes_blacklist_oem", table_name="oem_spec_codes_blacklist")
    op.drop_table("oem_spec_codes_blacklist")

    op.drop_index("ix_oem_spec_codes_pending_spec_code", table_name="oem_spec_codes_pending")
    op.drop_index("ix_oem_spec_codes_pending_oem", table_name="oem_spec_codes_pending")
    op.drop_table("oem_spec_codes_pending")

    op.drop_index("ix_oem_spec_codes_spec_code", table_name="oem_spec_codes")
    op.drop_index("ix_oem_spec_codes_oem", table_name="oem_spec_codes")
    op.drop_table("oem_spec_codes")
