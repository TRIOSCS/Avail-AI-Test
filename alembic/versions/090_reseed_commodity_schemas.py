"""Reseed commodity_spec_schemas after the filter-rework seed curation.

The boot seeder only INSERTS missing (commodity, spec_key) pairs and never updates an
existing row, so changed seeds (e.g. hdd.interface gaining SCSI, connectors.connector_type
promoted to the primary family facet, dram.ddr_type generations) need an explicit
reconcile. This migration: (1) inserts any brand-new spec rows, then (2) delete-then-
reinserts rows whose definition drifted from the seed. Both steps are idempotent.

Revision ID: 090_reseed_commodity_schemas
Revises: 089_oem_enrichment_columns
Create Date: 2026-06-08
"""

from sqlalchemy.orm import Session

from alembic import op

revision = "090_reseed_commodity_schemas"
down_revision = "089_oem_enrichment_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.commodity_registry import reseed_changed_schemas, seed_commodity_schemas

    session = Session(bind=op.get_bind())
    try:
        inserted = seed_commodity_schemas(session)
        reseeded = reseed_changed_schemas(session)
        print(f"[migration 090] inserted {inserted} new + reseeded {reseeded} changed commodity_spec_schemas rows")
    finally:
        session.close()


def downgrade() -> None:
    # Data reconciliation — the prior seed state is not retained, so this is not reversible.
    pass
