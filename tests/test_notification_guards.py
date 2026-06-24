"""test_notification_guards.py — Per-user notification-preference suppression.

Task 9 (settings-refine, Wave 2 Profile): the Profile toggles
``User.notify_buyplan_email_enabled`` and ``User.notify_new_offer_alert_enabled``
must actually SUPPRESS the firing of the corresponding notification when off:

- Buy-plan emails: ``buyplan_notifications._send_email`` skips the Microsoft Graph
  send when the recipient has ``notify_buyplan_email_enabled=False``. The in-app
  ``ActivityLog`` row (written by the calling notify_* function) is unaffected —
  nothing is lost in-app, only the email is suppressed.
- New-offer alert badge: ``OfferConfirmedSource.count_for_user`` /
  ``new_items_for_user`` return 0 / empty for a user with
  ``notify_new_offer_alert_enabled=False``, so the FYI nav badge is suppressed
  per-user.

Called by: pytest autodiscovery.
Depends on: conftest fixtures (db_session, test_user, test_requisition),
            app.services.buyplan_notifications._send_email,
            app.services.alerts.sources.offers.OfferConfirmedSource.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import OfferStatus, QualificationStatus
from app.models import ActivityLog, User
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.alerts.sources.offers import OfferConfirmedSource


def _make_user(db: Session, *, email: str, notify_buyplan_email_enabled=True, notify_new_offer_alert_enabled=True):
    u = User(
        email=email,
        name="Pref User",
        role="buyer",
        azure_id=f"az-{email}",
        m365_connected=True,
        notify_buyplan_email_enabled=notify_buyplan_email_enabled,
        notify_new_offer_alert_enabled=notify_new_offer_alert_enabled,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ═══════════════════════════════════════════════════════════════════════
# Guard 1 — buy-plan email suppression in _send_email
# ═══════════════════════════════════════════════════════════════════════


class TestBuyplanEmailGuard:
    @pytest.mark.asyncio
    async def test_email_suppressed_when_disabled(self, db_session):
        """notify_buyplan_email_enabled=False → Graph send NOT invoked."""
        from app.services.buyplan_notifications import _send_email

        user = _make_user(db_session, email="optout@trioscs.com", notify_buyplan_email_enabled=False)
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok") as mock_tok:
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc) as mock_client:
                await _send_email(user, "Subject", "<b>body</b>", db_session)

        # The Graph send must NOT happen — ideally we never even build the client
        # or fetch a token for an opted-out recipient.
        mock_gc.post_json.assert_not_awaited()
        mock_client.assert_not_called()
        mock_tok.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_email_sent_when_enabled(self, db_session):
        """notify_buyplan_email_enabled=True → Graph send IS invoked (control)."""
        from app.services.buyplan_notifications import _send_email

        user = _make_user(db_session, email="optin@trioscs.com", notify_buyplan_email_enabled=True)
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                await _send_email(user, "Subject", "<b>body</b>", db_session)

        mock_gc.post_json.assert_awaited_once()
        assert mock_gc.post_json.call_args[0][0] == "/me/sendMail"

    @pytest.mark.asyncio
    async def test_activitylog_preserved_when_email_suppressed(self, db_session):
        """Through notify_rejected: opted-out submitter still gets the in-app
        ActivityLog row, but no Graph email is sent."""
        from app.models import Company, CustomerSite, Quote
        from app.models.buy_plan import BuyPlan
        from app.services.buyplan_notifications import notify_rejected

        # Opted-out submitter (the recipient of the rejection email).
        submitter = _make_user(db_session, email="rejsub@trioscs.com", notify_buyplan_email_enabled=False)
        mgr = _make_user(db_session, email="rejmgr@trioscs.com")

        req = Requisition(name="REQ-G", status="active", created_by=submitter.id, created_at=datetime.now(timezone.utc))
        db_session.add(req)
        db_session.flush()
        co = Company(name="Guard Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Guard HQ")
        db_session.add(site)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-GUARD",
            status="sent",
            line_items=[],
            subtotal=1.0,
            total_cost=1.0,
            total_margin_pct=0.0,
            created_by_id=submitter.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.flush()
        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=req.id,
            submitted_by_id=submitter.id,
            status="pending",
            so_status="pending",
            sales_order_number="SO-G",
            approved_by_id=mgr.id,
            approval_notes="No budget",
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock):
                    await notify_rejected(plan, db_session)

        # Email suppressed for the opted-out submitter…
        mock_gc.post_json.assert_not_awaited()
        # …but the in-app ActivityLog row is still written.
        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected", user_id=submitter.id).first()
        assert act is not None
        assert f"Buy plan #{plan.id} rejected" in act.subject


# ═══════════════════════════════════════════════════════════════════════
# Guard 2 — new-offer alert badge suppression in OfferConfirmedSource
# ═══════════════════════════════════════════════════════════════════════


def _seed_approved_offer(db: Session, requirement: Requirement) -> Offer:
    offer = Offer(
        requisition_id=requirement.requisition_id,
        requirement_id=requirement.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        status=OfferStatus.APPROVED,
        qualification_status=QualificationStatus.ESSENTIALS,
        approved_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


class TestNewOfferAlertGuard:
    @pytest.fixture()
    def source(self) -> OfferConfirmedSource:
        return OfferConfirmedSource()

    def test_badge_zero_when_disabled(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        source: OfferConfirmedSource,
    ):
        """notify_new_offer_alert_enabled=False → count 0, no items, even though an
        eligible approved offer exists on the user's requirement."""
        requirement = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        _seed_approved_offer(db_session, requirement)

        test_user.notify_new_offer_alert_enabled = False
        db_session.commit()

        assert source.count_for_user(db_session, test_user) == 0
        assert source.new_items_for_user(db_session, test_user) == []

    def test_badge_nonzero_when_enabled(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        source: OfferConfirmedSource,
    ):
        """notify_new_offer_alert_enabled=True → the eligible offer is counted
        (control)."""
        requirement = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _seed_approved_offer(db_session, requirement)

        assert test_user.notify_new_offer_alert_enabled is True
        assert source.count_for_user(db_session, test_user) == 1
        items = source.new_items_for_user(db_session, test_user)
        assert [i.ref_id for i in items] == [offer.id]
