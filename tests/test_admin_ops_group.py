"""test_admin_ops_group.py — Ops verification group admin surface + ADMIN_EMAILS seed.

Covers: ops_group_context helper, GET settings tab (admin-gated), POST toggle
(add / deactivate, unique-row preserved, unknown user 404), and the startup seed.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.buy_plan import VerificationGroupMember


@pytest.fixture()
def _noclose_db(db_session):
    """db_session whose close() is a no-op (startup seed calls
    SessionLocal().close())."""
    original = db_session.close
    db_session.close = lambda: None
    yield db_session
    db_session.close = original


class TestOpsGroupContext:
    def test_rows_and_active_count(self, db_session, test_user):
        from app.routers.admin.buy_plan_ops import ops_group_context

        ctx = ops_group_context(db_session)
        assert {"rows", "active_count"} <= set(ctx)
        assert test_user.email in [r["user"].email for r in ctx["rows"]]
        assert ctx["active_count"] == 0

        db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        db_session.flush()
        assert ops_group_context(db_session)["active_count"] == 1


class TestOpsGroupTab:
    def test_forbidden_for_non_admin(self, client):
        # default client runs as test_user (buyer) via the require_user override
        assert client.get("/v2/partials/settings/ops-group").status_code == 403

    def test_renders_for_admin(self, db_session, admin_user):
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            r = TestClient(app).get("/v2/partials/settings/ops-group")
            assert r.status_code == 200
            assert "Ops Verification Group" in r.text
        finally:
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(get_db, None)


class TestToggleOpsMember:
    def test_toggle_add_then_deactivate(self, client, db_session, test_user, sales_user):
        # Keep test_user (the authed admin) active so deactivating sales_user trips
        # neither the self-removal nor the last-member guard — this exercises the
        # add -> deactivate toggle mechanic, which the guards must not interfere with.
        db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        db_session.commit()

        r = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
        assert r.status_code == 200
        m = db_session.query(VerificationGroupMember).filter_by(user_id=sales_user.id).first()
        assert m is not None and m.is_active is True

        r2 = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
        assert r2.status_code == 200
        db_session.refresh(m)
        assert m.is_active is False
        # unique(user_id): still exactly one row (toggle, not delete+reinsert)
        assert db_session.query(VerificationGroupMember).filter_by(user_id=sales_user.id).count() == 1

    def test_toggle_unknown_user_404(self, client):
        assert client.post("/api/admin/ops-group/toggle", data={"user_id": 999999}).status_code == 404


class TestSeedFromAdminEmails:
    def _seed(self, noclose_db, emails):
        from app import startup

        fake_settings = MagicMock()
        fake_settings.admin_emails = emails
        with patch("app.config.settings", fake_settings):
            with patch("app.startup.SessionLocal", return_value=noclose_db):
                startup._seed_verification_group_from_admin_emails()

    def test_seeds_known_email(self, _noclose_db, test_user):
        self._seed(_noclose_db, [test_user.email])
        m = _noclose_db.query(VerificationGroupMember).filter_by(user_id=test_user.id).first()
        assert m is not None and m.is_active is True

    def test_seed_idempotent(self, _noclose_db, test_user):
        self._seed(_noclose_db, [test_user.email])
        self._seed(_noclose_db, [test_user.email])
        assert _noclose_db.query(VerificationGroupMember).filter_by(user_id=test_user.id).count() == 1

    def test_seed_skips_unknown_email(self, _noclose_db):
        self._seed(_noclose_db, ["nobody@nowhere.test"])
        assert _noclose_db.query(VerificationGroupMember).count() == 0
