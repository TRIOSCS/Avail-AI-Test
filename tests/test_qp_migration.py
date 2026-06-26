"""test_qp_migration.py — Migration round-trip checks for 157_qp_approvals.

Verifies that migration 157 creates exactly the expected new tables and that
the Alembic graph remains a single head after the migration is added.

Does NOT connect to a live database — uses alembic's ScriptDirectory to
introspect the revision graph. The upgrade/downgrade/upgrade round-trip is
tested manually against real Postgres during development; here we confirm
the graph structure and that all expected table names appear in the migration
script body.

NOTE: approval_gate_configs is intentionally absent from _EXPECTED_NEW_TABLES.
Approver eligibility is stored as per-user toggles on the users table
(can_approve_prepayments, prepayment_approval_limit) — no gate-config table.

Called by: pytest
Depends on: alembic/ script directory only (no DB connection required)
"""

from __future__ import annotations

import pathlib

from alembic.script import ScriptDirectory

_ALEMBIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "alembic"
_MIGRATION_ID = "157_qp_approvals"

# New tables that must be created by upgrade()
_EXPECTED_NEW_TABLES = {
    "prepayments",
    "quality_plans",
    "approval_requests",
    "approval_events",
    "approval_outbox",
    "approval_steps",
    "approval_step_recipients",
}

# New columns on offers that must be added by upgrade()
_EXPECTED_NEW_OFFER_COLS = {
    "is_primary",
    "sourcing_type",
    "vendor_rating",
    "terms",
    "location",
    "specifics",
}

# New columns on users (per-gate per-user toggles — no gate-config table)
_EXPECTED_NEW_USER_COLS = {
    "can_approve_prepayments",
    "prepayment_approval_limit",
}


def _get_script(rev_id: str):
    sd = ScriptDirectory(str(_ALEMBIC_DIR))
    return sd.get_revision(rev_id)


def test_single_head_after_migration():
    """Migration graph must resolve to exactly one head."""
    heads = ScriptDirectory(str(_ALEMBIC_DIR)).get_heads()
    assert len(heads) == 1, (
        f"Expected single head, got {len(heads)}: {sorted(heads)}. Run: alembic merge heads -m 'merge heads'"
    )
    # Don't pin the head to a specific revision — later migrations (e.g. 158_req_pipeline_hotlist)
    # legitimately chain on top, so the single-head invariant above is the durable check. Chain
    # integrity for 157_qp_approvals is verified by test_migration_chains_onto_156.


def test_migration_chains_onto_156():
    """157_qp_approvals must chain onto 156_user_avatar."""
    script = _get_script(_MIGRATION_ID)
    assert script.down_revision == "156_user_avatar", (
        f"down_revision should be '156_user_avatar', got {script.down_revision!r}"
    )


def test_migration_creates_expected_tables():
    """Upgrade() body must reference every expected new table name."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()
    for table in _EXPECTED_NEW_TABLES:
        assert table in source, (
            f"Migration source does not mention table {table!r}. Check that create_table() is present for this table."
        )


def test_migration_does_not_create_gate_config_table():
    """approval_gate_configs must NOT appear in upgrade() — replaced by user toggles."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()
    upgrade_body = source.split("def downgrade")[0]
    assert "approval_gate_configs" not in upgrade_body, (
        "upgrade() must not create approval_gate_configs — approver model uses per-user toggles on users table."
    )


def test_migration_adds_offer_columns():
    """Upgrade() must add all 6 new offer columns."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()
    for col in _EXPECTED_NEW_OFFER_COLS:
        assert col in source, (
            f"Migration source does not mention offer column {col!r}. Check that add_column('offers', ...) is present."
        )


def test_migration_adds_user_approval_columns():
    """Upgrade() must add the per-user prepayment approval columns to users."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()
    for col in _EXPECTED_NEW_USER_COLS:
        assert col in source, (
            f"Migration source does not mention user column {col!r}. Check that add_column('users', ...) is present."
        )


def test_migration_has_downgrade():
    """Downgrade() must exist and drop the new tables."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()
    assert "def downgrade" in source, "Migration is missing downgrade() function"
    assert "drop_table" in source, "downgrade() should contain drop_table calls"
    assert "drop_column" in source, "downgrade() should contain drop_column calls"


def test_no_drops_in_upgrade():
    """Upgrade() must not contain drop_table or drop_column — additive only."""
    script = _get_script(_MIGRATION_ID)
    source = pathlib.Path(script.path).read_text()

    # Split on 'def downgrade' to isolate the upgrade body
    upgrade_body = source.split("def downgrade")[0]
    assert "drop_table" not in upgrade_body, "upgrade() contains drop_table — this migration must be additive only"
    assert "drop_column" not in upgrade_body, "upgrade() contains drop_column — this migration must be additive only"
