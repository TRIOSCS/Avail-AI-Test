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
from app.models.auth import User
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

    def test_safety_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "HighSafetyVendor", vendor_safety_band="high")
        _sourcing_lead(db_session, req, requirement, "LowSafetyVendor", vendor_safety_band="low")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?safety=high")

        assert resp.status_code == 200
        assert "HighSafetyVendor" in resp.text
        assert "LowSafetyVendor" not in resp.text

    def test_status_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "ActiveStatusVendor", buyer_status="active")
        _sourcing_lead(db_session, req, requirement, "NewStatusVendor", buyer_status="new")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?status=active")

        assert resp.status_code == 200
        assert "ActiveStatusVendor" in resp.text
        assert "NewStatusVendor" not in resp.text

    def test_contactability_has_phone_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "HasPhoneVendor", contact_phone="+15551234567")
        _sourcing_lead(db_session, req, requirement, "NoPhoneVendor", contact_phone=None)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?contactability=has_phone")

        assert resp.status_code == 200
        assert "HasPhoneVendor" in resp.text
        assert "NoPhoneVendor" not in resp.text

    def test_corroborated_no_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "UncorroboratedVendor", corroborated=False)
        _sourcing_lead(db_session, req, requirement, "CorroboratedVendor", corroborated=True)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?corroborated=no")

        assert resp.status_code == 200
        assert "UncorroboratedVendor" in resp.text
        assert "CorroboratedVendor" not in resp.text

    def test_corroborated_yes_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "CorroboratedVendor2", corroborated=True)
        _sourcing_lead(db_session, req, requirement, "UncorroboratedVendor2", corroborated=False)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?corroborated=yes")

        assert resp.status_code == 200
        assert "CorroboratedVendor2" in resp.text
        assert "UncorroboratedVendor2" not in resp.text

    def test_multiple_filters_combined(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "MatchesAllFilters",
            vendor_safety_band="high",
            buyer_status="active",
            corroborated=False,
        )
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "FailsSafetyFilter",
            vendor_safety_band="low",
            buyer_status="active",
            corroborated=False,
        )
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "FailsCorroboratedFilter",
            vendor_safety_band="high",
            buyer_status="active",
            corroborated=True,
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}?safety=high&status=active&corroborated=no")

        assert resp.status_code == 200
        assert "MatchesAllFilters" in resp.text
        assert "FailsSafetyFilter" not in resp.text
        assert "FailsCorroboratedFilter" not in resp.text


# ── sourcing workspace partial — filter paths ─────────────────────────


class TestSourcingWorkspaceFilters:
    """Same P6.1 treatment as TestSourcingResultsFilters, against the workspace partial
    (``lead_row.html`` also renders ``{{ lead.vendor_name }}``)."""

    def _req_and_requirement(self, test_requisition: Requisition):
        return test_requisition, test_requisition.requirements[0]

    def test_source_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "BrokerbinVendor", primary_source_type="brokerbin")
        _sourcing_lead(db_session, req, requirement, "NexarVendor", primary_source_type="nexar")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?source=brokerbin")

        assert resp.status_code == 200
        assert "BrokerbinVendor" in resp.text
        assert "NexarVendor" not in resp.text

    def test_status_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "WsActiveVendor", buyer_status="active")
        _sourcing_lead(db_session, req, requirement, "WsNewVendor", buyer_status="new")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?status=active")

        assert resp.status_code == 200
        assert "WsActiveVendor" in resp.text
        assert "WsNewVendor" not in resp.text

    def test_contactability_has_phone_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "WsHasPhoneVendor", contact_phone="+15559876543")
        _sourcing_lead(db_session, req, requirement, "WsNoPhoneVendor", contact_phone=None)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?contactability=has_phone")

        assert resp.status_code == 200
        assert "WsHasPhoneVendor" in resp.text
        assert "WsNoPhoneVendor" not in resp.text

    def test_contactability_has_email_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "WsHasEmailVendor", contact_email="sales@vendor.example")
        _sourcing_lead(db_session, req, requirement, "WsNoEmailVendor", contact_email=None)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?contactability=has_email")

        assert resp.status_code == 200
        assert "WsHasEmailVendor" in resp.text
        assert "WsNoEmailVendor" not in resp.text

    def test_corroborated_no_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "WsUncorrobVendor", corroborated=False)
        _sourcing_lead(db_session, req, requirement, "WsCorrobVendor", corroborated=True)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?corroborated=no")

        assert resp.status_code == 200
        assert "WsUncorrobVendor" in resp.text
        assert "WsCorrobVendor" not in resp.text

    def test_corroborated_yes_filter(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(db_session, req, requirement, "WsCorrobVendor2", corroborated=True)
        _sourcing_lead(db_session, req, requirement, "WsUncorrobVendor2", corroborated=False)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?corroborated=yes")

        assert resp.status_code == 200
        assert "WsCorrobVendor2" in resp.text
        assert "WsUncorrobVendor2" not in resp.text

    def test_multiple_workspace_filters(self, client, db_session: Session, test_requisition: Requisition):
        req, requirement = self._req_and_requirement(test_requisition)
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "WsMatchesAll",
            primary_source_type="nexar",
            corroborated=True,
            buyer_status="new",
        )
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "WsFailsSource",
            primary_source_type="brokerbin",
            corroborated=True,
            buyer_status="new",
        )
        _sourcing_lead(
            db_session,
            req,
            requirement,
            "WsFailsStatus",
            primary_source_type="nexar",
            corroborated=True,
            buyer_status="active",
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{requirement.id}/workspace?source=nexar&corroborated=yes&status=new")

        assert resp.status_code == 200
        assert "WsMatchesAll" in resp.text
        assert "WsFailsSource" not in resp.text
        assert "WsFailsStatus" not in resp.text
