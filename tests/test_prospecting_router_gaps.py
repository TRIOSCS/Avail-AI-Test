"""tests/test_prospecting_router_gaps.py — Coverage gap tests for
app/routers/htmx/prospecting.py.

Targets uncovered lines:
  65-66  : _enrich_is_stale with invalid ISO string (ValueError / TypeError paths)
  252    : prospecting list default sort branch (unknown sort= value)
  331-351: add_prospect_domain ValueError/RuntimeError error path
  408    : claim endpoint second 404 (prospect gone after claim service succeeds)
  487    : release endpoint second 404 (prospect gone after release service succeeds)
  assign : manager-only Assign endpoint — the 403 gate + service-error → error-toast
           branches (the reclaim/reassign endpoints it replaced were retired in the O-rework)

Called by: pytest autodiscovery
Depends on: conftest.py (client, db_session, test_user, admin_user, manager_user),
            app.routers.htmx.prospecting, app.services.prospect_claim
"""

import os

os.environ["TESTING"] = "1"

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

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
        created_at=datetime.now(UTC),
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


# ── 6. Assign endpoint (O-rework): manager gate + error branches ─────────────
# The reclaim/reassign endpoints were retired; the manager-only Assign action replaced
# them. Behavioral coverage lives in test_prospecting_o_rework.py — here we pin the router
# branches: the manager 403 gate and the service-error → error-toast path.


class TestAssignEndpoint:
    def test_non_manager_post_is_403(self, client, db_session):
        """The default client is a buyer — a non-manager Assign POST is a 403."""
        p = make_prospect(db_session)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/assign",
            data={"to_user_id": "1"},
        )
        assert resp.status_code == 403

    def test_service_value_error_surfaces_as_error_toast(self, db_session, admin_user):
        """ValueError from assign_prospect → 200 + error showToast, HX-Reswap:none (the
        modal keeps its context instead of a silently-suppressed 4xx)."""
        p = make_prospect(db_session)
        with _client_as(db_session, admin_user) as c:
            with patch(
                "app.services.prospect_claim.assign_prospect",
                side_effect=ValueError("Target user is inactive"),
            ):
                resp = c.post(
                    f"/v2/partials/prospects/{p.id}/assign",
                    data={"to_user_id": str(admin_user.id)},
                )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "inactive" in trigger

    def test_missing_prospect_after_assign_returns_error_toast(self, db_session, admin_user):
        """assign_prospect mocked to succeed on a non-existent id → the second look-up
        returns None → error toast (never a bare 4xx that no-ops the modal)."""
        with _client_as(db_session, admin_user) as c:
            with (
                patch("app.services.prospect_claim.assign_prospect"),
                patch(
                    "app.services.prospect_claim.trigger_deep_enrichment_bg",
                    return_value=MagicMock(),
                ),
                patch(
                    "app.utils.async_helpers.safe_background_task",
                    new_callable=AsyncMock,
                ),
            ):
                resp = c.post(
                    "/v2/partials/prospects/999996/assign",
                    data={"to_user_id": str(admin_user.id)},
                )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "error" in trigger
        assert "Prospect not found" in trigger
