"""tests/scripts/test_validate_001_against_chain.py — Tests for the 001-vs-chain
validator's schema model and migration walker.

Called by: pytest
Depends on: scripts.validate_001_against_chain
"""

from scripts.validate_001_against_chain import SchemaModel, walk_migration_ops


def test_schema_model_add_then_drop():
    m = SchemaModel()
    m.add_table("users", ["id", "email"])
    assert m.has_table("users")
    assert m.has_column("users", "email")
    m.drop_table("users")
    assert not m.has_table("users")


def test_schema_model_add_drop_column():
    m = SchemaModel()
    m.add_table("users", ["id"])
    m.add_column("users", "email")
    assert m.has_column("users", "email")
    m.drop_column("users", "email")
    assert not m.has_column("users", "email")


def test_schema_model_rename_table():
    m = SchemaModel()
    m.add_table("users_v1", ["id"])
    m.rename_table("users_v1", "users")
    assert m.has_table("users")
    assert not m.has_table("users_v1")


def test_walk_migration_ops_detects_drop_of_unknown_table(tmp_path):
    """A migration that drops a table not in the model produces a Gap."""
    mig = tmp_path / "002_drop_ghosts.py"
    mig.write_text("def upgrade():\n    op.drop_table('ghost_table')\n")
    m = SchemaModel()
    m.add_table("real_table", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert len(gaps) == 1
    assert gaps[0].migration == "002_drop_ghosts.py"
    assert "ghost_table" in gaps[0].description


def test_walk_migration_ops_handles_add_column_on_known_table(tmp_path):
    mig = tmp_path / "002_add_email.py"
    mig.write_text("def upgrade():\n    op.add_column('users', sa.Column('email', sa.String(255)))\n")
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == []
    assert m.has_column("users", "email")


def test_walk_migration_ops_flags_alter_on_unknown_column(tmp_path):
    mig = tmp_path / "002_alter_missing.py"
    mig.write_text("def upgrade():\n    op.alter_column('users', 'never_existed', nullable=False)\n")
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert len(gaps) == 1
    assert "never_existed" in gaps[0].description


def test_walk_migration_ops_skips_data_only_migrations(tmp_path):
    """A migration with only data ops (op.execute, op.bulk_insert) produces no gaps."""
    mig = tmp_path / "002_seed_data.py"
    mig.write_text("def upgrade():\n    op.execute('INSERT INTO users (id) VALUES (1)')\n")
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == []


def test_walk_migration_ops_ignores_downgrade_drops_when_upgrade_uses_idempotent_guard(tmp_path):
    """Regression: downgrade()'s drop_column must not fire as a gap before
    the if-guarded add_column in upgrade() runs.

    Real-world trigger: 8aad37e73b45_add_req_offer_user_columns.py wraps
    op.add_column in `if not _column_exists(...): ...` (depth-5 in AST), and
    its downgrade() has a bare op.drop_column (depth-3). ast.walk's BFS
    visits depth-3 nodes before depth-5 nodes, so the drop_column was being
    flagged as a gap before the matching add_column was processed. Fix:
    walk only def upgrade(), not the full module.
    """
    mig = tmp_path / "002_idempotent_add.py"
    mig.write_text(
        "def upgrade():\n"
        "    if not _column_exists('users', 'email'):\n"
        "        op.add_column('users', sa.Column('email', sa.String(255)))\n"
        "\n"
        "def downgrade():\n"
        "    op.drop_column('users', 'email')\n"
    )
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == [], f"expected no gaps; got {[g.description for g in gaps]}"
    assert m.has_column("users", "email"), "upgrade()'s add_column should have run"


def test_walk_migration_ops_skips_migrations_without_upgrade(tmp_path):
    """A migration file with only def downgrade() (no upgrade) is silently skipped — the
    forward chain has nothing to validate from it."""
    mig = tmp_path / "002_downgrade_only.py"
    mig.write_text("def downgrade():\n    op.drop_table('users')\n")
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == []
    assert m.has_table("users"), "downgrade()'s drop_table should be ignored"
