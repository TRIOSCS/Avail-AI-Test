"""test_po_cancellation_service.py — Tests for app/services/po_cancellation_service.py.

Covers the seam contract: recording the immutable POCancellation fact, the idempotent
offer→SOLD transition (with ChangeLog + ActivityLog), the vendor-unavailable mapping,
and the VendorCard cancellation-metric refresh.

Called by: pytest
Depends on: app/services/po_cancellation_service.py, conftest fixtures
"""

from datetime import UTC, datetime, timedelta

from app.constants import POCancellationReason
from app.models import Offer, Requirement
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import ActivityLog, ChangeLog
from app.models.po_cancellation import POCancellation
from app.models.quotes import Quote
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.services.po_cancellation_service import (
    SLOW_CANCEL_THRESHOLD_DAYS,
    mark_offer_sold,
    mark_vendor_unavailable,
    record_po_cancellation,
    refresh_vendor_cancellation_metrics,
)
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name

# ── Helpers ──────────────────────────────────────────────────────────


def _requirement(db, req):
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def _make_offer(
    db, req, user, card, *, mpn="LM317T", normalized_mpn=None, vendor_name="Arrow Electronics", condition=None
):
    o = Offer(
        requisition_id=req.id,
        requirement_id=_requirement(db, req).id,
        vendor_card_id=card.id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn=mpn,
        normalized_mpn=normalized_mpn,
        qty_available=100,
        unit_price=1.00,
        entered_by_id=user.id,
        status="active",
        condition=condition,
        created_at=datetime.now(UTC),
    )
    db.add(o)
    db.flush()
    return o


def _make_line(db, req, *, po_confirmed_at, po_number="PO-123"):
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{datetime.now(UTC).timestamp()}",
        status="sent",
        line_items=[],
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(requisition_id=req.id, quote_id=q.id, status="active", created_at=datetime.now(UTC))
    db.add(bp)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=_requirement(db, req).id,
        quantity=10,
        status="awaiting_po",
        po_number=po_number,
        po_confirmed_at=po_confirmed_at,
    )
    db.add(line)
    db.flush()
    return line


def _make_cancellation(db, card, *, days_to_cancel, reason=POCancellationReason.OTHER.value):
    row = POCancellation(
        vendor_card_id=card.id,
        vendor_name_normalized="arrow electronics",
        normalized_mpn="LM317T",
        po_number="PO-X",
        cancelled_at=datetime.now(UTC),
        days_to_cancel=days_to_cancel,
        reason_code=reason,
    )
    db.add(row)
    db.flush()
    return row


# ═══════════════════════════════════════════════════════════════════════
#  record_po_cancellation
# ═══════════════════════════════════════════════════════════════════════


class TestRecordPoCancellation:
    def test_records_immutable_row_with_days_to_cancel(self, db_session, test_requisition, test_user, test_vendor_card):
        req = test_requisition
        offer = _make_offer(db_session, req, test_user, test_vendor_card)
        line = _make_line(db_session, req, po_confirmed_at=datetime.now(UTC) - timedelta(days=10))

        row = record_po_cancellation(
            db_session,
            line=line,
            offer=offer,
            requirement=_requirement(db_session, req),
            reason_code=POCancellationReason.NO_STOCK.value,
            reason_text="stock gone",
            user=test_user,
        )

        assert row.id is not None
        assert row.days_to_cancel == 10
        assert row.buy_plan_line_id == line.id
        assert row.buy_plan_id == line.buy_plan_id
        assert row.offer_id == offer.id
        assert row.vendor_card_id == test_vendor_card.id
        assert row.reason_code == "no_stock"
        assert row.reason_text == "stock gone"
        assert row.cancelled_by_id == test_user.id
        # @validates normalizes the keys through the canonical helpers
        assert row.vendor_name_normalized == normalize_vendor_name("Arrow Electronics")
        assert row.normalized_mpn == normalize_mpn_key("LM317T")

    def test_no_po_confirmed_at_gives_none_days(self, db_session, test_requisition, test_user, test_vendor_card):
        req = test_requisition
        offer = _make_offer(db_session, req, test_user, test_vendor_card)
        line = _make_line(db_session, req, po_confirmed_at=None)

        row = record_po_cancellation(
            db_session,
            line=line,
            offer=offer,
            requirement=_requirement(db_session, req),
            reason_code=POCancellationReason.OTHER.value,
            reason_text=None,
            user=test_user,
        )
        assert row.days_to_cancel is None
        assert row.po_cut_at is None

    def test_mpn_falls_back_to_requirement_primary(self, db_session, test_requisition, test_user, test_vendor_card):
        req = test_requisition
        # Offer with no usable MPN forces the requirement.primary_mpn fallback.
        offer = _make_offer(db_session, req, test_user, test_vendor_card, mpn="", normalized_mpn=None)
        line = _make_line(db_session, req, po_confirmed_at=None)

        row = record_po_cancellation(
            db_session,
            line=line,
            offer=offer,
            requirement=_requirement(db_session, req),
            reason_code=POCancellationReason.OTHER.value,
            reason_text=None,
            user=test_user,
        )
        assert row.normalized_mpn == normalize_mpn_key("LM317T")


# ═══════════════════════════════════════════════════════════════════════
#  mark_offer_sold
# ═══════════════════════════════════════════════════════════════════════


class TestMarkOfferSold:
    def test_transitions_to_sold_with_audit(self, db_session, test_requisition, test_user, test_vendor_card):
        offer = _make_offer(db_session, test_requisition, test_user, test_vendor_card)

        mark_offer_sold(db_session, offer, test_user)

        assert offer.status == "sold"
        changelog = db_session.query(ChangeLog).filter(ChangeLog.entity_id == offer.id).all()
        assert len(changelog) == 1
        assert changelog[0].field_name == "status"
        assert changelog[0].new_value == "sold"
        acts = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "offer_status_changed").all()
        assert len(acts) == 1
        assert acts[0].vendor_card_id == test_vendor_card.id

    def test_idempotent_when_already_sold(self, db_session, test_requisition, test_user, test_vendor_card):
        offer = _make_offer(db_session, test_requisition, test_user, test_vendor_card)
        mark_offer_sold(db_session, offer, test_user)
        # Second call is a no-op — no second ChangeLog row, no error.
        mark_offer_sold(db_session, offer, test_user)
        assert offer.status == "sold"
        assert db_session.query(ChangeLog).filter(ChangeLog.entity_id == offer.id).count() == 1


# ═══════════════════════════════════════════════════════════════════════
#  mark_vendor_unavailable
# ═══════════════════════════════════════════════════════════════════════


class TestMarkVendorUnavailable:
    def test_requirement_none_returns_zero(self, db_session, test_requisition, test_user, test_vendor_card):
        offer = _make_offer(db_session, test_requisition, test_user, test_vendor_card)
        assert (
            mark_vendor_unavailable(
                db_session, requirement=None, offer=offer, reason_code="no_stock", note=None, user=test_user
            )
            == 0
        )

    def test_records_unavailability_with_mapped_reason(self, db_session, test_requisition, test_user, test_vendor_card):
        req = test_requisition
        offer = _make_offer(db_session, req, test_user, test_vendor_card)

        count = mark_vendor_unavailable(
            db_session,
            requirement=_requirement(db_session, req),
            offer=offer,
            reason_code="no_stock",  # → not_really_there
            note="gone",
            user=test_user,
        )
        assert count >= 1
        db_session.flush()
        rec = (
            db_session.query(VendorPartUnavailability)
            .filter(VendorPartUnavailability.vendor_name_normalized == normalize_vendor_name("Arrow Electronics"))
            .first()
        )
        assert rec is not None
        assert rec.reason == "not_really_there"

    def test_condition_new_is_forwarded_to_unavailability_record(
        self, db_session, test_requisition, test_user, test_vendor_card
    ):
        """mark_vendor_unavailable must forward offer.condition into
        record_unavailability.

        A sold_elsewhere reason is condition-specific; when the offer has
        condition="new" the resulting VendorPartUnavailability row must store
        condition="new" (normalised). RED before Task-7 wires condition=offer.condition
        into the call site.
        """
        req = test_requisition
        offer = _make_offer(db_session, req, test_user, test_vendor_card, condition="new")

        count = mark_vendor_unavailable(
            db_session,
            requirement=_requirement(db_session, req),
            offer=offer,
            reason_code="sold_elsewhere",  # condition-specific reason
            note="sold new stock elsewhere",
            user=test_user,
        )
        assert count >= 1
        db_session.flush()
        rec = (
            db_session.query(VendorPartUnavailability)
            .filter(VendorPartUnavailability.vendor_name_normalized == normalize_vendor_name("Arrow Electronics"))
            .first()
        )
        assert rec is not None
        assert rec.condition == "new", (
            f"Expected condition='new' but got condition={rec.condition!r}. "
            "mark_vendor_unavailable must pass condition=offer.condition to record_unavailability."
        )


# ═══════════════════════════════════════════════════════════════════════
#  refresh_vendor_cancellation_metrics
# ═══════════════════════════════════════════════════════════════════════


class TestRefreshVendorCancellationMetrics:
    def test_missing_card_is_noop(self, db_session):
        # Must not raise.
        refresh_vendor_cancellation_metrics(db_session, 999999)

    def test_recomputes_rate_avg_and_slow_count(self, db_session, test_vendor_card):
        test_vendor_card.total_pos = 10
        db_session.flush()
        # 3 cancels: days 2 (fast), 10 (slow), 20 (slow) → slow_count=2
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=2)
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=10)
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=20)

        refresh_vendor_cancellation_metrics(db_session, test_vendor_card.id)

        assert test_vendor_card.cancellation_rate == 0.3  # 3/10
        assert test_vendor_card.avg_days_to_cancel == round((2 + 10 + 20) / 3, 1)
        assert test_vendor_card.slow_cancel_count == 2
        assert SLOW_CANCEL_THRESHOLD_DAYS == 7

    def test_rate_capped_at_one(self, db_session, test_vendor_card):
        test_vendor_card.total_pos = 2
        db_session.flush()
        for _ in range(5):
            _make_cancellation(db_session, test_vendor_card, days_to_cancel=1)
        refresh_vendor_cancellation_metrics(db_session, test_vendor_card.id)
        assert test_vendor_card.cancellation_rate == 1.0

    def test_no_total_pos_with_cancels_is_one(self, db_session, test_vendor_card):
        test_vendor_card.total_pos = 0
        db_session.flush()
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=3)
        refresh_vendor_cancellation_metrics(db_session, test_vendor_card.id)
        assert test_vendor_card.cancellation_rate == 1.0

    def test_no_cancels_no_pos_is_none(self, db_session, test_vendor_card):
        test_vendor_card.total_pos = 0
        db_session.flush()
        refresh_vendor_cancellation_metrics(db_session, test_vendor_card.id)
        assert test_vendor_card.cancellation_rate is None
        assert test_vendor_card.avg_days_to_cancel is None
        assert test_vendor_card.slow_cancel_count == 0

    def test_null_days_excluded_from_avg(self, db_session, test_vendor_card):
        test_vendor_card.total_pos = 5
        db_session.flush()
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=None)
        _make_cancellation(db_session, test_vendor_card, days_to_cancel=4)
        refresh_vendor_cancellation_metrics(db_session, test_vendor_card.id)
        assert test_vendor_card.cancellation_rate == 0.4  # 2 cancels / 5 pos
        assert test_vendor_card.avg_days_to_cancel == 4.0  # only the non-null day counts
        assert test_vendor_card.slow_cancel_count == 0
