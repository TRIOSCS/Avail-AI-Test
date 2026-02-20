"""
test_schemas_v13_features.py — Tests for v1.3 feature schemas.

Validates PhoneCallLog and StrategicToggle schemas.

Called by: pytest
Depends on: app.schemas.v13_features
"""

import pytest
from pydantic import ValidationError

from app.schemas.v13_features import (
    PhoneCallLog,
    StrategicToggle,
)

# ── PhoneCallLog ────────────────────────────────────────────────────

class TestPhoneCallLog:
    def test_defaults(self):
        p = PhoneCallLog()
        assert p.phone == ""
        assert p.direction == "outbound"
        assert p.duration_seconds is None

    def test_inbound_call(self):
        p = PhoneCallLog(
            phone="+1-555-1234",
            direction="inbound",
            duration_seconds=120,
            contact_name="Jane Doe",
        )
        assert p.direction == "inbound"
        assert p.duration_seconds == 120

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValidationError, match="direction"):
            PhoneCallLog(direction="missed")


# ── StrategicToggle ─────────────────────────────────────────────────

class TestStrategicToggle:
    def test_default_none_means_flip(self):
        s = StrategicToggle()
        assert s.is_strategic is None

    def test_explicit_true(self):
        s = StrategicToggle(is_strategic=True)
        assert s.is_strategic is True

    def test_empty_body_accepted(self):
        """Endpoint uses default param, so empty body = StrategicToggle()."""
        s = StrategicToggle()
        assert s.is_strategic is None
