"""tests/test_email_service_coverage.py — Coverage tests for uncovered lines in
app/email_service.py.

Covers:
- _handle_excess_bid_reply: empty body (921-922), None parse result (958-959),
  incomplete parse (971-972)
- _auto_create_offers_from_parse: mpn_to_card_id assignment (1043),
  existing offer dedup continue (1059), task auto-gen exception (1107-1108),
  knowledge capture exception (1115-1116), tag propagation exception (1126-1127),
  strategic vendor clock reset (1131-1136), existing ActivityLog update (1151-1154),
  offer creation loop exception (1166-1167), SSE publish exception (1186-1187)

Called by: pytest
Depends on: app.email_service, app.models, tests.conftest
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.email_service import _auto_create_offers_from_parse, _handle_excess_bid_reply
from app.models import (
    ActivityLog,
    Offer,
    Requirement,
    Requisition,
    User,
    VendorResponse,
)
from app.models.excess import BidSolicitation, ExcessLineItem, ExcessList
from tests.conftest import engine  # noqa: F401

# ── Helpers ──────────────────────────────────────────────────────────


def _make_excess_solicitation(db: Session, user: User) -> BidSolicitation:
    """Create Company -> ExcessList -> ExcessLineItem -> BidSolicitation chain."""
    from app.models import Company

    co = Company(
        name="Buyer Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()

    el = ExcessList(
        company_id=co.id,
        owner_id=user.id,
        title="Surplus Lot A",
        status="active",
    )
    db.add(el)
    db.flush()

    item = ExcessLineItem(
        excess_list_id=el.id,
        part_number="LM317T",
        quantity=500,
        asking_price=0.75,
    )
    db.add(item)
    db.flush()

    sol = BidSolicitation(
        excess_line_item_id=item.id,
        contact_id=1,
        sent_by=user.id,
        recipient_email="buyer@example.com",
        recipient_name="Buyer Bob",
        status="sent",
        sent_at=datetime.now(timezone.utc),
    )
    db.add(sol)
    db.commit()
    db.refresh(sol)
    return sol


def _make_vendor_response(db: Session, user: User, requisition: Requisition, confidence: float = 0.6) -> VendorResponse:
    """Create a VendorResponse linked to a requisition."""
    vr = VendorResponse(
        requisition_id=requisition.id,
        vendor_name="TestVendor Inc",
        vendor_email="sales@testvendor.com",
        confidence=confidence,
        scanned_by_user_id=user.id,
        status="new",
        received_at=datetime.now(timezone.utc),
        message_id=f"msg-cov-{id(requisition)}-{confidence}",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ── _handle_excess_bid_reply: empty body (lines 921-922) ─────────────


class TestHandleExcessBidReplyEmptyBody:
    @pytest.mark.asyncio
    async def test_empty_body_skipped(self, db_session: Session, test_user: User):
        """Whitespace-only body returns early without changing status (lines
        921-922)."""
        sol = _make_excess_solicitation(db_session, test_user)
        msg = {"body": {"content": "   "}, "bodyPreview": "   "}

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_claude:
            await _handle_excess_bid_reply(msg, sol.id, db_session)
            mock_claude.assert_not_called()

        db_session.refresh(sol)
        assert sol.status == "sent"

    @pytest.mark.asyncio
    async def test_empty_body_preview_skipped(self, db_session: Session, test_user: User):
        """Msg with no 'body' key but empty bodyPreview also returns early."""
        sol = _make_excess_solicitation(db_session, test_user)
        msg = {"bodyPreview": ""}

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_claude:
            await _handle_excess_bid_reply(msg, sol.id, db_session)
            mock_claude.assert_not_called()

        db_session.refresh(sol)
        assert sol.status == "sent"


# ── _handle_excess_bid_reply: None parse result (lines 958-959) ──────


class TestHandleExcessBidReplyNoneResult:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "parse_result"),
        [
            pytest.param("I have some parts, please advise.", None, id="none_result"),
            pytest.param("We may be interested.", {}, id="empty_dict_falsy"),
        ],
    )
    async def test_falsy_result_leaves_solicitation_unchanged(
        self, content: str, parse_result, db_session: Session, test_user: User
    ):
        """claude_structured returning None or {} (falsy) leaves solicitation status
        unchanged (lines 958-959)."""
        sol = _make_excess_solicitation(db_session, test_user)
        msg = {"body": {"content": content}}

        # claude_structured is imported lazily from app.utils.claude_client inside the function
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = parse_result
            await _handle_excess_bid_reply(msg, sol.id, db_session)

        db_session.refresh(sol)
        assert sol.status == "sent"


# ── _handle_excess_bid_reply: incomplete parse (lines 971-972) ───────


class TestHandleExcessBidReplyIncomplete:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "result"),
        [
            pytest.param(
                "We can supply 200 units.",
                {"unit_price": None, "quantity_wanted": 200, "lead_time_days": 5, "notes": None},
                id="missing_unit_price",
            ),
            pytest.param(
                "Our unit price is $0.50.",
                {"unit_price": 0.50, "quantity_wanted": None, "lead_time_days": None, "notes": None},
                id="missing_quantity",
            ),
        ],
    )
    async def test_incomplete_parse_skips_bid_creation(
        self, content: str, result: dict, db_session: Session, test_user: User
    ):
        """Parse result missing unit_price or quantity returns early without creating
        bid (lines 971-972)."""
        sol = _make_excess_solicitation(db_session, test_user)
        msg = {"body": {"content": content}}

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = result
            await _handle_excess_bid_reply(msg, sol.id, db_session)

        db_session.refresh(sol)
        assert sol.status == "sent"


# ── _auto_create_offers_from_parse: mpn_to_card_id path (line 1043) ──


class TestAutoCreateOffersCardIdPath:
    def test_requirement_with_material_card_id_linked_to_offer(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Requirement.material_card_id populates mpn_to_card_id; offer inherits it
        (line 1043)."""
        from app.models import MaterialCard

        mc = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        req_item.material_card_id = mc.id
        db_session.flush()

        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        parsed = {"confidence": 0.9}
        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc", "unit_price": 1.5}

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None
        assert offer.material_card_id == mc.id


# ── _auto_create_offers_from_parse: dedup continue (line 1059) ───────


class TestAutoCreateOffersDedup:
    def test_existing_offer_for_same_vr_and_mpn_skipped(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Existing offer with same vendor_response_id + mpn causes continue (line
        1059)."""
        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)

        # Pre-create the duplicate offer
        existing = Offer(
            requisition_id=test_requisition.id,
            vendor_name="TestVendor Inc",
            mpn="LM317T",
            vendor_response_id=vr.id,
            status="active",
        )
        db_session.add(existing)
        db_session.commit()

        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        with patch("app.services.response_parser.extract_draft_offers", return_value=[draft]):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        # Still just 1 offer — no duplicate created
        count = db_session.query(Offer).filter_by(vendor_response_id=vr.id).count()
        assert count == 1


# ── _auto_create_offers_from_parse: exception handlers ───────────────


class TestAutoCreateOffersExceptionHandlers:
    def _make_vr_and_draft(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        mpn: str = "LM999T",
    ):
        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        parsed = {"confidence": 0.9}
        draft = {"mpn": mpn, "vendor_name": "TestVendor Inc", "unit_price": 2.0}
        return vr, parsed, draft

    def test_task_service_exception_swallowed(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """on_email_offer_parsed raising does not propagate (lines 1107-1108)."""
        vr, parsed, draft = self._make_vr_and_draft(db_session, test_user, test_requisition, "LM991T")

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed", side_effect=RuntimeError("task boom")),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None

    def test_knowledge_capture_exception_swallowed(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """capture_offer_fact raising does not propagate (lines 1115-1116)."""
        vr, parsed, draft = self._make_vr_and_draft(db_session, test_user, test_requisition, "LM882T")

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact", side_effect=RuntimeError("knowledge boom")),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None

    def test_tag_propagation_exception_swallowed(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """propagate_tags_to_entity raising does not propagate (lines 1126-1127)."""
        from app.models import MaterialCard

        mc = MaterialCard(
            normalized_mpn="lm555t",
            display_mpn="LM555T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "LM555T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.search_service.resolve_material_card", return_value=mc),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.tagging.propagate_tags_to_entity", side_effect=RuntimeError("tag boom")),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None

    def test_offer_loop_exception_swallowed(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Exception inside the per-offer loop is caught (lines 1166-1167); no
        propagation."""
        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "LM404T", "vendor_name": "BadVendor"}
        parsed = {"confidence": 0.9}

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", side_effect=RuntimeError("tier boom")),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)


# ── _auto_create_offers_from_parse: strategic vendor clock (1131-1136) ─


class TestAutoCreateOffersStrategicVendorClock:
    def test_strategic_vendor_clock_exception_swallowed(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Exception in sv_record is swallowed (lines 1135-1136) when vendor_card_id is
        set."""
        from app.models import VendorCard

        vc = VendorCard(
            normalized_name="sv2vendor",
            display_name="StrategicVendor2",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc)
        db_session.flush()

        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "SV888", "vendor_name": "StrategicVendor2"}
        parsed = {"confidence": 0.9}

        original_init = Offer.__init__

        def patched_init(self_offer, **kwargs):
            original_init(self_offer, **kwargs)
            self_offer.vendor_card_id = vc.id

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.strategic_vendor_service.record_offer", side_effect=RuntimeError("sv boom")),
            patch.object(Offer, "__init__", patched_init),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)
        # No exception means test passes

    def test_strategic_vendor_clock_called_when_vendor_card_id_set(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """sv_record is called when offer.vendor_card_id is truthy (lines 1131-1134)."""
        from app.models import VendorCard

        vc = VendorCard(
            normalized_name="sv3vendor",
            display_name="StrategicVendor3",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc)
        db_session.flush()

        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "SV777", "vendor_name": "StrategicVendor3"}
        parsed = {"confidence": 0.9}
        mock_sv = MagicMock()

        original_init = Offer.__init__

        def patched_init(self_offer, **kwargs):
            original_init(self_offer, **kwargs)
            self_offer.vendor_card_id = vc.id

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.strategic_vendor_service.record_offer", mock_sv),
            patch.object(Offer, "__init__", patched_init),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        mock_sv.assert_called_once()


# ── _auto_create_offers_from_parse: existing ActivityLog update (1151-1154) ─


class TestAutoCreateOffersExistingNotification:
    def test_existing_unread_notification_updated_not_duplicated(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Unread offer_pending_review log is updated in place, not duplicated (lines
        1151-1154)."""
        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.6)

        existing_notif = ActivityLog(
            user_id=test_user.id,
            activity_type="offer_pending_review",
            channel="system",
            requisition_id=test_requisition.id,
            subject="Old subject -- OLD-MPN",
            dismissed_at=None,
        )
        db_session.add(existing_notif)
        db_session.commit()
        db_session.refresh(existing_notif)
        old_notif_id = existing_notif.id

        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.6}

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=2),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        # Flush so the in-session attribute updates are written before re-loading
        db_session.flush()
        db_session.expire(existing_notif)
        db_session.refresh(existing_notif)
        # Same row, updated subject
        assert existing_notif.id == old_notif_id
        assert "LM317T" in existing_notif.subject or "TestVendor" in existing_notif.subject

        # Exactly 1 notification — no duplicate
        count = (
            db_session.query(ActivityLog)
            .filter_by(
                user_id=test_user.id,
                activity_type="offer_pending_review",
                requisition_id=test_requisition.id,
            )
            .count()
        )
        assert count == 1


# ── _auto_create_offers_from_parse: SSE publish exception (1186-1187) ─


class TestAutoCreateOffersSSEException:
    def test_sse_broker_exception_swallowed(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Exception from broker.publish is swallowed (lines 1186-1187); no
        propagation."""
        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        mock_broker = MagicMock()
        mock_broker.publish.side_effect = RuntimeError("sse boom")

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None

    def test_sse_no_event_loop_swallowed(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """RuntimeError when getting event loop is swallowed (lines 1186-1187)."""
        import asyncio

        vr = _make_vendor_response(db_session, test_user, test_requisition, confidence=0.9)
        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch.object(asyncio, "get_event_loop", side_effect=RuntimeError("no loop")),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None


# ── Unsolicited (requisition_id=None) offer creation — fix regression ──────


def _make_unsolicited_vr(db: Session, user: User, confidence: float = 0.7) -> VendorResponse:
    """Create a VendorResponse with NO requisition (unsolicited inbound email)."""
    vr = VendorResponse(
        requisition_id=None,
        vendor_name="Unsolicited Vendor LLC",
        vendor_email="stocklist@unsolv.com",
        confidence=confidence,
        scanned_by_user_id=user.id,
        status="quote_provided",
        received_at=datetime.now(timezone.utc),
        message_id=f"unsol-msg-{id(user)}-{confidence}",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


class TestUnsolicitedOfferCreation:
    """Tests for the removed `and vr.requisition_id` guard.

    Before the fix, all unsolicited VendorResponses (requisition_id=None) were silently
    dropped by _auto_create_offers_from_parse even when confidence >= 0.5 and parsed
    parts had quoted prices.  These tests document the correct behavior after the fix.
    """

    def test_unsolicited_high_confidence_quoted_part_creates_offer(self, db_session: Session, test_user: User):
        """RED->GREEN: unsolicited VR with confidence >= 0.5, status='quoted',
        unit_price set, and MPN resolving to a material card MUST create an Offer with
        that material_card_id."""
        from app.models import MaterialCard

        mc = MaterialCard(
            normalized_mpn="abc123",
            display_mpn="ABC123",
            manufacturer="Acme",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        vr = _make_unsolicited_vr(db_session, test_user, confidence=0.75)
        parsed = {"confidence": 0.75}
        draft = {
            "mpn": "ABC123",
            "vendor_name": "Unsolicited Vendor LLC",
            "unit_price": 3.50,
            "status": "quoted",
        }

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.search_service.resolve_material_card", return_value=mc),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=2),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is not None, "Offer was not created for unsolicited high-confidence VR"
        assert offer.requisition_id is None
        assert offer.material_card_id == mc.id

    def test_low_confidence_unsolicited_creates_no_offer(self, db_session: Session, test_user: User):
        """Guard still holds: confidence < 0.5 must NOT create an Offer."""
        vr = _make_unsolicited_vr(db_session, test_user, confidence=0.3)
        parsed = {"confidence": 0.3}
        draft = {
            "mpn": "LOWCONF99",
            "vendor_name": "Unsolicited Vendor LLC",
            "unit_price": 1.0,
        }

        with patch("app.services.response_parser.extract_draft_offers", return_value=[draft]):
            _auto_create_offers_from_parse(vr, parsed, db_session)

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).first()
        assert offer is None, "Offer must NOT be created when confidence < 0.5"

    def test_null_req_dedup_scoped_per_vendor(self, db_session: Session, test_user: User):
        """Two unsolicited VRs from DIFFERENT vendors each produce their own
        notification -- the null-req dedup must NOT cross-suppress between vendors."""
        from app.models import MaterialCard

        mc = MaterialCard(
            normalized_mpn="xde789",
            display_mpn="XDE789",
            manufacturer="Test Corp",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        # VR from Vendor A
        vr_a = VendorResponse(
            requisition_id=None,
            vendor_name="Vendor Alpha",
            vendor_email="alpha@vendora.com",
            confidence=0.8,
            scanned_by_user_id=test_user.id,
            status="quote_provided",
            received_at=datetime.now(timezone.utc),
            message_id="unsol-a-001",
        )
        db_session.add(vr_a)

        # VR from Vendor B
        vr_b = VendorResponse(
            requisition_id=None,
            vendor_name="Vendor Beta",
            vendor_email="beta@vendorb.com",
            confidence=0.8,
            scanned_by_user_id=test_user.id,
            status="quote_provided",
            received_at=datetime.now(timezone.utc),
            message_id="unsol-b-001",
        )
        db_session.add(vr_b)
        db_session.commit()
        db_session.refresh(vr_a)
        db_session.refresh(vr_b)

        parsed = {"confidence": 0.8}

        # Fire VR from Vendor A (distinct mpn to avoid offer-level dedup)
        with (
            patch(
                "app.services.response_parser.extract_draft_offers",
                return_value=[{"mpn": "XDE789A", "vendor_name": "Vendor Alpha", "unit_price": 2.0}],
            ),
            patch("app.search_service.resolve_material_card", return_value=mc),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr_a, parsed, db_session)

        # Fire VR from Vendor B (distinct mpn)
        with (
            patch(
                "app.services.response_parser.extract_draft_offers",
                return_value=[{"mpn": "XDE789B", "vendor_name": "Vendor Beta", "unit_price": 2.0}],
            ),
            patch("app.search_service.resolve_material_card", return_value=mc),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
        ):
            _auto_create_offers_from_parse(vr_b, parsed, db_session)

        # Flush pending adds (auto-flush triggers before queries in production;
        # explicit flush ensures determinism in tests with no event-loop SSE code).
        db_session.flush()

        # Each vendor must have its own notification (not cross-suppressed)
        notifs = (
            db_session.query(ActivityLog)
            .filter_by(
                user_id=test_user.id,
                activity_type="offer_pending_review",
                requisition_id=None,
            )
            .all()
        )
        vendor_names = {n.contact_name for n in notifs}
        assert "Vendor Alpha" in vendor_names, "Vendor Alpha notification missing"
        assert "Vendor Beta" in vendor_names, "Vendor Beta notification missing"


class TestOnEmailOfferParsedNullReq:
    """Tests for task_service.on_email_offer_parsed handling requisition_id=None."""

    def test_none_requisition_id_skips_task_creation(self, db_session: Session):
        """on_email_offer_parsed with requisition_id=None returns without creating a
        task (avoids NOT NULL DB constraint on RequisitionTask.requisition_id)."""
        from app.models.task import RequisitionTask
        from app.services.task_service import on_email_offer_parsed

        on_email_offer_parsed(db_session, None, "SomeVendor", "MPN-XYZ", 9999)

        count = db_session.query(RequisitionTask).filter_by(source_ref="email_offer:9999").count()
        assert count == 0, "Task must NOT be created when requisition_id is None"
