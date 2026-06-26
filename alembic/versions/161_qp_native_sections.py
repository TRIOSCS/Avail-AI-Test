"""QP Phase C2b: native Sales + Purchasing sections, serial entries, FRU lookups.

Replaces the Excel QP template's Sales and Purchasing "Quality Questions" sections
with native columns on ``quality_plans`` (all nullable — the completeness gate enforces
the required subset at submit time, not at the DB), adds the two per-section approved-at
timestamps stamped by the approval engine, and creates the two QP child tables:

  - ``qp_serial_entries`` — the Serial-preapproval tracking rows (one per submission;
    FK qp_id CASCADE so they drop with the QP; buyer/submitted_by SET NULL).
  - ``qp_fru_lookups``    — FRU part numbers pinned to a QP (normalized key only;
    the view live-joins the shared ``fru_links`` crosswalk by fru_norm).
    Unique per (qp_id, fru_norm) so a FRU can't be pinned twice.

Additive and fully reversible — every column is nullable and the new tables drop
cleanly on downgrade. No data backfill.

Called by: alembic. Depends on: 160_qp_so_po_approvers.

Revision ID: 161_qp_native_sections
Revises: 160_qp_so_po_approvers
Create Date: 2026-06-26
"""

import sqlalchemy as sa

import app.database
from alembic import op

revision = "161_qp_native_sections"
down_revision = "160_qp_so_po_approvers"
branch_labels = None
depends_on = None


# Sales "Quality Questions" columns (name, type) in template order.
_SALES_COLUMNS = [
    ("sales_so_number", sa.String(length=255)),
    ("sales_condition", sa.String(length=255)),
    ("sales_quantity", sa.Integer()),
    ("sales_fw_hw_rev", sa.Text()),
    ("sales_product_commodity", sa.String(length=255)),
    ("sales_testing_required", sa.Boolean()),
    ("sales_testing_option", sa.String(length=255)),
    ("sales_testing_specifics", sa.Text()),
    ("sales_test_location", sa.String(length=255)),
    ("sales_serial_preapproval_required", sa.Boolean()),
    ("sales_authorized_ship_early", sa.Boolean()),
    ("sales_authorized_ship_partial", sa.Boolean()),
    ("sales_routing_prescreening_whs", sa.String(length=255)),
    ("sales_vendor_rating", sa.String(length=255)),
    ("sales_third_party_pkg_ok", sa.Boolean()),
    ("sales_pkg_requirements", sa.Text()),
    ("sales_bom_matrix_links", sa.Text()),
    ("sales_notes", sa.Text()),
]

# Purchasing "Quality Questions" columns (name, type) in template order.
_PURCHASING_COLUMNS = [
    ("purchasing_po_number", sa.String(length=255)),
    ("purchasing_condition", sa.String(length=255)),
    ("purchasing_fw_hw_rev", sa.Text()),
    ("purchasing_product_commodity", sa.String(length=255)),
    ("purchasing_testing_required", sa.Boolean()),
    ("purchasing_testing_option", sa.String(length=255)),
    ("purchasing_routing_prescreening_whs", sa.String(length=255)),
    ("purchasing_packaging", sa.Text()),
    ("purchasing_tpo_ship_complete", sa.Boolean()),
    ("purchasing_tpo_notes", sa.Text()),
]


def upgrade() -> None:
    # ── Sales + Purchasing section columns (all nullable, additive).
    for name, col_type in _SALES_COLUMNS + _PURCHASING_COLUMNS:
        op.add_column("quality_plans", sa.Column(name, col_type, nullable=True))

    # ── Per-section approved-at timestamps (stamped by _on_section_approved).
    op.add_column("quality_plans", sa.Column("sales_section_approved_at", app.database.UTCDateTime(), nullable=True))
    op.add_column(
        "quality_plans", sa.Column("purchasing_section_approved_at", app.database.UTCDateTime(), nullable=True)
    )

    # ── Serial-preapproval tracking child table.
    op.create_table(
        "qp_serial_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("qp_id", sa.Integer(), nullable=False),
        sa.Column("buyer_id", sa.Integer(), nullable=True),
        sa.Column("submitted_by_id", sa.Integer(), nullable=True),
        sa.Column("buyer_date", sa.Date(), nullable=True),
        sa.Column("has_sn_prev_received", sa.Boolean(), nullable=True),
        sa.Column("purchase_order", sa.String(length=255), nullable=True),
        sa.Column("part_number", sa.String(length=255), nullable=True),
        sa.Column("serial_number", sa.String(length=255), nullable=True),
        sa.Column("seagate_sn", sa.String(length=255), nullable=True),
        sa.Column("tso", sa.String(length=255), nullable=True),
        sa.Column("customer_po", sa.String(length=255), nullable=True),
        sa.Column("submitted_to_customer_date", sa.Date(), nullable=True),
        sa.Column("customer_approved", sa.Boolean(), nullable=True),
        sa.Column("customer_approved_date", sa.Date(), nullable=True),
        sa.Column("ops_received", sa.Boolean(), nullable=True),
        sa.Column("created_at", app.database.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["qp_id"], ["quality_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["buyer_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qp_serial_qp", "qp_serial_entries", ["qp_id"], unique=False)

    # ── FRU crosswalk pins child table.
    op.create_table(
        "qp_fru_lookups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("qp_id", sa.Integer(), nullable=False),
        sa.Column("fru_norm", sa.String(length=64), nullable=False),
        sa.Column("created_at", app.database.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["qp_id"], ["quality_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qp_id", "fru_norm", name="uq_qp_fru_lookup"),
    )
    op.create_index("ix_qp_fru_qp", "qp_fru_lookups", ["qp_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_qp_fru_qp", table_name="qp_fru_lookups")
    op.drop_table("qp_fru_lookups")

    op.drop_index("ix_qp_serial_qp", table_name="qp_serial_entries")
    op.drop_table("qp_serial_entries")

    op.drop_column("quality_plans", "purchasing_section_approved_at")
    op.drop_column("quality_plans", "sales_section_approved_at")
    for name, _col_type in reversed(_SALES_COLUMNS + _PURCHASING_COLUMNS):
        op.drop_column("quality_plans", name)
