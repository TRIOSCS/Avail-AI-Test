"""Tests for /metrics endpoint authentication.

Purpose: Verify that the /metrics endpoint requires a valid X-Metrics-Token
    header and rejects anonymous or incorrect requests.
Called by: pytest test runner.
Depends on: app.main (FastAPI app), app.config (settings).
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def _client():
    """Create a TestClient without triggering lifespan (avoid DB/startup issues)."""
    return TestClient(app, raise_server_exceptions=False)


def test_metrics_returns_403_when_token_configured_but_missing():
    """When metrics_token is set, requests without the header get 403."""
    with patch("app.main.settings.metrics_token", "super-secret-token"):
        with _client() as client:
            resp = client.get("/metrics")
    assert resp.status_code == 403


def test_metrics_returns_403_when_token_configured_but_wrong():
    """When metrics_token is set, requests with wrong token get 403."""
    with patch("app.main.settings.metrics_token", "super-secret-token"):
        with _client() as client:
            resp = client.get("/metrics", headers={"X-Metrics-Token": "wrong-token"})
    assert resp.status_code == 403


def test_metrics_returns_200_when_token_configured_and_correct():
    """When metrics_token is set and correct header is provided, returns 200."""
    with patch("app.main.settings.metrics_token", "super-secret-token"):
        with _client() as client:
            resp = client.get("/metrics", headers={"X-Metrics-Token": "super-secret-token"})
    assert resp.status_code == 200


def test_metrics_returns_403_when_token_empty():
    """When metrics_token is empty (default), /metrics is blocked."""
    with patch("app.main.settings.metrics_token", ""):
        with _client() as client:
            resp = client.get("/metrics")
    assert resp.status_code == 403


def test_metrics_returns_403_when_token_empty_even_with_header():
    """When metrics_token is empty, even sending a header doesn't help."""
    with patch("app.main.settings.metrics_token", ""):
        with _client() as client:
            resp = client.get("/metrics", headers={"X-Metrics-Token": "anything"})
    assert resp.status_code == 403
