"""End-to-end display-timezone propagation test (the coverage gap that let the bug
ship).

Drives a REAL request through the actual ASGI middleware stack + the REAL (un-overridden)
``require_user`` sync dependency via ``TestClient`` — so ``require_user`` genuinely runs in
the threadpool, exactly as in production. A user whose ``display_timezone='Asia/Tokyo'``
hits a probe endpoint that renders a KNOWN UTC instant through the real ``|localtime`` /
``|localdate`` Jinja filters, and we assert the output is in Tokyo — NOT the Eastern
business default.

Why the prior suite missed the bug: every existing client fixture overrides
``require_user``, so the real sync dependency (whose contextvar ``.set()`` lands in a
DISCARDED threadpool-context copy) never ran end-to-end. A direct ``contextvar.set()`` unit
test cannot catch this; only a real request through the middleware + threadpool can. This
test FAILS against the pre-fix code (render shows Eastern) and PASSES after the fix (the
middleware sets the zone in the async context, which propagates to the threadpool).

Called by: pytest
Depends on: app.main.app (middleware stack + routes), app.dependencies.require_user (real),
    app.request_context (resolver/cache), app.template_env (localtime/localdate filters).
"""

import base64
import json
from datetime import datetime, timezone

import itsdangerous
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.responses import PlainTextResponse

# A fixed UTC instant that straddles the day boundary for both zones we assert on:
#   Eastern (UTC-4, summer) → Jul 04, 19:30  / date 2026-07-04
#   Asia/Tokyo (UTC+9)      → Jul 05, 08:30  / date 2026-07-05
_PROBE_UTC = datetime(2026, 7, 4, 23, 30, tzinfo=timezone.utc)
_PROBE_PATH = "/v2/__display_tz_probe__"
_EASTERN_EXPECTED = "Jul 04, 19:30|2026-07-04"
_TOKYO_EXPECTED = "Jul 05, 08:30|2026-07-05"


@pytest.fixture()
def real_auth_client(db_session, monkeypatch):
    """TestClient authed via a REAL session cookie, with ``require_user`` LEFT REAL.

    - ``get_db`` is overridden to the in-memory test session (so the real ``require_user``
      loads the user from the test DB).
    - ``require_user`` is intentionally NOT overridden → it runs for real in the threadpool.
    - ``app.database.SessionLocal`` is monkeypatched to the test engine so the middleware's
      out-of-DI ``resolve_display_tz`` query hits the same DB the fixtures populate.
    - A temporary probe route renders the known UTC instant via ``|localtime``/``|localdate``.
    """
    from app.config import settings
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.request_context import clear_display_tz_cache
    from app.template_env import templates

    # Point the middleware's direct SessionLocal() at the test engine (same DB as db_session).
    test_sessionmaker = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    monkeypatch.setattr("app.database.SessionLocal", test_sessionmaker)

    # A SYNC endpoint (def) depending on the REAL require_user — both run in the threadpool,
    # the exact production shape. Render both filters from the live Jinja env.
    def probe(user=Depends(require_user)) -> PlainTextResponse:
        rendered = templates.env.from_string(
            "{{ dt|localtime('%b %d, %H:%M') }}|{{ dt|localdate('%Y-%m-%d') }}"
        ).render(dt=_PROBE_UTC)
        return PlainTextResponse(rendered)

    app.add_api_route(_PROBE_PATH, probe, methods=["GET"])
    # Real require_user needs the test DB session; do NOT override require_user itself.
    app.dependency_overrides[get_db] = lambda: db_session

    signer = itsdangerous.TimestampSigner(str(settings.secret_key))

    def _make_client(user):
        clear_display_tz_cache()
        cookie = signer.sign(base64.b64encode(json.dumps({"user_id": user.id}).encode())).decode()
        c = TestClient(app)
        c.cookies.set("session", cookie)
        return c

    try:
        yield _make_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != _PROBE_PATH]
        clear_display_tz_cache()


def _authed_user(db_session, tz):
    from app.models import User

    user = User(
        email="tzviewer@trioscs.com",
        name="TZ Viewer",
        role="buyer",
        azure_id="test-azure-tz-viewer",
        is_active=True,
        display_timezone=tz,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestDisplayTzPropagatesToRealRequest:
    def test_tokyo_viewer_renders_in_tokyo_not_eastern(self, real_auth_client, db_session):
        """The load-bearing regression test: a Tokyo user's real request renders in Tokyo.

        FAILS on the pre-fix code (require_user's threadpool set() is discarded → Eastern
        default) and PASSES after the middleware sets the zone in the async context.
        """
        user = _authed_user(db_session, "Asia/Tokyo")
        resp = real_auth_client(user).get(_PROBE_PATH)
        assert resp.status_code == 200
        assert resp.text == _TOKYO_EXPECTED, (
            f"expected Tokyo render {_TOKYO_EXPECTED!r}, got {resp.text!r} — the viewer's "
            "display_timezone did not reach the request/template render"
        )

    def test_null_tz_viewer_falls_back_to_eastern_default(self, real_auth_client, db_session):
        """A user with no display_timezone still renders in the business default
        (control)."""
        user = _authed_user(db_session, None)
        resp = real_auth_client(user).get(_PROBE_PATH)
        assert resp.status_code == 200
        assert resp.text == _EASTERN_EXPECTED
