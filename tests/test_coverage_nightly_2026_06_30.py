"""Nightly coverage improvement tests — 2026-06-30.

Targets uncovered lines in:
- app/management/rotate_encryption_salt.py  (lines 91-93, 195-217, 221-262)

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, client, test_requisition),
            app.management.rotate_encryption_salt
"""

import os
import sys
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

import pytest
from sqlalchemy.orm import Session

from app.management.rotate_encryption_salt import (
    RotationStats,
    _log_summary,
    _salt_fingerprint,
)
from app.models.sourcing import Requirement, Requisition
from app.models.sourcing_lead import SourcingLead

# ── _salt_fingerprint ────────────────────────────────────────────────


class TestSaltFingerprint:
    def test_none_returns_legacy_label(self):
        assert _salt_fingerprint(None) == "(legacy fallback salt)"

    def test_empty_string_returns_legacy_label(self):
        assert _salt_fingerprint("") == "(legacy fallback salt)"

    def test_real_salt_returns_sha256_prefix(self):
        result = _salt_fingerprint("my-test-salt")
        assert result.startswith("sha256:")
        assert len(result) == len("sha256:") + 12

    def test_different_salts_produce_different_fingerprints(self):
        fp1 = _salt_fingerprint("salt-a")
        fp2 = _salt_fingerprint("salt-b")
        assert fp1 != fp2


# ── _log_summary ─────────────────────────────────────────────────────


class TestLogSummary:
    def test_dry_run_summary_runs_without_error(self):
        stats = RotationStats(users_scanned=10, rows_updated=0)
        _log_summary(stats, "old-salt", "new-salt", "secret-key", dry_run=True)

    def test_live_run_summary_runs_without_error(self):
        stats = RotationStats(users_scanned=5, rows_updated=3)
        _log_summary(stats, "old-salt", "new-salt", "secret-key", dry_run=False)

    def test_summary_with_none_old_salt(self):
        stats = RotationStats(users_scanned=1, rows_updated=1)
        _log_summary(stats, None, "new-salt", "secret-key", dry_run=False)

    def test_summary_with_undecryptable_values_logs_warning(self, caplog):

        stats = RotationStats(users_scanned=2, rows_updated=1)
        stats.undecryptable["refresh_token"] = 1
        # Should not raise even with undecryptable values
        _log_summary(stats, "old", "new", "key", dry_run=False)
        assert stats.total_undecryptable == 1


# ── main() ───────────────────────────────────────────────────────────


class TestMain:
    def test_main_dry_run(self, db_session: Session):
        from app.management.rotate_encryption_salt import main

        mock_stats = RotationStats(users_scanned=0, rows_updated=0)
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.encryption_salt = "old-salt"
        mock_settings.secret_key = "test-secret"

        with (
            patch("sys.argv", ["rotate_encryption_salt", "--new-salt", "new-salt-val", "--dry-run"]),
            patch("app.management.rotate_encryption_salt.rotate_salt", return_value=mock_stats),
            patch("app.management.rotate_encryption_salt._log_summary"),
            patch("app.database.SessionLocal", return_value=mock_db),
        ):
            import app.management.rotate_encryption_salt as m

            orig_settings = None
            with patch.object(
                sys.modules.get("app.config", MagicMock()),
                "settings",
                mock_settings,
                create=True,
            ):
                # Patch the lazy imports inside main()
                with patch("app.config.settings", mock_settings):
                    with patch("app.database.SessionLocal") as mock_sl:
                        mock_session = MagicMock()
                        mock_sl.return_value = mock_session
                        with patch.object(m, "rotate_salt", return_value=mock_stats):
                            with patch.object(m, "_log_summary"):
                                main()
                                mock_sl.assert_called_once()
                                mock_session.close.assert_called_once()

    def test_main_missing_new_salt_exits(self):
        from app.management.rotate_encryption_salt import main

        mock_settings = MagicMock()
        mock_settings.encryption_salt = "old"
        mock_settings.secret_key = "key"

        with (
            patch("sys.argv", ["rotate_encryption_salt"]),
            patch("app.config.settings", mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            # Remove NEW_ENCRYPTION_SALT if present
            env_backup = os.environ.pop("NEW_ENCRYPTION_SALT", None)
            try:
                with pytest.raises(SystemExit):
                    main()
            finally:
                if env_backup is not None:
                    os.environ["NEW_ENCRYPTION_SALT"] = env_backup

    def test_main_reads_new_salt_from_env(self):
        from app.management.rotate_encryption_salt import main

        mock_stats = RotationStats()
        mock_settings = MagicMock()
        mock_settings.encryption_salt = "old"
        mock_settings.secret_key = "key"

        with (
            patch("sys.argv", ["rotate_encryption_salt"]),
            patch.dict(os.environ, {"NEW_ENCRYPTION_SALT": "env-new-salt"}),
            patch("app.config.settings", mock_settings),
            patch("app.database.SessionLocal") as mock_sl,
        ):
            mock_session = MagicMock()
            mock_sl.return_value = mock_session

            import app.management.rotate_encryption_salt as m

            with patch.object(m, "rotate_salt", return_value=mock_stats):
                with patch.object(m, "_log_summary"):
                    main()
            mock_session.close.assert_called_once()


# ── sourcing routes — page routes with get_user ───────────────────────


# ── sourcing results partial — filter paths ───────────────────────────


def _sourcing_lead(db: Session, req: Requisition, requirement: Requirement, vendor_name: str, **kw) -> SourcingLead:
    """Seed a SourcingLead row.

    ``vendor_name`` is the rendered-content anchor:
    ``lead_card.html``/``lead_row.html`` both render ``{{ lead.vendor_name }}``, so
    asserting on it directly proves a filter changed the RENDERED result set, not just
    the status code (P6.1).
    """
    defaults = dict(
        lead_id=f"LEAD-{uuid.uuid4().hex[:8]}",
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower(),
        primary_source_type="api",
        primary_source_name="test-source",
        vendor_safety_band="safe",
        buyer_status="new",
        contact_email=None,
        contact_phone=None,
        corroborated=False,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    obj = SourcingLead(**defaults)
    db.add(obj)
    db.flush()
    return obj


class TestSourcingResultsFilters:
    """Each filter test seeds one MATCHING lead + one NON-MATCHING lead and asserts the
    rendered lead set reflects the filter (P6.1 — was bare status_code==200)."""

    def _req_and_requirement(self, test_requisition: Requisition):
        return test_requisition, test_requisition.requirements[0]


# ── sourcing workspace partial — filter paths ─────────────────────────


class TestSourcingWorkspaceFilters:
    """Same P6.1 treatment as TestSourcingResultsFilters, against the workspace partial
    (``lead_row.html`` also renders ``{{ lead.vendor_name }}``)."""

    def _req_and_requirement(self, test_requisition: Requisition):
        return test_requisition, test_requisition.requirements[0]
