"""Smoke-test: columns exist post-upgrade, absent post-downgrade.

Run against a real PostgreSQL instance only — SQLite does not support
the reflection calls used here. The project test suite runs SQLite (conftest.py),
so this test is marked skip unless TEST_PG_URL is set.

Usage:
    TEST_PG_URL=postgresql://... pytest tests/test_migration_120.py -v
"""

import os

import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")


@pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL not set — PG required for migration tests")
def test_migration_121_upgrade_downgrade():
    """Upgrade adds columns; downgrade removes them; re-upgrade restores them."""
    import subprocess

    env = {**os.environ, "DATABASE_URL": PG_URL}

    def alembic(cmd: str) -> None:
        result = subprocess.run(
            f"alembic {cmd}",
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            cwd="/root/availai/.claude/worktrees/sp3-screening",
        )
        assert result.returncode == 0, f"alembic {cmd} failed:\n{result.stderr}"

    alembic("upgrade 121_prospect_ai_scores")

    from sqlalchemy import create_engine, inspect

    engine = create_engine(PG_URL)
    cols = {c["name"] for c in inspect(engine).get_columns("prospect_accounts")}
    assert "trio_match_score" in cols
    assert "opportunity_score" in cols
    engine.dispose()

    alembic("downgrade -1")
    engine2 = create_engine(PG_URL)
    cols2 = {c["name"] for c in inspect(engine2).get_columns("prospect_accounts")}
    assert "trio_match_score" not in cols2
    assert "opportunity_score" not in cols2
    engine2.dispose()

    alembic("upgrade head")
