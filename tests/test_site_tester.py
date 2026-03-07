"""Tests for the Playwright site tester service.

Tests class logic only -- no actual browser launch.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.site_tester import (
    SiteTester,
    TEST_AREAS,
    create_tickets_from_issues,
)


# ---------------------------------------------------------------------------
# TEST_AREAS validation
# ---------------------------------------------------------------------------


def test_test_areas_comprehensive():
    area_names = [a["name"] for a in TEST_AREAS]
    assert "search" in area_names
    assert "vendors" in area_names
    assert "materials" in area_names
    assert "customers" in area_names
    assert "contacts" in area_names
    assert "prospecting" in area_names
    assert "dashboard" in area_names
    assert "scorecard" in area_names
    assert "proactive" in area_names
    assert "offers" in area_names
    assert "buyplans" in area_names
    assert "alerts" in area_names
    assert "settings" in area_names
    assert "tickets" in area_names
    assert "apihealth" in area_names
    assert len(area_names) == 15


def test_test_areas_have_required_keys():
    for area in TEST_AREAS:
        assert "name" in area
        assert "hash" in area
        assert "description" in area
        assert area["hash"].startswith("#")


def test_test_areas_unique_names():
    names = [a["name"] for a in TEST_AREAS]
    assert len(names) == len(set(names)), "Duplicate area names found"


# ---------------------------------------------------------------------------
# SiteTester init + record_issue
# ---------------------------------------------------------------------------


def test_site_tester_init():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test123")
    assert tester.base_url == "http://localhost:8000"
    assert tester.session_cookie == "test123"
    assert tester.issues == []
    assert tester.progress == []


def test_site_tester_strips_trailing_slash():
    tester = SiteTester(base_url="http://localhost:8000/", session_cookie="x")
    assert tester.base_url == "http://localhost:8000"


def test_record_issue_basic():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test")
    tester.record_issue(
        area="search",
        title="Console error on search",
        description="TypeError on load",
    )
    assert len(tester.issues) == 1
    assert tester.issues[0]["area"] == "search"
    assert tester.issues[0]["title"] == "Console error on search"
    assert tester.issues[0]["description"] == "TypeError on load"
    assert "timestamp" in tester.issues[0]


def test_record_issue_with_all_fields():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test")
    tester.record_issue(
        area="customers",
        title="Network failure",
        description="Failed to fetch companies",
        url="http://localhost:8000/#customers",
        screenshot_b64="abc123",
        network_errors=[{"url": "/api/companies", "method": "GET", "failure": "net::ERR_FAILED"}],
        console_errors=["TypeError: Cannot read property 'map' of undefined"],
        performance_ms=4500.0,
    )
    issue = tester.issues[0]
    assert issue["url"] == "http://localhost:8000/#customers"
    assert issue["screenshot_b64"] == "abc123"
    assert len(issue["network_errors"]) == 1
    assert len(issue["console_errors"]) == 1
    assert issue["performance_ms"] == 4500.0


def test_record_issue_defaults():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test")
    tester.record_issue(area="auth", title="Test", description="Desc")
    issue = tester.issues[0]
    assert issue["url"] == "http://localhost:8000"
    assert issue["screenshot_b64"] is None
    assert issue["network_errors"] == []
    assert issue["console_errors"] == []
    assert issue["performance_ms"] is None


def test_record_multiple_issues():
    tester = SiteTester(base_url="http://localhost:8000", session_cookie="test")
    for i in range(5):
        tester.record_issue(area=f"area_{i}", title=f"Issue {i}", description="desc")
    assert len(tester.issues) == 5


# ---------------------------------------------------------------------------
# create_tickets_from_issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tickets_from_issues():
    issues = [
        {
            "area": "search",
            "title": "Console error on search",
            "description": "TypeError on load",
            "url": "http://localhost:8000/#rfqs",
            "console_errors": ["TypeError: x is not defined"],
            "network_errors": [],
        },
        {
            "area": "rfq",
            "title": "Slow load on RFQ",
            "description": "Took 5000ms",
            "url": "http://localhost:8000/#rfqs",
            "console_errors": [],
            "network_errors": [],
        },
    ]

    mock_db = MagicMock()

    with patch("app.services.trouble_ticket_service.create_ticket") as mock_create:
        mock_create.return_value = MagicMock(id=1)
        count = await create_tickets_from_issues(issues, mock_db)

    assert count == 2
    assert mock_create.call_count == 2
    mock_db.commit.assert_called_once()

    # Verify first call args
    first_call = mock_create.call_args_list[0]
    assert first_call.kwargs["source"] == "playwright"
    assert first_call.kwargs["current_view"] == "search"


@pytest.mark.asyncio
async def test_create_tickets_handles_errors():
    issues = [
        {"area": "search", "title": "Error", "description": "desc", "url": "/", "console_errors": [], "network_errors": []},
    ]

    mock_db = MagicMock()

    with patch("app.services.trouble_ticket_service.create_ticket", side_effect=Exception("DB error")):
        count = await create_tickets_from_issues(issues, mock_db)

    assert count == 0
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_tickets_empty_list():
    mock_db = MagicMock()
    count = await create_tickets_from_issues([], mock_db)
    assert count == 0
    mock_db.commit.assert_not_called()
