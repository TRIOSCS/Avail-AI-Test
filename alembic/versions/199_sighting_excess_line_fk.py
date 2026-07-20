"""Add sightings.excess_line_item_id FK + index — line-identity for the excess mirror.

What (DDL, reversible):
  - ADD COLUMN sightings.excess_line_item_id (Integer, NULLABLE) — the specific
    ExcessLineItem a ``customer_excess`` mirror row shadows.
  - ADD FOREIGN KEY fk_sightings_excess_line_item → excess_line_items(id)
    ON DELETE SET NULL (deleting a line detaches its shadow; teardown removes it).
  - ADD INDEX ix_sightings_excess_line_item on that column (declared on the Sighting
    model too, so the fresh-DB schema-drift gate stays green).
  - DELETE the legacy ``customer_excess`` shadow rows. They keyed on
    (source_company_id, material_card_id, requirement_id), which is AMBIGUOUS exactly
    for two duplicate-part lines on one list — a match-and-backfill cannot decide which
    line each old row belongs to. They are disposable: the dual-write owner
    (excess_mirror.sync_list_mirror) rebuilds them line-keyed on the next publish/sync.

Why: the mirror upsert/retire previously collapsed two lines with the SAME part
(material_card) on ONE list into a single Sighting, hiding live supply and letting one
award/withdraw wipe the twin (finding #18). Keying on the line's own id makes each line
its own Sighting.

Downgrade: drop index → drop FK → drop column (the legacy-row DELETE is not restored —
those rows are rebuilt on the next mirror sync, mirroring 100/173/176 one-way cleanups).

Called by: alembic (upgrade/downgrade).
Depends on: sightings (001_initial_schema), excess_line_items (excess-module migration).

Revision ID: 199_sighting_excess_line_fk
Revises: 198_sighting_req_id_nullable
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "199_sighting_excess_line_fk"
down_revision = "198_sighting_req_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sightings", sa.Column("excess_line_item_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_sightings_excess_line_item",
        "sightings",
        "excess_line_items",
        ["excess_line_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sightings_excess_line_item", "sightings", ["excess_line_item_id"])
    # Clean-sweep the legacy shadows: the old (company, material_card, requirement) key is
    # ambiguous for duplicate-part lines, so match-and-backfill is impossible. They rebuild
    # line-keyed on the next publish/sync.
    op.execute("DELETE FROM sightings WHERE source_type = 'customer_excess'")


def downgrade() -> None:
    op.drop_index("ix_sightings_excess_line_item", table_name="sightings")
    op.drop_constraint("fk_sightings_excess_line_item", "sightings", type_="foreignkey")
    op.drop_column("sightings", "excess_line_item_id")
