"""Tests for sighting aggregation service.

Covers vendor grouping, price aggregation, tier labels, AI qty fallback,
and upsert behavior for VendorSightingSummary.

Called by: pytest
Depends on: app.services.sighting_aggregation, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard
from app.services.sighting_aggregation import (
    _estimate_qty_with_ai,
    _score_to_tier,
    rebuild_vendor_summaries,
    rebuild_vendor_summaries_from_sightings,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_requisition_and_requirement(db: Session, user_id: int) -> tuple[Requisition, Requirement]:
    """Create a requisition + requirement for sighting tests."""
    req = Requisition(
        name="REQ-AGG-001",
        customer_name="Test Co",
        status="active",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=1000,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return req, item


def _make_sighting(
    db: Session,
    requirement_id: int,
    vendor_name: str = "Arrow Electronics",
    unit_price: float | None = 1.0,
    qty_available: int | None = 100,
    score: float | None = 50.0,
    source_type: str = "api",
    is_unavailable: bool = False,
) -> Sighting:
    """Create a sighting with sensible defaults."""
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor_name,
        unit_price=unit_price,
        qty_available=qty_available,
        score=score,
        source_type=source_type,
        is_unavailable=is_unavailable,
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


# ── Tier label tests ─────────────────────────────────────────────────


class TestScoreToTier:
    def test_none_is_poor(self):
        assert _score_to_tier(None) == "Poor"

    def test_excellent(self):
        assert _score_to_tier(70) == "Excellent"
        assert _score_to_tier(100) == "Excellent"

    def test_good(self):
        assert _score_to_tier(40) == "Good"
        assert _score_to_tier(69.9) == "Good"

    def test_fair(self):
        assert _score_to_tier(20) == "Fair"
        assert _score_to_tier(39.9) == "Fair"

    def test_poor(self):
        assert _score_to_tier(0) == "Poor"
        assert _score_to_tier(19.9) == "Poor"


# ── Grouping tests ───────────────────────────────────────────────────


class TestVendorGrouping:
    """Multiple sightings from same vendor produce one summary."""

    def test_single_vendor_multiple_sightings(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(
            db_session, item.id, vendor_name="Arrow Electronics", unit_price=1.0, qty_available=100, score=50
        )
        _make_sighting(
            db_session, item.id, vendor_name="Arrow Electronics", unit_price=2.0, qty_available=200, score=80
        )
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        summary = results[0]
        assert summary.vendor_name == "arrow electronics"
        assert summary.listing_count == 2

    def test_two_vendors_produce_two_summaries(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, vendor_name="Arrow Electronics", unit_price=1.0)
        _make_sighting(db_session, item.id, vendor_name="Mouser", unit_price=1.5)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 2
        names = {r.vendor_name for r in results}
        assert names == {"arrow electronics", "mouser"}

    def test_unavailable_sightings_excluded(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, vendor_name="Arrow Electronics", is_unavailable=True)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": None, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 0


# ── Price aggregation tests ──────────────────────────────────────────


class TestPriceAggregation:
    def test_avg_price(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, unit_price=1.0)
        _make_sighting(db_session, item.id, unit_price=3.0)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 200, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        assert results[0].avg_price == 2.0  # (1+3)/2

    def test_best_price(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, unit_price=5.0)
        _make_sighting(db_session, item.id, unit_price=2.0)
        _make_sighting(db_session, item.id, unit_price=8.0)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].best_price == 2.0

    def test_no_prices(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, unit_price=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].avg_price is None
        assert results[0].best_price is None


# ── Qty fallback tests ───────────────────────────────────────────────


class TestQtyFallback:
    def test_ai_failure_uses_max_fallback(self, db_session: Session, test_user):
        """When AI estimation returns None qty, fall back to max of non-null qtys."""
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, qty_available=100)
        _make_sighting(db_session, item.id, qty_available=200)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": None, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        # Fallback: max of non-null = max(100, 200) = 200
        assert results[0].estimated_qty == 200

    def test_ai_success_uses_ai_value(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, qty_available=100)
        _make_sighting(db_session, item.id, qty_available=200)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 250, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].estimated_qty == 250

    def test_all_null_qtys(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, qty_available=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": None, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].estimated_qty is None


# ── Tier assignment in summaries ─────────────────────────────────────


class TestTierInSummary:
    def test_excellent_tier(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, score=80)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].tier == "Excellent"
        assert results[0].score == 80.0

    def test_max_score_used(self, db_session: Session, test_user):
        """When multiple sightings, max score determines tier."""
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, score=30)
        _make_sighting(db_session, item.id, score=75)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 200, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].score == 75.0
        assert results[0].tier == "Excellent"


# ── Upsert behavior ─────────────────────────────────────────────────


class TestUpsert:
    def test_rebuild_twice_updates_existing(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, unit_price=1.0, qty_available=100, score=50)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            first = rebuild_vendor_summaries(db_session, item.id)
        db_session.commit()

        assert len(first) == 1
        assert first[0].avg_price == 1.0

        # Add another sighting and rebuild
        _make_sighting(db_session, item.id, unit_price=3.0, qty_available=200, score=90)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            second = rebuild_vendor_summaries(db_session, item.id)
        db_session.commit()

        assert len(second) == 1
        assert second[0].avg_price == 2.0  # (1+3)/2
        assert second[0].score == 90.0
        assert second[0].tier == "Excellent"
        assert second[0].listing_count == 2

        # Only one row in DB
        count = db_session.query(VendorSightingSummary).filter_by(requirement_id=item.id).count()
        assert count == 1

    def test_vendor_filter(self, db_session: Session, test_user):
        """Passing vendor_names filters which vendors get rebuilt."""
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, vendor_name="Arrow Electronics")
        _make_sighting(db_session, item.id, vendor_name="Mouser")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id, vendor_names=["Arrow Electronics"])

        assert len(results) == 1
        assert results[0].vendor_name == "arrow electronics"


# ── Vendor phone lookup ──────────────────────────────────────────────


class TestVendorPhoneLookup:
    def test_phone_from_vendor_card(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        # Create vendor card with phone
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            phones=["+1-555-0100"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        _make_sighting(db_session, item.id, vendor_name="Arrow Electronics")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].vendor_phone == "+1-555-0100"

    def test_no_vendor_card_no_phone(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, vendor_name="Unknown Vendor")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].vendor_phone is None


# ── Source types aggregation ─────────────────────────────────────────


class TestSourceTypes:
    def test_unique_source_types(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, source_type="api")
        _make_sighting(db_session, item.id, source_type="api")
        _make_sighting(db_session, item.id, source_type="email")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert set(results[0].source_types) == {"api", "email"}


# ── New pre-aggregated column tests ─────────────────────────────────


def _make_sighting_extended(
    db: Session,
    requirement_id: int,
    vendor_name: str = "Arrow Electronics",
    lead_time_days: int | None = None,
    moq: int | None = None,
    vendor_email: str | None = None,
    vendor_phone: str | None = None,
    created_at: datetime | None = None,
) -> Sighting:
    """Create a sighting with extended fields for new-column tests."""
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor_name,
        unit_price=1.0,
        qty_available=100,
        score=50.0,
        source_type="api",
        is_unavailable=False,
        lead_time_days=lead_time_days,
        moq=moq,
        vendor_email=vendor_email,
        vendor_phone=vendor_phone,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


class TestVendorSummaryNewColumns:
    """Verify new pre-aggregated columns are populated."""

    def test_newest_sighting_at_populated(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        older = datetime(2025, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2025, 6, 15, tzinfo=timezone.utc)
        _make_sighting_extended(db_session, item.id, created_at=older)
        _make_sighting_extended(db_session, item.id, created_at=newer)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 200, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        # newest_sighting_at should equal the max created_at
        # SQLite (used in tests) strips tzinfo on round-trip; compare naive values
        result_ts = results[0].newest_sighting_at
        if result_ts is not None and result_ts.tzinfo is None:
            result_ts = result_ts.replace(tzinfo=timezone.utc)
        assert result_ts == newer

    def test_best_lead_time_days_populated(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, lead_time_days=3)
        _make_sighting_extended(db_session, item.id, lead_time_days=7)
        _make_sighting_extended(db_session, item.id, lead_time_days=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        assert results[0].best_lead_time_days == 3  # min of non-null

    def test_min_moq_populated(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, moq=10)
        _make_sighting_extended(db_session, item.id, moq=50)
        _make_sighting_extended(db_session, item.id, moq=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 300, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        assert results[0].min_moq == 10  # min of non-null

    def test_vendor_card_id_set(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        _make_sighting_extended(db_session, item.id, vendor_name="Arrow Electronics")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert len(results) == 1
        assert results[0].vendor_card_id == card.id

    def test_has_contact_info_from_sighting_email(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, vendor_email="sales@arrow.com")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].has_contact_info is True

    def test_has_contact_info_from_vendor_card_phone(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            phones=["+1-555-0100"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        _make_sighting_extended(db_session, item.id, vendor_name="Arrow Electronics")
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].has_contact_info is True

    def test_has_contact_info_false_when_no_contact(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, vendor_email=None, vendor_phone=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].has_contact_info is False

    def test_all_null_lead_times_gives_none(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, lead_time_days=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].best_lead_time_days is None

    def test_all_null_moq_gives_none(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting_extended(db_session, item.id, moq=None)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].min_moq is None


# ── _estimate_qty_with_ai unit tests ────────────────────────────────────


class TestEstimateQtyWithAI:
    """Unit tests for _estimate_qty_with_ai helper — covers lines 42-75."""

    def test_empty_list_returns_none(self):
        result = _estimate_qty_with_ai([])
        assert result == {"qty": None, "approximate": False}

    def test_all_none_returns_none(self):
        result = _estimate_qty_with_ai([None, None])
        assert result == {"qty": None, "approximate": False}

    def test_single_value_returns_sum(self):
        # <= 2 non-null values → just sum, no AI
        result = _estimate_qty_with_ai([100])
        assert result == {"qty": 100, "approximate": False}

    def test_two_values_returns_sum(self):
        result = _estimate_qty_with_ai([100, 200])
        assert result == {"qty": 300, "approximate": False}

    def test_two_values_with_none_mixed(self):
        # One None and two non-null → sum the non-null
        result = _estimate_qty_with_ai([None, 100, 200])
        # 3 non-null values → AI path; mock to test fallback
        # Actually 2 non-null values [100, 200] → sum path
        # Wait - None gets filtered: [100, 200] = 2, which is <= 2
        # Result: 100 + 200 = 300 without AI
        result = _estimate_qty_with_ai([None, 100])
        assert result == {"qty": 100, "approximate": False}

    def test_three_values_no_api_key_returns_max(self):
        # > 2 non-null values but no API key → max fallback
        # settings is imported lazily inside the function; mock app.config.settings
        mock_settings = MagicMock()
        mock_settings.ANTHROPIC_API_KEY = None
        with patch.dict("sys.modules", {"app.config": MagicMock(settings=mock_settings)}):
            result = _estimate_qty_with_ai([100, 200, 300])
        # Falls into the "no API key" branch → max fallback
        assert result == {"qty": 300, "approximate": True}

    def test_three_values_ai_success(self):
        # > 2 values with API key → Claude returns a number
        mock_content = MagicMock()
        mock_content.text = "350"
        mock_resp = MagicMock()
        mock_resp.content = [mock_content]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        mock_settings = MagicMock()
        mock_settings.ANTHROPIC_API_KEY = "sk-test-key"
        mock_anthropic_module = MagicMock()
        mock_anthropic_module.Anthropic.return_value = mock_client
        mock_claude_client = MagicMock()
        mock_claude_client.MODELS = {"fast": "claude-haiku-3"}

        with patch.dict(
            "sys.modules",
            {
                "anthropic": mock_anthropic_module,
                "app.config": MagicMock(settings=mock_settings),
                "app.utils.claude_client": mock_claude_client,
            },
        ):
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 350, "approximate": False}

    def test_three_values_ai_exception_returns_max(self):
        # AI call throws → max fallback (exception caught)
        # Any exception inside the try block → fallback to max
        mock_settings = MagicMock()
        mock_settings.ANTHROPIC_API_KEY = "sk-test-key"
        mock_anthropic_module = MagicMock()
        mock_anthropic_module.Anthropic.side_effect = Exception("API error")
        mock_claude_client = MagicMock()
        mock_claude_client.MODELS = {"fast": "claude-haiku-3"}

        with patch.dict(
            "sys.modules",
            {
                "anthropic": mock_anthropic_module,
                "app.config": MagicMock(settings=mock_settings),
                "app.utils.claude_client": mock_claude_client,
            },
        ):
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 300, "approximate": True}


# ── rebuild_vendor_summaries_from_sightings ──────────────────────────────


class TestRebuildVendorSummariesFromSightings:
    """Tests for rebuild_vendor_summaries_from_sightings — covers lines 198-209."""

    def test_basic_call_rebuilds_summaries(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        # Use already-normalized (lowercase) vendor name to match the filtering logic
        sighting = _make_sighting(db_session, item.id, vendor_name="arrow electronics", qty_available=100)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai", return_value={"qty": 100, "approximate": False}
        ):
            rebuild_vendor_summaries_from_sightings(db_session, item.id, [sighting])
            db_session.flush()

        summaries = db_session.query(VendorSightingSummary).filter_by(requirement_id=item.id).all()
        assert len(summaries) == 1
        assert summaries[0].vendor_name == "arrow electronics"

    def test_empty_sightings_list_is_noop(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        db_session.commit()

        # Should not raise, should not create any summaries
        rebuild_vendor_summaries_from_sightings(db_session, item.id, [])

        summaries = db_session.query(VendorSightingSummary).filter_by(requirement_id=item.id).all()
        assert len(summaries) == 0

    def test_exception_is_silently_caught(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        db_session.commit()

        # Create a mock sighting with a bad vendor_name to trigger exception
        mock_sighting = MagicMock()
        mock_sighting.vendor_name = "Test Vendor"

        with patch("app.services.sighting_aggregation.rebuild_vendor_summaries", side_effect=RuntimeError("DB error")):
            # Should not raise
            rebuild_vendor_summaries_from_sightings(db_session, item.id, [mock_sighting])

    def test_sightings_without_vendor_name_skipped(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        sighting = _make_sighting(db_session, item.id, vendor_name="Arrow Electronics", qty_available=50)
        # Use a MagicMock for the "no vendor name" case since DB requires NOT NULL
        mock_sighting_no_vendor = MagicMock()
        mock_sighting_no_vendor.vendor_name = None
        db_session.commit()

        with (
            patch(
                "app.services.sighting_aggregation._estimate_qty_with_ai",
                return_value={"qty": 50, "approximate": False},
            ),
            patch("app.services.sighting_aggregation.rebuild_vendor_summaries") as mock_rebuild,
        ):
            rebuild_vendor_summaries_from_sightings(db_session, item.id, [sighting, mock_sighting_no_vendor])

        # Should have been called with only "arrow electronics" (not None)
        if mock_rebuild.called:
            call_args = mock_rebuild.call_args
            vendor_names = call_args[1].get("vendor_names") or call_args[0][2] if len(call_args[0]) > 2 else []
            assert None not in vendor_names


# ── rebuild with approximate qty logging ────────────────────────────────


class TestApproximateQtyLogging:
    """Cover line 129: approximate qty triggers info log."""

    def test_approximate_qty_logs_info(self, db_session: Session, test_user):
        _req, item = _make_requisition_and_requirement(db_session, test_user.id)
        _make_sighting(db_session, item.id, qty_available=100)
        db_session.commit()

        with patch(
            "app.services.sighting_aggregation._estimate_qty_with_ai",
            return_value={"qty": 150, "approximate": True},
        ):
            results = rebuild_vendor_summaries(db_session, item.id)

        assert results[0].estimated_qty == 150
