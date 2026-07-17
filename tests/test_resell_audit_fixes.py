"""test_resell_audit_fixes.py — the four confirmed Resell audit findings (2026-07-03).

Regression coverage for the QA/audit sweep fixes:

- L2  non-positive quantity → 400 (not an unhandled 500): the add-line and per-line
       offer routers validate ``quantity > 0`` before the model @validates ValueError.
- L3  ``confirm_import`` re-validates client-submitted rows server-side, so a hand-
       crafted POST cannot bypass ``_parse_import_row`` preview validation.
- M6  the owner is notified (deduped per (list, buyer)) on an inbound offer / a buyer
       reply — a ``channel="system"`` ActivityLog to ``excess_list.owner_id``.
- M9  ``award_offer`` locks the list + its lines up front, so the already-awarded guard
       holds under the lock path (an already-awarded line cannot be re-awarded).

Called by: pytest
Depends on: app.services.excess_service, app.services.resell_outreach_service,
            app.routers.resell, tests.conftest
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferStatus,
    OfferLineMatchStatus,
)
from app.models import ActivityLog, Company, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from app.services import excess_service
from app.services import resell_outreach_service as outreach_svc
from tests.conftest import engine

_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def owner(db_session: Session) -> User:
    """The list owner — a trader (can_post + can_offer) who receives the
    notifications."""
    u = User(email="audit-owner@trioscs.com", name="Audit Owner", role="trader", azure_id="audit-owner-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer(db_session: Session) -> User:
    """A buyer (can_offer) who submits inbound offers — never the owner."""
    u = User(email="audit-buyer@trioscs.com", name="Audit Buyer", role="buyer", azure_id="audit-buyer-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer2(db_session: Session) -> User:
    """A second, distinct buyer — proves the notification dedup is per (list, buyer)."""
    u = User(email="audit-buyer2@trioscs.com", name="Audit Buyer Two", role="buyer", azure_id="audit-buyer-002")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Audit Seller Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def draft_list(db_session: Session, company: Company, owner: User) -> ExcessList:
    el = ExcessList(company_id=company.id, owner_id=owner.id, title="Audit Draft", status=ExcessListStatus.DRAFT)
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def posted_list(db_session: Session, company: Company, owner: User) -> ExcessList:
    el = ExcessList(company_id=company.id, owner_id=owner.id, title="Audit Posted", status=ExcessListStatus.COLLECTING)
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="LM358N",
            normalized_part_number="LM358N",
            quantity=500,
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _line(db: Session, excess_list: ExcessList, part_number: str, qty: int = 100) -> ExcessLineItem:
    li = ExcessLineItem(
        excess_list_id=excess_list.id, part_number=part_number, quantity=qty, asking_price=Decimal("1.00")
    )
    db.add(li)
    db.flush()
    return li


def _open_offer(
    db: Session, excess_list: ExcessList, submitter: User, line: ExcessLineItem, price: Decimal
) -> ExcessOffer:
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=submitter.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=line.quantity,
            unit_price=price,
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db.flush()
    return offer


def _system_notifs(db: Session, *, owner_id: int, list_id: int, activity_type: str) -> list[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == owner_id,
            ActivityLog.excess_list_id == list_id,
            ActivityLog.activity_type == activity_type,
            ActivityLog.channel == "system",
        )
        .all()
    )


# ═══════════════════════════════════════════════════════════════════════
#  L2 — non-positive quantity → 400, not 500
# ═══════════════════════════════════════════════════════════════════════


class TestL2QuantityBound:
    def _as_owner(self, owner: User):
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: owner

    def _reset(self):
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides.pop(require_user, None)

    @pytest.mark.parametrize("bad_qty", ["0", "-5"])
    def test_add_line_non_positive_quantity_400(self, client, db_session, owner, draft_list, bad_qty):
        """A non-positive add-line quantity returns 400 (not the model-ValueError
        500)."""
        self._as_owner(owner)
        try:
            resp = client.post(
                f"/api/resell/{draft_list.id}/lines",
                data={"part_number": "LM358N", "quantity": bad_qty, "condition": "New"},
            )
        finally:
            self._reset()
        assert resp.status_code == 400
        # Nothing was inserted.
        assert db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count() == 0

    def test_add_line_positive_quantity_still_ok(self, client, db_session, owner, draft_list):
        """The happy path is unaffected: a positive quantity still adds the line."""
        self._as_owner(owner)
        try:
            resp = client.post(
                f"/api/resell/{draft_list.id}/lines",
                data={"part_number": "LM358N", "quantity": "500", "condition": "New"},
            )
        finally:
            self._reset()
        assert resp.status_code == 200
        assert db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count() == 1

    def test_submit_offer_non_positive_quantity_400(self, client, db_session, buyer, posted_list):
        """A per-line offer with quantity 0 returns 400 (not the ExcessOfferLine
        500)."""
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: buyer
        try:
            resp = client.post(
                f"/api/resell/{posted_list.id}/offers",
                data={"scope": "per_line", "mpn_raw": "LM358N", "quantity": "0", "unit_price": "1.25"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 400
        assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


# ═══════════════════════════════════════════════════════════════════════
#  L3 — confirm_import re-validates client rows server-side
# ═══════════════════════════════════════════════════════════════════════


class TestL3ConfirmImportRevalidation:
    def test_invalid_row_rejected_server_side(self, db_session, draft_list):
        """A hand-crafted invalid row (non-positive qty / blank PN) is NOT inserted,
        even though it was posted as if it had passed preview."""
        rows = [
            {"part_number": "GOOD-1", "quantity": 5},
            {"part_number": "ZERO-QTY", "quantity": 0},  # tampered — fails preview validation
            {"part_number": "", "quantity": 9},  # tampered — blank part number
        ]
        result = excess_service.confirm_import(db_session, draft_list.id, rows)

        assert result["imported"] == 1
        assert result["skipped"] == 2
        items = db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).all()
        assert [i.part_number for i in items] == ["GOOD-1"]

    def test_negative_quantity_rejected(self, db_session, draft_list):
        result = excess_service.confirm_import(db_session, draft_list.id, [{"part_number": "NEG", "quantity": -3}])
        assert result["imported"] == 0
        assert db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count() == 0

    def test_valid_rows_still_import(self, db_session, draft_list):
        """The legitimate path is unaffected — valid rows import and bump the
        counter."""
        rows = [{"part_number": "A1", "quantity": 10}, {"part_number": "B2", "quantity": 20, "condition": "Used"}]
        result = excess_service.confirm_import(db_session, draft_list.id, rows)
        assert result["imported"] == 2
        db_session.refresh(draft_list)
        assert draft_list.total_line_items == 2


# ═══════════════════════════════════════════════════════════════════════
#  M6 — owner notification on an inbound offer / buyer reply (deduped)
# ═══════════════════════════════════════════════════════════════════════


class TestM6OwnerNotification:
    def test_submit_offer_notifies_owner_once(self, db_session, posted_list, owner, buyer):
        excess_service.submit_offer(
            db_session,
            list_id=posted_list.id,
            user=buyer,
            scope="per_line",
            lines=[{"mpn_raw": "LM358N", "quantity": 40, "unit_price": Decimal("1.25")}],
        )
        notifs = _system_notifs(
            db_session, owner_id=owner.id, list_id=posted_list.id, activity_type=ActivityType.NEW_OFFER
        )
        assert len(notifs) == 1
        assert notifs[0].contact_name == buyer.name

    def test_second_offer_same_buyer_does_not_duplicate(self, db_session, posted_list, owner, buyer):
        for _ in range(2):
            excess_service.submit_offer(
                db_session,
                list_id=posted_list.id,
                user=buyer,
                scope="per_line",
                lines=[{"mpn_raw": "LM358N", "quantity": 40, "unit_price": Decimal("1.25")}],
            )
        notifs = _system_notifs(
            db_session, owner_id=owner.id, list_id=posted_list.id, activity_type=ActivityType.NEW_OFFER
        )
        assert len(notifs) == 1  # deduped per (list, buyer)

    def test_distinct_buyers_get_distinct_notifications(self, db_session, posted_list, owner, buyer, buyer2):
        for b in (buyer, buyer2):
            excess_service.submit_offer(
                db_session,
                list_id=posted_list.id,
                user=b,
                scope="take_all",
                take_all_total_price=Decimal("100.00"),
            )
        notifs = _system_notifs(
            db_session, owner_id=owner.id, list_id=posted_list.id, activity_type=ActivityType.NEW_OFFER
        )
        assert len(notifs) == 2  # one per distinct buyer

    def test_reply_with_offer_notifies_owner_and_dedups(self, db_session, posted_list, owner):
        """A buyer reply carrying a bid notifies the owner (BID_RECEIVED); a second
        reply from the same buyer refreshes rather than duplicates."""
        card = VendorCard(normalized_name="reply buyer", display_name="Reply Buyer", emails=["r@buyer.com"])
        db_session.add(card)
        db_session.flush()
        outreach = ExcessOutreach(
            excess_list_id=posted_list.id,
            target_vendor_card_id=card.id,
            submitted_by=owner.id,
            channel="email",
            status="sent",
            graph_conversation_id="conv-audit",
            graph_message_id="msg-audit",
        )
        db_session.add(outreach)
        db_session.commit()

        for _ in range(2):
            outreach_svc.record_response(
                db_session,
                conversation_id="conv-audit",
                has_offer=True,
                offer_lines=[{"mpn_raw": "LM358N", "quantity": 100, "unit_price": "1.10"}],
            )

        notifs = _system_notifs(
            db_session, owner_id=owner.id, list_id=posted_list.id, activity_type=ActivityType.BID_RECEIVED
        )
        assert len(notifs) == 1
        assert notifs[0].vendor_card_id == card.id


# ═══════════════════════════════════════════════════════════════════════
#  M9 — award locking: the already-awarded guard holds under the lock path
# ═══════════════════════════════════════════════════════════════════════


class TestM9AwardLocking:
    def test_award_happy_path_still_works_under_lock(self, db_session, company, owner, buyer):
        excess_list = ExcessList(
            company_id=company.id, owner_id=owner.id, title="Lock OK", status=ExcessListStatus.COLLECTING
        )
        db_session.add(excess_list)
        db_session.flush()
        line = _line(db_session, excess_list, "GRM188R")
        offer = _open_offer(db_session, excess_list, buyer, line, Decimal("0.80"))
        db_session.commit()

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        db_session.refresh(line)
        assert line.status == ExcessLineItemStatus.AWARDED

    def test_already_awarded_line_cannot_be_reawarded(self, db_session, company, owner, buyer):
        """Two offers on one line: after the first wins, awarding the second 409s under the
        lock path — the line can never be double-awarded."""
        excess_list = ExcessList(
            company_id=company.id, owner_id=owner.id, title="Lock Race", status=ExcessListStatus.COLLECTING
        )
        db_session.add(excess_list)
        db_session.flush()
        line = _line(db_session, excess_list, "GRM188R")
        first = _open_offer(db_session, excess_list, buyer, line, Decimal("0.90"))
        db_session.commit()

        excess_service.award_offer(db_session, first.id, owner)

        # A new open offer on the now-sold line (created AFTER the award, so it is genuinely
        # open — not closed as a pre-existing competitor) hits the lock/already-awarded path.
        second = _open_offer(db_session, excess_list, buyer, line, Decimal("0.80"))
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, second.id, owner)
        assert exc.value.status_code == 409
        assert "already awarded" in exc.value.detail
        db_session.refresh(line)
        assert line.status == ExcessLineItemStatus.AWARDED  # still the first winner's

    def test_unaward_then_reaward_works_under_lock(self, db_session, company, owner, buyer):
        """Unaward is also locked; the award→unaward→award round-trip still succeeds."""
        excess_list = ExcessList(
            company_id=company.id, owner_id=owner.id, title="Lock Round", status=ExcessListStatus.COLLECTING
        )
        db_session.add(excess_list)
        db_session.flush()
        line = _line(db_session, excess_list, "GRM188R")
        offer = _open_offer(db_session, excess_list, buyer, line, Decimal("0.80"))
        db_session.commit()

        excess_service.award_offer(db_session, offer.id, owner)
        excess_service.unaward_offer(db_session, offer.id, owner)
        db_session.refresh(line)
        assert line.status == ExcessLineItemStatus.AVAILABLE

        excess_service.award_offer(db_session, offer.id, owner)
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON
