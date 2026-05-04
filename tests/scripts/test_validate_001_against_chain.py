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


def test_walk_migration_ops_create_index_resolves_table_at_arg_1(tmp_path):
    """`op.create_index('ix_name', 'table', cols)` puts the table at arg 1, not arg 0.

    Bug class: an earlier validator version resolved arg 0 ('ix_name') as
    the table name and then flagged 'ix_name' as missing from the model.
    The dispatch must look at arg 1 for create_index.
    """
    # Known table — must NOT gap.
    mig_ok = tmp_path / "002_create_index_known.py"
    mig_ok.write_text("def upgrade():\n    op.create_index('ix_users_email', 'users', ['email'])\n")
    m = SchemaModel()
    m.add_table("users", ["id", "email"])
    gaps = walk_migration_ops(m, [mig_ok])
    assert gaps == [], f"create_index against known table should not gap; got {gaps}"

    # Unknown table — must gap on the table name (arg 1), not the index name (arg 0).
    mig_bad = tmp_path / "002_create_index_unknown.py"
    mig_bad.write_text("def upgrade():\n    op.create_index('ix_ghost_email', 'ghost', ['email'])\n")
    m2 = SchemaModel()
    m2.add_table("users", ["id"])
    gaps = walk_migration_ops(m2, [mig_bad])
    assert len(gaps) == 1
    assert gaps[0].target == "ghost", f"gap should target the table 'ghost', not the index name; got {gaps[0].target!r}"


def test_walk_migration_ops_drop_constraint_resolves_table_via_kwarg(tmp_path):
    """`op.drop_constraint('uq_x', table_name='users', type_='unique')` puts the table
    only in the `table_name` kwarg.

    Dispatch must read the kwarg first for drop_constraint / drop_index (the only ops
    where table is exclusively kwarg-addressable in real alembic call sites).
    """
    mig = tmp_path / "002_drop_constraint.py"
    mig.write_text("def upgrade():\n    op.drop_constraint('uq_users_email', table_name='users', type_='unique')\n")
    m = SchemaModel()
    m.add_table("users", ["id", "email"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == [], f"drop_constraint with kwarg table_name='users' should resolve; got {gaps}"

    # Same op against an unknown table — must gap on the kwarg value.
    mig_bad = tmp_path / "002_drop_constraint_unknown.py"
    mig_bad.write_text("def upgrade():\n    op.drop_constraint('uq_x', table_name='ghost', type_='unique')\n")
    m2 = SchemaModel()
    m2.add_table("users", ["id"])
    gaps = walk_migration_ops(m2, [mig_bad])
    assert len(gaps) == 1
    assert gaps[0].target == "ghost"


def test_walk_migration_ops_create_foreign_key_resolves_source_table_at_arg_1(tmp_path):
    """`op.create_foreign_key('fk_name', 'src', 'ref', ['col'], ['id'])`: source table
    is at arg 1; ref table is at arg 2.

    The validator only checks the source table exists in the model
    (the ref table is checked transitively via the FK column on src).
    Bug class: earlier code resolved arg 0 ('fk_name') as the source.
    """
    mig = tmp_path / "002_create_fk.py"
    mig.write_text(
        "def upgrade():\n    op.create_foreign_key('fk_orders_user', 'orders', 'users', ['user_id'], ['id'])\n"
    )
    m = SchemaModel()
    m.add_table("users", ["id"])
    m.add_table("orders", ["id", "user_id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == [], f"create_foreign_key against known src/ref should not gap; got {gaps}"

    # Unknown source table — must gap on 'orders' (arg 1), not 'fk_name' (arg 0).
    mig_bad = tmp_path / "002_create_fk_unknown.py"
    mig_bad.write_text(
        "def upgrade():\n    op.create_foreign_key('fk_x', 'ghost_orders', 'users', ['user_id'], ['id'])\n"
    )
    m2 = SchemaModel()
    m2.add_table("users", ["id"])
    gaps = walk_migration_ops(m2, [mig_bad])
    assert len(gaps) == 1
    assert gaps[0].target == "ghost_orders"
