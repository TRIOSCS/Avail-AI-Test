"""Tests for app/services/prospect_reclamation.py — SP4 sweep, surface, reclaim.

Covers: reclaim_prospect_account error paths, job_account_sweep wrapper,
job_account_sweep_with_db sweep logic, _send_sweep_notification,
job_auto_surface_reactivation wrapper, job_auto_surface_with_db surface logic.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.models.auth import User
from app.models.crm import Company
from app.models.prospect_account import ProspectAccount
from app.models.sourcing import Requisition


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _user(db: Session, email: str | None = None) -> User:
    u = User(
        email=email or f"user-{_uid()}@test.com",
        name="Test",
        role="buyer",
        azure_id=_uid(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _company(db: Session, *, owner_id: int | None = None, domain: str | None = None) -> Company:
    c = Company(
        name=f"Co-{_uid()}",
        domain=domain or f"co-{_uid()}.com",
        is_active=True,
        account_owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


def _prospect(
    db: Session,
    *,
    company_id: int | None = None,
    swept_from_owner_id: int | None = None,
    status: str = "suggested",
    domain: str | None = None,
) -> ProspectAccount:
    pa = ProspectAccount(
        name=f"PA-{_uid()}",
        domain=domain or f"pa-{_uid()}.com",
        status=status,
        discovery_source="test",
        fit_score=0,
        readiness_score=0,
        company_id=company_id,
        swept_from_owner_id=swept_from_owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(pa)
    db.flush()
    return pa


# ── reclaim_prospect_account ──────────────────────────────────────────────────


class TestReclaimProspectAccount:
    def test_happy_path_reclaims_company(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        co = _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id)
        db_session.commit()

        result = reclaim_prospect_account(pa.id, owner.id, db_session)
        assert result["status"] == "reclaimed"
        assert result["company_id"] == co.id
        db_session.refresh(pa)
        assert pa.status == ProspectAccountStatus.DISMISSED
        assert pa.dismiss_reason == "reclaimed"

    def test_admin_can_reclaim(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        admin = _user(db_session)
        co = _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id)
        db_session.commit()

        result = reclaim_prospect_account(pa.id, admin.id, db_session, is_admin=True)
        assert result["status"] == "reclaimed"

    def test_not_found_raises_lookup_error(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        with pytest.raises(LookupError):
            reclaim_prospect_account(99999, 1, db_session)

    def test_wrong_status_raises_value_error(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        pa = _prospect(db_session, status="dismissed", swept_from_owner_id=owner.id)
        db_session.commit()

        with pytest.raises(ValueError, match="Cannot reclaim"):
            reclaim_prospect_account(pa.id, owner.id, db_session)

    def test_user_not_found_raises_runtime_error(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        pa = _prospect(db_session, swept_from_owner_id=None)
        db_session.commit()

        with pytest.raises(RuntimeError, match="not found"):
            reclaim_prospect_account(pa.id, 99999, db_session)

    def test_permission_denied_raises_value_error(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        stranger = _user(db_session)
        pa = _prospect(db_session, swept_from_owner_id=owner.id)
        db_session.commit()

        with pytest.raises(ValueError, match="permission denied"):
            reclaim_prospect_account(pa.id, stranger.id, db_session)

    def test_manager_email_can_reclaim(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        manager = _user(db_session, email="manager@trio.com")
        co = _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id)
        db_session.commit()

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "manager@trio.com")
        result = reclaim_prospect_account(pa.id, manager.id, db_session)
        assert result["status"] == "reclaimed"


# ── job_account_sweep wrapper ────────────────────────────────────────────────


async def test_job_account_sweep_opens_and_closes_session() -> None:
    from app.services import prospect_reclamation as pr

    called = []

    async def mock_with_db(db) -> None:
        called.append("called")

    mock_db = MagicMock()
    with patch("app.database.SessionLocal", return_value=mock_db):
        with patch.object(pr, "job_account_sweep_with_db", mock_with_db):
            await pr.job_account_sweep()

    assert "called" in called
    mock_db.close.assert_called_once()


# ── job_auto_surface_reactivation wrapper ─────────────────────────────────────


async def test_job_auto_surface_reactivation_opens_and_closes_session() -> None:
    from app.services import prospect_reclamation as pr

    called = []

    async def mock_with_db(db) -> None:
        called.append("called")

    mock_db = MagicMock()
    with patch("app.database.SessionLocal", return_value=mock_db):
        with patch.object(pr, "job_auto_surface_with_db", mock_with_db):
            await pr.job_auto_surface_reactivation()

    assert "called" in called
    mock_db.close.assert_called_once()


# ── job_account_sweep_with_db ─────────────────────────────────────────────────


class TestJobAccountSweepWithDb:
    async def test_skips_already_swept(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 30)
        owner = _user(db_session)
        co = _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session, company_id=co.id)
        pa.swept_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_notify = AsyncMock()
        with patch.object(pr, "_send_sweep_notification", mock_notify):
            await pr.job_account_sweep_with_db(db_session)

        mock_notify.assert_not_called()

    async def test_skips_recent_activity(self, db_session: Session, monkeypatch) -> None:
        from datetime import timedelta

        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 30)
        owner = _user(db_session)
        _company(db_session, owner_id=owner.id)
        db_session.commit()

        recent = datetime.now(timezone.utc) - timedelta(days=5)
        mock_send = MagicMock()
        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=recent),
            patch("app.services.prospect_claim.send_company_to_prospecting", mock_send),
        ):
            await pr.job_account_sweep_with_db(db_session)

        mock_send.assert_not_called()

    async def test_skips_missing_owner(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 0)
        owner = _user(db_session)
        _company(db_session, owner_id=owner.id)
        db_session.commit()

        original_get = db_session.get

        def mock_get(model, pk):
            if model is User:
                return None
            return original_get(model, pk)

        mock_send = MagicMock()
        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch("app.services.prospect_claim.send_company_to_prospecting", mock_send),
            patch.object(db_session, "get", side_effect=mock_get),
        ):
            await pr.job_account_sweep_with_db(db_session)

        mock_send.assert_not_called()

    async def test_sweep_exception_does_not_propagate(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 0)
        owner = _user(db_session)
        _company(db_session, owner_id=owner.id)
        db_session.commit()

        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch("app.services.prospect_claim.send_company_to_prospecting", side_effect=RuntimeError("boom")),
        ):
            await pr.job_account_sweep_with_db(db_session)

    async def test_successful_sweep_updates_prospect_account(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 0)
        owner = _user(db_session)
        _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session)
        db_session.commit()

        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch(
                "app.services.prospect_claim.send_company_to_prospecting",
                return_value={"prospect_id": pa.id},
            ),
            patch.object(pr, "_send_sweep_notification", AsyncMock()),
        ):
            await pr.job_account_sweep_with_db(db_session)

        db_session.refresh(pa)
        assert pa.swept_at is not None
        assert pa.swept_from_owner_id is not None


# ── _send_sweep_notification ──────────────────────────────────────────────────


class TestSendSweepNotification:
    async def test_skips_when_no_token(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        owner = _user(db_session)
        co = _company(db_session)
        db_session.commit()

        with patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)):
            await pr._send_sweep_notification(owner, co, None, 1, db_session)

    async def test_sends_with_cc_when_manager_email_set(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "manager@trio.com")
        owner = _user(db_session)
        co = _company(db_session)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await pr._send_sweep_notification(owner, co, None, 1, db_session)

        call_args = mock_gc.post_json.call_args
        payload = call_args[0][1]
        assert len(payload["message"]["ccRecipients"]) == 1

    async def test_notification_exception_does_not_propagate(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        owner = _user(db_session)
        co = _company(db_session)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=RuntimeError("network error"))

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await pr._send_sweep_notification(owner, co, None, 1, db_session)


# ── job_auto_surface_with_db ──────────────────────────────────────────────────


class TestJobAutoSurfaceWithDb:
    async def test_skips_already_in_pool(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        domain = f"surface-{_uid()}.com"
        co = _company(db_session, owner_id=None, domain=domain)
        req = Requisition(
            name=f"REQ-{_uid()}",
            company_id=co.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        pa = _prospect(db_session, company_id=co.id, status="suggested", domain=domain)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        count = db_session.query(ProspectAccount).filter(ProspectAccount.company_id == co.id).count()
        assert count == 1

    async def test_skips_no_domain(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        co = Company(
            name=f"No Domain {_uid()}",
            is_active=True,
            account_owner_id=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.flush()
        req = Requisition(
            name=f"REQ-{_uid()}",
            company_id=co.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        assert db_session.query(ProspectAccount).count() == 0

    async def test_surfaces_eligible_company(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        domain = f"eligible-{_uid()}.com"
        co = _company(db_session, owner_id=None, domain=domain)
        req = Requisition(
            name=f"REQ-{_uid()}",
            company_id=co.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        pa = db_session.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
        assert pa is not None
        assert pa.discovery_source == "reactivation"
        assert pa.status == ProspectAccountStatus.SUGGESTED

    async def test_domain_collision_links_company(self, db_session: Session) -> None:
        from app.services import prospect_reclamation as pr

        domain = f"collision-{_uid()}.com"
        co = _company(db_session, owner_id=None, domain=domain)
        req = Requisition(
            name=f"REQ-{_uid()}",
            company_id=co.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        pa_existing = ProspectAccount(
            name=f"Old-{_uid()}",
            domain=domain,
            status=ProspectAccountStatus.SUGGESTED,
            discovery_source="clay",
            fit_score=0,
            readiness_score=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pa_existing)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        db_session.refresh(pa_existing)
        assert pa_existing.company_id == co.id
