"""Tests for app/services/prospect_reclamation.py — SP4 sweep, surface, reclaim.

Covers: reclaim_prospect_account error paths, job_account_sweep wrapper,
job_account_sweep_with_db sweep logic, _send_sweep_notification,
job_auto_surface_reactivation wrapper, job_auto_surface_with_db surface logic,
and Phase 4 compliance: reclaim cooldown enforcement, manager reassign, and the
rep + manager sweep notification fan-out.

Policy (auto-park spec): a 45-day inactivity trigger measured across ALL of a
company's sites (a recent contact at ANY site keeps the account active); on park
BOTH the owner and every manager/admin are alerted; a 30-day post-park cooldown
blocks only the FORMER owner (other reps may claim normally); a manager/admin may
reassign it back early, overriding the cooldown. These behaviours are exercised by
TestAnySiteInactivityTrigger, TestParkAlertsOwnerAndManager,
TestCooldownPoolAvailability, and TestPolicyCodeDefaults below.
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


def _user(db: Session, email: str | None = None, *, role: str = "buyer", is_active: bool = True) -> User:
    u = User(
        email=email or f"user-{_uid()}@test.com",
        name="Test",
        role=role,
        is_active=is_active,
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
    reclaim_blocked_until: datetime | None = None,
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
        reclaim_blocked_until=reclaim_blocked_until,
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

    async def test_sends_to_rep_and_configured_manager_email(self, db_session: Session, monkeypatch) -> None:
        """Rep + configured manager email each receive a (deduped) send."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "manager@trio.com")
        owner = _user(db_session, email="rep-cc@trio.com")
        co = _company(db_session)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await pr._send_sweep_notification(owner, co, None, 1, db_session)

        sent_to = {
            r["emailAddress"]["address"]
            for call in mock_gc.post_json.call_args_list
            for r in call[0][1]["message"]["toRecipients"]
        }
        assert "rep-cc@trio.com" in sent_to
        assert "manager@trio.com" in sent_to

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
            status="open",
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
            status="open",
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
            status="open",
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
            status="open",
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

    async def test_surfaced_prospect_carries_won_quote_history(self, db_session: Session) -> None:
        """M3: a reactivated past customer with a WON quote surfaces WARM, not ice-cold.

        historical_context is lifted from the same reqs/quotes the job filtered on, and
        apply_historical_bonus pulls fit/readiness off zero so genuine warm accounts no
        longer render as fit=0/readiness=0.
        """
        from app.models.crm import CustomerSite
        from app.models.quotes import Quote
        from app.services import prospect_reclamation as pr

        domain = f"warm-{_uid()}.com"
        co = _company(db_session, owner_id=None, domain=domain)
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        req = Requisition(name=f"REQ-{_uid()}", company_id=co.id, status="open", created_at=datetime.now(timezone.utc))
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=f"Q-{_uid()}",
            status="won",
            result="won",
            won_revenue=5000,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        pa = db_session.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
        assert pa is not None
        hc = pa.historical_context or {}
        assert hc.get("bought_before") is True
        assert hc.get("quoted_before") is True
        assert hc.get("quote_count") == 1
        assert hc.get("last_activity")  # ISO date present
        # No longer ice-cold: the historical bonus lifted fit/readiness off zero.
        assert pa.fit_score > 0
        assert pa.readiness_score > 0
        assert (pa.buyer_ready_score or 0) > 0

    async def test_surfaced_prospect_requisition_only_records_last_activity(self, db_session: Session) -> None:
        """M3: a req-only past customer records last_activity; quoted/bought stay False."""
        from app.services import prospect_reclamation as pr

        domain = f"reqonly-{_uid()}.com"
        co = _company(db_session, owner_id=None, domain=domain)
        req = Requisition(name=f"REQ-{_uid()}", company_id=co.id, status="open", created_at=datetime.now(timezone.utc))
        db_session.add(req)
        db_session.commit()

        await pr.job_auto_surface_with_db(db_session)

        pa = db_session.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
        assert pa is not None
        hc = pa.historical_context or {}
        assert hc.get("quote_count") == 0
        assert hc.get("quoted_before") is False
        assert hc.get("bought_before") is False
        assert hc.get("last_activity")  # carried from the requisition
        # last_activity recency drives a readiness bump even with no quotes.
        assert pa.readiness_score > 0


# ── Phase 4: reclaim cooldown ─────────────────────────────────────────────────


class TestReclaimCooldown:
    def test_former_owner_within_cooldown_denied(self, db_session: Session) -> None:
        """A former owner cannot reclaim while reclaim_blocked_until is in the
        future."""
        from datetime import timedelta

        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=future)
        db_session.commit()

        with pytest.raises(ValueError, match="30-day cooldown"):
            reclaim_prospect_account(pa.id, owner.id, db_session)

        # Account is untouched — still in the pool, owner not restored.
        db_session.refresh(pa)
        assert pa.status == ProspectAccountStatus.SUGGESTED
        db_session.refresh(co)
        assert co.account_owner_id is None

    def test_former_owner_after_cooldown_allowed(self, db_session: Session) -> None:
        """A former owner CAN reclaim once reclaim_blocked_until has passed."""
        from datetime import timedelta

        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        co = _company(db_session, owner_id=None)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=past)
        db_session.commit()

        result = reclaim_prospect_account(pa.id, owner.id, db_session)
        assert result["status"] == "reclaimed"
        db_session.refresh(co)
        assert co.account_owner_id == owner.id

    def test_no_cooldown_set_allows_former_owner(self, db_session: Session) -> None:
        """When reclaim_blocked_until is NULL the former owner reclaims freely."""
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        co = _company(db_session, owner_id=None)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=None)
        db_session.commit()

        result = reclaim_prospect_account(pa.id, owner.id, db_session)
        assert result["status"] == "reclaimed"

    def test_manager_within_cooldown_allowed(self, db_session: Session, monkeypatch) -> None:
        """A manager bypasses the cooldown even though the former owner is blocked."""
        from datetime import timedelta

        from app.services import prospect_reclamation as pr
        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        manager = _user(db_session, role="manager")
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=future)
        db_session.commit()

        # Ensure the bypass is by ROLE, not the configured manager email.
        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")
        result = reclaim_prospect_account(pa.id, manager.id, db_session)
        assert result["status"] == "reclaimed"
        db_session.refresh(co)
        assert co.account_owner_id == manager.id

    def test_admin_within_cooldown_allowed(self, db_session: Session) -> None:
        """An admin (is_admin=True) bypasses the cooldown."""
        from datetime import timedelta

        from app.services.prospect_reclamation import reclaim_prospect_account

        owner = _user(db_session)
        admin = _user(db_session, role="admin")
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=future)
        db_session.commit()

        result = reclaim_prospect_account(pa.id, admin.id, db_session, is_admin=True)
        assert result["status"] == "reclaimed"


# ── Phase 4: sweep sets cooldown ──────────────────────────────────────────────


class TestSweepSetsCooldown:
    async def test_sweep_sets_reclaim_blocked_until(self, db_session: Session, monkeypatch) -> None:
        """A successful sweep stamps reclaim_blocked_until = swept_at + 30 days."""
        from datetime import timedelta

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
        assert pa.reclaim_blocked_until is not None
        delta = pa.reclaim_blocked_until - pa.swept_at
        assert abs(delta - timedelta(days=30)) < timedelta(seconds=5)


# ── Phase 4: reassign_account ─────────────────────────────────────────────────


class TestReassignAccount:
    def test_manager_reassigns_sets_owner_dismisses_clears_cooldown(self, db_session: Session) -> None:
        """Manager reassign sets the new owner, dismisses the swept prospect, clears
        cooldown."""
        from datetime import timedelta

        from app.models.intelligence import ActivityLog
        from app.services.prospect_reclamation import reassign_account

        manager = _user(db_session, role="manager")
        target = _user(db_session)
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=target.id, reclaim_blocked_until=future)
        db_session.commit()

        result = reassign_account(co.id, target.id, manager, db_session)
        assert result["status"] == "reassigned"

        db_session.refresh(co)
        assert co.account_owner_id == target.id
        db_session.refresh(pa)
        assert pa.status == ProspectAccountStatus.DISMISSED
        assert pa.reclaim_blocked_until is None

        log = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.company_id == co.id, ActivityLog.activity_type == "reassign")
            .first()
        )
        assert log is not None

    def test_admin_reassigns(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reassign_account

        admin = _user(db_session, role="admin")
        target = _user(db_session)
        co = _company(db_session, owner_id=None)
        db_session.commit()

        result = reassign_account(co.id, target.id, admin, db_session)
        assert result["status"] == "reassigned"
        db_session.refresh(co)
        assert co.account_owner_id == target.id

    def test_rep_reassign_raises_permission_error(self, db_session: Session) -> None:
        """Fix 3: service raises PermissionError (not HTTPException) for non-manager."""
        from app.services.prospect_reclamation import reassign_account

        rep = _user(db_session, role="sales")
        target = _user(db_session)
        co = _company(db_session, owner_id=None)
        db_session.commit()

        with pytest.raises(PermissionError, match="manager or admin"):
            reassign_account(co.id, target.id, rep, db_session)

    def test_reassign_missing_company_raises_lookup(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reassign_account

        manager = _user(db_session, role="manager")
        target = _user(db_session)
        db_session.commit()

        with pytest.raises(LookupError):
            reassign_account(999999, target.id, manager, db_session)

    def test_reassign_missing_target_user_raises(self, db_session: Session) -> None:
        from app.services.prospect_reclamation import reassign_account

        manager = _user(db_session, role="manager")
        co = _company(db_session, owner_id=None)
        db_session.commit()

        with pytest.raises(ValueError, match="not found"):
            reassign_account(co.id, 999999, manager, db_session)

    def test_reassign_inactive_target_raises(self, db_session: Session) -> None:
        """Fix 2: reassigning to an inactive user must be rejected."""
        from app.services.prospect_reclamation import reassign_account

        manager = _user(db_session, role="manager")
        inactive_target = _user(db_session, is_active=False)
        co = _company(db_session, owner_id=None)
        db_session.commit()

        original_owner = co.account_owner_id

        with pytest.raises(ValueError, match="inactive"):
            reassign_account(co.id, inactive_target.id, manager, db_session)

        # Ownership must be unchanged after the rejection.
        db_session.refresh(co)
        assert co.account_owner_id == original_owner


# ── Phase 4: sweep notification fans out to managers/admins ───────────────────


class TestSweepNotificationManagerFanout:
    async def test_includes_active_managers_and_admins(self, db_session: Session, monkeypatch) -> None:
        """Notification recipients include every active MANAGER/ADMIN plus the
        configured manager email and the rep, all deduped."""
        from app.services import prospect_reclamation as pr

        owner = _user(db_session, email="rep@trio.com")
        mgr = _user(db_session, email="boss@trio.com", role="manager")
        adm = _user(db_session, email="admin@trio.com", role="admin")
        # An inactive manager must NOT be notified.
        _user(db_session, email="ghost@trio.com", role="manager", is_active=False)
        co = _company(db_session)
        db_session.commit()

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "configured@trio.com")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await pr._send_sweep_notification(owner, co, None, 1, db_session)

        # One send per unique recipient.
        sent_to = set()
        for call in mock_gc.post_json.call_args_list:
            payload = call[0][1]
            for r in payload["message"]["toRecipients"]:
                sent_to.add(r["emailAddress"]["address"])

        assert "rep@trio.com" in sent_to
        assert "boss@trio.com" in sent_to
        assert "admin@trio.com" in sent_to
        assert "configured@trio.com" in sent_to
        assert "ghost@trio.com" not in sent_to

    async def test_one_failure_does_not_break_others(self, db_session: Session, monkeypatch) -> None:
        """If one recipient send raises, the others still go out (try/except per
        send)."""
        from app.services import prospect_reclamation as pr

        owner = _user(db_session, email="rep2@trio.com")
        _user(db_session, email="boss2@trio.com", role="manager")
        co = _company(db_session)
        db_session.commit()

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")

        calls = {"n": 0}

        async def flaky_post(path, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first send failed")
            return {}

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=flaky_post)
        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            # Must not raise despite the first send blowing up.
            await pr._send_sweep_notification(owner, co, None, 1, db_session)

        assert calls["n"] >= 2


# ── Policy helpers: real sites + activity rows ────────────────────────────────


def _site(db: Session, company_id: int, name: str | None = None):
    from app.models.crm import CustomerSite

    s = CustomerSite(company_id=company_id, site_name=name or f"Site-{_uid()}")
    db.add(s)
    db.flush()
    return s


def _activity(db: Session, *, company_id: int, site_id: int, days_ago: int):
    """Insert a real non-note ActivityLog row (so get_last_activity_at reads it).

    Every site-scoped activity carries the parent company_id — the mechanism by which
    get_last_activity_at (MAX over company_id) aggregates across ALL of a company's
    sites.
    """
    from datetime import timedelta

    from app.constants import ActivityType, Channel
    from app.models.intelligence import ActivityLog

    a = ActivityLog(
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.MANUAL,
        company_id=company_id,
        customer_site_id=site_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(a)
    db.flush()
    return a


# ── Policy 1: 45-day trigger measured across ALL sites ────────────────────────


class TestAnySiteInactivityTrigger:
    """Inactivity is the MOST RECENT activity across ALL of a company's sites."""

    async def test_recent_contact_at_one_site_keeps_account_active(self, db_session: Session, monkeypatch) -> None:
        """Stale contact at site A + fresh contact (< 45d) at site B => NOT swept."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 45)
        owner = _user(db_session)
        co = _company(db_session, owner_id=owner.id)
        site_a = _site(db_session, co.id)
        site_b = _site(db_session, co.id)
        _activity(db_session, company_id=co.id, site_id=site_a.id, days_ago=90)  # stale site
        _activity(db_session, company_id=co.id, site_id=site_b.id, days_ago=10)  # fresh site
        db_session.commit()

        mock_send = MagicMock()
        with (
            patch("app.services.prospect_claim.send_company_to_prospecting", mock_send),
            patch.object(pr, "_send_sweep_notification", AsyncMock()) as mock_notify,
        ):
            await pr.job_account_sweep_with_db(db_session)

        # A contact at ANY site inside 45d keeps the whole account active.
        mock_send.assert_not_called()
        mock_notify.assert_not_called()
        db_session.refresh(co)
        assert co.account_owner_id == owner.id

    async def test_all_sites_stale_beyond_45d_sweeps_account(self, db_session: Session, monkeypatch) -> None:
        """Every site's most recent contact is older than 45d => swept."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 45)
        owner = _user(db_session)
        co = _company(db_session, owner_id=owner.id)
        site_a = _site(db_session, co.id)
        site_b = _site(db_session, co.id)
        _activity(db_session, company_id=co.id, site_id=site_a.id, days_ago=60)
        _activity(db_session, company_id=co.id, site_id=site_b.id, days_ago=50)
        pa = _prospect(db_session)
        db_session.commit()

        with (
            patch(
                "app.services.prospect_claim.send_company_to_prospecting",
                return_value={"prospect_id": pa.id},
            ) as mock_send,
            patch.object(pr, "_send_sweep_notification", AsyncMock()),
        ):
            await pr.job_account_sweep_with_db(db_session)

        mock_send.assert_called_once()
        db_session.refresh(pa)
        assert pa.swept_at is not None


# ── Policy 2: park alerts BOTH the owner AND their manager ─────────────────────


class TestParkAlertsOwnerAndManager:
    """A real park (through the sweep) emails both the owner and the manager."""

    async def test_park_notifies_owner_and_manager(self, db_session: Session, monkeypatch) -> None:
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_inactivity_days", 0)
        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")
        owner = _user(db_session, email="rep-park@trio.com")
        _user(db_session, email="manager-park@trio.com", role="manager")
        _company(db_session, owner_id=owner.id)
        pa = _prospect(db_session)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch(
                "app.services.prospect_claim.send_company_to_prospecting",
                return_value={"prospect_id": pa.id},
            ),
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="TOKEN")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await pr.job_account_sweep_with_db(db_session)

        sent_to = {
            r["emailAddress"]["address"]
            for call in mock_gc.post_json.call_args_list
            for r in call[0][1]["message"]["toRecipients"]
        }
        assert "rep-park@trio.com" in sent_to  # the salesperson (former owner)
        assert "manager-park@trio.com" in sent_to  # their manager


# ── Policy 3: cooldown keeps the account claimable by OTHER reps ───────────────


class TestCooldownPoolAvailability:
    """During the 30-day cooldown only the FORMER owner is blocked; other reps may claim
    the parked account normally from the pool."""

    def test_other_rep_can_claim_during_cooldown(self, db_session: Session) -> None:
        from datetime import timedelta

        from app.services.prospect_claim import claim_prospect

        owner = _user(db_session, role="sales")
        other = _user(db_session, role="sales")
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=future)
        db_session.commit()

        result = claim_prospect(pa.id, other.id, db_session)
        assert result["status"] == "claimed"
        db_session.refresh(co)
        assert co.account_owner_id == other.id

    def test_former_owner_blocked_from_claiming_during_cooldown(self, db_session: Session) -> None:
        from datetime import timedelta

        from app.services.prospect_claim import claim_prospect

        owner = _user(db_session, role="sales")
        co = _company(db_session, owner_id=None)
        future = datetime.now(timezone.utc) + timedelta(days=15)
        pa = _prospect(db_session, company_id=co.id, swept_from_owner_id=owner.id, reclaim_blocked_until=future)
        db_session.commit()

        with pytest.raises(ValueError, match="cooldown"):
            claim_prospect(pa.id, owner.id, db_session)

        db_session.refresh(co)
        assert co.account_owner_id is None  # stays open in the pool


# ── Policy config: the numbers are code defaults in app/config.py ─────────────


class TestPolicyCodeDefaults:
    """Env-independent: assert the class-level defaults (not the loaded settings, which
    an env var could override) so the 45/30 policy numbers live in code."""

    def test_inactivity_default_is_45(self) -> None:
        from app.config import Settings

        assert Settings.model_fields["account_sweep_inactivity_days"].default == 45

    def test_cooldown_default_is_30(self) -> None:
        from app.config import Settings

        assert Settings.model_fields["account_sweep_reclaim_cooldown_days"].default == 30


# ── Per-rep manager routing (User.reports_to_id) ──────────────────────────────


class TestPerRepManagerRouting:
    """_sweep_notification_recipients targets the rep's specific manager when set, and
    falls back to every active MANAGER/ADMIN when unset (all deduped, rep always
    first)."""

    def test_specific_manager_only_when_set(self, db_session: Session, monkeypatch) -> None:
        """reports_to_id set + active => ONLY that manager (not the other
        supervisors)."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")
        specific = _user(db_session, email="specific-mgr@trio.com", role="manager")
        _user(db_session, email="other-mgr@trio.com", role="manager")
        _user(db_session, email="some-admin@trio.com", role="admin")
        owner = _user(db_session, email="rep-routed@trio.com")
        owner.reports_to_id = specific.id
        db_session.commit()

        recips = {r.lower() for r in pr._sweep_notification_recipients(owner, db_session)}
        assert "rep-routed@trio.com" in recips  # the rep is always notified
        assert "specific-mgr@trio.com" in recips  # their designated manager
        assert "other-mgr@trio.com" not in recips  # other supervisors are NOT fanned out
        assert "some-admin@trio.com" not in recips

    def test_all_managers_when_unset(self, db_session: Session, monkeypatch) -> None:
        """No manager set => fall back to every active MANAGER/ADMIN (unchanged)."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")
        _user(db_session, email="mgr1@trio.com", role="manager")
        _user(db_session, email="mgr2@trio.com", role="admin")
        _user(db_session, email="ghost@trio.com", role="manager", is_active=False)
        owner = _user(db_session, email="rep-unrouted@trio.com")  # reports_to_id is None
        db_session.commit()

        recips = {r.lower() for r in pr._sweep_notification_recipients(owner, db_session)}
        assert "rep-unrouted@trio.com" in recips
        assert "mgr1@trio.com" in recips
        assert "mgr2@trio.com" in recips
        assert "ghost@trio.com" not in recips  # inactive supervisors never alerted

    def test_inactive_specific_manager_falls_back_to_all(self, db_session: Session, monkeypatch) -> None:
        """reports_to_id set but that manager is inactive => all-managers fallback."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "")
        dead_mgr = _user(db_session, email="dead-mgr@trio.com", role="manager", is_active=False)
        _user(db_session, email="live-mgr@trio.com", role="manager")
        owner = _user(db_session, email="rep-dead-mgr@trio.com")
        owner.reports_to_id = dead_mgr.id
        db_session.commit()

        recips = {r.lower() for r in pr._sweep_notification_recipients(owner, db_session)}
        assert "rep-dead-mgr@trio.com" in recips
        assert "dead-mgr@trio.com" not in recips  # inactive designated manager skipped
        assert "live-mgr@trio.com" in recips  # fallback fan-out preserved

    def test_configured_manager_email_always_included(self, db_session: Session, monkeypatch) -> None:
        """The configured account_sweep_manager_email is added regardless of routing."""
        from app.services import prospect_reclamation as pr

        monkeypatch.setattr(pr.settings, "account_sweep_manager_email", "config@trio.com")
        specific = _user(db_session, email="mgr-set@trio.com", role="manager")
        owner = _user(db_session, email="rep-cfg@trio.com")
        owner.reports_to_id = specific.id
        db_session.commit()

        recips = {r.lower() for r in pr._sweep_notification_recipients(owner, db_session)}
        assert "config@trio.com" in recips
        assert "mgr-set@trio.com" in recips
