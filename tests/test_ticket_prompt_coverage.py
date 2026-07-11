"""test_ticket_prompt_coverage.py — Coverage gap tests for ticket_prompt_service.

Covers lines not reached by test_ticket_kind_and_prompt.py:
  - _build_bug_prompt: all optional field branches (browser_info, console_errors,
    network_errors, page_state, screenshot_path, screenshot_b64)
  - _build_feature_prompt: current_view and screenshot branches
  - generate_ticket_prompt: returns None when claude_text returns falsy

Called by: pytest
Depends on: app.services.ticket_prompt_service, app.models.trouble_ticket,
            conftest (db_session), unittest.mock.
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import TicketType
from app.models.trouble_ticket import TroubleTicket
from app.services.ticket_prompt_service import (
    _build_bug_prompt,
    _build_feature_prompt,
    generate_ticket_prompt,
)

_CLAUDE_PATCH = "app.services.ticket_prompt_service.claude_text"


# ── Fixture ───────────────────────────────────────────────────────────────────


def _make_ticket(db: Session, *, num: str = "TT-GAP-001", **kw) -> TroubleTicket:
    t = TroubleTicket(
        ticket_number=num,
        title=kw.pop("title", "Gap test"),
        description=kw.pop("description", "A description"),
        status=kw.pop("status", "submitted"),
        source=kw.pop("source", "report_button"),
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── _build_bug_prompt ─────────────────────────────────────────────────────────


def test_build_bug_prompt_all_optional_fields(db_session: Session):
    """_build_bug_prompt includes every optional field when all are present."""
    t = _make_ticket(
        db_session,
        num="TT-BUG-ALL",
        ticket_type=TicketType.BUG,
        description="It crashed",
        current_page="/v2/search",
        current_view="SearchResults",
        browser_info="Chrome 120 / macOS",
        console_errors="TypeError: Cannot read properties of null",
        network_errors='[{"status":500,"url":"/api/search"}]',
        page_state='{"query":"LM317","rows":0}',
        screenshot_path="/uploads/ss_001.png",
        admin_notes="Check the null-guard in search_service.py",
    )

    result = _build_bug_prompt(t)

    assert "TT-BUG-ALL" in result
    assert "It crashed" in result
    assert "/v2/search" in result
    assert "SearchResults" in result
    assert "Chrome 120" in result
    assert "TypeError" in result
    assert "/api/search" in result
    assert "LM317" in result
    assert "screenshot" in result.lower()
    assert "Check the null-guard" in result


def test_build_bug_prompt_screenshot_b64_branch(db_session: Session):
    """_build_bug_prompt adds the screenshot note when only screenshot_b64 is set."""
    t = _make_ticket(
        db_session,
        num="TT-BUG-B64",
        ticket_type=TicketType.BUG,
        screenshot_b64="data:image/png;base64,abc123",
    )

    result = _build_bug_prompt(t)

    assert "screenshot" in result.lower()


def test_build_bug_prompt_no_optional_fields(db_session: Session):
    """_build_bug_prompt works when all optional fields are absent."""
    t = _make_ticket(
        db_session,
        num="TT-BUG-MIN",
        ticket_type=TicketType.BUG,
        # All optional fields left at their column defaults (None).
    )

    result = _build_bug_prompt(t)

    assert "TT-BUG-MIN" in result
    # None of the optional sections should appear.
    assert "Browser:" not in result
    assert "JS/console errors:" not in result
    assert "Network log:" not in result
    assert "Page state:" not in result
    assert "screenshot" not in result.lower()
    assert "Admin notes" not in result


# ── _build_feature_prompt ─────────────────────────────────────────────────────


def test_build_feature_prompt_all_optional_fields(db_session: Session):
    """_build_feature_prompt includes current_view and screenshot when set."""
    t = _make_ticket(
        db_session,
        num="TT-FEAT-ALL",
        ticket_type=TicketType.FEATURE,
        description="Add dark mode",
        current_page="/v2/dashboard",
        current_view="DashboardMain",
        screenshot_path="/uploads/ss_feat.png",
        admin_notes="Use Tailwind dark: classes",
    )

    result = _build_feature_prompt(t)

    assert "TT-FEAT-ALL" in result
    assert "Add dark mode" in result
    assert "/v2/dashboard" in result
    assert "DashboardMain" in result
    assert "screenshot" in result.lower()
    assert "Tailwind dark:" in result


def test_build_feature_prompt_screenshot_b64_branch(db_session: Session):
    """_build_feature_prompt adds the screenshot note when screenshot_b64 is set."""
    t = _make_ticket(
        db_session,
        num="TT-FEAT-B64",
        ticket_type=TicketType.FEATURE,
        screenshot_b64="data:image/png;base64,xyz",
    )

    result = _build_feature_prompt(t)

    assert "screenshot" in result.lower()


def test_build_feature_prompt_no_optional_fields(db_session: Session):
    """_build_feature_prompt works when all optional fields are absent."""
    t = _make_ticket(
        db_session,
        num="TT-FEAT-MIN",
        ticket_type=TicketType.FEATURE,
        # All optional fields left at their column defaults (None).
    )

    result = _build_feature_prompt(t)

    assert "TT-FEAT-MIN" in result
    assert "Current view:" not in result
    assert "screenshot" not in result.lower()
    assert "Admin notes" not in result


# ── generate_ticket_prompt ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch(_CLAUDE_PATCH, new_callable=AsyncMock)
async def test_generate_prompt_returns_none_when_claude_returns_empty(mock_ai, db_session: Session):
    """generate_ticket_prompt returns None when claude_text returns an empty string."""
    mock_ai.return_value = ""  # falsy → the `if not text` branch
    t = _make_ticket(db_session, num="TT-EMPTY", ticket_type=TicketType.BUG)

    result = await generate_ticket_prompt(db_session, t)

    assert result is None
    # generated_prompt must NOT be persisted.
    db_session.refresh(t)
    assert t.generated_prompt is None


@pytest.mark.asyncio
@patch(_CLAUDE_PATCH, new_callable=AsyncMock)
async def test_generate_prompt_returns_none_when_claude_returns_none(mock_ai, db_session: Session):
    """generate_ticket_prompt returns None when claude_text returns None."""
    mock_ai.return_value = None
    t = _make_ticket(db_session, num="TT-NONE", ticket_type=TicketType.FEATURE)

    result = await generate_ticket_prompt(db_session, t)

    assert result is None
    db_session.refresh(t)
    assert t.generated_prompt is None
