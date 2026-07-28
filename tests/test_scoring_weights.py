"""Tests for settings-driven score_sighting_v2 factor weights.

The five SIGHTING_WEIGHT_* Settings fields feed app.scoring.SIGHTING_V2_WEIGHTS
(built once at import via _build_sighting_weights). These tests pin:
- the defaults equal the historical hardcoded split,
- env overrides flow through Settings into the weights map,
- score_sighting_v2 and score_sighting_v2_breakdown honor an overridden map in
  lock-step (the breakdown must always sum to the score),
- a weight set that doesn't sum to 1.0 fails fast at Settings construction.

Called by: pytest
Depends on: app.scoring, app.config.Settings
"""

import os

os.environ["TESTING"] = "1"

import pytest
from pydantic import ValidationError

import app.scoring as scoring
from app.config import Settings

_WEIGHT_ENV_KEYS = (
    "SIGHTING_WEIGHT_TRUST",
    "SIGHTING_WEIGHT_PRICE",
    "SIGHTING_WEIGHT_QUANTITY",
    "SIGHTING_WEIGHT_FRESHNESS",
    "SIGHTING_WEIGHT_COMPLETENESS",
)


@pytest.fixture(autouse=True)
def _clean_weight_env(monkeypatch):
    """Isolate each test from any ambient SIGHTING_WEIGHT_* env vars."""
    for key in _WEIGHT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_weights_match_historical_hardcoded_values():
    """Defaults preserve the pre-settings hardcoded split exactly."""
    assert scoring.SIGHTING_V2_WEIGHTS == {
        "trust": 0.30,
        "price": 0.25,
        "qty": 0.20,
        "freshness": 0.15,
        "completeness": 0.10,
    }


def test_env_override_flows_into_weights(monkeypatch):
    """SIGHTING_WEIGHT_* env vars land on Settings and map onto the factor keys."""
    monkeypatch.setenv("SIGHTING_WEIGHT_TRUST", "0.40")
    monkeypatch.setenv("SIGHTING_WEIGHT_PRICE", "0.30")
    monkeypatch.setenv("SIGHTING_WEIGHT_QUANTITY", "0.10")
    monkeypatch.setenv("SIGHTING_WEIGHT_FRESHNESS", "0.10")
    monkeypatch.setenv("SIGHTING_WEIGHT_COMPLETENESS", "0.10")
    cfg = Settings(_env_file=None)
    assert scoring._build_sighting_weights(cfg) == {
        "trust": 0.40,
        "price": 0.30,
        "qty": 0.10,
        "freshness": 0.10,
        "completeness": 0.10,
    }


def test_overridden_weights_keep_score_and_breakdown_in_lock_step(monkeypatch):
    """Both the score AND its breakdown read the same weights map — an override moves
    them together (the drivers a hover shows can never drift from the number)."""
    override = {"trust": 0.60, "price": 0.10, "qty": 0.10, "freshness": 0.10, "completeness": 0.10}
    monkeypatch.setattr(scoring, "SIGHTING_V2_WEIGHTS", override)

    total, components = scoring.score_sighting_v2(
        vendor_score=80.0,
        is_authorized=False,
        unit_price=10.0,
        median_price=10.0,  # ratio 1.0 → price factor 50
        qty_available=100,
        target_qty=100,  # full coverage → qty factor 100
        age_hours=0.0,  # fresh → freshness 100
        has_price=True,
        has_qty=True,
        has_lead_time=True,
        has_condition=True,  # 4/4 → completeness 100
    )
    # trust 80*0.6 + price 50*0.1 + qty 100*0.1 + freshness 100*0.1 + completeness 100*0.1
    assert total == pytest.approx(48.0 + 5.0 + 10.0 + 10.0 + 10.0, abs=0.11)

    contributions = scoring.score_sighting_v2_breakdown(components)
    assert sum(c for _, c in contributions) == pytest.approx(total, abs=0.3)
    assert dict(contributions)["Vendor trust"] == pytest.approx(80.0 * 0.60, abs=0.1)


def test_weights_not_summing_to_one_fail_fast(monkeypatch):
    """A weight set that doesn't sum to 1.0 refuses to boot (ValidationError at Settings
    construction — never a silent rescale)."""
    monkeypatch.setenv("SIGHTING_WEIGHT_TRUST", "0.50")  # others default → total 1.20
    with pytest.raises(ValidationError, match="sum to 1.0"):
        Settings(_env_file=None)


def test_default_weights_sum_accepted_within_tolerance():
    """The float defaults (which sum to 1.0000000000000002) pass the tolerance."""
    cfg = Settings(_env_file=None)
    weights = scoring._build_sighting_weights(cfg)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
