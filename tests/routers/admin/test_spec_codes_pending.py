"""Smoke tests for the pending OEM spec-code approval admin router.

Covers: list page render, approve (with and without buyer-edited AVL), reject
(MPNs default to all proposed when rejected_mpns is empty), and reason
validation. Uses a local admin TestClient fixture matching the pattern from
tests/test_routers_admin.py.

Called by: pytest
Depends on: app/routers/admin/spec_codes.py, conftest.py (db_session, admin_user)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)

# ── Test client fixture ───────────────────────────────────────────────


@pytest.fixture()
def client_with_settings_user(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with overrides for settings-access (admin) auth."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_admin
    app.dependency_overrides[require_user] = _override_admin

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_admin, require_settings_access, require_user]:
            app.dependency_overrides.pop(dep, None)


# ── Pending row fixture ───────────────────────────────────────────────


@pytest.fixture()
def pending_row(db_session: Session) -> OemSpecCodePending:
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {
                "mpn": "GRM188R71H103KA01D",
                "manufacturer": "Murata",
                "rank": 1,
                "notes": None,
            }
        ],
        llm_confidence=0.8,
        citations=[{"url": "https://example.com", "snippet": "datasheet"}],
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# ── Tests ─────────────────────────────────────────────────────────────


def test_list_pending_returns_200(client_with_settings_user, pending_row):
    resp = client_with_settings_user.get("/admin/spec-codes/pending")
    assert resp.status_code == 200
    assert b"SPREJ" in resp.content


def test_approve_promotes_to_oem_spec_codes(client_with_settings_user, pending_row, db_session, admin_user):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    promoted = db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert promoted.source == "llm_approved"
    assert promoted.approved_by_user_id == admin_user.id
    assert promoted.approved_at is not None
    assert promoted.avl[0]["mpn"] == "GRM188R71H103KA01D"


def test_approve_with_edited_avl_uses_edited(client_with_settings_user, pending_row, db_session):
    edited = [
        {
            "mpn": "CORRECTED_MPN",
            "manufacturer": "Murata",
            "rank": 1,
            "notes": "corrected by buyer",
        }
    ]
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": edited},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    promoted = db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert promoted.avl[0]["mpn"] == "CORRECTED_MPN"
    assert promoted.avl[0]["notes"] == "corrected by buyer"


def test_reject_moves_mpns_to_blacklist(client_with_settings_user, pending_row, db_session, admin_user):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"reason": "wrong package", "rejected_mpns": []},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    bl = db_session.query(OemSpecCodeBlacklist).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert "GRM188R71H103KA01D" in bl.rejected_mpns
    assert bl.reason == "wrong package"
    assert bl.rejected_by_user_id == admin_user.id


def test_reject_requires_reason(client_with_settings_user, pending_row):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"rejected_mpns": []},  # no reason field
    )
    assert resp.status_code == 422


def test_re_resolve_escapes_html_in_unresolved_response(client_with_settings_user, db_session, monkeypatch):
    """Defense-in-depth: the re-resolve unresolved-response path must escape
    HTML in spec_code so a malicious payload that somehow got persisted (e.g.
    via a future bug or a direct DB write) can't fire as XSS when an admin
    clicks Re-resolve.

    Bypasses the `@validates` decorator on OemSpecCodePending.spec_code by
    issuing a raw INSERT, since the validator only uppercases/strips — it
    does NOT strip angle brackets or quotes.
    """
    from sqlalchemy import text

    from app.services import spec_code_resolver as resolver_mod
    from app.services.spec_code_resolver import ResolverResult

    # Raw INSERT bypasses the @validates hook on the ORM model.
    db_session.execute(
        text(
            """
            INSERT INTO oem_spec_codes_pending
                (oem, spec_code, proposed_avl, llm_confidence, citations,
                 used_in_requirement_ids)
            VALUES
                (:oem, :spec_code, :proposed_avl, :llm_confidence, :citations,
                 :used_in_requirement_ids)
            """
        ),
        {
            "oem": "IBM",
            "spec_code": "<script>alert(1)</script>",
            "proposed_avl": '[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": null}]',
            "llm_confidence": 0.5,
            "citations": "[]",
            "used_in_requirement_ids": "[]",
        },
    )
    db_session.commit()
    pending = (
        db_session.query(OemSpecCodePending).filter(OemSpecCodePending.spec_code == "<script>alert(1)</script>").one()
    )

    async def fake_propose(self, spec_code, oem="IBM", *, allow_pending_reuse=True):
        return ResolverResult(status="unresolved"), None

    monkeypatch.setattr(resolver_mod.SpecCodeResolver, "propose", fake_propose)

    resp = client_with_settings_user.post(f"/admin/spec-codes/pending/{pending.id}/re-resolve")
    assert resp.status_code == 200
    # Literal `<script>` MUST NOT appear in the rendered HTML; it must be
    # HTML-escaped (e.g. `&lt;script&gt;`) so the browser cannot parse it as
    # an executable tag.
    assert b"<script>" not in resp.content
    assert b"&lt;script&gt;" in resp.content


def test_re_resolve_preserves_row_when_resolver_returns_unresolved(
    client_with_settings_user,
    pending_row,
    db_session,
    monkeypatch,
):
    """A re-resolve that legitimately comes back ``unresolved`` must NOT throw away the
    original pending row.

    Today the buyer audit trail (citations, confidence, proposed AVL) is the only record
    of what the LLM previously proposed; silently deleting it on a fresh miss is
    irreversible data loss. The endpoint must preserve the row and tell the admin
    nothing new was found.
    """

    async def fake_propose(self, spec_code, oem="IBM", *, allow_pending_reuse=True):
        from app.services.spec_code_resolver import ResolverResult

        return ResolverResult(status="unresolved"), None

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.propose",
        fake_propose,
    )

    pending_id = pending_row.id
    resp = client_with_settings_user.post(f"/admin/spec-codes/pending/{pending_id}/re-resolve")
    assert resp.status_code == 200

    # The original pending row and its full audit trail must survive.
    db_session.expire_all()
    still_there = db_session.get(OemSpecCodePending, pending_id)
    assert still_there is not None, "unresolved re-resolve must not delete the audit trail"
    assert still_there.spec_code == "SPREJ"
    assert still_there.proposed_avl[0]["mpn"] == "GRM188R71H103KA01D"
    assert still_there.citations[0]["url"] == "https://example.com"
    assert still_there.llm_confidence == 0.8


def test_re_resolve_swaps_row_on_fresh_success(
    client_with_settings_user,
    pending_row,
    db_session,
    monkeypatch,
):
    """A successful fresh re-resolve replaces the old pending row with the new proposal
    (old gone, new persisted) — the success path the original code never covered."""

    async def fake_propose(self, spec_code, oem="IBM", *, allow_pending_reuse=True):
        from app.services.spec_code_resolver import ResolverResult

        result = ResolverResult(
            status="pending",
            avl=[{"mpn": "NEW_MPN", "manufacturer": "Vishay", "rank": 1, "notes": None}],
            confidence=0.91,
            citations=[{"url": "https://new.example.com", "snippet": "fresh"}],
            source="llm",
        )
        payload = {
            "oem": oem,
            "spec_code": spec_code,
            "proposed_avl": result.avl,
            "llm_confidence": result.confidence,
            "citations": result.citations,
        }
        return result, payload

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.propose",
        fake_propose,
    )

    resp = client_with_settings_user.post(f"/admin/spec-codes/pending/{pending_row.id}/re-resolve")
    assert resp.status_code == 200

    db_session.expire_all()
    # Exactly one pending row remains, carrying the FRESH proposal — the old
    # GRM188R71H103KA01D proposal has been swapped out for NEW_MPN. (We assert by
    # data, not row id: SQLite reuses the deleted rowid for the replacement insert.)
    rows = db_session.query(OemSpecCodePending).filter_by(oem="IBM", spec_code="SPREJ").all()
    assert len(rows) == 1
    assert rows[0].proposed_avl[0]["mpn"] == "NEW_MPN"
    assert rows[0].llm_confidence == 0.91
    assert all(r.proposed_avl[0]["mpn"] != "GRM188R71H103KA01D" for r in rows)


def test_re_resolve_preserves_row_on_resolver_exception(
    client_with_settings_user,
    pending_row,
    db_session,
    monkeypatch,
):
    """If the resolver raises during re-resolve, the original pending row must NOT be
    destroyed.

    The route must return a friendly error fragment, not a 500.
    """

    async def fake_propose(self, spec_code, oem="IBM", *, allow_pending_reuse=True):
        raise RuntimeError("LLM API down")

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.propose",
        fake_propose,
    )

    pending_id = pending_row.id
    resp = client_with_settings_user.post(f"/admin/spec-codes/pending/{pending_id}/re-resolve")
    assert resp.status_code == 200
    # The error variant of the partial mentions the preservation.
    assert b"original" in resp.content.lower() or b"preserved" in resp.content.lower()

    # The original pending row must still exist with its data intact.
    db_session.expire_all()
    still_there = db_session.get(OemSpecCodePending, pending_id)
    assert still_there is not None
    assert still_there.spec_code == "SPREJ"
    assert still_there.oem == "IBM"
    assert still_there.proposed_avl[0]["mpn"] == "GRM188R71H103KA01D"


def test_approve_concurrent_returns_409_not_500(
    client_with_settings_user,
    pending_row,
    db_session,
):
    """If two admins approve the same (oem, spec_code) mapping concurrently, the loser
    must get a clean 409 — not a 500 from the uq_oem_spec_code IntegrityError leaking up
    the stack."""
    from datetime import datetime, timezone

    # Simulate "another admin already approved" by pre-inserting a
    # conflicting OemSpecCode row that will collide with the approve.
    db_session.add(
        OemSpecCode(
            oem=pending_row.oem,
            spec_code=pending_row.spec_code,
            avl=[{"mpn": "OTHER", "manufacturer": "X", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 409


def _hx_trigger_toast_message(resp) -> str | None:
    """Extract the showToast message from an HX-Trigger response header, if any."""
    import json

    raw = resp.headers.get("HX-Trigger")
    if not raw:
        return None
    payload = json.loads(raw)
    return payload.get("showToast", {}).get("message")


def test_approve_emits_toast_header(client_with_settings_user, pending_row):
    """Approve must fire a showToast HX-Trigger so the row doesn't vanish silently (Task
    4.4)."""
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 200
    assert _hx_trigger_toast_message(resp) is not None
    assert "IBM" in _hx_trigger_toast_message(resp)


def test_reject_emits_toast_header(client_with_settings_user, pending_row):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"reason": "wrong package", "rejected_mpns": []},
    )
    assert resp.status_code == 200
    assert _hx_trigger_toast_message(resp) is not None
    assert "IBM" in _hx_trigger_toast_message(resp)


def test_approve_last_row_renders_empty_state_oob(client_with_settings_user, pending_row):
    """Removing the LAST pending row must OOB-swap in the empty-state so the page
    doesn't show a headerless empty table (Task 4.4)."""
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'hx-swap-oob="true"' in body
    assert 'id="spec-codes-content"' in body
    assert "No pending mappings" in body


def test_reject_with_remaining_rows_no_empty_state(client_with_settings_user, pending_row, db_session):
    """When other pending rows remain, the response body stays empty (just the row
    removal) — no empty-state OOB swap."""
    # Seed a second pending row so one survives the reject.
    db_session.add(
        OemSpecCodePending(
            oem="DELL",
            spec_code="OTHER",
            proposed_avl=[{"mpn": "Y", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.7,
            citations=[],
        )
    )
    db_session.commit()

    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"reason": "wrong package", "rejected_mpns": []},
    )
    assert resp.status_code == 200
    assert b"No pending mappings" not in resp.content
    assert b"hx-swap-oob" not in resp.content
    # Toast still fires.
    assert _hx_trigger_toast_message(resp) is not None


def test_pending_list_strips_javascript_url_from_citations(client_with_settings_user, db_session):
    """Defense: citation URLs with non-http(s) schemes (e.g. javascript:)
    must NOT be rendered as clickable links in the admin UI, even if a
    malicious LLM proposed them."""
    import re

    from sqlalchemy import text

    # Bypass @validates and pydantic by inserting raw SQL with a malicious
    # citation. (In production this could happen via a malicious or
    # prompt-injected LLM response that slips through ResolverLlmResponse
    # validation — the URL field is just str, no scheme check.)
    db_session.execute(
        text(
            """
            INSERT INTO oem_spec_codes_pending
                (oem, spec_code, proposed_avl, llm_confidence, citations,
                 used_in_requirement_ids)
            VALUES
                (:oem, :spec_code, :proposed_avl, :llm_confidence, :citations,
                 :used_in_requirement_ids)
            """
        ),
        {
            "oem": "IBM",
            "spec_code": "SPREJ",
            "proposed_avl": '[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": null}]',
            "llm_confidence": 0.8,
            "citations": '[{"url": "javascript:alert(1)", "snippet": "evil"}]',
            "used_in_requirement_ids": "[]",
        },
    )
    db_session.commit()

    resp = client_with_settings_user.get("/admin/spec-codes/pending")
    assert resp.status_code == 200
    body = resp.content.decode()

    # The malicious URL must NOT appear as an href attribute value
    assert 'href="javascript:' not in body
    assert "href='javascript:" not in body
    # The exact javascript: payload string must not appear inside any
    # href attribute. (It may appear in a title attribute as escaped
    # text — that's fine; browsers don't execute title content.)
    # Find all href="..." substrings and ensure none start with javascript:
    href_values = re.findall(r'href="([^"]*)"', body)
    for h in href_values:
        assert not h.lower().startswith("javascript:"), f"javascript: scheme leaked into href: {h!r}"
