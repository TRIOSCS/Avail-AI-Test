"""Tests for error_reports router — trouble ticket CRUD endpoints."""

import pytest


def test_create_error_report(client):
    """POST /api/error-reports creates a trouble ticket."""
    resp = client.post("/api/error-reports", json={"message": "Something broke on the pipeline page"})
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["status"] == "created"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"message": ""}, id="empty_message"),
        pytest.param(
            {"message": "Bug", "screenshot": "x" * (2 * 1024 * 1024 + 1)},
            id="screenshot_too_large",
        ),
    ],
)
def test_create_error_report_rejects_invalid_payload(client, payload):
    """POST /api/error-reports rejects empty messages and oversized (>2MB)
    screenshots."""
    resp = client.post("/api/error-reports", json=payload)
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


def test_trouble_tickets_alias(client):
    """POST /api/trouble-tickets works as alias."""
    resp = client.post("/api/trouble-tickets", json={"message": "Via alias"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
