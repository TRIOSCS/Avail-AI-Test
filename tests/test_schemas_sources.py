"""test_schemas_sources.py — Tests for app/schemas/sources.py.

Called by: pytest
Depends on: app/schemas/sources.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.sources import MiningOptions, SourceStatusToggle


class TestSourceStatusToggle:
    @pytest.mark.parametrize("status", ["live", "disabled"], ids=["live", "disabled"])
    def test_valid_status(self, status):
        assert SourceStatusToggle(status=status).status == status

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

    @pytest.mark.parametrize(
        "lookback_days",
        [0, -5, 999],
        ids=["zero", "negative", "above_max"],
    )
    def test_invalid_lookback_raises(self, lookback_days):
        with pytest.raises(ValidationError):
            MiningOptions(lookback_days=lookback_days)
