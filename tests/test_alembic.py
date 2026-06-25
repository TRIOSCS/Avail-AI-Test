"""test_alembic.py — Verify Alembic migration setup and structure.

Tests migration file validity, model-metadata consistency,
and env.py configuration without requiring a live database.

Called by: pytest
Depends on: alembic/, app.models
"""

import importlib.util
import inspect
from pathlib import Path

import pytest

MIGRATION_DIR = Path(__file__).parent.parent / "alembic" / "versions"
ENV_PY = Path(__file__).parent.parent / "alembic" / "env.py"


def _env_py_source():
    """Read the alembic env.py source for static-analysis assertions."""
    return ENV_PY.read_text()


def _load_migration():
    """Load the initial migration module dynamically.

    Imports the migration file in isolation via ``importlib`` (the migration's
    ``from alembic import op`` resolves to the real alembic ``op`` proxy, which imports
    fine outside a live alembic run — these tests only read the module's source and
    metadata, never invoke ``op.*``). The module is NOT registered in ``sys.modules`` and
    nothing in the global ``alembic`` package is mutated: an earlier version stubbed
    ``sys.modules["alembic"]`` / ``alembic.op`` with ``MagicMock``s and never restored
    them, which under pytest-xdist corrupted the shared ``alembic`` import for every later
    test on the same worker (the migration round-trip harness' ``from alembic.migration
    import MigrationContext`` then failed with ``'alembic' is not a package``). Keeping the
    load hermetic — no global import-state side effects — is the fix for that flake.
    """
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
    assert "from app.models import Base" in _env_py_source()


def test_env_py_installs_idempotent_op_wrappers():
    """env.py must monkey-patch every alembic op the chain depends on.

    Lock-the-contract regression: a future refactor that deletes one of these
    assignments would re-introduce the original CI failure (DuplicateObject /
    DependentObjectsStillExist on chain replay). Each wrapper has a
    well-documented purpose; removing one silently is the failure mode this
    test prevents.
    """
    content = _env_py_source()
    expected_assignments = [
        "op.add_column = _idempotent_add_column",
        "op.alter_column = _idempotent_alter_column",
        "op.create_foreign_key = _idempotent_create_foreign_key",
        "op.create_unique_constraint = _idempotent_create_unique_constraint",
        "op.create_check_constraint = _idempotent_create_check_constraint",
        "op.drop_constraint = _idempotent_drop_constraint",
        "op.create_table = _idempotent_create_table",
        "op.drop_table = _idempotent_drop_table",
        "op.create_index = _idempotent_create_index",
        "op.drop_index = _idempotent_drop_index",
    ]
    missing = [a for a in expected_assignments if a not in content]
    assert not missing, f"env.py is missing wrapper assignments: {missing}"


def test_env_py_alembic_version_widened_to_128():
    """env.py must pre-create alembic_version with VARCHAR(128) on first run.

    Some revision IDs are >32 chars (e.g. 009_prospect_accounts_discovery_batches, 39
    chars). The default VARCHAR(32) trips StringDataRightTruncation before migration 016
    (which retroactively widens the column) gets a chance to run. Pre-creating the table
    at env-setup avoids the bootstrap race.
    """
    content = _env_py_source()
    assert "VARCHAR(128)" in content, "env.py must widen alembic_version.version_num"
    assert "CREATE TABLE IF NOT EXISTS alembic_version" in content


def test_drop_table_cascade_is_env_var_gated():
    """`_idempotent_drop_table` must NOT cascade by default.

    Default-CASCADE was a behavior change beyond idempotency that masked
    legitimate dependent-object errors. Cascade is enabled only when the
    caller passes ``cascade=True`` or ``ALEMBIC_ALLOW_CASCADE=1`` is set
    (the latter is what CI uses for chain-replay smoke tests).
    """
    content = _env_py_source()
    assert "ALEMBIC_ALLOW_CASCADE" in content, "Cascade gate env var missing — drop_table must opt into CASCADE."
    # Sanity: the default-False kwarg must be present in the wrapper signature.
    assert "cascade: bool = False" in content


def test_no_create_all_in_main():
    """main.py must NOT use create_all — Alembic manages schema."""
    main_path = Path(__file__).parent.parent / "app" / "main.py"
    content = main_path.read_text()
    assert "create_all" not in content, "Remove Base.metadata.create_all — use Alembic"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known break (post-PR-108 follow-up): d2bea118f720 _recreate_fk iterates a "
        "hard-coded FK list that includes the 'error_reports' table, but "
        "a3f9c1d82e47_drop_dead_tables drops 'error_reports' earlier in the chain. "
        "On a `downgrade base` chain replay, d2bea118f720 runs before a3f9c1d82e47's "
        "downgrade restores the table, so reflection inside _recreate_fk raises "
        "NoSuchTableError: error_reports. Fix is a has_table guard before each "
        "recreate (or de-hardcoding the FK list). This static-analysis test pins "
        "the fragile pattern so the next refactor can't silently fix-or-paper-over."
    ),
)
def test_d2bea_recreate_fk_does_not_hardcode_dropped_tables():
    """Pin the known `error_reports` downgrade-chain break.

    Static check: the d2bea118f720 migration source contains the literal string
    'error_reports' inside its hard-coded FK lists, AND a3f9c1d82e47 drops that
    same table. Until the migration is reworked to guard each `_recreate_fk`
    call with `has_table()` (or stops hard-coding now-dropped tables), this
    pattern will break `alembic downgrade base` chain replays. xfail(strict)
    means the day someone fixes the underlying bug, this test must be
    deleted/updated — silently passing here would defeat the pin.
    """
    versions = MIGRATION_DIR
    d2bea_src = (versions / "d2bea118f720_fix_remaining_ondelete_server_default_.py").read_text()
    a3f9c_src = (versions / "a3f9c1d82e47_drop_dead_tables.py").read_text()
    # Both conditions must hold for the bug to exist:
    # 1) d2bea hard-codes error_reports in its FK recreate list, AND
    # 2) a3f9c1 drops the error_reports table.
    has_hardcoded_error_reports = '"error_reports"' in d2bea_src or "'error_reports'" in d2bea_src
    drops_error_reports = 'drop_table("error_reports"' in a3f9c_src or "drop_table('error_reports'" in a3f9c_src
    # The xfail assertion: we expect the fragile pattern to be GONE.
    # When both flags are True, the bug is still present → assertion fails →
    # xfail(strict) records expected-fail → test reports xfail (not failure).
    # The day the migration is fixed, both flags drop to False → assertion
    # passes → xfail(strict) flips to XPASS → test fails loudly, prompting
    # the engineer to remove this pin.
    assert not (has_hardcoded_error_reports and drops_error_reports), (
        "d2bea118f720 still hard-codes 'error_reports' in its FK recreate list while "
        "a3f9c1d82e47 still drops that table. This is the documented downgrade-chain "
        "break — see the xfail reason for the fix."
    )
