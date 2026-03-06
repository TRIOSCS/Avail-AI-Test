"""Tests for 8x8 Work Analytics config and user model fields."""

from unittest.mock import patch

from app.config import Settings
from app.models.auth import User


def test_settings_loads_8x8_fields():
    """Settings loads all EIGHT_BY_EIGHT_* fields without error."""
    env_overrides = {
        "EIGHT_BY_EIGHT_API_KEY": "",
        "EIGHT_BY_EIGHT_USERNAME": "",
        "EIGHT_BY_EIGHT_PASSWORD": "",
        "EIGHT_BY_EIGHT_PBX_ID": "",
        "EIGHT_BY_EIGHT_ENABLED": "false",
    }
    with patch.dict("os.environ", env_overrides):
        s = Settings()
        assert s.eight_by_eight_api_key == ""
        assert s.eight_by_eight_username == ""
        assert s.eight_by_eight_password == ""
        assert s.eight_by_eight_pbx_id == ""
        assert s.eight_by_eight_timezone == "America/Los_Angeles"
        assert s.eight_by_eight_enabled is False
        assert s.eight_by_eight_poll_interval_minutes == 30


def test_user_has_8x8_extension_column():
    """User model has eight_by_eight_extension column (nullable)."""
    col = User.__table__.columns["eight_by_eight_extension"]
    assert col.nullable is True
    assert str(col.type) == "VARCHAR(20)"


def test_user_has_8x8_enabled_column():
    """User model has eight_by_eight_enabled column (default False)."""
    col = User.__table__.columns["eight_by_eight_enabled"]
    assert col.default.arg is False
