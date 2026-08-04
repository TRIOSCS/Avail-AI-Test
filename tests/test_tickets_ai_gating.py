"""test_tickets_ai_gating.py — Wave-1 keys-off gating for the trouble-ticket AI calls.

Spec §5.5/§7: the ticket flow's hidden Anthropic calls (submit-time summary, admin
diagnose single/bulk, analyze grouping, create-prompt) all gate on the same AI
predicate the rest of the app uses (``claude_configured`` via ``_ai_keys_present``).
Keys absent → the call is skipped, the ticket is still created/updated un-enriched,
and the admin surfaces show an honest "AI is off" state. Keys present → the calls
still go through.

Called by: pytest
Depends on: conftest.py fixtures (db_session), app/routers/error_reports.py
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_user
from app.main import app
from app.models import User
from app.models.trouble_ticket import TroubleTicket

_KEYS = "app.routers.error_reports._ai_keys_present"
_DIAG = "app.services.ticket_diagnosis_service.claude_structured_with_usage"
_PROMPT = "app.services.ticket_prompt_service.claude_text"
_SUMMARY = "app.utils.claude_client.claude_text"
_ANALYZE = "app.utils.claude_client.claude_structured"

_FAKE_DIAGNOSIS = {
    "root_cause": "Submit handler swallows the validation error",
    "severity": "high",
    "affected_areas": ["app/routers/error_reports.py"],
    "reproduction_steps": ["Open the form", "Submit empty"],
    "fix_prompt": "Surface the 422 to the user.",
}
_FAKE_USAGE = {"input_tokens": 100, "output_tokens": 50}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _admin_client(db_session: Session, user: User) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


def _make_ticket(db: Session, *, num: str = "TT-0101", **kw) -> TroubleTicket:
    t = TroubleTicket(
        ticket_number=num,
        title=kw.pop("title", "Something broke"),
        description=kw.pop("description", "The submit button did nothing"),
        status=kw.pop("status", "submitted"),
        source=kw.pop("source", "report_button"),
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── Submit-time summary (the hidden background call) ─────────────────────────


def test_submit_keys_off_creates_unenriched_ticket(db_session, test_user):
    client = _admin_client(db_session, test_user)
    with patch(_KEYS, return_value=False), patch(_SUMMARY, new_callable=AsyncMock) as claude:
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "Broken with keys off", "page_url": "/v2/sightings"},
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert "TT-" in resp.text
    claude.assert_not_awaited()
    ticket = db_session.query(TroubleTicket).filter(TroubleTicket.description == "Broken with keys off").one()
    assert ticket.ai_summary is None


def test_generate_ai_summary_keys_off_skips_call():
    from app.routers.error_reports import _generate_ai_summary

    with patch(_KEYS, return_value=False), patch(_SUMMARY, new_callable=AsyncMock) as claude:
        asyncio.run(_generate_ai_summary(999999))
    claude.assert_not_awaited()


# ── Admin diagnose (single + bulk) ───────────────────────────────────────────


def test_diagnose_keys_off_shows_ai_off(db_session, test_user):
    client = _admin_client(db_session, test_user)
    ticket = _make_ticket(db_session)
    with patch(_KEYS, return_value=False), patch(_DIAG, new_callable=AsyncMock) as claude:
        resp = client.post(f"/api/trouble-tickets/{ticket.id}/diagnose")
    assert resp.status_code == 200
    assert "AI is off" in resp.text
    claude.assert_not_awaited()


def test_diagnose_keys_on_still_calls_claude(db_session, test_user):
    client = _admin_client(db_session, test_user)
    ticket = _make_ticket(db_session, num="TT-0102")
    with (
        patch(_KEYS, return_value=True),
        patch(_DIAG, new_callable=AsyncMock, return_value=(_FAKE_DIAGNOSIS, _FAKE_USAGE)) as claude,
    ):
        resp = client.post(f"/api/trouble-tickets/{ticket.id}/diagnose")
    assert resp.status_code == 200
    claude.assert_awaited_once()


def test_diagnose_bulk_keys_off_returns_503(db_session, test_user):
    client = _admin_client(db_session, test_user)
    ticket = _make_ticket(db_session, num="TT-0103")
    with patch(_KEYS, return_value=False), patch(_DIAG, new_callable=AsyncMock) as claude:
        resp = client.post("/api/trouble-tickets/diagnose-bulk", json={"ticket_ids": [ticket.id]})
    assert resp.status_code == 503
    assert "AI is off" in resp.text
    claude.assert_not_awaited()


# ── Create-prompt ────────────────────────────────────────────────────────────


def test_generate_prompt_keys_off_persists_notes_without_ai(db_session, test_user):
    client = _admin_client(db_session, test_user)
    ticket = _make_ticket(db_session, num="TT-0104")
    with patch(_KEYS, return_value=False), patch(_PROMPT, new_callable=AsyncMock) as claude:
        resp = client.post(
            f"/api/trouble-tickets/{ticket.id}/generate-prompt",
            data={"admin_notes": "Fix the thing"},
        )
    assert resp.status_code == 200
    assert "AI is off" in resp.text
    claude.assert_not_awaited()
    db_session.refresh(ticket)
    assert ticket.admin_notes == "Fix the thing"
    assert ticket.generated_prompt is None


# ── Analyze (root-cause grouping) ────────────────────────────────────────────


def test_analyze_keys_off_shows_state_and_keeps_list(db_session, test_user):
    client = _admin_client(db_session, test_user)
    _make_ticket(db_session, num="TT-0105")
    with patch(_KEYS, return_value=False), patch(_ANALYZE, new_callable=AsyncMock) as claude:
        resp = client.post("/api/trouble-tickets/analyze")
    assert resp.status_code == 200
    assert "AI is off" in resp.text
    # the swap targets #ticket-list, so the list itself must ride along
    assert "TT-0105" in resp.text
    claude.assert_not_awaited()
