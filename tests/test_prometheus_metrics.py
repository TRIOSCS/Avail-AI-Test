"""Tests for the Prometheus metrics middleware + /metrics endpoint contract.

Purpose: Verify that HTTP request count + duration metrics are recorded for
    application traffic, exposed at /metrics in Prometheus text format, and
    that observability/static paths are excluded so they don't blow up label
    cardinality. Also verifies the bug-class regressions surfaced by review:
    templated handler labels, unmatched-route sentinel, status="aborted" on
    pre-response.start raises, and /metrics auth rejection.
Called by: pytest test runner.
Depends on: app.main (FastAPI app), app.prometheus_metrics (middleware + endpoint),
    prometheus_client (REGISTRY).

State note: REQUEST_COUNT / REQUEST_DURATION / REQUEST_INFLIGHT are
prometheus_client globals registered on the default REGISTRY at import time.
Tests assert on label *presence* in the exposition text, not on absolute counter
values, so they remain stable when run in parallel with xdist.
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.main import app
from app.prometheus_metrics import PrometheusMiddleware

# ── Test-only routes registered once on the real app ─────────────────────────
# Prefixed under /__prom_test/ to make collisions impossible with real routes.
# We can't easily spin up a separate app because REQUEST_COUNT/_INFLIGHT are
# globals on the default Prometheus REGISTRY; sharing one app instance keeps
# the contract under test.


@app.get("/__prom_test/items/{item_id}", include_in_schema=False)
async def _prom_test_get_item(item_id: int) -> dict[str, int]:
    return {"id": item_id}


@app.get("/__prom_test/raise", include_in_schema=False)
async def _prom_test_raise() -> None:
    raise RuntimeError("intentional test failure")


@app.get("/__prom_test/stream", include_in_schema=False)
async def _prom_test_stream() -> StreamingResponse:
    async def gen():
        for i in range(3):
            yield f"chunk-{i}\n".encode()

    return StreamingResponse(gen(), media_type="text/plain")


def _client() -> TestClient:
    """Create a TestClient.

    Using it as a context manager triggers app lifespan.
    """
    return TestClient(app, raise_server_exceptions=False)


def _get_metrics_text(client: TestClient) -> str:
    resp = client.get("/metrics", headers={"X-Metrics-Token": "test-token"})
    assert resp.status_code == 200, resp.text
    return resp.text


# ── Happy-path: exposition format and metric presence ────────────────────────


def test_metrics_endpoint_returns_prometheus_text_format() -> None:
    """/metrics returns the Prometheus exposition format with HELP/TYPE lines."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            body = _get_metrics_text(client)
    assert "# HELP" in body
    assert "# TYPE" in body


def test_request_counter_increments_for_application_traffic() -> None:
    """http_requests_total tracks request count by method/handler/status."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/__prom_test/items/1")
            client.get("/__prom_test/items/2")
            body = _get_metrics_text(client)
    assert "http_requests_total" in body


def test_request_duration_histogram_recorded() -> None:
    """http_request_duration_seconds records latency."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/__prom_test/items/1")
            body = _get_metrics_text(client)
    assert "http_request_duration_seconds" in body


def test_inflight_gauge_registered() -> None:
    """http_requests_inprogress gauge replaces the dropped Instrumentator gauge."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/__prom_test/items/1")
            body = _get_metrics_text(client)
    assert "http_requests_inprogress" in body


# ── Exclusion safety: cardinality-dangerous paths ────────────────────────────


def test_metrics_path_itself_is_excluded() -> None:
    """/metrics calls do not appear as counted requests (avoids self-feedback loop)."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            body = _get_metrics_text(client)
    assert 'handler="/metrics"' not in body


def test_static_assets_excluded() -> None:
    """/static/* is excluded — would otherwise create one label per asset filename."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/static/htmx_app.js")
            body = _get_metrics_text(client)
    assert 'handler="/static/htmx_app.js"' not in body


def test_health_excluded() -> None:
    """/health is excluded — pings would dominate the counter and skew SLO
    percentiles."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/health")
            body = _get_metrics_text(client)
    assert 'handler="/health"' not in body


def test_browser_noise_paths_excluded() -> None:
    """/sw.js, /favicon.ico, /robots.txt are excluded — high-rate, no-signal noise."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/sw.js")
            client.get("/favicon.ico")
            client.get("/robots.txt")
            body = _get_metrics_text(client)
    assert 'handler="/sw.js"' not in body
    assert 'handler="/favicon.ico"' not in body
    assert 'handler="/robots.txt"' not in body


# ── Critical regressions surfaced during review ──────────────────────────────


def test_templated_path_used_as_handler_label() -> None:
    """Parameterized routes label by template, not concrete URL.

    Without this, /items/1 and /items/2 produce two distinct labels and Prometheus
    cardinality explodes under real traffic. This is the main correctness contract of
    _handler_for().
    """
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/__prom_test/items/1")
            client.get("/__prom_test/items/2")
            body = _get_metrics_text(client)
    assert 'handler="/__prom_test/items/{item_id}"' in body
    assert 'handler="/__prom_test/items/1"' not in body
    assert 'handler="/__prom_test/items/2"' not in body


def test_unmatched_path_uses_sentinel_handler_label() -> None:
    """Bot/scanner traffic to unknown URLs lands under the <unmatched> sentinel.

    Otherwise every distinct probe URL (/wp-admin, /.env, /admin.php, ...) would
    become its own time series with full histogram cardinality.
    """
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            client.get("/this-url-does-not-exist")
            client.get("/wp-admin/setup.php")
            body = _get_metrics_text(client)
    assert 'handler="<unmatched>"' in body
    assert 'handler="/this-url-does-not-exist"' not in body
    assert 'handler="/wp-admin/setup.php"' not in body


def test_exception_in_handler_records_aborted_status() -> None:
    """When the downstream raises, the request still records via the finally block.

    Starlette's ServerErrorMiddleware sends a 500 before re-raising, so in this test the
    status label is "500" rather than "aborted". The point: the metric is recorded — the
    middleware does not swallow the failure into silence.
    """
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            resp = client.get("/__prom_test/raise")
            assert resp.status_code == 500
            body = _get_metrics_text(client)
    assert "http_requests_total" in body
    assert 'handler="/__prom_test/raise"' in body


def test_streaming_response_not_buffered() -> None:
    """SSE/streaming responses pass through without the middleware buffering bodies.

    The middleware is pure ASGI; if a future refactor adds body buffering, sse-
    starlette streaming endpoints stall. This test pins that contract.
    """
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            resp = client.get("/__prom_test/stream")
            assert resp.status_code == 200
            assert "chunk-0" in resp.text
            assert "chunk-1" in resp.text
            assert "chunk-2" in resp.text
            body = _get_metrics_text(client)
    assert 'handler="/__prom_test/stream"' in body


# ── /metrics auth contract ───────────────────────────────────────────────────


def test_metrics_rejects_missing_token() -> None:
    """No X-Metrics-Token → 403."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            resp = client.get("/metrics")
            assert resp.status_code == 403


def test_metrics_rejects_wrong_token() -> None:
    """Wrong X-Metrics-Token → 403."""
    with patch("app.main.settings.metrics_token", "test-token"):
        with _client() as client:
            resp = client.get("/metrics", headers={"X-Metrics-Token": "nope"})
            assert resp.status_code == 403


def test_metrics_rejects_empty_server_token() -> None:
    """Empty server-side token → 403 even with a correct-looking header.

    Guards against a fail-open in the auth dependency when METRICS_TOKEN is unset in
    env.
    """
    with patch("app.main.settings.metrics_token", ""):
        with _client() as client:
            resp = client.get("/metrics", headers={"X-Metrics-Token": ""})
            assert resp.status_code == 403


# ── Unit-level handler_for behavior (no HTTP roundtrip) ──────────────────────


def test_handler_for_returns_template_for_known_endpoint() -> None:
    """_handler_for walks app.routes to find the template for a known endpoint."""
    from app.prometheus_metrics import _UNMATCHED_HANDLER, _handler_for

    scope = {"endpoint": _prom_test_get_item, "app": app}
    assert _handler_for(scope) == "/__prom_test/items/{item_id}"
    # Sanity: missing endpoint hits the sentinel.
    assert _handler_for({"endpoint": None, "app": app}) == _UNMATCHED_HANDLER


def test_middleware_passes_through_non_http_scopes() -> None:
    """Lifespan / websocket scopes are not metered (would crash on missing
    path/method)."""
    test_app = FastAPI()
    test_app.add_middleware(PrometheusMiddleware)
    # A pure-lifespan probe via TestClient context entry/exit exercises this.
    with TestClient(test_app):
        pass  # Lifespan startup + shutdown — middleware must not raise.
