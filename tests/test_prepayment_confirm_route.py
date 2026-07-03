"""test_prepayment_confirm_route.py — the PUBLIC tokenized confirm-paid route (Task 5).

Covers app.routers.prepayment_confirm (GET/POST /p/confirm/{token}) + the confirm link the
approval notice embeds:
  - POST confirm marks an approved prepayment PAID with NO login, stamps
    paid_via="accounting_email", and clears the single-use pay_token;
  - replaying a spent (cleared) token is inert → 404 (no re-mark, no double-fire);
  - a paid-but-still-tokenized prepayment (the pre-clear race window) renders the read-only
    "already paid" page and is never re-marked;
  - a voided prepayment's token renders the DO-NOT-WIRE page and CANNOT be paid;
  - an unknown token → 404;
  - the route is reachable with NO auth dependency (proved via the unauthenticated client)
    and its path is CSRF-exempt;
  - the approved-notice email body + Teams card embed the /p/confirm/ link.

Called by: pytest
Depends on: app.routers.prepayment_confirm, app.services.prepayment_notifications,
            app.main (CSRF_EXEMPT_URLS), conftest (db_session, unauthenticated_client),
            tests.test_po_line_signoff (_make_plan/_make_user builders).
"""

from __future__ import annotations

import json
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import PrepaymentStatus
from app.models import ActivityLog
from app.models.quality_plan import Prepayment
from app.services import prepayment_notifications as pn

# Reuse the plan/user builders the sibling prepayment tests rely on.
from tests.test_po_line_signoff import _make_plan, _make_user


def _build_prepay(
    db: Session,
    *,
    status: str,
    pay_token: str | None,
    void_reason: str | None = None,
    paid_by_label: str | None = None,
) -> Prepayment:
    """A minimal Prepayment in *status* on a fresh plan (buy_plan_id is NOT NULL)."""
    u = _make_user(db)
    plan = _make_plan(db, u)
    pp = Prepayment(
        buy_plan_id=plan.id,
        vendor_name="Acme Components LLC",
        total_incl_fees=Decimal("20002.38"),
        currency="USD",
        created_by_id=u.id,
        status=status,
        pay_token=pay_token,
        void_reason=void_reason,
        paid_by_label=paid_by_label,
    )
    db.add(pp)
    db.commit()
    return pp


# ── Happy path: public POST marks paid, stamps source, clears token ───────


def test_confirm_marks_paid_no_login(unauthenticated_client: TestClient, db_session: Session):
    pp = _build_prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-approved-abc123")
    token = pp.pay_token

    r = unauthenticated_client.post(f"/p/confirm/{token}", data={"wire_reference": "W1", "confirmer": "Katy"})

    assert r.status_code == 200, r.text
    assert "thank you" in r.text.lower()
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.paid_via == "accounting_email"
    assert pp.paid_by_label == "Katy"
    assert pp.wire_reference == "W1"
    assert pp.paid_amount == Decimal("20002.38")
    assert pp.paid_at is not None
    assert pp.pay_token is None  # single-use token cleared


def test_confirm_get_shows_form_for_approved(unauthenticated_client: TestClient, db_session: Session):
    pp = _build_prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-form-xyz789")

    r = unauthenticated_client.get(f"/p/confirm/{pp.pay_token}")

    assert r.status_code == 200, r.text
    assert "confirm wire sent" in r.text.lower()
    assert "20,002.38" in r.text  # amount summary rendered
    assert "Acme Components LLC" in r.text  # beneficiary summary


# ── Idempotency: a spent (cleared) token is inert; a paid-token replay no-ops ──


def test_confirm_spent_token_replay_is_inert_404(unauthenticated_client: TestClient, db_session: Session):
    """After the wire is confirmed the token is cleared, so the emailed link is single-
    use:

    replaying it resolves to nothing → 404, never re-marking or re-firing.
    """
    pp = _build_prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-spent-111")
    token = pp.pay_token
    first = unauthenticated_client.post(f"/p/confirm/{token}", data={"confirmer": "Katy"})
    assert first.status_code == 200

    # The same (now-cleared) token cannot act again.
    assert unauthenticated_client.get(f"/p/confirm/{token}").status_code == 404
    assert unauthenticated_client.post(f"/p/confirm/{token}", data={}).status_code == 404


def test_confirm_already_paid_token_is_idempotent(unauthenticated_client: TestClient, db_session: Session):
    """A paid prepayment that still resolves by token (the pre-clear race window)
    renders the read-only "already paid" page and is NEVER re-marked or re-fired."""
    pp = _build_prepay(
        db_session,
        status=PrepaymentStatus.PAID.value,
        pay_token="tok-paid-222",
        paid_by_label="Original",
    )

    before = db_session.query(ActivityLog).filter_by(channel="system").count()
    r_get = unauthenticated_client.get(f"/p/confirm/{pp.pay_token}")
    r_post = unauthenticated_client.post(f"/p/confirm/{pp.pay_token}", data={"confirmer": "Someone Else"})

    assert r_get.status_code == 200 and "already" in r_get.text.lower()
    assert r_post.status_code == 200 and "already" in r_post.text.lower()
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.paid_by_label == "Original"  # not overwritten by the replay
    # No paid fan-out re-fired.
    assert db_session.query(ActivityLog).filter_by(channel="system").count() == before


# ── Void safety: the DO-NOT-WIRE page, and the token cannot pay ───────────


def test_confirm_voided_token_shows_do_not_wire(unauthenticated_client: TestClient, db_session: Session):
    pp = _build_prepay(
        db_session,
        status=PrepaymentStatus.VOID.value,
        pay_token="tok-void-333",
        void_reason="plan cancelled",
    )

    r = unauthenticated_client.get(f"/p/confirm/{pp.pay_token}")

    assert r.status_code == 200, r.text
    assert "do not wire" in r.text.lower() or "voided" in r.text.lower()
    assert "plan cancelled" in r.text


def test_confirm_voided_token_cannot_be_paid(unauthenticated_client: TestClient, db_session: Session):
    pp = _build_prepay(
        db_session,
        status=PrepaymentStatus.VOID.value,
        pay_token="tok-void-444",
        void_reason="plan cancelled",
    )

    r = unauthenticated_client.post(f"/p/confirm/{pp.pay_token}", data={"confirmer": "Katy"})

    assert r.status_code == 200
    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.VOID.value  # stays void — never paid
    assert pp.paid_at is None and pp.paid_via is None


# ── Unknown token → 404 ───────────────────────────────────────────────────


def test_confirm_unknown_token_404(unauthenticated_client: TestClient):
    assert unauthenticated_client.get("/p/confirm/nope").status_code == 404
    assert unauthenticated_client.post("/p/confirm/nope", data={}).status_code == 404


# ── Security posture: no auth dependency + CSRF-exempt path ────────────────


def test_confirm_route_has_no_auth_dependency():
    """The public route must not carry require_user/require_admin/require_buyer — the
    token IS the authorization.

    (CSRF is disabled under TESTING, so we assert the dependency posture directly rather
    than exercising the middleware.)
    """
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    auth_deps = {require_user, require_admin, require_buyer}
    confirm_routes = [r for r in app.routes if getattr(r, "path", "") == "/p/confirm/{token}"]
    assert confirm_routes, "confirm route not registered"
    for route in confirm_routes:
        dep_calls = {d.call for d in route.dependant.dependencies}
        assert not (dep_calls & auth_deps), f"{route.methods} /p/confirm/{{token}} must be auth-less"


def test_confirm_path_is_csrf_exempt():
    from app.main import CSRF_EXEMPT_URLS

    assert any(p.match("/p/confirm/some-token") for p in CSRF_EXEMPT_URLS)


# ── The approval notice embeds the /p/confirm/ link ───────────────────────


def test_approved_email_and_card_embed_confirm_link(db_session: Session):
    pp = _build_prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-email-555")

    body = pn._email_html(pp, "approved")
    assert "/p/confirm/" in body
    assert pp.pay_token in body
    assert "Confirm wire sent" in body

    card_text = json.dumps(pn._card(pp, "approved"))
    assert "/p/confirm/" in card_text
    assert pp.pay_token in card_text


def test_approved_link_absent_without_token(db_session: Session):
    """No live pay_token (e.g. already spent) → no confirm button leaks into the
    notice."""
    pp = _build_prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token=None)

    assert "/p/confirm/" not in pn._email_html(pp, "approved")
    assert "/p/confirm/" not in json.dumps(pn._card(pp, "approved"))
