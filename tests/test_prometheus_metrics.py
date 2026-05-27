"""Tests for the Prometheus metrics middleware + /metrics endpoint contract.

Purpose: Verify that HTTP request count + duration metrics are recorded for
    application traffic, exposed at /metrics in Prometheus text format, and
    that observability/static paths are excluded so they don't blow up label
    cardinality.
Called by: pytest test runner.
Depends on: app.main (FastAPI app), app.prometheus_metrics (middleware + endpoint),
    prometheus_client (REGISTRY).
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def _client():
    """Create a TestClient without triggering lifespan."""
    return TestClient(app, raise_server_exceptions=False)


def _get_metrics_text(client):
    resp = client.get("/metrics", headers={"X-Metrics-Token": "test-token"})
    assert resp.status_code == 200, resp.text
    return resp.text


def test_metrics_endpoint_returns_prometheus_text_format():
    """/metrics returns the Prometheus exposition format with HELP/TYPE lines."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            body = _get_metrics_text(client)
    assert "# HELP" in body
    assert "# TYPE" in body


def test_request_counter_increments_for_application_traffic():
    """A counter named http_requests_total tracks request count by
    method/handler/status."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/health")  # warm the app
            client.get("/health")
            body = _get_metrics_text(client)
    assert "http_requests_total" in body


def test_request_duration_histogram_recorded():
    """A histogram named http_request_duration_seconds records latency."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/health")
            body = _get_metrics_text(client)
    assert "http_request_duration_seconds" in body


def test_metrics_path_itself_is_excluded_from_metrics():
    """/metrics calls do not appear as counted requests (avoids self-feedback loop)."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            body = _get_metrics_text(client)
    assert 'handler="/metrics"' not in body


def test_static_assets_excluded_from_metrics():
    """/static/* is excluded — would otherwise create one label per asset filename."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/static/htmx_app.js")
            body = _get_metrics_text(client)
    assert 'handler="/static/htmx_app.js"' not in body


def test_health_excluded_from_metrics():
    """/health is excluded — pings would dominate the counter and skew SLO
    percentiles."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/health")
            client.get("/health")
            body = _get_metrics_text(client)
    assert 'handler="/health"' not in body
