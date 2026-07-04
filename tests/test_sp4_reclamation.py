"""test_sp4_reclamation.py — Unit tests for SP4 Account Reclamation feature.

Covers: migration 123 hermetic round-trip, config defaults, get_last_activity_at,
        job_account_sweep, _send_sweep_notification, job_auto_surface_reactivation,
        reclaim_prospect_account.

Called by: pytest
Depends on: conftest.py fixtures, tests/migration_harness.run_ops,
            alembic/versions/123_sp4_park_provenance.py.

The migration round-trip runs in-process on a scratch in-memory SQLite engine via
the shared hermetic harness — no PG, no alembic CLI, no subprocess. Migration 123's
add_column uses inline FK references to users.id; SQLite cannot ALTER-add a constraint,
so the FK clause is stripped during the test (a fresh FK-free column with the same
name/type/nullability is added instead). That exercises the real column add/drop DDL;
the FK semantics themselves are PG-only and are verified on live Postgres at deploy time.
"""

import asyncio
import importlib.util
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from app.constants import ProspectAccountStatus
from app.models.auth import User
from app.models.crm import Company
from app.models.intelligence import ActivityLog
from app.models.prospect_account import ProspectAccount
from app.models.sourcing import Requisition
from tests.migration_harness import run_ops

# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_company(db, *, owner_id=None, name="Acme Corp", domain="acme.com"):
    """Create and persist a minimal Company."""
    co = Company(name=name, domain=domain, account_owner_id=owner_id)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _plant_activity(db, company_id, *, days_ago, activity_type="email_sent"):
    """Add an ActivityLog row `days_ago` days in the past.

    Uses email_sent by default — notes are excluded from dormancy calc per Item 3 of the
    CRM rubric (get_last_activity_at ignores note types).
    """
    from datetime import timedelta

    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(
        ActivityLog(
            company_id=company_id,
            activity_type=activity_type,
            channel="system",
            created_at=ts,
        )
    )
    db.commit()


# ── Migration 123 (hermetic SQLite round-trip) ───────────────────────────────

_MIGRATION_123_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "123_sp4_park_provenance.py")
_spec_123 = importlib.util.spec_from_file_location("migration_123", _MIGRATION_123_PATH)
_mod_123 = importlib.util.module_from_spec(_spec_123)
_spec_123.loader.exec_module(_mod_123)

_PARK_COLS = {"swept_from_owner_id", "swept_at", "parked_by_id"}

_orig_add_column = Operations.add_column


def _add_column_no_fk(self, table_name, column, **kwargs):
    """add_column that strips inline FK clauses so SQLite ADD COLUMN works.

    SQLite has no ALTER-ADD-CONSTRAINT, so an inline ``sa.ForeignKey`` in the column
    raises NotImplementedError. We add a fresh FK-free column with the same
    name/type/nullability — exercising the real DDL while skipping the PG-only FK
    (which is verified on live Postgres at deploy time).
    """
    fresh = sa.Column(column.name, column.type, nullable=column.nullable)
    return _orig_add_column(self, table_name, fresh, **kwargs)


def _engine_123() -> sa.engine.Engine:
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = sa.MetaData()
    sa.Table("users", meta, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(
        "prospect_accounts",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255)),
    )
    meta.create_all(engine)
    return engine


def _pa_cols(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("prospect_accounts")}


class TestMigration123:
    def test_revision_metadata(self):
        assert _mod_123.revision == "123_sp4_park_provenance"
        assert _mod_123.down_revision == "122_prospect_ai_scores"
        # alembic_version.version_num is VARCHAR(32) on PG; SQLite ignores length.
        assert len(_mod_123.revision) <= 32

    def test_upgrade_adds_park_columns(self):
        engine = _engine_123()
        with patch.object(Operations, "add_column", _add_column_no_fk):
            run_ops(engine, _mod_123.upgrade)
        assert _PARK_COLS <= _pa_cols(engine)

    def test_downgrade_removes_park_columns(self):
        engine = _engine_123()
        with patch.object(Operations, "add_column", _add_column_no_fk):
            run_ops(engine, _mod_123.upgrade)
            run_ops(engine, _mod_123.downgrade)
        assert not (_PARK_COLS & _pa_cols(engine))

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = _engine_123()
        with patch.object(Operations, "add_column", _add_column_no_fk):
            run_ops(engine, _mod_123.upgrade)
            run_ops(engine, _mod_123.downgrade)
            run_ops(engine, _mod_123.upgrade)
        assert _PARK_COLS <= _pa_cols(engine)


# ── Config ────────────────────────────────────────────────────────────────────


def test_sp4_config_defaults(monkeypatch):
    """SP4 config fields have correct CODE defaults.

    Builds a fresh Settings with no env file and the relevant vars cleared, so the
    assertions verify the in-code defaults regardless of any ambient prod ``.env``
    (e.g. ``ACCOUNT_SWEEP_INACTIVITY_DAYS=35``) that pytest would otherwise load.
    """
    from app.config import Settings

    for key in (
        "ACCOUNT_SWEEP_ENABLED",
        "ACCOUNT_SWEEP_INACTIVITY_DAYS",
        "ACCOUNT_SWEEP_MANAGER_EMAIL",
        "ACCOUNT_REACTIVATION_SWEEP_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)
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
    """Returns the datetime of the latest non-note ActivityLog row."""
    from app.services.activity_service import get_last_activity_at

    co = _make_company(db_session)
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for t in (t1, t2):
        db_session.add(
            ActivityLog(
                company_id=co.id,
                activity_type="email_sent",  # real activity (not a note)
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
    # Last-activity date renders in the business display zone (DEFAULT_DISPLAY_TZ), so a
    # UTC-midnight instant lands on the prior Eastern calendar day.
    from app.utils.timezones import DEFAULT_DISPLAY_TZ, format_localdate

    assert format_localdate(last_dt, "%Y-%m-%d", tz=DEFAULT_DISPLAY_TZ) in body


def test_sweep_notification_skips_on_no_token(db_session, test_user):
    """Missing token logs warning and returns without raising."""
    from app.services.prospect_reclamation import _send_sweep_notification

    co = _make_company(db_session, owner_id=test_user.id)
    with patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)):
        # should not raise
        asyncio.run(
            _send_sweep_notification(owner=test_user, company=co, last_activity_at=None, prospect_id=1, db=db_session)
        )


# ── Additional helpers for Tasks 6 & 7 ───────────────────────────────────────

_user_counter = 0


def _make_user(db, *, role: str = "buyer") -> User:
    """Create a unique User for testing."""
    global _user_counter
    _user_counter += 1
    u = User(
        email=f"user_{_user_counter}@test.com",
        name=f"Test User {_user_counter}",
        role=role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_requisition(db, *, company_id: int) -> Requisition:
    """Create a minimal Requisition linked to a company."""
    req = Requisition(
        name="Test Req",
        status="open",
        company_id=company_id,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_swept_company(db, *, swept_owner: User):
    """Create a Company with a ProspectAccount that was swept from swept_owner.

    Returns dict with prospect_id and company_id.
    """
    co = Company(
        name=f"Swept Corp {swept_owner.id}",
        domain=f"swept-{swept_owner.id}.com",
        account_owner_id=None,
    )
    db.add(co)
    db.commit()
    db.refresh(co)

    pa = ProspectAccount(
        name=co.name,
        domain=co.domain,
        discovery_source="auto_sweep",
        status=ProspectAccountStatus.SUGGESTED,
        fit_score=0,
        readiness_score=0,
        company_id=co.id,
        swept_from_owner_id=swept_owner.id,
    )
    db.add(pa)
    db.commit()
    db.refresh(pa)
    return {"prospect_id": pa.id, "company_id": co.id}


# ── Task 6: job_auto_surface_reactivation ─────────────────────────────────────


def test_reactivation_surfaces_past_customer_with_req(db_session, test_user):
    """Unassigned company with a Requisition gets a ProspectAccount."""
    from app.services.prospect_reclamation import job_auto_surface_with_db

    co = _make_company(db_session, owner_id=None, name="Reactivate Me", domain="reactivateme.com")
    _make_requisition(db_session, company_id=co.id)
    asyncio.run(job_auto_surface_with_db(db_session))
    pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
    assert pa is not None
    assert pa.discovery_source == "reactivation"


def test_reactivation_skips_owned_company(db_session, test_user):
    """Company with an owner is not auto-surfaced."""
    from app.services.prospect_reclamation import job_auto_surface_with_db

    co = _make_company(db_session, owner_id=test_user.id, name="Owned Corp", domain="ownedcorp.com")
    _make_requisition(db_session, company_id=co.id)
    asyncio.run(job_auto_surface_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0


def test_reactivation_skips_company_already_in_pool(db_session):
    """Company already linked to an active ProspectAccount is not duplicated."""
    from app.services.prospect_reclamation import job_auto_surface_with_db

    co = _make_company(db_session, owner_id=None, name="Already Pooled", domain="alreadypooled.com")
    _make_requisition(db_session, company_id=co.id)
    db_session.add(
        ProspectAccount(
            name=co.name,
            domain=co.domain,
            discovery_source="reactivation",
            status="suggested",
            fit_score=0,
            readiness_score=0,
            company_id=co.id,
        )
    )
    db_session.commit()
    asyncio.run(job_auto_surface_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 1


def test_reactivation_skips_no_history(db_session):
    """Unassigned company with no quote or requisition is not surfaced."""
    from app.services.prospect_reclamation import job_auto_surface_with_db

    co = _make_company(db_session, owner_id=None, name="No History Corp", domain="nohistory.com")
    asyncio.run(job_auto_surface_with_db(db_session))
    assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0


# ── Task 7: reclaim_prospect_account ─────────────────────────────────────────


def test_reclaim_by_former_owner(db_session, test_user):
    """Former owner can reclaim their swept account."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    co = _make_swept_company(db_session, swept_owner=test_user)
    result = reclaim_prospect_account(co["prospect_id"], test_user.id, db_session)
    assert result["status"] == "reclaimed"
    co_fresh = db_session.get(Company, co["company_id"])
    assert co_fresh.account_owner_id == test_user.id
    pa = db_session.get(ProspectAccount, co["prospect_id"])
    assert pa.status == ProspectAccountStatus.DISMISSED


def test_reclaim_logs_activity(db_session, test_user):
    """Reclaim creates an ActivityLog entry of type 'reclaim'."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    co = _make_swept_company(db_session, swept_owner=test_user)
    reclaim_prospect_account(co["prospect_id"], test_user.id, db_session)
    log = db_session.query(ActivityLog).filter_by(company_id=co["company_id"], activity_type="reclaim").first()
    assert log is not None


def test_reclaim_permission_denied_for_stranger(db_session, test_user):
    """Non-owner non-admin cannot reclaim."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    other = _make_user(db_session)
    co = _make_swept_company(db_session, swept_owner=test_user)
    with pytest.raises(ValueError, match="permission"):
        reclaim_prospect_account(co["prospect_id"], other.id, db_session)


def test_reclaim_allowed_for_admin(db_session, test_user):
    """Admin can reclaim any swept account."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    other_admin = _make_user(db_session, role="admin")
    co = _make_swept_company(db_session, swept_owner=test_user)
    result = reclaim_prospect_account(co["prospect_id"], other_admin.id, db_session, is_admin=True)
    assert result["status"] == "reclaimed"
