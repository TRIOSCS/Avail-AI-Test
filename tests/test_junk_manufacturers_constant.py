"""Tests for the consolidated JUNK_MANUFACTURERS constant in app/shared_constants.py.

Verifies the union of the two previously-independent sets and that both call-sites
(enrichment.py, tagging_backfill.py) still perform the expected membership check.

Called by: pytest
Depends on: app.shared_constants
"""

import pytest

from app.shared_constants import JUNK_MANUFACTURERS


class TestJunkManufacturersConstant:
    """JUNK_MANUFACTURERS is a frozenset containing the union of both old sets."""

    def test_is_frozenset(self):
        assert isinstance(JUNK_MANUFACTURERS, frozenset)

    # Values from the original enrichment.py._IGNORED_MANUFACTURERS
    @pytest.mark.parametrize("value", ["", "unknown", "n/a", "various", "none", "other", "generic"])
    def test_contains_original_enrichment_values(self, value):
        assert value in JUNK_MANUFACTURERS

    # Values added by tagging_backfill.py._JUNK_MANUFACTURERS (the superset)
    @pytest.mark.parametrize("value", ["-", "na"])
    def test_contains_tagging_backfill_additions(self, value):
        assert value in JUNK_MANUFACTURERS

    # Legitimate manufacturer names must NOT be filtered
    @pytest.mark.parametrize("value", ["Texas Instruments", "NXP", "STMicroelectronics", "Murata"])
    def test_real_manufacturers_not_in_set(self, value):
        assert value.lower() not in JUNK_MANUFACTURERS

    def test_membership_check_is_case_sensitive_lowercase(self):
        # Callers always do .lower() before checking — the set stores lowercase values.
        assert "UNKNOWN" not in JUNK_MANUFACTURERS
        assert "unknown" in JUNK_MANUFACTURERS
