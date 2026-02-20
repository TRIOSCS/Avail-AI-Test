"""
test_alembic.py — Verify Alembic migration setup and structure.

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

    Mocks alembic.op and sqlalchemy so migrations can be imported outside
    of Alembic's runtime context (where op is a stub).
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


def test_initial_migration_uses_metadata_create_all():
    """Baseline migration uses Base.metadata.create_all (covers all models)."""
    mod = _load_migration()
    up_src = inspect.getsource(mod.upgrade)
    assert "create_all" in up_src, "Baseline upgrade should use metadata.create_all"
    assert "Base" in up_src, "Baseline upgrade should reference Base"


def test_downgrade_uses_metadata_drop_all():
    """Baseline downgrade uses Base.metadata.drop_all."""
    mod = _load_migration()
    down_src = inspect.getsource(mod.downgrade)
    assert "drop_all" in down_src, "Baseline downgrade should use metadata.drop_all"


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
