"""
test_schemas_sources.py — Tests for app/schemas/sources.py

Called by: pytest
Depends on: app/schemas/sources.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.sources import MiningOptions, SourceStatusToggle


class TestSourceStatusToggle:
    def test_valid_live(self):
        assert SourceStatusToggle(status="live").status == "live"

    def test_valid_disabled(self):
        assert SourceStatusToggle(status="disabled").status == "disabled"

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            SourceStatusToggle(status="bogus")

    def test_missing_status_raises(self):
        with pytest.raises(ValidationError):
            SourceStatusToggle()


class TestMiningOptions:
    def test_defaults(self):
        m = MiningOptions()
        assert m.lookback_days == 30

    def test_custom_lookback(self):
        m = MiningOptions(lookback_days=7)
        assert m.lookback_days == 7

    def test_zero_lookback_raises(self):
        with pytest.raises(ValidationError):
            MiningOptions(lookback_days=0)

    def test_negative_lookback_raises(self):
        with pytest.raises(ValidationError):
            MiningOptions(lookback_days=-5)

    def test_max_lookback(self):
        with pytest.raises(ValidationError):
            MiningOptions(lookback_days=999)
