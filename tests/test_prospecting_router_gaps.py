"""tests/test_prospecting_router_gaps.py — Coverage gap tests for
app/routers/htmx/prospecting.py.

Targets uncovered lines:
  65-66  : _enrich_is_stale with invalid ISO string (ValueError / TypeError paths)
  252    : prospecting list default sort branch (unknown sort= value)
  331-351: add_prospect_domain ValueError/RuntimeError error path
  408    : claim endpoint second 404 (prospect gone after claim service succeeds)
  487    : release endpoint second 404 (prospect gone after release service succeeds)
  583-608: reclaim endpoint body (happy path, LookupError, RuntimeError, ValueError)
  640    : reassign endpoint 404 when prospect not found
  642    : reassign endpoint 400 when prospect has no company_id
  648-657: reassign endpoint PermissionError / LookupError / ValueError branches

Called by: pytest autodiscovery
Depends on: conftest.py (client, db_session, test_user, admin_user, manager_user),
            app.routers.htmx.prospecting, app.services.prospect_claim,
            app.services.prospect_reclamation
"""

import os

os.environ["TESTING"] = "1"

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company
from app.models.prospect_account import ProspectAccount

# ── helpers ──────────────────────────────────────────────────────────────────


def make_prospect(db: Session, **kw) -> ProspectAccount:
    defaults = dict(
        name=f"GapProspect-{uuid.uuid4().hex[:6]}",
        domain=f"gap-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=70,
        readiness_score=55,
        discovery_source="manual",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@contextmanager
def _client_as(db_session: Session, user):
    """Yield a TestClient authenticated as *user*, restoring overrides on exit."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


# ── 1. _enrich_is_stale: lines 65-66 (ValueError / TypeError) ────────────────


class TestEnrichIsStaleInvalidIso:
    def test_invalid_iso_string_returns_false(self):
        """ValueError path — 'not-a-date' triggers datetime.fromisoformat ValueError."""
        from app.routers.htmx.prospecting import _enrich_is_stale

        result = _enrich_is_stale("not-a-date-string")
        assert result is False

    def test_integer_input_returns_false(self):
        """TypeError path — integer passed to fromisoformat raises TypeError."""
        from app.routers.htmx.prospecting import _enrich_is_stale

        result = _enrich_is_stale(99999)  # type: ignore[arg-type]
        assert result is False


# ── 2. Prospecting list: line 252 (default/unknown sort) ─────────────────────


class TestListDefaultSort:
    def test_unknown_sort_value_uses_readiness_order(self, client, db_session):
        """sort=anything_unknown falls into else branch, ordering by readiness DESC."""
        p_hi = make_prospect(db_session, name="HighReadiness", readiness_score=90)
        p_lo = make_prospect(db_session, name="LowReadiness", readiness_score=10)
        resp = client.get("/v2/partials/prospecting?sort=readiness_desc")
        assert resp.status_code == 200
        # Higher readiness should rank first under the default ordering
        assert resp.text.index("HighReadiness") < resp.text.index("LowReadiness")


# ── 3. add_prospect_domain: lines 331-351 (ValueError + RuntimeError paths) ──


class TestAddDomainErrorPaths:
    def test_value_error_returns_error_chip_with_toast(self, client):
        """ValueError from add_prospect_manually produces a rose-coloured error chip."""
        with patch(
            "app.services.prospect_claim.add_prospect_manually",
            side_effect=ValueError("domain already exists"),
        ):
            resp = client.post(
                "/v2/partials/prospecting/add-domain",
                data={"domain": "duplicate.com"},
            )
        assert resp.status_code == 200
        assert "Could not add" in resp.text
        assert "HX-Trigger" in resp.headers
        assert "error" in resp.headers["HX-Trigger"]

    def test_runtime_error_returns_error_chip(self, client):
        """RuntimeError from add_prospect_manually produces the same error chip."""
        with patch(
            "app.services.prospect_claim.add_prospect_manually",
            side_effect=RuntimeError("external connector down"),
        ):
            resp = client.post(
                "/v2/partials/prospecting/add-domain",
                data={"domain": "rterror.com"},
            )
        assert resp.status_code == 200
        assert "Could not add" in resp.text
        assert "HX-Trigger" in resp.headers

    def test_error_chip_contains_escaped_domain(self, client):
        """Domain name is HTML-escaped in the error chip (html.escape applied)."""
        with patch(
            "app.services.prospect_claim.add_prospect_manually",
            side_effect=ValueError("bad"),
        ):
            resp = client.post(
                "/v2/partials/prospecting/add-domain",
                data={"domain": "evil<script>.com"},
            )
        assert resp.status_code == 200
        # The raw < from the domain must appear as &lt; — not a literal <script> tag
        assert "&lt;script&gt;" in resp.text
        assert "<script>" not in resp.text


# ── 4. Claim endpoint: line 408 (second 404) ─────────────────────────────────


class TestClaimSecond404:
    def test_returns_404_when_prospect_gone_after_claim_succeeds(self, client):
        """Mocking claim_prospect to succeed but using a non-existent ID so the
        subsequent DB look-up returns None, hitting the second 404 guard."""
        with (
            patch("app.services.prospect_claim.claim_prospect"),
            patch(
                "app.services.prospect_claim.trigger_deep_enrichment_bg",
                return_value=MagicMock(),
            ),
            patch(
                "app.utils.async_helpers.safe_background_task",
                new_callable=AsyncMock,
            ),
        ):
            resp = client.post("/v2/partials/prospecting/999998/claim")
        assert resp.status_code == 404


# ── 5. Release endpoint: line 487 (second 404) ───────────────────────────────


class TestReleaseSecond404:
    def test_returns_404_when_prospect_gone_after_release_succeeds(self, client):
        """Mocking release_prospect to succeed; non-existent ID → second 404."""
        with patch("app.services.prospect_claim.release_prospect"):
            resp = client.post("/v2/partials/prospecting/999997/release")
        assert resp.status_code == 404


# ── 6. Reclaim endpoint: lines 583-608 ───────────────────────────────────────


class TestReclaimEndpoint:
    def test_lookup_error_returns_404(self, client, db_session):
        """LookupError from reclaim_prospect_account → HTTPException 404."""
        p = make_prospect(db_session)
        with patch(
            "app.services.prospect_reclamation.reclaim_prospect_account",
            side_effect=LookupError("prospect missing"),
        ):
            resp = client.post(f"/v2/partials/prospects/{p.id}/reclaim")
        assert resp.status_code == 404

    def test_runtime_error_returns_500(self, client, db_session):
        """RuntimeError from reclaim_prospect_account → HTTPException 500."""
        p = make_prospect(db_session)
        with patch(
            "app.services.prospect_reclamation.reclaim_prospect_account",
            side_effect=RuntimeError("session user record not found"),
        ):
            resp = client.post(f"/v2/partials/prospects/{p.id}/reclaim")
        assert resp.status_code == 500

    def test_value_error_surfaces_as_error_toast(self, client, db_session):
        """ValueError → error is captured and returned via _prospect_action_response."""
        p = make_prospect(db_session)
        with (
            patch(
                "app.services.prospect_reclamation.reclaim_prospect_account",
                side_effect=ValueError("reclaim cooldown active"),
            ),
            patch(
                "app.routers.htmx.prospecting.template_response",
                return_value=HTMLResponse("<html/>"),
            ),
        ):
            resp = client.post(f"/v2/partials/prospects/{p.id}/reclaim")
        assert resp.status_code == 200
        assert "error" in resp.headers.get("HX-Trigger", "")

    def test_happy_path_returns_success_toast(self, client, db_session):
        """Successful reclaim returns 200 with success toast."""
        p = make_prospect(db_session)
        with (
            patch(
                "app.services.prospect_reclamation.reclaim_prospect_account",
                return_value={"company_name": "Reclaimed Corp"},
            ),
            patch(
                "app.routers.htmx.prospecting.template_response",
                return_value=HTMLResponse("<html/>"),
            ),
        ):
            resp = client.post(f"/v2/partials/prospects/{p.id}/reclaim")
        assert resp.status_code == 200
        assert "success" in resp.headers.get("HX-Trigger", "")


# ── 7. Reassign endpoint: lines 640, 642, 648-657 ────────────────────────────


class TestReassignEndpointErrors:
    def test_missing_prospect_returns_error_toast(self, db_session, admin_user):
        """Unknown prospect_id → 200 + error showToast (DC-02 toast pattern; a 4xx would
        leave the HTMX modal with zero feedback)."""
        with _client_as(db_session, admin_user) as c:
            resp = c.post(
                "/v2/partials/prospects/999996/reassign",
                data={"to_user_id": str(admin_user.id)},
            )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "Prospect not found" in trigger

    def test_no_company_returns_error_toast(self, db_session, admin_user):
        """prospect.company_id is None → 200 + 'not linked to a company' error toast."""
        p = make_prospect(db_session, company_id=None)
        with _client_as(db_session, admin_user) as c:
            resp = c.post(
                f"/v2/partials/prospects/{p.id}/reassign",
                data={"to_user_id": str(admin_user.id)},
            )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "not linked to a company" in trigger

    def test_permission_error_returns_error_toast(self, db_session, admin_user):
        """PermissionError from reassign_account → 200 + its message as an error
        toast."""
        co = Company(
            name="PermCo",
            domain=f"permco-{uuid.uuid4().hex[:6]}.com",
            is_active=True,
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        p = make_prospect(db_session, company_id=co.id)

        with _client_as(db_session, admin_user) as c:
            with patch(
                "app.services.prospect_reclamation.reassign_account",
                side_effect=PermissionError("not your territory"),
            ):
                resp = c.post(
                    f"/v2/partials/prospects/{p.id}/reassign",
                    data={"to_user_id": str(admin_user.id)},
                )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "not your territory" in trigger

    def test_lookup_error_returns_error_toast(self, db_session, admin_user):
        """LookupError from reassign_account → 200 + 'Company not found' error toast."""
        co = Company(
            name="LookupCo",
            domain=f"lookupco-{uuid.uuid4().hex[:6]}.com",
            is_active=True,
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        p = make_prospect(db_session, company_id=co.id)

        with _client_as(db_session, admin_user) as c:
            with patch(
                "app.services.prospect_reclamation.reassign_account",
                side_effect=LookupError("company vanished"),
            ):
                resp = c.post(
                    f"/v2/partials/prospects/{p.id}/reassign",
                    data={"to_user_id": str(admin_user.id)},
                )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "Company not found" in trigger

    def test_value_error_surfaces_as_error_toast(self, db_session, admin_user):
        """Line 653: ValueError from reassign_account → error captured, 200 response."""
        co = Company(
            name="ValErrCo",
            domain=f"valerr-{uuid.uuid4().hex[:6]}.com",
            is_active=True,
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        p = make_prospect(db_session, company_id=co.id)

        with _client_as(db_session, admin_user) as c:
            with (
                patch(
                    "app.services.prospect_reclamation.reassign_account",
                    side_effect=ValueError("cooldown not expired yet"),
                ),
                patch(
                    "app.routers.htmx.prospecting.template_response",
                    return_value=HTMLResponse("<html/>"),
                ),
            ):
                resp = c.post(
                    f"/v2/partials/prospects/{p.id}/reassign",
                    data={"to_user_id": str(admin_user.id)},
                )
        assert resp.status_code == 200
        assert "error" in resp.headers.get("HX-Trigger", "")
