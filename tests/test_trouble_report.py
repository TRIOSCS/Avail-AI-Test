"""tests/test_trouble_report.py — Tests for the trouble report feature.

Covers the GET form endpoint and the POST submission endpoint, including
success, validation errors, missing gh CLI, and subprocess failures.

Called by: pytest
Depends on: conftest (client, test_user, db_session fixtures)
"""

from unittest.mock import AsyncMock, patch

from tests.conftest import engine  # noqa: F401


class TestTroubleReportForm:
    """GET /api/trouble-report/form — returns the form partial."""

    def test_returns_form_html(self, client):
        resp = client.get("/api/trouble-report/form")
        assert resp.status_code == 200
        assert "Report a Problem" in resp.text
        assert 'name="description"' in resp.text

    def test_form_contains_hidden_inputs(self, client):
        resp = client.get("/api/trouble-report/form")
        assert 'name="page_url"' in resp.text
        assert 'name="user_agent"' in resp.text
        assert 'name="viewport"' in resp.text
        assert 'name="error_log"' in resp.text


class TestTroubleReportSubmit:
    """POST /api/trouble-report — files a GitHub Issue."""

    def test_short_description_rejected(self, client):
        resp = client.post(
            "/api/trouble-report",
            data={"description": "short"},
        )
        assert resp.status_code == 422
        assert "at least 10 characters" in resp.text

    def test_empty_description_rejected(self, client):
        resp = client.post(
            "/api/trouble-report",
            data={"description": "   "},
        )
        assert resp.status_code == 422

    @patch("app.routers.trouble_report._gh_available", return_value=False)
    def test_gh_not_available(self, mock_gh, client):
        resp = client.post(
            "/api/trouble-report",
            data={"description": "Something broke badly on the search page"},
        )
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.text

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_successful_submission(self, mock_exec, mock_gh, client):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"https://github.com/TRIOSCS/Avail-AI-Test/issues/42\n",
            b"",
        )
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        resp = client.post(
            "/api/trouble-report",
            data={
                "description": "The search page crashes when I click filter",
                "page_url": "https://app.example.com/v2/search",
                "user_agent": "Mozilla/5.0",
                "viewport": "1920x1080",
                "error_log": '[{"msg":"TypeError","ts":"2026-03-20T10:00:00Z"}]',
            },
        )
        assert resp.status_code == 200
        assert "submitted successfully" in resp.text
        assert "issues/42" in resp.text

        # Verify gh was called with correct args
        call_args = mock_exec.call_args
        args = call_args[0]
        assert args[0] == "gh"
        assert args[1] == "issue"
        assert args[2] == "create"
        assert "--repo" in args
        assert "--title" in args
        assert "--label" in args

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_gh_command_failure(self, mock_exec, mock_gh, client):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"auth required\n")
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        resp = client.post(
            "/api/trouble-report",
            data={"description": "Something broke badly on the search page"},
        )
        assert resp.status_code == 500
        assert "Failed to file" in resp.text

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_gh_timeout(self, mock_exec, mock_gh, client):
        import asyncio

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_exec.return_value = mock_proc

        resp = client.post(
            "/api/trouble-report",
            data={"description": "Something broke badly on the search page"},
        )
        assert resp.status_code == 504
        assert "timed out" in resp.text

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_unexpected_exception(self, mock_exec, mock_gh, client):
        mock_exec.side_effect = OSError("spawn failed")

        resp = client.post(
            "/api/trouble-report",
            data={"description": "Something broke badly on the search page"},
        )
        assert resp.status_code == 500
        assert "unexpected error" in resp.text

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_body_includes_context(self, mock_exec, mock_gh, client):
        """Verify the issue body contains reporter info and context."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"https://github.com/TRIOSCS/Avail-AI-Test/issues/99\n",
            b"",
        )
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        client.post(
            "/api/trouble-report",
            data={
                "description": "Filter button is unresponsive",
                "page_url": "https://app.example.com/v2/search",
                "viewport": "1024x768",
            },
        )

        call_args = mock_exec.call_args[0]
        # Find the body argument (comes after "--body")
        body_idx = list(call_args).index("--body") + 1
        body = call_args[body_idx]
        assert "Filter button is unresponsive" in body
        assert "Test Buyer" in body
        assert "1024x768" in body
        assert "Page URL" in body

    @patch("app.routers.trouble_report._gh_available", return_value=True)
    @patch("app.routers.trouble_report.asyncio.create_subprocess_exec")
    def test_no_error_log_section_when_empty(self, mock_exec, mock_gh, client):
        """Error log section should not appear when error_log is empty."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"https://github.com/TRIOSCS/Avail-AI-Test/issues/100\n",
            b"",
        )
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        client.post(
            "/api/trouble-report",
            data={
                "description": "Something is off with the layout here",
                "error_log": "[]",
            },
        )

        call_args = mock_exec.call_args[0]
        body_idx = list(call_args).index("--body") + 1
        body = call_args[body_idx]
        assert "Recent JS Errors" not in body
