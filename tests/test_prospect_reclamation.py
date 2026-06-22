"""Tests for app/services/prospect_reclamation.py — SP4 sweep, surface, reclaim."""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.models.auth import User
from app.models.crm import Company
from app.models.prospect_account import ProspectAccount


def _make_user(db: Session, email: str | None = None) -> User:
    u = User(
        email=email or f"user-{uuid.uuid4().hex[:6]}@test.com",
        name="Test",
        role="buyer",
        azure_id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_company(db: Session, owner_id=None, domain: str | None = None) -> Company:
    c = Company(
        name=f"Co-{uuid.uuid4().hex[:6]}",
        domain=domain or f"{uuid.uuid4().hex[:8]}.com",
        is_active=True,
        account_owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


def _make_prospect(
    db: Session,
    company: Company | None = None,
    status: str = "suggested",
    swept_at=None,
    swept_from_owner_id=None,
    domain: str | None = None,
) -> ProspectAccount:
    pa = ProspectAccount(
        name=f"Prospect-{uuid.uuid4().hex[:6]}",
        domain=domain or (company.domain if company else f"{uuid.uuid4().hex[:8]}.com"),
        discovery_source="clay",
        status=status,
        fit_score=0,
        readiness_score=0,
        company_id=company.id if company else None,
        swept_at=swept_at,
        swept_from_owner_id=swept_from_owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(pa)
    db.flush()
    return pa


# ── job_account_sweep_with_db ─────────────────────────────────────────────────


async def test_sweep_moves_dormant_account_to_prospecting(db_session: Session, monkeypatch):
    """A dormant owned account gets swept — owner cleared, ProspectAccount updated."""
    from app.services import prospect_reclamation as pr

    owner = _make_user(db_session)
    co = _make_company(db_session, owner_id=owner.id)
    pa = _make_prospect(db_session, company=co)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_inactivity_days", 30)

    last_active = datetime.now(timezone.utc) - timedelta(days=45)
    mock_result = {"prospect_id": pa.id}

    with (
        patch("app.services.activity_service.get_last_activity_at", return_value=last_active),
        patch("app.services.prospect_claim.send_company_to_prospecting", return_value=mock_result),
        patch("app.services.prospect_reclamation._send_sweep_notification", new_callable=AsyncMock),
    ):
        await pr.job_account_sweep_with_db(db_session)

    db_session.refresh(pa)
    assert pa.swept_from_owner_id == owner.id
    assert pa.swept_at is not None
    assert pa.discovery_source == "auto_sweep"


async def test_sweep_skips_already_swept_account(db_session: Session, monkeypatch):
    """A company with an already-swept ProspectAccount is skipped (idempotent)."""
    from app.services import prospect_reclamation as pr

    owner = _make_user(db_session)
    co = _make_company(db_session, owner_id=owner.id)
    swept_time = datetime.now(timezone.utc) - timedelta(days=1)
    _make_prospect(db_session, company=co, swept_at=swept_time)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_inactivity_days", 30)

    mock_sweep = MagicMock()
    with patch("app.services.prospect_claim.send_company_to_prospecting", mock_sweep):
        await pr.job_account_sweep_with_db(db_session)

    mock_sweep.assert_not_called()


async def test_sweep_skips_active_account(db_session: Session, monkeypatch):
    """A company with recent activity (within inactivity_days) is skipped."""
    from app.services import prospect_reclamation as pr

    owner = _make_user(db_session)
    co = _make_company(db_session, owner_id=owner.id)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_inactivity_days", 30)

    recent_activity = datetime.now(timezone.utc) - timedelta(days=5)
    mock_sweep = MagicMock()

    with (
        patch("app.services.activity_service.get_last_activity_at", return_value=recent_activity),
        patch("app.services.prospect_claim.send_company_to_prospecting", mock_sweep),
    ):
        await pr.job_account_sweep_with_db(db_session)

    mock_sweep.assert_not_called()


async def test_sweep_skips_when_owner_not_found(db_session: Session, monkeypatch):
    """Company whose account_owner_id resolves to no User is skipped gracefully."""
    from app.services import prospect_reclamation as pr

    real_owner = _make_user(db_session)
    co = _make_company(db_session, owner_id=real_owner.id)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_inactivity_days", 30)

    old_activity = datetime.now(timezone.utc) - timedelta(days=60)
    mock_sweep = MagicMock()

    original_get = db_session.get

    def mock_db_get(model, pk):
        from app.models.auth import User as UserModel

        if model is UserModel and pk == real_owner.id:
            return None
        return original_get(model, pk)

    with (
        patch("app.services.activity_service.get_last_activity_at", return_value=old_activity),
        patch("app.services.prospect_claim.send_company_to_prospecting", mock_sweep),
        patch.object(db_session, "get", side_effect=mock_db_get),
    ):
        await pr.job_account_sweep_with_db(db_session)

    mock_sweep.assert_not_called()


async def test_sweep_handles_never_active_account(db_session: Session, monkeypatch):
    """A company with no activity at all (last_activity=None) is swept."""
    from app.services import prospect_reclamation as pr

    owner = _make_user(db_session)
    co = _make_company(db_session, owner_id=owner.id)
    pa = _make_prospect(db_session, company=co)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_inactivity_days", 30)

    mock_result = {"prospect_id": pa.id}

    with (
        patch("app.services.activity_service.get_last_activity_at", return_value=None),
        patch("app.services.prospect_claim.send_company_to_prospecting", return_value=mock_result),
        patch("app.services.prospect_reclamation._send_sweep_notification", new_callable=AsyncMock),
    ):
        await pr.job_account_sweep_with_db(db_session)

    db_session.refresh(pa)
    assert pa.swept_from_owner_id == owner.id


# ── job_auto_surface_with_db ──────────────────────────────────────────────────


async def test_auto_surface_creates_prospect_for_past_customer(db_session: Session):
    """Company with a requisition but no owner gets surfaced as reactivation."""
    from app.models.sourcing import Requisition
    from app.services import prospect_reclamation as pr

    co = _make_company(db_session, owner_id=None, domain="past-customer-123.com")
    req = Requisition(
        company_id=co.id,
        name="Old Order",
        status="closed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    await pr.job_auto_surface_with_db(db_session)

    pa = db_session.query(ProspectAccount).filter(ProspectAccount.company_id == co.id).first()
    assert pa is not None
    assert pa.discovery_source == "reactivation"
    assert pa.status == ProspectAccountStatus.SUGGESTED


async def test_auto_surface_skips_already_in_pool(db_session: Session):
    """Company already in pool (non-dismissed ProspectAccount) is skipped."""
    from app.models.sourcing import Requisition
    from app.services import prospect_reclamation as pr

    co = _make_company(db_session, owner_id=None, domain="already-pooled.com")
    req = Requisition(
        company_id=co.id,
        name="Old Order",
        status="closed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    existing_pa = _make_prospect(db_session, company=co, status="suggested", domain="already-pooled.com")
    db_session.commit()

    initial_count = db_session.query(ProspectAccount).filter(ProspectAccount.company_id == co.id).count()
    await pr.job_auto_surface_with_db(db_session)

    final_count = db_session.query(ProspectAccount).filter(ProspectAccount.company_id == co.id).count()
    assert final_count == initial_count


async def test_auto_surface_skips_no_domain(db_session: Session):
    """Company with no domain is skipped with a warning."""
    from app.models.sourcing import Requisition
    from app.services import prospect_reclamation as pr

    co = Company(
        name="NoDomain Corp",
        domain=None,
        is_active=True,
        account_owner_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()

    req = Requisition(
        company_id=co.id,
        name="Old Req",
        status="closed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    await pr.job_auto_surface_with_db(db_session)

    pa = db_session.query(ProspectAccount).filter(ProspectAccount.company_id == co.id).first()
    assert pa is None


async def test_auto_surface_merges_existing_domain_prospect(db_session: Session):
    """Domain collision: existing ProspectAccount without company_id gets linked."""
    from app.models.sourcing import Requisition
    from app.services import prospect_reclamation as pr

    domain = "domain-collision.com"
    co = _make_company(db_session, owner_id=None, domain=domain)
    req = Requisition(
        company_id=co.id,
        name="Old Req",
        status="closed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)

    orphan_pa = ProspectAccount(
        name="Orphan",
        domain=domain,
        discovery_source="clay",
        status="suggested",
        fit_score=0,
        readiness_score=0,
        company_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(orphan_pa)
    db_session.commit()

    await pr.job_auto_surface_with_db(db_session)

    db_session.refresh(orphan_pa)
    assert orphan_pa.company_id == co.id


# ── reclaim_prospect_account ──────────────────────────────────────────────────


def test_reclaim_succeeds_for_former_owner(db_session: Session):
    """Former owner can reclaim their swept account."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    owner = _make_user(db_session)
    co = _make_company(db_session, domain=f"reclaim-{uuid.uuid4().hex[:6]}.com")
    pa = _make_prospect(db_session, company=co, swept_from_owner_id=owner.id)
    db_session.commit()

    with patch("app.services.activity_service.log_activity"):
        result = reclaim_prospect_account(pa.id, owner.id, db_session)

    assert result["status"] == "reclaimed"
    assert result["prospect_id"] == pa.id
    db_session.refresh(pa)
    assert pa.status == ProspectAccountStatus.DISMISSED


def test_reclaim_succeeds_for_admin(db_session: Session):
    """Admin can reclaim any swept account regardless of former ownership."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    owner = _make_user(db_session)
    admin = _make_user(db_session)
    co = _make_company(db_session, domain=f"admin-reclaim-{uuid.uuid4().hex[:6]}.com")
    pa = _make_prospect(db_session, company=co, swept_from_owner_id=owner.id)
    db_session.commit()

    with patch("app.services.activity_service.log_activity"):
        result = reclaim_prospect_account(pa.id, admin.id, db_session, is_admin=True)

    assert result["status"] == "reclaimed"


def test_reclaim_raises_lookup_error_when_not_found(db_session: Session):
    """Raises LookupError for unknown prospect_id."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    owner = _make_user(db_session)
    db_session.commit()

    with pytest.raises(LookupError, match="not found"):
        reclaim_prospect_account(99999, owner.id, db_session)


def test_reclaim_raises_value_error_for_wrong_status(db_session: Session):
    """Raises ValueError when prospect is already dismissed."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    owner = _make_user(db_session)
    pa = _make_prospect(db_session, status="dismissed", swept_from_owner_id=owner.id)
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot reclaim"):
        reclaim_prospect_account(pa.id, owner.id, db_session)


def test_reclaim_raises_permission_denied_for_other_user(db_session: Session):
    """Non-owner, non-admin user cannot reclaim another's account."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    owner = _make_user(db_session)
    other = _make_user(db_session)
    pa = _make_prospect(db_session, swept_from_owner_id=owner.id)
    db_session.commit()

    with pytest.raises(ValueError, match="permission denied"):
        reclaim_prospect_account(pa.id, other.id, db_session, is_admin=False)


def test_reclaim_manager_email_grants_access(db_session: Session, monkeypatch):
    """Manager email in settings gets reclaim permission."""
    from app.services.prospect_reclamation import reclaim_prospect_account

    manager = _make_user(db_session, email="manager@trio.com")
    owner = _make_user(db_session)
    pa = _make_prospect(db_session, swept_from_owner_id=owner.id)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_manager_email", "manager@trio.com")

    with patch("app.services.activity_service.log_activity"):
        result = reclaim_prospect_account(pa.id, manager.id, db_session, is_admin=False)

    assert result["status"] == "reclaimed"


# ── _send_sweep_notification ──────────────────────────────────────────────────


async def test_sweep_notification_skips_without_token(db_session: Session):
    """Notification is silently skipped when no valid token exists for the user."""
    from app.services.prospect_reclamation import _send_sweep_notification

    owner = _make_user(db_session)
    co = _make_company(db_session)
    db_session.commit()

    with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None):
        await _send_sweep_notification(
            owner=owner,
            company=co,
            last_activity_at=None,
            prospect_id=1,
            db=db_session,
        )


async def test_sweep_notification_sends_with_token(db_session: Session, monkeypatch):
    """Notification sends via GraphClient when a valid token exists."""
    from app.services.prospect_reclamation import _send_sweep_notification

    owner = _make_user(db_session)
    co = _make_company(db_session)
    db_session.commit()

    monkeypatch.setattr("app.config.settings.account_sweep_manager_email", "mgr@trio.com")

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        await _send_sweep_notification(
            owner=owner,
            company=co,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=40),
            prospect_id=1,
            db=db_session,
        )

    mock_gc.post_json.assert_called_once()
