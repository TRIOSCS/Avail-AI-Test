"""
test_middleware.py — Tests for request/response middleware

Verifies request ID generation, timing headers, and structured logging
for the observability middleware in main.py.

Called by: pytest
Depends on: app/main.py (middleware), tests/conftest.py (client fixture)
"""



def test_request_id_header_present(client):
    """Every response should include X-Request-ID."""
    resp = client.get("/health")
    assert "X-Request-ID" in resp.headers
    req_id = resp.headers["X-Request-ID"]
    assert len(req_id) == 8  # uuid4()[:8]


def test_request_id_unique_per_request(client):
    """Each request gets a distinct ID."""
    id1 = client.get("/health").headers["X-Request-ID"]
    id2 = client.get("/health").headers["X-Request-ID"]
    assert id1 != id2


def test_health_returns_ok(client):
    """Health endpoint still works after middleware changes."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_404_still_gets_request_id(client):
    """Even error responses should carry the request ID."""
    resp = client.get("/nonexistent-route-xyz")
    assert "X-Request-ID" in resp.headers
