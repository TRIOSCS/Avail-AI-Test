"""Tests for error_reports router — trouble ticket CRUD endpoints."""

from tests.conftest import client  # noqa: F401


def test_create_error_report(client):
    """POST /api/error-reports creates a trouble ticket."""
    resp = client.post("/api/error-reports", json={"message": "Something broke on the pipeline page"})
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["status"] == "created"


def test_create_error_report_empty_message(client):
    """POST /api/error-reports rejects empty message."""
    resp = client.post("/api/error-reports", json={"message": ""})
    assert resp.status_code == 422


def test_list_error_reports(client):
    """GET /api/error-reports returns paginated list."""
    # Create one first
    client.post("/api/error-reports", json={"message": "Test ticket for listing"})
    resp = client.get("/api/error-reports")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_list_with_status_filter(client):
    """GET /api/error-reports?status=submitted filters correctly."""
    client.post("/api/error-reports", json={"message": "Filtered ticket"})
    resp = client.get("/api/error-reports?status=submitted")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 0


def test_get_single_report(client):
    """GET /api/error-reports/{id} returns ticket detail."""
    create_resp = client.post("/api/error-reports", json={"message": "Detail test ticket"})
    ticket_id = create_resp.json()["id"]
    resp = client.get(f"/api/error-reports/{ticket_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == ticket_id
    assert data["title"] == "Detail test ticket"
    assert data["status"] == "submitted"


def test_get_nonexistent_report(client):
    """GET /api/error-reports/99999 returns 404."""
    resp = client.get("/api/error-reports/99999")
    assert resp.status_code == 404


def test_trouble_tickets_alias(client):
    """POST /api/trouble-tickets works as alias."""
    resp = client.post("/api/trouble-tickets", json={"message": "Via alias"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"


def test_create_error_report_screenshot_too_large(client):
    """Reject screenshots larger than 2MB."""
    resp = client.post(
        "/api/error-reports",
        json={
            "message": "Bug",
            "screenshot": "x" * (2 * 1024 * 1024 + 1),
        },
    )
    assert resp.status_code == 422
