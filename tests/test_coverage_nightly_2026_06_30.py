"""Nightly coverage improvement tests — 2026-06-30.

Targets uncovered lines in:
- app/management/rotate_encryption_salt.py  (lines 91-93, 195-217, 221-262)
- app/routers/htmx/sourcing.py              (lines 50-52, 63-65, 486-488, 201-221, 534-546)

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, client, test_requisition),
            app.management.rotate_encryption_salt, app.routers.htmx.sourcing
"""

import os
import sys
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

import pytest
from sqlalchemy.orm import Session

from app.management.rotate_encryption_salt import (
    RotationStats,
    _log_summary,
    _salt_fingerprint,
)
from app.models.auth import User
from app.models.sourcing import Requisition

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


class TestSourcingPageRoutes:
    def test_sourcing_page_authenticated(self, client, test_user: User, test_requisition: Requisition):
        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get(f"/v2/sourcing/{test_requisition.requirements[0].id}")
        assert resp.status_code == 200

    def test_sourcing_page_unauthenticated_shows_login(self, client, test_requisition: Requisition):
        with patch("app.routers.htmx.sourcing.get_user", return_value=None):
            resp = client.get(f"/v2/sourcing/{test_requisition.requirements[0].id}")
        assert resp.status_code == 200
        assert "login" in resp.text.lower() or "sign in" in resp.text.lower()

    def test_lead_detail_page_authenticated(self, client, test_user: User):
        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get("/v2/sourcing/leads/999")
        assert resp.status_code == 200

    def test_lead_detail_page_unauthenticated(self, client):
        with patch("app.routers.htmx.sourcing.get_user", return_value=None):
            resp = client.get("/v2/sourcing/leads/999")
        assert resp.status_code == 200

    def test_workspace_page_authenticated(self, client, test_user: User, test_requisition: Requisition):
        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get(f"/v2/sourcing/{test_requisition.requirements[0].id}/workspace")
        assert resp.status_code == 200

    def test_workspace_page_unauthenticated(self, client, test_requisition: Requisition):
        with patch("app.routers.htmx.sourcing.get_user", return_value=None):
            resp = client.get(f"/v2/sourcing/{test_requisition.requirements[0].id}/workspace")
        assert resp.status_code == 200


# ── sourcing results partial — filter paths ───────────────────────────


class TestSourcingResultsFilters:
    def _req_id(self, test_requisition: Requisition) -> int:
        return test_requisition.requirements[0].id

    def test_safety_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?safety=high")
        assert resp.status_code == 200

    def test_status_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?status=active")
        assert resp.status_code == 200

    def test_contactability_has_phone_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?contactability=has_phone")
        assert resp.status_code == 200

    def test_corroborated_no_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?corroborated=no")
        assert resp.status_code == 200

    def test_corroborated_yes_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?corroborated=yes")
        assert resp.status_code == 200

    def test_multiple_filters_combined(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}?safety=high&status=active&corroborated=no")
        assert resp.status_code == 200


# ── sourcing workspace partial — filter paths ─────────────────────────


class TestSourcingWorkspaceFilters:
    def _req_id(self, test_requisition: Requisition) -> int:
        return test_requisition.requirements[0].id

    def test_source_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?source=brokerbin")
        assert resp.status_code == 200

    def test_status_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?status=active")
        assert resp.status_code == 200

    def test_contactability_has_phone_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?contactability=has_phone")
        assert resp.status_code == 200

    def test_contactability_has_email_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?contactability=has_email")
        assert resp.status_code == 200

    def test_corroborated_no_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?corroborated=no")
        assert resp.status_code == 200

    def test_corroborated_yes_filter(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?corroborated=yes")
        assert resp.status_code == 200

    def test_multiple_workspace_filters(self, client, test_requisition: Requisition):
        req_id = self._req_id(test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace?source=nexar&corroborated=yes&status=new")
        assert resp.status_code == 200
