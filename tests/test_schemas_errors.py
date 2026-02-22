"""Tests for app/schemas/errors.py -- ErrorResponse model."""

from app.schemas.errors import ErrorResponse


class TestErrorResponse:
    def test_minimal(self):
        e = ErrorResponse(error="Not Found", status_code=404)
        assert e.error == "Not Found"
        assert e.status_code == 404
        assert e.request_id == ""
        assert e.detail is None

    def test_with_request_id(self):
        e = ErrorResponse(error="Bad Request", status_code=400, request_id="req-123")
        assert e.request_id == "req-123"

    def test_with_detail(self):
        e = ErrorResponse(
            error="Validation Error",
            status_code=422,
            detail=[{"loc": ["body", "name"], "msg": "required"}],
        )
        assert e.detail is not None
        assert len(e.detail) == 1

    def test_serialization(self):
        e = ErrorResponse(error="err", status_code=500, request_id="r1")
        d = e.model_dump()
        assert d["error"] == "err"
        assert d["status_code"] == 500
        assert d["request_id"] == "r1"
        assert d["detail"] is None
