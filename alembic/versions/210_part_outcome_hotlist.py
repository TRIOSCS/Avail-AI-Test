"""210_part_outcome_hotlist — part-level archive becomes a Won/Lost/Hotlist outcome
view.

Mirrors the requisition-level design (migration 158): `archived` stops being a
`requirements.sourcing_status` value — the Archive view in the parts workspace
is now a lens over WON/LOST/HOTLIST parts. HOTLIST is the new off-pipeline
monitor state ("customer uses the part but doesn't need it now — keep
watching"). Existing archived rows are remapped to `lost`, stamping
`outcome_reason='Archived (legacy)'` only where no reason exists, so the
downgrade path is deterministic (sentinel rows return to `archived`; rows that
already carried a reason stay `lost` — documented lossy, same as 158).

Also adds `requirements.cloned_from_id` (self-FK, SET NULL, indexed) — the
provenance link for Clone-to-Active, mirroring `requisitions.cloned_from_id`.
The index name matches the model `__table_args__` and the FK name matches the
auto-generated `<table>_<col>_fkey` form (there is no metadata
naming_convention — see 188's docstring), so the fresh-DB drift gate stays
green. Constraint DDL is PostgreSQL-only (SQLite cannot ALTER constraints; the
SQLite test DB never carried the CHECK anyway) — the data UPDATEs are portable
and always run. Round-tripped on throwaway PG. Chains onto
209_buyplan_halt_snapshot.

Revision ID: 210_part_outcome_hotlist
Revises: 209_buyplan_halt_snapshot
"""

import sqlalchemy as sa

from alembic import op

revision = "210_part_outcome_hotlist"
down_revision = "209_buyplan_halt_snapshot"
branch_labels = None
depends_on = None

_NEW = "'open','sourcing','offered','quoted','won','lost','hotlist'"
_OLD = "'open','sourcing','offered','quoted','won','lost','archived'"
_LEGACY_REASON = "Archived (legacy)"
_FK_NAME = "requirements_cloned_from_id_fkey"


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.drop_constraint("ck_requirements_sourcing_status", "requirements", type_="check")
    bind.execute(
        sa.text(
            "UPDATE requirements SET outcome_reason = :r "
            "WHERE sourcing_status = 'archived' AND (outcome_reason IS NULL OR outcome_reason = '')"
        ),
        {"r": _LEGACY_REASON},
    )
    bind.execute(sa.text("UPDATE requirements SET sourcing_status = 'lost' WHERE sourcing_status = 'archived'"))
    if is_pg:
        op.create_check_constraint(
            "ck_requirements_sourcing_status",
            "requirements",
            f"sourcing_status IN ({_NEW})",
        )
    op.add_column(
        "requirements",
        sa.Column("cloned_from_id", sa.Integer(), nullable=True),
    )
    if is_pg:
        op.create_foreign_key(
            _FK_NAME,
            "requirements",
            "requirements",
            ["cloned_from_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_requirements_cloned_from", "requirements", ["cloned_from_id"])


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    op.drop_index("ix_requirements_cloned_from", table_name="requirements")
    if is_pg:
        op.drop_constraint(_FK_NAME, "requirements", type_="foreignkey")
    op.drop_column("requirements", "cloned_from_id")
    if is_pg:
        op.drop_constraint("ck_requirements_sourcing_status", "requirements", type_="check")
    # Best-effort reverse: only sentinel-stamped rows go back to archived (rows
    # that already had a real reason stay lost — documented lossy, mirrors 158).
    bind.execute(
        sa.text(
            "UPDATE requirements SET sourcing_status = 'archived', outcome_reason = NULL "
            "WHERE sourcing_status = 'lost' AND outcome_reason = :r"
        ),
        {"r": _LEGACY_REASON},
    )
    # hotlist has no legacy equivalent; archived is the nearest out-of-pipeline state.
    bind.execute(sa.text("UPDATE requirements SET sourcing_status = 'archived' WHERE sourcing_status = 'hotlist'"))
    if is_pg:
        op.create_check_constraint(
            "ck_requirements_sourcing_status",
            "requirements",
            f"sourcing_status IN ({_OLD})",
        )
