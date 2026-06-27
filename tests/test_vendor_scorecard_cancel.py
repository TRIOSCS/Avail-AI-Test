"""test_vendor_scorecard_cancel.py — Windowed cancellation metrics for
vendor_scorecard.py.

Proves compute_vendor_scorecard derives a 90-day windowed cancellation_rate and
avg_days_to_cancel, and that compute_all_vendor_scorecards stores both onto the
VendorMetricsSnapshot. Lives in a NEW file (does not edit tests/test_vendor_scorecard.py).

Called by: pytest
Depends on: app/services/vendor_scorecard.py, app/services/po_cancellation_service.py, conftest
"""

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.constants import POCancellationReason
from app.models import VendorCard
from app.models.performance import VendorMetricsSnapshot
from app.models.po_cancellation import POCancellation
from app.services.vendor_scorecard import compute_all_vendor_scorecards, compute_vendor_scorecard

# ── Helpers ──────────────────────────────────────────────────────────


def _make_vendor(db: Session, *, total_pos=0) -> VendorCard:
    vc = VendorCard(
        normalized_name="cancel vendor",
        display_name="Cancel Vendor",
        emails=["c@vendor.com"],
        phones=[],
        domain="vendor.com",
        domain_aliases=[],
        total_pos=total_pos,
        created_at=datetime.now(timezone.utc),
    )
    db.add(vc)
    db.flush()
    return vc


def _add_cancel(db: Session, vc: VendorCard, days, *, cancelled_at=None):
    db.add(
        POCancellation(
            vendor_card_id=vc.id,
            vendor_name_normalized=vc.normalized_name,
            normalized_mpn="LM317T",
            po_number="PO",
            cancelled_at=cancelled_at or datetime.now(timezone.utc),
            days_to_cancel=days,
            reason_code=POCancellationReason.OTHER.value,
        )
    )
    db.flush()


# ── compute_vendor_scorecard ─────────────────────────────────────────


class TestScorecardCancellation:
    def test_no_cancels_no_pos_is_none(self, db_session):
        vc = _make_vendor(db_session, total_pos=0)
        db_session.commit()
        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["cancellation_rate"] is None
        assert result["avg_days_to_cancel"] is None

    def test_rate_over_total_pos_and_avg_days(self, db_session):
        vc = _make_vendor(db_session, total_pos=10)
        _add_cancel(db_session, vc, days=4)
        _add_cancel(db_session, vc, days=12)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        # No in-window PO offers → denominator falls back to total_pos (10).
        assert result["cancellation_rate"] == 0.2  # 2/10
        assert result["avg_days_to_cancel"] == 8.0  # mean(4, 12)

    def test_rate_capped_at_one(self, db_session):
        vc = _make_vendor(db_session, total_pos=1)
        _add_cancel(db_session, vc, days=2)
        _add_cancel(db_session, vc, days=3)
        db_session.commit()
        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["cancellation_rate"] == 1.0

    def test_null_days_excluded_from_avg(self, db_session):
        vc = _make_vendor(db_session, total_pos=4)
        _add_cancel(db_session, vc, days=None)
        _add_cancel(db_session, vc, days=6)
        db_session.commit()
        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["avg_days_to_cancel"] == 6.0


# ── compute_all_vendor_scorecards stores onto the snapshot ───────────


class TestScorecardSnapshotStores:
    def test_snapshot_persists_cancellation_metrics(self, db_session):
        vc = _make_vendor(db_session, total_pos=10)
        _add_cancel(db_session, vc, days=10)
        _add_cancel(db_session, vc, days=20)
        db_session.commit()

        compute_all_vendor_scorecards(db_session)

        snap = (
            db_session.query(VendorMetricsSnapshot)
            .filter(
                VendorMetricsSnapshot.vendor_card_id == vc.id,
                VendorMetricsSnapshot.snapshot_date == date.today(),
            )
            .first()
        )
        assert snap is not None
        assert snap.cancellation_rate == 0.2  # 2/10
        assert snap.avg_days_to_cancel == 15.0  # mean(10, 20)
