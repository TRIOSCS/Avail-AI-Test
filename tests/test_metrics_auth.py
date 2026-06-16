"""Tests for /metrics endpoint authentication.

Purpose: Verify that the /metrics endpoint requires a valid X-Metrics-Token
    header and rejects anonymous or incorrect requests.
Called by: pytest test runner.
Depends on: app.main (FastAPI app), app.config (settings).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _client():
    """Create a TestClient without triggering lifespan (avoid DB/startup issues)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("configured_token", "request_header", "expected_status"),
    [
        # When metrics_token is set, requests without the header get 403.
        ("super-secret-token", None, 403),
        # When metrics_token is set, requests with wrong token get 403.
        ("super-secret-token", "wrong-token", 403),
        # When metrics_token is set and correct header is provided, returns 200.
        ("super-secret-token", "super-secret-token", 200),
        # When metrics_token is empty (default), /metrics is blocked.
        ("", None, 403),
        # When metrics_token is empty, even sending a header doesn't help.
        ("", "anything", 403),
    ],
    ids=[
        "token_configured_but_missing",
        "token_configured_but_wrong",
        "token_configured_and_correct",
        "token_empty",
        "token_empty_even_with_header",
    ],
)
def test_metrics_auth(configured_token, request_header, expected_status):
    headers = {"X-Metrics-Token": request_header} if request_header is not None else None
    with patch("app.main.settings.metrics_token", configured_token):
        with _client() as client:
            resp = client.get("/metrics", headers=headers)
    assert resp.status_code == expected_status
