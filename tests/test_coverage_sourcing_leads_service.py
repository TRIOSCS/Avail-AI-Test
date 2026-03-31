"""test_coverage_sourcing_leads_service.py — Tests for app/services/sourcing_leads.py utility functions.

Focuses on pure functions that don't require database access.

Called by: pytest
Depends on: app.services.sourcing_leads
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

# ── normalize_mpn ─────────────────────────────────────────────────────


class TestNormalizeMpn:
    def test_none_returns_empty(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn(None) == ""

    def test_empty_returns_empty(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("") == ""

    def test_uppercase(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("lm317t") == "LM317T"

    def test_removes_dashes(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("LM-317T") == "LM317T"

    def test_removes_spaces(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("LM 317T") == "LM317T"

    def test_removes_slashes(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("BC/547") == "BC547"

    def test_removes_dots(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("BC.547") == "BC547"

    def test_removes_underscores(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("BC_547") == "BC547"

    def test_complex_mpn(self):
        from app.services.sourcing_leads import normalize_mpn

        assert normalize_mpn("  AT-mega328p/PU  ") == "ATMEGA328PPU"


# ── _normalize_phone ──────────────────────────────────────────────────


class TestNormalizePhone:
    def test_none_returns_empty(self):
        from app.services.sourcing_leads import _normalize_phone

        assert _normalize_phone(None) == ""

    def test_empty_returns_empty(self):
        from app.services.sourcing_leads import _normalize_phone

        assert _normalize_phone("") == ""

    def test_strips_non_digits(self):
        from app.services.sourcing_leads import _normalize_phone

        assert _normalize_phone("+1 (555) 123-4567") == "15551234567"

    def test_plain_digits_preserved(self):
        from app.services.sourcing_leads import _normalize_phone

        assert _normalize_phone("5551234567") == "5551234567"


# ── _clamp ────────────────────────────────────────────────────────────


class TestClamp:
    def test_within_range(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(50.0) == 50.0

    def test_below_minimum(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(-10.0) == 0.0

    def test_above_maximum(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(150.0) == 100.0

    def test_at_minimum(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(0.0) == 0.0

    def test_at_maximum(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(100.0) == 100.0

    def test_custom_range(self):
        from app.services.sourcing_leads import _clamp

        assert _clamp(5.0, minimum=10.0, maximum=20.0) == 10.0
        assert _clamp(25.0, minimum=10.0, maximum=20.0) == 20.0
        assert _clamp(15.0, minimum=10.0, maximum=20.0) == 15.0


# ── _confidence_band ─────────────────────────────────────────────────


class TestConfidenceBand:
    def test_high_threshold(self):
        from app.services.sourcing_leads import _confidence_band

        assert _confidence_band(75.0) == "high"
        assert _confidence_band(100.0) == "high"

    def test_medium_threshold(self):
        from app.services.sourcing_leads import _confidence_band

        assert _confidence_band(50.0) == "medium"
        assert _confidence_band(74.9) == "medium"

    def test_low_threshold(self):
        from app.services.sourcing_leads import _confidence_band

        assert _confidence_band(0.0) == "low"
        assert _confidence_band(49.9) == "low"


# ── _safety_band ──────────────────────────────────────────────────────


class TestSafetyBand:
    def test_low_risk(self):
        from app.services.sourcing_leads import _safety_band

        assert _safety_band(75.0) == "low_risk"
        assert _safety_band(100.0) == "low_risk"

    def test_medium_risk(self):
        from app.services.sourcing_leads import _safety_band

        assert _safety_band(50.0) == "medium_risk"
        assert _safety_band(74.9) == "medium_risk"

    def test_high_risk(self):
        from app.services.sourcing_leads import _safety_band

        assert _safety_band(0.0) == "high_risk"
        assert _safety_band(49.9) == "high_risk"

    def test_no_vendor_data_returns_unknown(self):
        from app.services.sourcing_leads import _safety_band

        assert _safety_band(80.0, has_vendor_data=False) == "unknown"


# ── _source_reliability ───────────────────────────────────────────────


class TestSourceReliability:
    def test_digikey_high_reliability(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("digikey", None)
        assert score >= 80.0

    def test_mouser_high_reliability(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("mouser", None)
        assert score >= 80.0

    def test_brokerbin_medium_reliability(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("brokerbin", None)
        assert 60.0 <= score <= 85.0

    def test_ai_low_reliability(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("ai", None)
        assert score <= 55.0

    def test_unknown_source_medium(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("unknown_source", None)
        assert 50.0 <= score <= 75.0

    def test_tier1_bonus(self):
        from app.services.sourcing_leads import _source_reliability

        base = _source_reliability("brokerbin", None)
        with_tier = _source_reliability("brokerbin", "T1")
        assert with_tier > base

    def test_tier7_penalty(self):
        from app.services.sourcing_leads import _source_reliability

        base = _source_reliability("brokerbin", None)
        with_penalty = _source_reliability("brokerbin", "T7")
        assert with_penalty < base

    def test_empty_source_type(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("", None)
        assert 0.0 <= score <= 100.0

    def test_none_source_type(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability(None, None)
        assert 0.0 <= score <= 100.0

    def test_result_clamped_to_100(self):
        from app.services.sourcing_leads import _source_reliability

        # T1 bonus on a high-reliability source should not exceed 100
        score = _source_reliability("digikey", "T1")
        assert score <= 100.0

    def test_salesforce_high_reliability(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("salesforce", None)
        assert score >= 75.0


# ── _freshness_score ──────────────────────────────────────────────────


class TestFreshnessScore:
    def test_none_datetime_returns_45(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(None) == 45.0

    def test_very_fresh_high_score(self):
        from app.services.sourcing_leads import _freshness_score

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        score = _freshness_score(recent)
        assert score == 95.0

    def test_3_day_old(self):
        from app.services.sourcing_leads import _freshness_score

        dt = datetime.now(timezone.utc) - timedelta(days=2)
        score = _freshness_score(dt)
        assert score == 85.0

    def test_1_week_old(self):
        from app.services.sourcing_leads import _freshness_score

        dt = datetime.now(timezone.utc) - timedelta(days=5)
        score = _freshness_score(dt)
        assert score == 72.0

    def test_2_weeks_old(self):
        from app.services.sourcing_leads import _freshness_score

        dt = datetime.now(timezone.utc) - timedelta(days=10)
        score = _freshness_score(dt)
        assert score == 58.0

    def test_1_month_old(self):
        from app.services.sourcing_leads import _freshness_score

        dt = datetime.now(timezone.utc) - timedelta(days=20)
        score = _freshness_score(dt)
        assert score == 42.0

    def test_very_old_returns_25(self):
        from app.services.sourcing_leads import _freshness_score

        dt = datetime.now(timezone.utc) - timedelta(days=90)
        score = _freshness_score(dt)
        assert score == 25.0

    def test_naive_datetime_treated_as_utc(self):
        from app.services.sourcing_leads import _freshness_score

        naive = datetime.now() - timedelta(hours=1)
        score = _freshness_score(naive)
        assert score >= 85.0  # Recent — either 95 or 85 depending on precision


# ── _as_utc ───────────────────────────────────────────────────────────


class TestAsUtc:
    def test_none_returns_none(self):
        from app.services.sourcing_leads import _as_utc

        assert _as_utc(None) is None

    def test_aware_datetime_returned_as_utc(self):
        from app.services.sourcing_leads import _as_utc

        now = datetime.now(timezone.utc)
        result = _as_utc(now)
        assert result.tzinfo is not None
        assert result == now

    def test_naive_datetime_gets_utc_tzinfo(self):
        from app.services.sourcing_leads import _as_utc

        naive = datetime(2026, 1, 1, 12, 0, 0)
        result = _as_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026
