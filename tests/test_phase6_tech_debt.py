"""
tests/test_phase6_tech_debt.py — Tests for Phase 6 tech debt cleanup.

Covers:
- Redundant index=True removal on unique columns
- Buy Plan V1 config flag exists (even if unused)

Called by: pytest
Depends on: app/models/config.py, intelligence.py, offers.py, vendors.py, enrichment.py
"""

from app.models.config import SystemConfig
from app.models.enrichment import IntelCache
from app.models.intelligence import MaterialCard
from app.models.offers import VendorResponse
from app.models.vendors import VendorCard


class TestRedundantIndexRemoval:
    """Verify unique columns no longer have redundant index=True."""

    def _col(self, model, name):
        return model.__table__.columns[name]

    def test_system_config_key_unique_no_explicit_index(self):
        col = self._col(SystemConfig, "key")
        assert col.unique is True
        assert col.index is not True, "key should rely on unique constraint for indexing"

    def test_intel_cache_key_unique_no_explicit_index(self):
        col = self._col(IntelCache, "cache_key")
        assert col.unique is True
        assert col.index is not True

    def test_material_card_mpn_unique_no_explicit_index(self):
        col = self._col(MaterialCard, "normalized_mpn")
        assert col.unique is True
        assert col.index is not True

    def test_vendor_response_message_id_unique_no_explicit_index(self):
        col = self._col(VendorResponse, "message_id")
        assert col.unique is True
        assert col.index is not True

    def test_vendor_card_name_unique_no_explicit_index(self):
        col = self._col(VendorCard, "normalized_name")
        assert col.unique is True
        assert col.index is not True


class TestMvpModeConfigExists:
    """Verify MVP mode flag is properly defined."""

    def test_mvp_mode_setting_exists(self):
        from app.config import Settings

        s = Settings()
        assert hasattr(s, "mvp_mode")
        assert isinstance(s.mvp_mode, bool)

    def test_buy_plan_v1_flag_exists(self):
        from app.config import Settings

        s = Settings()
        assert hasattr(s, "buy_plan_v1_enabled")
        assert s.buy_plan_v1_enabled is False, "V1 should remain disabled"
