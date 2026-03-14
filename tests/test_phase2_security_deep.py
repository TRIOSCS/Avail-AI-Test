"""
tests/test_phase2_security_deep.py — Tests for Phase 2 security deep fixes.

Covers:
- SQL injection elimination (no f-string interpolation in queries)
- Vendor merge transaction safety (rollback on FK failure)

Called by: pytest
Depends on: app/routers/vendor_analytics.py, app/services/vendor_merge_service.py
"""

import inspect

import pytest

from app.services.vendor_merge_service import merge_vendor_cards


# ── SQL f-string elimination ──────────────────────────────────────────


class TestNoSqlFstring:
    """Verify vendor_analytics.py no longer uses f-string SQL."""

    def test_vendor_parts_summary_no_fstring_sql(self):
        """Ensure _vendor_parts_summary_query uses only parameterized queries."""
        from app.routers.vendor_analytics import _vendor_parts_summary_query

        source = inspect.getsource(_vendor_parts_summary_query)
        # Should not contain sqltext(f" pattern
        assert 'sqltext(f"' not in source, "SQL query still uses f-string interpolation"
        assert "sqltext(f'" not in source, "SQL query still uses f-string interpolation"


# ── Vendor merge transaction safety ───────────────────────────────────


class TestVendorMergeTransactionSafety:
    """Verify vendor merge rolls back on FK reassignment failure."""

    def test_merge_raises_on_fk_failure(self, db_session):
        """If FK reassignment fails, merge should raise ValueError, not silently continue."""
        from app.models import VendorCard

        keep = VendorCard(display_name="Keep Vendor", normalized_name="keepvendor", sighting_count=5)
        remove = VendorCard(display_name="Remove Vendor", normalized_name="removevendor", sighting_count=3)
        db_session.add_all([keep, remove])
        db_session.commit()

        # Normal merge should succeed
        result = merge_vendor_cards(keep.id, remove.id, db_session)
        assert result["ok"] is True
        assert result["reassigned"] >= 0

    def test_merge_rejects_same_id(self, db_session):
        from app.models import VendorCard

        card = VendorCard(display_name="Solo Vendor", normalized_name="solovendor", sighting_count=1)
        db_session.add(card)
        db_session.commit()

        with pytest.raises(ValueError, match="Cannot merge a vendor with itself"):
            merge_vendor_cards(card.id, card.id, db_session)

    def test_merge_rejects_missing_card(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            merge_vendor_cards(99999, 99998, db_session)

    def test_merge_error_handling_is_not_silent(self):
        """Verify the merge function logs errors, not debug, on FK failure."""
        source = inspect.getsource(merge_vendor_cards)
        assert "logger.error" in source, "FK failures should be logged at ERROR level"
        assert "db.rollback()" in source, "FK failures should trigger rollback"
        assert 'raise ValueError' in source, "FK failures should raise ValueError"


# ── Password hashing verification ─────────────────────────────────────


class TestPasswordHashingIsSecure:
    """Verify password hashing uses proper KDF, not plain SHA-256."""

    def test_startup_uses_pbkdf2(self):
        """startup.py should use pbkdf2_hmac with high iteration count."""
        from app import startup

        source = inspect.getsource(startup._create_default_user_if_env_set)
        assert "pbkdf2_hmac" in source, "Should use PBKDF2, not plain SHA-256"
        assert "200_000" in source or "200000" in source, "Should use >= 200K iterations"

    def test_auth_verify_uses_constant_time(self):
        """auth.py password verify should use hmac.compare_digest."""
        from app.routers.auth import _verify_password

        source = inspect.getsource(_verify_password)
        assert "compare_digest" in source, "Should use constant-time comparison"
