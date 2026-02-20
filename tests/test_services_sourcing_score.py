"""
test_services_sourcing_score.py -- Unit tests for app/services/sourcing_score.py

Covers:
- _sigmoid(): mathematical correctness, midpoint, steepness
- score_requirement(): composite scoring with various input combinations
- _color(): color band mapping (red/yellow/green)
- _build_signals(): per-signal breakdown for tooltip display
- _signal_level(): low/mid/good classification
- _empty_signals(): zeroed signals dict
- compute_requisition_score_fast(): lightweight aggregate scoring
- compute_requisition_scores(): DB-backed per-requirement scoring

Pure functions tested directly; compute_requisition_scores uses
in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/services/sourcing_score.py, conftest.py
"""

import math
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Contact,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorResponse,
)
from app.services.sourcing_score import (
    _build_signals,
    _color,
    _empty_signals,
    _sigmoid,
    _signal_level,
    compute_requisition_score_fast,
    compute_requisition_scores,
    score_requirement,
)


# ── 1. _sigmoid() ────────────────────────────────────────────────────


class TestSigmoid:
    def test_at_midpoint_returns_half(self):
        """At x == midpoint, sigmoid should return exactly 0.5."""
        assert _sigmoid(5.0, midpoint=5.0) == 0.5

    def test_at_midpoint_different_steepness(self):
        """Steepness doesn't affect value at midpoint."""
        assert _sigmoid(3.0, midpoint=3.0, steepness=0.5) == 0.5
        assert _sigmoid(3.0, midpoint=3.0, steepness=5.0) == 0.5

    def test_far_above_midpoint_approaches_one(self):
        """Large x relative to midpoint approaches 1."""
        result = _sigmoid(100.0, midpoint=2.0, steepness=1.0)
        assert result > 0.99

    def test_far_below_midpoint_approaches_zero(self):
        """Very negative x approaches 0."""
        result = _sigmoid(-100.0, midpoint=2.0, steepness=1.0)
        assert result < 0.01

    def test_zero_input(self):
        """x=0 with midpoint=2 should be below 0.5."""
        result = _sigmoid(0.0, midpoint=2.0, steepness=1.0)
        expected = 1 / (1 + math.exp(-1.0 * (0.0 - 2.0)))
        assert result == pytest.approx(expected)
        assert result < 0.5

    def test_higher_steepness_sharper_curve(self):
        """Higher steepness makes the sigmoid steeper around midpoint."""
        gentle = _sigmoid(3.0, midpoint=2.0, steepness=0.5)
        steep = _sigmoid(3.0, midpoint=2.0, steepness=5.0)
        # Both above 0.5 (since x > midpoint), but steep should be closer to 1
        assert gentle > 0.5
        assert steep > gentle
        assert steep > 0.9

    def test_negative_steepness_inverts(self):
        """Negative steepness inverts the curve."""
        normal = _sigmoid(5.0, midpoint=2.0, steepness=1.0)
        inverted = _sigmoid(5.0, midpoint=2.0, steepness=-1.0)
        assert normal == pytest.approx(1.0 - inverted)

    def test_symmetry_around_midpoint(self):
        """sigmoid(mid + d) + sigmoid(mid - d) == 1."""
        mid = 3.0
        d = 1.5
        above = _sigmoid(mid + d, midpoint=mid, steepness=1.0)
        below = _sigmoid(mid - d, midpoint=mid, steepness=1.0)
        assert above + below == pytest.approx(1.0)

    def test_returns_float(self):
        result = _sigmoid(1, midpoint=2, steepness=1)
        assert isinstance(result, float)

    def test_monotonically_increasing(self):
        """Sigmoid is monotonically increasing for positive steepness."""
        values = [_sigmoid(x, midpoint=2.0, steepness=1.0) for x in range(-5, 10)]
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1]


# ── 2. score_requirement() ───────────────────────────────────────────


class TestScoreRequirement:
    def test_all_zeros_returns_low_score(self):
        """No activity should produce a low score."""
        sc = score_requirement(
            sighting_count=0,
            offer_count=0,
            rfqs_per_part=0.0,
            reply_rate=0.0,
            calls_per_part=0.0,
            emails_per_part=0.0,
        )
        assert 0 <= sc <= 100
        # With all zeros, every sigmoid is evaluated at 0 which is below midpoint
        # so the overall score should be very low
        assert sc < 25  # red band

    def test_moderate_activity_mid_score(self):
        """Moderate activity should produce a yellow score."""
        sc = score_requirement(
            sighting_count=2,
            offer_count=1,
            rfqs_per_part=1.5,
            reply_rate=0.3,
            calls_per_part=0.3,
            emails_per_part=0.5,
        )
        # These are exactly at midpoints, so each sigmoid = 0.5
        # 0.5 * (0.15+0.15+0.20+0.20+0.15+0.15) * 100 = 0.5 * 1.0 * 100 = 50
        assert sc == pytest.approx(50.0, abs=1.0)

    def test_high_activity_high_score(self):
        """High activity across all channels should give a high score."""
        sc = score_requirement(
            sighting_count=10,
            offer_count=5,
            rfqs_per_part=5.0,
            reply_rate=0.8,
            calls_per_part=2.0,
            emails_per_part=3.0,
        )
        assert sc >= 60  # green band
        assert sc <= 100

    def test_score_capped_at_100(self):
        """Score should never exceed 100."""
        sc = score_requirement(
            sighting_count=1000,
            offer_count=1000,
            rfqs_per_part=1000.0,
            reply_rate=1.0,
            calls_per_part=1000.0,
            emails_per_part=1000.0,
        )
        assert sc <= 100

    def test_score_never_negative(self):
        """Score should never be negative, even with zero inputs."""
        sc = score_requirement(
            sighting_count=0,
            offer_count=0,
            rfqs_per_part=0.0,
            reply_rate=0.0,
            calls_per_part=0.0,
            emails_per_part=0.0,
        )
        assert sc >= 0

    def test_returns_rounded_float(self):
        """Score should be a float rounded to 1 decimal."""
        sc = score_requirement(
            sighting_count=3,
            offer_count=2,
            rfqs_per_part=2.0,
            reply_rate=0.5,
            calls_per_part=0.5,
            emails_per_part=1.0,
        )
        assert isinstance(sc, float)
        parts = str(sc).split(".")
        assert len(parts) <= 2
        if len(parts) == 2:
            assert len(parts[1]) <= 1

    def test_only_sightings_partial_score(self):
        """Having only sightings (no offers, rfqs, etc.) gives a higher score
        than zero activity but still below green threshold."""
        sc_none = score_requirement(
            sighting_count=0,
            offer_count=0,
            rfqs_per_part=0.0,
            reply_rate=0.0,
            calls_per_part=0.0,
            emails_per_part=0.0,
        )
        sc_sightings = score_requirement(
            sighting_count=5,
            offer_count=0,
            rfqs_per_part=0.0,
            reply_rate=0.0,
            calls_per_part=0.0,
            emails_per_part=0.0,
        )
        # Having sightings should meaningfully boost the score
        assert sc_sightings > sc_none
        # But sightings alone shouldn't reach green
        assert sc_sightings < 60

    def test_only_offers_partial_score(self):
        """Having only offers should boost the score partially."""
        sc_no_offers = score_requirement(0, 0, 0.0, 0.0, 0.0, 0.0)
        sc_with_offers = score_requirement(0, 3, 0.0, 0.0, 0.0, 0.0)
        assert sc_with_offers > sc_no_offers

    def test_reply_rate_effect(self):
        """Higher reply rate should increase the score."""
        sc_low = score_requirement(2, 1, 1.5, 0.0, 0.3, 0.5)
        sc_high = score_requirement(2, 1, 1.5, 0.8, 0.3, 0.5)
        assert sc_high > sc_low

    def test_calls_boost(self):
        """Calls should increase the score."""
        sc_no_calls = score_requirement(2, 1, 1.5, 0.3, 0.0, 0.5)
        sc_calls = score_requirement(2, 1, 1.5, 0.3, 1.0, 0.5)
        assert sc_calls > sc_no_calls

    def test_emails_boost(self):
        """Email exchanges should increase the score."""
        sc_no_emails = score_requirement(2, 1, 1.5, 0.3, 0.3, 0.0)
        sc_emails = score_requirement(2, 1, 1.5, 0.3, 0.3, 2.0)
        assert sc_emails > sc_no_emails

    def test_weight_sum_produces_max_near_100(self):
        """When all sigmoids are near 1.0, raw sum of weights * 100 ~ 100."""
        # Weights: 0.15 + 0.15 + 0.20 + 0.20 + 0.15 + 0.15 = 1.0
        # If all sigmoids = 1.0, score = 100
        sc = score_requirement(
            sighting_count=100,
            offer_count=100,
            rfqs_per_part=100.0,
            reply_rate=1.0,
            calls_per_part=100.0,
            emails_per_part=100.0,
        )
        assert sc >= 99.0


# ── 3. _color() ──────────────────────────────────────────────────────


class TestColor:
    def test_zero_is_red(self):
        assert _color(0) == "red"

    def test_below_25_is_red(self):
        assert _color(24.9) == "red"

    def test_exactly_25_is_yellow(self):
        assert _color(25) == "yellow"

    def test_mid_range_is_yellow(self):
        assert _color(40) == "yellow"
        assert _color(59.9) == "yellow"

    def test_exactly_60_is_green(self):
        assert _color(60) == "green"

    def test_high_score_is_green(self):
        assert _color(80) == "green"
        assert _color(100) == "green"

    def test_boundary_24_is_red(self):
        assert _color(24) == "red"

    def test_boundary_25_is_yellow(self):
        assert _color(25) == "yellow"

    def test_boundary_59_is_yellow(self):
        assert _color(59) == "yellow"

    def test_boundary_60_is_green(self):
        assert _color(60) == "green"


# ── 4. _signal_level() ──────────────────────────────────────────────


class TestSignalLevel:
    def test_low_range(self):
        assert _signal_level(0.0) == "low"
        assert _signal_level(0.1) == "low"
        assert _signal_level(0.34) == "low"

    def test_mid_range(self):
        assert _signal_level(0.35) == "mid"
        assert _signal_level(0.5) == "mid"
        assert _signal_level(0.64) == "mid"

    def test_good_range(self):
        assert _signal_level(0.65) == "good"
        assert _signal_level(0.8) == "good"
        assert _signal_level(1.0) == "good"

    def test_exact_boundaries(self):
        assert _signal_level(0.3499) == "low"
        assert _signal_level(0.35) == "mid"
        assert _signal_level(0.6499) == "mid"
        assert _signal_level(0.65) == "good"


# ── 5. _empty_signals() ─────────────────────────────────────────────


class TestEmptySignals:
    def test_structure(self):
        """Empty signals should have the correct keys."""
        s = _empty_signals()
        expected_keys = {"sources", "offers", "rfqs", "replies", "calls", "emails"}
        assert set(s.keys()) == expected_keys

    def test_all_values_zeroed(self):
        """All numeric values should be zero."""
        s = _empty_signals()
        for key in ["sources", "offers", "rfqs", "calls", "emails"]:
            assert s[key]["val"] == 0
            assert s[key]["parts"] == 0
            assert s[key]["pct"] == 0
            assert s[key]["level"] == "low"

    def test_replies_has_of_field(self):
        """Replies signal has 'of' instead of 'parts'."""
        s = _empty_signals()
        assert s["replies"]["val"] == 0
        assert s["replies"]["of"] == 0
        assert s["replies"]["pct"] == 0
        assert s["replies"]["level"] == "low"
        assert "parts" not in s["replies"]

    def test_returns_new_dict_each_call(self):
        """Each call should return a new dict (no shared mutable state)."""
        s1 = _empty_signals()
        s2 = _empty_signals()
        s1["sources"]["val"] = 99
        assert s2["sources"]["val"] == 0


# ── 6. _build_signals() ─────────────────────────────────────────────


class TestBuildSignals:
    def test_structure(self):
        """Build signals should have all expected keys."""
        s = _build_signals(
            sighting_count=3, offer_count=2,
            rfqs_per_part=1.0, reply_rate=0.3,
            calls_per_part=0.5, emails_per_part=0.5,
            raw_sourced=3, raw_rfqs=4, raw_replies=1,
            raw_offers=2, raw_calls=2, raw_emails=2, parts=4,
        )
        expected_keys = {"sources", "offers", "rfqs", "replies", "calls", "emails"}
        assert set(s.keys()) == expected_keys

    def test_raw_values_propagated(self):
        """Raw count values should be passed through to signals."""
        s = _build_signals(
            sighting_count=5, offer_count=3,
            rfqs_per_part=2.0, reply_rate=0.5,
            calls_per_part=1.0, emails_per_part=1.0,
            raw_sourced=5, raw_rfqs=10, raw_replies=5,
            raw_offers=3, raw_calls=4, raw_emails=4, parts=4,
        )
        assert s["sources"]["val"] == 5
        assert s["sources"]["parts"] == 4
        assert s["offers"]["val"] == 3
        assert s["rfqs"]["val"] == 10
        assert s["replies"]["val"] == 5
        assert s["replies"]["of"] == 10
        assert s["calls"]["val"] == 4
        assert s["emails"]["val"] == 4

    def test_pct_values_are_integers(self):
        """pct values should be rounded integers."""
        s = _build_signals(
            sighting_count=2, offer_count=1,
            rfqs_per_part=1.5, reply_rate=0.3,
            calls_per_part=0.3, emails_per_part=0.5,
        )
        for key in s:
            assert isinstance(s[key]["pct"], int)

    def test_pct_range(self):
        """All pct values should be in 0-100 range."""
        s = _build_signals(
            sighting_count=10, offer_count=5,
            rfqs_per_part=5.0, reply_rate=0.8,
            calls_per_part=2.0, emails_per_part=3.0,
        )
        for key in s:
            assert 0 <= s[key]["pct"] <= 100

    def test_at_midpoints_pct_around_50(self):
        """At midpoint values, sigmoid = 0.5, so pct ~ 50."""
        s = _build_signals(
            sighting_count=2,   # midpoint=2
            offer_count=1,      # midpoint=1 (but steepness=1.5)
            rfqs_per_part=1.5,  # midpoint=1.5
            reply_rate=0.3,     # reply_rate*5=1.5, midpoint=1.5
            calls_per_part=0.3, # midpoint=0.3
            emails_per_part=0.5, # midpoint=0.5
        )
        assert s["sources"]["pct"] == 50
        assert s["rfqs"]["pct"] == 50
        assert s["replies"]["pct"] == 50
        assert s["calls"]["pct"] == 50
        assert s["emails"]["pct"] == 50

    def test_level_classification(self):
        """Levels should be correctly classified based on sigmoid output."""
        # Very high values -> all levels should be "good"
        s = _build_signals(
            sighting_count=100, offer_count=100,
            rfqs_per_part=100.0, reply_rate=1.0,
            calls_per_part=100.0, emails_per_part=100.0,
        )
        for key in s:
            assert s[key]["level"] == "good"

    def test_zero_inputs_level_low(self):
        """Zero values should produce low-level signals (below midpoints)."""
        s = _build_signals(
            sighting_count=0, offer_count=0,
            rfqs_per_part=0.0, reply_rate=0.0,
            calls_per_part=0.0, emails_per_part=0.0,
        )
        for key in s:
            assert s[key]["level"] == "low"

    def test_default_raw_values(self):
        """When raw values are not passed, they default to 0."""
        s = _build_signals(
            sighting_count=5, offer_count=3,
            rfqs_per_part=2.0, reply_rate=0.5,
            calls_per_part=1.0, emails_per_part=1.0,
        )
        assert s["sources"]["val"] == 0  # default raw_sourced
        assert s["rfqs"]["val"] == 0     # default raw_rfqs
        assert s["sources"]["parts"] == 1  # default parts

    def test_replies_has_of_field(self):
        """Replies signal should have 'of' field referencing raw_rfqs."""
        s = _build_signals(
            sighting_count=0, offer_count=0,
            rfqs_per_part=0.0, reply_rate=0.0,
            calls_per_part=0.0, emails_per_part=0.0,
            raw_rfqs=15,
        )
        assert s["replies"]["of"] == 15


# ── 7. compute_requisition_score_fast() ──────────────────────────────


class TestComputeRequisitionScoreFast:
    def test_zero_requirements_returns_empty(self):
        """With 0 requirements, should return 0 score, red, empty signals."""
        sc, color, signals = compute_requisition_score_fast(
            req_count=0,
            sourced_count=0,
            rfq_sent_count=0,
            reply_count=0,
            offer_count=0,
        )
        assert sc == 0
        assert color == "red"
        assert signals == _empty_signals()

    def test_basic_scoring(self):
        """Non-zero inputs should produce a positive score."""
        sc, color, signals = compute_requisition_score_fast(
            req_count=5,
            sourced_count=3,
            rfq_sent_count=10,
            reply_count=3,
            offer_count=2,
            call_count=2,
            email_count=5,
        )
        assert sc > 0
        assert color in ("red", "yellow", "green")
        assert "sources" in signals

    def test_returns_tuple_of_three(self):
        """Should return a (score, color, signals) tuple."""
        result = compute_requisition_score_fast(
            req_count=2, sourced_count=1,
            rfq_sent_count=3, reply_count=1,
            offer_count=1,
        )
        assert len(result) == 3
        sc, color, signals = result
        assert isinstance(sc, float)
        assert isinstance(color, str)
        assert isinstance(signals, dict)

    def test_score_range(self):
        """Score should always be between 0 and 100."""
        sc, _, _ = compute_requisition_score_fast(
            req_count=10,
            sourced_count=100,
            rfq_sent_count=200,
            reply_count=100,
            offer_count=50,
            call_count=50,
            email_count=100,
        )
        assert 0 <= sc <= 100

    def test_signals_contain_raw_counts(self):
        """Signals should reflect the raw counts passed in."""
        _, _, signals = compute_requisition_score_fast(
            req_count=4,
            sourced_count=8,
            rfq_sent_count=12,
            reply_count=4,
            offer_count=3,
            call_count=6,
            email_count=10,
        )
        assert signals["sources"]["val"] == 8
        assert signals["rfqs"]["val"] == 12
        assert signals["replies"]["val"] == 4
        assert signals["replies"]["of"] == 12
        assert signals["offers"]["val"] == 3
        assert signals["calls"]["val"] == 6
        assert signals["emails"]["val"] == 10
        assert signals["sources"]["parts"] == 4

    def test_color_matches_score(self):
        """Color should be consistent with the score value."""
        # Zero activity => low score => red
        sc, color, _ = compute_requisition_score_fast(
            req_count=1, sourced_count=0,
            rfq_sent_count=0, reply_count=0,
            offer_count=0,
        )
        assert color == _color(sc)

    def test_no_rfqs_zero_reply_rate(self):
        """When rfq_sent_count=0, reply_rate should be 0 (no division by zero)."""
        sc, color, _ = compute_requisition_score_fast(
            req_count=5,
            sourced_count=2,
            rfq_sent_count=0,
            reply_count=0,
            offer_count=1,
        )
        assert sc >= 0

    def test_high_activity_approaches_green(self):
        """High activity across all channels should push toward green."""
        sc, color, _ = compute_requisition_score_fast(
            req_count=3,
            sourced_count=15,
            rfq_sent_count=20,
            reply_count=10,
            offer_count=10,
            call_count=10,
            email_count=15,
        )
        assert sc >= 50
        assert color in ("yellow", "green")

    def test_default_call_email_counts(self):
        """call_count and email_count default to 0."""
        sc1, _, _ = compute_requisition_score_fast(
            req_count=3, sourced_count=5,
            rfq_sent_count=6, reply_count=2,
            offer_count=2,
        )
        sc2, _, _ = compute_requisition_score_fast(
            req_count=3, sourced_count=5,
            rfq_sent_count=6, reply_count=2,
            offer_count=2, call_count=0, email_count=0,
        )
        assert sc1 == sc2

    def test_sighting_count_derived_from_ratio(self):
        """Internal sighting_count = int(sourced_ratio * 5)."""
        # With req_count=5, sourced_count=5 -> sourced_ratio=1.0 -> sighting_count=5
        sc_high, _, _ = compute_requisition_score_fast(
            req_count=5, sourced_count=5,
            rfq_sent_count=0, reply_count=0, offer_count=0,
        )
        # With req_count=5, sourced_count=0 -> sighting_count=0
        sc_low, _, _ = compute_requisition_score_fast(
            req_count=5, sourced_count=0,
            rfq_sent_count=0, reply_count=0, offer_count=0,
        )
        assert sc_high > sc_low


# ── 8. compute_requisition_scores() — DB integration ────────────────


class TestComputeRequisitionScores:
    """Tests for compute_requisition_scores using in-memory SQLite."""

    def test_no_requirements_returns_empty(self, db_session: Session, test_user: User):
        """Requisition with no requirements returns 0 score and empty list."""
        req = Requisition(
            name="EMPTY-REQ",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert result["requisition_score"] == 0
        assert result["requisition_color"] == "red"
        assert result["requirements"] == []

    def test_nonexistent_requisition_returns_empty(self, db_session: Session):
        """A requisition_id that doesn't exist returns 0 score."""
        result = compute_requisition_scores(99999, db_session)
        assert result["requisition_score"] == 0
        assert result["requisition_color"] == "red"
        assert result["requirements"] == []

    def test_single_requirement_no_activity(self, db_session: Session, test_user: User):
        """Requirement with zero activity should get a low (red) score."""
        req = Requisition(
            name="REQ-NOACTIVITY",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert result["requisition_score"] < 25
        assert result["requisition_color"] == "red"
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["mpn"] == "ABC123"
        assert result["requirements"][0]["color"] == "red"

    def test_requirement_with_sightings(self, db_session: Session, test_user: User):
        """Sightings should increase the per-requirement score."""
        req = Requisition(
            name="REQ-SIGHTINGS",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="DEF456",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        # Add sightings
        for i in range(5):
            sighting = Sighting(
                requirement_id=item.id,
                vendor_name=f"Vendor {i}",
                mpn_matched="DEF456",
                qty_available=100,
                source_type="api",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(sighting)
        db_session.commit()

        result_with = compute_requisition_scores(req.id, db_session)

        # Compare against baseline (no sightings, just the structure check)
        assert result_with["requirements"][0]["score"] > 0
        assert result_with["requirements"][0]["mpn"] == "DEF456"

    def test_full_activity_produces_higher_score(self, db_session: Session, test_user: User):
        """Full activity (sightings, offers, RFQs, replies, calls, emails) should
        produce a significantly higher score than no activity."""
        req = Requisition(
            name="REQ-FULL",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="FULL-MPN",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        # Sightings
        for i in range(5):
            db_session.add(Sighting(
                requirement_id=item.id,
                vendor_name=f"Vendor {i}",
                mpn_matched="FULL-MPN",
                qty_available=100,
                source_type="api",
                created_at=datetime.now(timezone.utc),
            ))

        # Offers
        for i in range(3):
            db_session.add(Offer(
                requisition_id=req.id,
                requirement_id=item.id,
                vendor_name=f"Vendor {i}",
                mpn="FULL-MPN",
                qty_available=100,
                unit_price=1.50,
                entered_by_id=test_user.id,
                status="active",
                created_at=datetime.now(timezone.utc),
            ))

        # RFQs (Contacts with status="sent")
        for i in range(4):
            db_session.add(Contact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="rfq",
                vendor_name=f"Vendor {i}",
                status="sent",
                created_at=datetime.now(timezone.utc),
            ))

        # Vendor responses
        for i in range(2):
            db_session.add(VendorResponse(
                requisition_id=req.id,
                vendor_name=f"Vendor {i}",
                vendor_email=f"vendor{i}@test.com",
                status="new",
                created_at=datetime.now(timezone.utc),
            ))

        # Phone calls
        for i in range(3):
            db_session.add(ActivityLog(
                user_id=test_user.id,
                activity_type="call",
                channel="phone",
                requisition_id=req.id,
                subject=f"Call {i}",
                created_at=datetime.now(timezone.utc),
            ))

        # Emails
        for i in range(5):
            db_session.add(ActivityLog(
                user_id=test_user.id,
                activity_type="email_sent",
                channel="email",
                requisition_id=req.id,
                subject=f"Email {i}",
                created_at=datetime.now(timezone.utc),
            ))

        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert result["requisition_score"] > 40
        assert result["requisition_color"] in ("yellow", "green")
        assert len(result["requirements"]) == 1

    def test_multiple_requirements_averaged(self, db_session: Session, test_user: User):
        """Score of multiple requirements should be the average."""
        req = Requisition(
            name="REQ-MULTI",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        # Requirement 1: some sightings
        item1 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-A",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item1)
        db_session.flush()

        for i in range(5):
            db_session.add(Sighting(
                requirement_id=item1.id,
                vendor_name=f"Vendor {i}",
                mpn_matched="MPN-A",
                qty_available=100,
                source_type="api",
                created_at=datetime.now(timezone.utc),
            ))

        # Requirement 2: no sightings
        item2 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-B",
            target_qty=200,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert len(result["requirements"]) == 2

        scores = [r["score"] for r in result["requirements"]]
        expected_avg = round(sum(scores) / len(scores), 1)
        assert result["requisition_score"] == expected_avg

    def test_result_structure(self, db_session: Session, test_user: User):
        """Verify the structure of the returned dict."""
        req = Requisition(
            name="REQ-STRUCT",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="STRUCT-MPN",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)

        # Top-level keys
        assert "requisition_score" in result
        assert "requisition_color" in result
        assert "requirements" in result

        # Per-requirement keys
        req_result = result["requirements"][0]
        assert "requirement_id" in req_result
        assert "mpn" in req_result
        assert "score" in req_result
        assert "color" in req_result
        assert "signals" in req_result

        # Signals structure
        signals = req_result["signals"]
        assert "sources" in signals
        assert "offers" in signals
        assert "rfqs" in signals
        assert "replies" in signals
        assert "calls" in signals
        assert "emails" in signals

    def test_null_primary_mpn(self, db_session: Session, test_user: User):
        """Requirement with null primary_mpn should return empty string."""
        req = Requisition(
            name="REQ-NULL-MPN",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn=None,
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert result["requirements"][0]["mpn"] == ""

    def test_color_consistency(self, db_session: Session, test_user: User):
        """Requisition color should match _color() applied to the average score."""
        req = Requisition(
            name="REQ-COLOR",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="COLOR-MPN",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        assert result["requisition_color"] == _color(result["requisition_score"])

    def test_only_counts_sent_contacts_as_rfqs(self, db_session: Session, test_user: User):
        """Only contacts with status='sent' should count as RFQs."""
        req = Requisition(
            name="REQ-RFQFILTER",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="RFQ-MPN",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        # One sent RFQ
        db_session.add(Contact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Vendor A",
            status="sent",
            created_at=datetime.now(timezone.utc),
        ))
        # One draft RFQ (should NOT be counted)
        db_session.add(Contact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Vendor B",
            status="draft",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        signals = result["requirements"][0]["signals"]
        # Only 1 RFQ counted, not 2
        assert signals["rfqs"]["val"] == 1

    def test_phone_vs_email_channel_filtering(self, db_session: Session, test_user: User):
        """Only channel='phone' counts for calls, channel='email' for emails."""
        req = Requisition(
            name="REQ-CHANNELS",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="CHAN-MPN",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        # 2 phone calls
        for i in range(2):
            db_session.add(ActivityLog(
                user_id=test_user.id,
                activity_type="call",
                channel="phone",
                requisition_id=req.id,
                subject=f"Call {i}",
                created_at=datetime.now(timezone.utc),
            ))

        # 3 emails
        for i in range(3):
            db_session.add(ActivityLog(
                user_id=test_user.id,
                activity_type="email_sent",
                channel="email",
                requisition_id=req.id,
                subject=f"Email {i}",
                created_at=datetime.now(timezone.utc),
            ))

        # 1 system channel (should NOT count as phone or email)
        db_session.add(ActivityLog(
            user_id=test_user.id,
            activity_type="system_event",
            channel="system",
            requisition_id=req.id,
            subject="System event",
            created_at=datetime.now(timezone.utc),
        ))

        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        signals = result["requirements"][0]["signals"]
        assert signals["calls"]["val"] == 2
        assert signals["emails"]["val"] == 3

    def test_offers_per_requirement(self, db_session: Session, test_user: User):
        """Offers are counted per-requirement, not shared."""
        req = Requisition(
            name="REQ-OFFERS",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item1 = Requirement(
            requisition_id=req.id,
            primary_mpn="OFFER-A",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        item2 = Requirement(
            requisition_id=req.id,
            primary_mpn="OFFER-B",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([item1, item2])
        db_session.flush()

        # 3 offers for item1, 0 for item2
        for i in range(3):
            db_session.add(Offer(
                requisition_id=req.id,
                requirement_id=item1.id,
                vendor_name=f"Vendor {i}",
                mpn="OFFER-A",
                qty_available=100,
                unit_price=1.00,
                entered_by_id=test_user.id,
                status="active",
                created_at=datetime.now(timezone.utc),
            ))
        db_session.commit()

        result = compute_requisition_scores(req.id, db_session)
        req_map = {r["mpn"]: r for r in result["requirements"]}
        assert req_map["OFFER-A"]["score"] > req_map["OFFER-B"]["score"]
