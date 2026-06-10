"""Add fru_links table — IBM/Lenovo FRU crosswalk (FRU ↔ 11S ↔ model ↔ tray edges).

One row per (FRU, related PN, relationship kind, source sheet) edge parsed from the
"FRU_PN_TRAY matrix" workbook by app/management/ingest_fru_matrix.py. Read by
app/services/fru_matrix_service.py for the materials detail FRU panels.

Downgrade drops the table (data is re-ingestable from the source workbook).

Revision ID: 094_fru_links
Revises: 093_normalize_legacy_categories
Create Date: 2026-06-10

NOTE: 092 is skipped in the chain (reserved by a concurrent branch at the time 093
shipped); this revision chains onto 093 as the single head.
"""

import sqlalchemy as sa

from alembic import op

revision = "094_fru_links"
down_revision = "093_normalize_legacy_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fru_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fru_raw", sa.String(length=64), nullable=False),
        sa.Column("fru_norm", sa.String(length=64), nullable=False),
        sa.Column("related_raw", sa.String(length=64), nullable=False),
        sa.Column("related_norm", sa.String(length=64), nullable=False),
        sa.Column("rel_kind", sa.String(length=24), nullable=False),
        sa.Column("manufacturer", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("series", sa.String(length=64), nullable=True),
        sa.Column("machine", sa.String(length=128), nullable=True),
        sa.Column("qual_status", sa.String(length=64), nullable=True),
        sa.Column("qual_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_sheet", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fru_norm", "related_norm", "rel_kind", "source_sheet", name="uq_fru_links_edge"),
    )
    op.create_index("ix_fru_links_fru_norm", "fru_links", ["fru_norm"], unique=False)
    op.create_index("ix_fru_links_related_norm", "fru_links", ["related_norm"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fru_links_related_norm", table_name="fru_links")
    op.drop_index("ix_fru_links_fru_norm", table_name="fru_links")
    op.drop_table("fru_links")
