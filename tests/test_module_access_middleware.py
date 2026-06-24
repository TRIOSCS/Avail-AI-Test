"""Tests for ModuleAccessMiddleware and its pure path→key resolver.

Two layers:

1. Pure-function tests for ``app.access_paths.module_key_for_path`` — the single
   source of truth for WHICH paths are module-exclusive. These nail the
   over-blocking hazard: shared partials (customers/contacts/vendors/offers/…),
   the confirmed-shared module entry-partials (parts/sightings/materials/search/
   buy-plans), global search, and non-module paths MUST resolve to None.

2. Integration tests driving the real ASGI stack through a REAL signed session
   cookie (NOT ``dependency_overrides[require_user]`` — the middleware reads
   ``scope["session"]``, which only ``SessionMiddleware`` populates from a signed
   cookie). They prove a user with a module revoked is blocked on that module's
   sub-partial, that a SHARED partial is NOT blocked, that an allowed user and an
   admin pass, and that a logged-out request is left to the route's own auth.
"""

import base64
import json
from contextlib import contextmanager

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.access_paths import module_key_for_path
from app.config import settings
from app.constants import AccessKey
from app.models.auth import User

# Distinctive body the middleware returns on a block — asserted present on a
# guarded path and ABSENT on a shared path, so the shared-path test can't be
# fooled by some unrelated 403 from the route itself.
DENIED_BODY = "Module access denied"


# ──────────────────────────────────────────────────────────────────────────
# Pure function: module_key_for_path
# ──────────────────────────────────────────────────────────────────────────


class TestModuleKeyForPathGuarded:
    """Module-exclusive prefixes resolve to their AccessKey."""

    @pytest.mark.parametrize(
        "path,key",
        [
            ("/v2/partials/crm/shell", AccessKey.CRM),
            ("/v2/partials/resell/workspace", AccessKey.RESELL),
            ("/v2/partials/resell/lists", AccessKey.RESELL),
            ("/v2/partials/resell/123/offers", AccessKey.RESELL),
            ("/v2/partials/resell/create-form", AccessKey.RESELL),
            ("/v2/partials/proactive", AccessKey.PROACTIVE),
            ("/v2/partials/proactive/badge", AccessKey.PROACTIVE),
            ("/v2/partials/proactive/42/convert", AccessKey.PROACTIVE),
            ("/v2/partials/prospecting", AccessKey.PROSPECTING),
            ("/v2/partials/prospecting/stats", AccessKey.PROSPECTING),
            ("/v2/partials/prospecting/9/claim", AccessKey.PROSPECTING),
            ("/v2/partials/my-day", AccessKey.MY_DAY),
        ],
    )
    def test_guarded_prefix_returns_key(self, path, key):
        assert module_key_for_path(path) == key


class TestModuleKeyForPathShared:
    """SHARED partials must resolve to None — gating them would over-block."""

    @pytest.mark.parametrize(
        "path",
        [
            # CRM *data* partials — embedded by my_day/quotes/search etc.
            "/v2/partials/customers",
            "/v2/partials/customers/1",
            "/v2/partials/contacts/2",
            "/v2/partials/vendors",
            "/v2/partials/vendors/7",
            "/v2/partials/vendor-contacts/3",
            # Capability-gated / global — never a module.
            "/v2/partials/offers/1/promote",
            "/v2/partials/quotes/5",
            "/v2/partials/settings",
            "/v2/partials/settings/api-keys",
            "/v2/partials/alerts",
            "/v2/partials/follow-ups",
            "/v2/partials/trouble-tickets/2",
            # Confirmed SHARED module entry-partials (cross-module embedded).
            "/v2/partials/parts/workspace",
            "/v2/partials/sightings/workspace",
            "/v2/partials/sightings/9/refresh",
            "/v2/partials/materials/5",
            "/v2/partials/materials/workspace",
            "/v2/partials/buy-plans/3",
            "/v2/partials/buy-plans",
        ],
    )
    def test_shared_prefix_returns_none(self, path):
        assert module_key_for_path(path) is None


class TestModuleKeyForPathSearchAndGlobal:
    """The whole SEARCH prefix is shared (dashboard + topbar embed it), so every search
    path — including the global-search and AI endpoints — resolves to None."""

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/search",
            "/v2/partials/search/global",
            "/v2/partials/search/ai",
            "/v2/partials/search/results",
            "/v2/partials/search/dossier/hero",
        ],
    )
    def test_search_returns_none(self, path):
        assert module_key_for_path(path) is None


class TestModuleKeyForPathNonModule:
    """Non-partial paths, full pages, and edges resolve to None."""

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/requisitions",
            "/v2/crm",
            "/health",
            "/",
            "",
            "/static/app.css",
            "/v2/partials",
            "/v2/partials/",
        ],
    )
    def test_non_module_returns_none(self, path):
        assert module_key_for_path(path) is None

    @pytest.mark.parametrize(
        "path",
        [
            # Sibling-prefix safety: a path that merely STARTS WITH a guarded base
            # but is a different segment must NOT match.
            "/v2/partials/proactivex",
            "/v2/partials/proactive-extra/thing",
            "/v2/partials/my-day-summary",
            "/v2/partials/resellers",
            "/v2/partials/crmx",
        ],
    )
    def test_sibling_prefix_not_overmatched(self, path):
        assert module_key_for_path(path) is None


# ──────────────────────────────────────────────────────────────────────────
# Integration: real signed session cookie + the live ASGI stack
# ──────────────────────────────────────────────────────────────────────────


def _sign_session(user_id: int) -> str:
    """Build a real signed session cookie value the way SessionMiddleware does.

    Starlette signs ``b64encode(json.dumps(session))`` with a
    ``TimestampSigner(secret_key)``. Reproducing that here gives us a cookie the
    live SessionMiddleware will decode into ``scope["session"]`` — the same source
    the middleware (and require_user) read — without any dependency override.
    """
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    payload = base64.b64encode(json.dumps({"user_id": user_id}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


@contextmanager
def _live_client(db_session: Session, monkeypatch):
    """A TestClient on the real app with the test DB wired everywhere.

    - ``get_db`` override → the route handler uses the test session.
    - ``app.database.SessionLocal`` patched → the MIDDLEWARE's own short session
      (opened via ``from .database import SessionLocal``) also hits the test DB.
    require_user is deliberately NOT overridden — auth flows through the real
    signed session cookie so the middleware actually runs.
    """
    from app.database import get_db
    from app.main import app

    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


def _revoke(db_session: Session, user: User, key: AccessKey) -> None:
    user.access_overrides = {str(key): False}
    db_session.add(user)
    db_session.commit()


def test_revoked_module_subpartial_is_blocked(db_session, test_user, monkeypatch):
    """A buyer with RESELL revoked is 403'd on a resell sub-partial that has NO entry
    gate of its own — proving the MIDDLEWARE (not a route dep) blocks it."""
    _revoke(db_session, test_user, AccessKey.RESELL)
    with _live_client(db_session, monkeypatch) as c:
        c.cookies.set("session", _sign_session(test_user.id))
        resp = c.get("/v2/partials/resell/lists")
    assert resp.status_code == 403
    assert DENIED_BODY in resp.text


def test_revoked_user_shared_partial_not_blocked(db_session, test_user, monkeypatch):
    """The SAME resell-revoked user is NOT blocked by the middleware on a SHARED CRM
    data partial — module revocation must never reach shared fragments."""
    _revoke(db_session, test_user, AccessKey.RESELL)
    with _live_client(db_session, monkeypatch) as c:
        c.cookies.set("session", _sign_session(test_user.id))
        resp = c.get("/v2/partials/customers")
    # Whatever the route does, the middleware must not have produced its block.
    assert DENIED_BODY not in resp.text
    assert resp.status_code != 403


def test_allowed_user_passes(db_session, test_user, monkeypatch):
    """A default buyer (full access) reaches the guarded resell sub-partial."""
    with _live_client(db_session, monkeypatch) as c:
        c.cookies.set("session", _sign_session(test_user.id))
        resp = c.get("/v2/partials/resell/lists")
    assert resp.status_code == 200
    assert DENIED_BODY not in resp.text


def test_admin_with_module_revoked_is_never_blocked(db_session, admin_user, monkeypatch):
    """Admin is never blocked even with an explicit revoke override — user_has_access
    short-circuits True for admins."""
    admin_user.access_overrides = {str(AccessKey.RESELL): False}
    db_session.add(admin_user)
    db_session.commit()
    with _live_client(db_session, monkeypatch) as c:
        c.cookies.set("session", _sign_session(admin_user.id))
        resp = c.get("/v2/partials/resell/lists")
    assert resp.status_code == 200
    assert DENIED_BODY not in resp.text


def test_no_session_is_passed_through(db_session, monkeypatch):
    """No session cookie → middleware passes through; the route's own auth answers
    (401), and the middleware never emits its 403."""
    with _live_client(db_session, monkeypatch) as c:
        resp = c.get("/v2/partials/resell/lists")
    assert resp.status_code != 403
    assert DENIED_BODY not in resp.text
