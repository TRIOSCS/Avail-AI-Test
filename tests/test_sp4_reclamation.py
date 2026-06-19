"""test_sp4_reclamation.py — Unit tests for SP4 Account Reclamation feature.

Covers: migration 123 round-trip (PG only), config defaults.

Called by: pytest
Depends on: conftest.py fixtures.

Migration round-trip requires TEST_PG_URL — SQLite cannot run CREATE EXTENSION (migration 001).
Set TEST_PG_URL=postgresql://... to include those tests.
"""

import os
import subprocess

import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")


# ── Migration 123 ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL not set — PG required for migration tests")
def test_migration_123_upgrade_downgrade():
    """Upgrade adds park provenance columns; downgrade removes them."""
    from sqlalchemy import create_engine, inspect

    env = {**os.environ, "DATABASE_URL": PG_URL}

    def alembic(cmd: str) -> None:
        result = subprocess.run(
            f"alembic {cmd}",
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            cwd="/root/availai/.claude/worktrees/sp4-reclamation",
        )
        assert result.returncode == 0, f"alembic {cmd} failed:\n{result.stderr}"

    alembic("upgrade 123_sp4_park_provenance")

    engine = create_engine(PG_URL)
    cols = {c["name"] for c in inspect(engine).get_columns("prospect_accounts")}
    assert "swept_from_owner_id" in cols, "swept_from_owner_id column missing after upgrade"
    assert "swept_at" in cols, "swept_at column missing after upgrade"
    assert "parked_by_id" in cols, "parked_by_id column missing after upgrade"
    engine.dispose()

    alembic("downgrade -1")
    engine2 = create_engine(PG_URL)
    cols2 = {c["name"] for c in inspect(engine2).get_columns("prospect_accounts")}
    assert "swept_from_owner_id" not in cols2, "swept_from_owner_id still present after downgrade"
    assert "swept_at" not in cols2, "swept_at still present after downgrade"
    assert "parked_by_id" not in cols2, "parked_by_id still present after downgrade"
    engine2.dispose()

    alembic("upgrade head")


# ── Config ────────────────────────────────────────────────────────────────────


def test_sp4_config_defaults():
    """SP4 config fields have correct defaults."""
    from app.config import Settings

    s = Settings()
    assert s.account_sweep_enabled is False
    assert s.account_sweep_inactivity_days == 90
    assert s.account_sweep_manager_email == ""
    assert s.account_reactivation_sweep_enabled is True
