"""test_alembic.py — Verify Alembic migration setup and structure.

Tests migration file validity, model-metadata consistency,
and env.py configuration without requiring a live database.

Called by: pytest
Depends on: alembic/, app.models
"""

import importlib.util
import inspect
from pathlib import Path

MIGRATION_DIR = Path(__file__).parent.parent / "alembic" / "versions"


def _load_migration():
    """Load the initial migration module dynamically.

    Mocks alembic.op and sqlalchemy so migrations can be imported outside of Alembic's
    runtime context (where op is a stub).
    """
    import sys
    from unittest.mock import MagicMock

    # The project's alembic/ dir shadows the real package — inject stubs
    alembic_mod = sys.modules.get("alembic") or MagicMock()
    alembic_mod.op = MagicMock()
    sys.modules["alembic"] = alembic_mod
    sys.modules["alembic.op"] = alembic_mod.op

    files = sorted(MIGRATION_DIR.glob("*.py"))
    assert len(files) >= 1, "No migration files found"
    spec = importlib.util.spec_from_file_location("mig", files[0])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_initial_migration_has_required_attributes():
    mod = _load_migration()
    assert hasattr(mod, "revision")
    assert hasattr(mod, "down_revision")
    assert hasattr(mod, "upgrade")
    assert hasattr(mod, "downgrade")
    assert mod.down_revision is None, "Initial migration should have no parent"


def test_initial_migration_emits_explicit_ddl():
    """Baseline upgrade emits explicit DDL — op.create_table / op.create_index /
    op.create_foreign_key calls — not Base.metadata.create_all().

    Floors are set well below current counts (87 tables / 285 indexes / 158 cross-table
    FKs) so adding new tables doesn't break the test, but a regression to the
    create_all() form (which produces ZERO op.create_table calls) fails loudly. Counts
    ignore comments because we use literal-call substrings like "op.create_table(" which
    only appear in actual code.
    """
    mod = _load_migration()
    up_src = inspect.getsource(mod.upgrade)
    create_tables = up_src.count("op.create_table(")
    create_indexes = up_src.count("op.create_index(")
    create_fks = up_src.count("op.create_foreign_key(")
    assert create_tables >= 80, (
        f"Expected at least 80 op.create_table calls in upgrade(); got {create_tables}. "
        "If this fails, 001 may have regressed to Base.metadata.create_all()."
    )
    assert create_indexes >= 250, f"Expected at least 250 op.create_index calls in upgrade(); got {create_indexes}."
    assert create_fks >= 150, (
        f"Expected at least 150 op.create_foreign_key calls in upgrade(); got {create_fks}. "
        "Cross-table FKs must be emitted as separate op.create_foreign_key calls (not "
        "inlined in op.create_table) to avoid 'relation does not exist' errors when 001 "
        "creates many tables in one migration."
    )


def test_initial_migration_downgrade_is_symmetric():
    """Baseline downgrade is the symmetric inverse of upgrade — explicit
    op.drop_constraint / op.drop_index / op.drop_table calls.

    Floors mirror the upgrade test: well below current counts but high enough
    that a regression to Base.metadata.drop_all() (zero op.drop_table calls)
    fails loudly.
    """
    mod = _load_migration()
    down_src = inspect.getsource(mod.downgrade)
    drop_tables = down_src.count("op.drop_table(")
    drop_indexes = down_src.count("op.drop_index(")
    drop_constraints = down_src.count("op.drop_constraint(")
    assert drop_tables >= 80, (
        f"Expected at least 80 op.drop_table calls in downgrade(); got {drop_tables}. "
        "If this fails, 001 may have regressed to Base.metadata.drop_all()."
    )
    assert drop_indexes >= 250, f"Expected at least 250 op.drop_index calls in downgrade(); got {drop_indexes}."
    assert drop_constraints >= 150, (
        f"Expected at least 150 op.drop_constraint calls in downgrade(); got {drop_constraints}. "
        "Cross-table FKs must be dropped before their tables to release the FK pin."
    )


def test_env_py_imports_all_models():
    """env.py must import Base so autogenerate sees all tables."""
    env_path = Path(__file__).parent.parent / "alembic" / "env.py"
    content = env_path.read_text()
    assert "from app.models import Base" in content


def test_no_create_all_in_main():
    """main.py must NOT use create_all — Alembic manages schema."""
    main_path = Path(__file__).parent.parent / "app" / "main.py"
    content = main_path.read_text()
    assert "create_all" not in content, "Remove Base.metadata.create_all — use Alembic"
