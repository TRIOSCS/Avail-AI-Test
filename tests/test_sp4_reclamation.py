"""test_sp4_reclamation.py — Unit tests for SP4 Account Reclamation feature.

Covers: migration 123 round-trip (PG only), config defaults, get_last_activity_at,
        job_account_sweep, _send_sweep_notification.

Called by: pytest
Depends on: conftest.py fixtures.

Migration round-trip requires TEST_PG_URL — SQLite cannot run CREATE EXTENSION (migration 001).
Set TEST_PG_URL=postgresql://... to include those tests.
"""

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.crm import Company
from app.models.intelligence import ActivityLog
from app.models.prospect_account import ProspectAccount

PG_URL = os.environ.get("TEST_PG_URL", "")


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_company(db, *, owner_id=None, name="Acme Corp", domain="acme.com"):
    """Create and persist a minimal Company."""
    co = Company(name=name, domain=domain, account_owner_id=owner_id)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _plant_activity(db, company_id, *, days_ago):
    """Add an ActivityLog row `days_ago` days in the past."""
    from datetime import timedelta

    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(
        ActivityLog(
            company_id=company_id,
            activity_type="note",
            channel="system",
            created_at=ts,
        )
    )
    db.commit()


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


# ── Task 3: get_last_activity_at ─────────────────────────────────────────────


def test_get_last_activity_at_no_activity(db_session):
    """Returns None when company has no activity."""
    from app.services.activity_service import get_last_activity_at

    co = _make_company(db_session)
    assert get_last_activity_at(co.id, db_session) is None


def test_get_last_activity_at_returns_latest(db_session):
    """Returns the datetime of the latest ActivityLog row."""
    from app.services.activity_service import get_last_activity_at

    co = _make_company(db_session)
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for t in (t1, t2):
        db_session.add(
            ActivityLog(
                company_id=co.id,
                activity_type="note",
                channel="system",
                created_at=t,
            )
        )
    db_session.commit()
    result = get_last_activity_at(co.id, db_session)
    assert result is not None
    assert result.replace(tzinfo=timezone.utc) == t2 or result == t2


# ── Task 5: job_account_sweep ─────────────────────────────────────────────────


def test_sweep_skips_unowned(db_session):
    """Company with no owner is not swept."""
    from app.services.prospect_reclamation import job_account_sweep_with_db

    co = _make_company(db_session, owner_id=None)
    _plant_activity(db_session, co.id, days_ago=100)
    asyncio.run(job_account_sweep_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0


def test_sweep_dormant_company(db_session, test_user):
    """Owned company with no activity in 100 days is swept."""
    from app.services.prospect_reclamation import job_account_sweep_with_db

    co = _make_company(db_session, owner_id=test_user.id)
    _plant_activity(db_session, co.id, days_ago=100)
    with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
        asyncio.run(job_account_sweep_with_db(db_session))
    pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
    assert pa is not None
    assert pa.discovery_source == "auto_sweep"
    assert pa.swept_from_owner_id == test_user.id
    assert pa.swept_at is not None
    co_fresh = db_session.get(Company, co.id)
    assert co_fresh.account_owner_id is None


def test_sweep_skips_recent_activity(db_session, test_user):
    """Owned company with activity 10 days ago is NOT swept."""
    from app.services.prospect_reclamation import job_account_sweep_with_db

    co = _make_company(db_session, owner_id=test_user.id)
    _plant_activity(db_session, co.id, days_ago=10)
    with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
        asyncio.run(job_account_sweep_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0


def test_sweep_idempotent(db_session, test_user):
    """Running sweep twice does not create duplicate ProspectAccounts."""
    from app.services.prospect_reclamation import job_account_sweep_with_db

    co = _make_company(db_session, owner_id=test_user.id)
    _plant_activity(db_session, co.id, days_ago=100)
    with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
        asyncio.run(job_account_sweep_with_db(db_session))
        asyncio.run(job_account_sweep_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 1


def test_sweep_notification_sends_to_rep(db_session, test_user):
    """Notification email is sent TO rep; includes last-activity date."""
    from app.services.prospect_reclamation import _send_sweep_notification

    mock_gc = AsyncMock()
    mock_gc.post_json = AsyncMock(return_value={})
    last_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    co = _make_company(db_session, owner_id=test_user.id)
    with (
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        asyncio.run(
            _send_sweep_notification(
                owner=test_user, company=co, last_activity_at=last_dt, prospect_id=1, db=db_session
            )
        )
    mock_gc.post_json.assert_awaited_once()
    call_args = mock_gc.post_json.call_args[0][1]
    recipients = call_args["message"]["toRecipients"]
    assert any(r["emailAddress"]["address"] == test_user.email for r in recipients)
    body = call_args["message"]["body"]["content"]
    assert "2026-01-01" in body


def test_sweep_notification_skips_on_no_token(db_session, test_user):
    """Missing token logs warning and returns without raising."""
    from app.services.prospect_reclamation import _send_sweep_notification

    co = _make_company(db_session, owner_id=test_user.id)
    with patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)):
        # should not raise
        asyncio.run(
            _send_sweep_notification(owner=test_user, company=co, last_activity_at=None, prospect_id=1, db=db_session)
        )
