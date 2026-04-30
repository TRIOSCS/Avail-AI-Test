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
