"""
tests/test_routers_documents.py -- Tests for routers/documents.py

Covers: PDF generation endpoints for requisitions and quotes
(success, not-found, and service error cases).

Called by: pytest
Depends on: app/routers/documents.py, conftest.py
"""

from unittest.mock import patch

import pytest


# ── RFQ PDF ──────────────────────────────────────────────────────────


@patch("app.services.document_service.generate_rfq_summary_pdf", return_value=b"%PDF-fake-content")
def test_rfq_pdf_success(mock_gen, client, test_requisition):
    """Valid requisition -> returns PDF bytes."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-fake-content"


@patch("app.services.document_service.generate_rfq_summary_pdf", side_effect=ValueError("Requisition not found"))
def test_rfq_pdf_not_found(mock_gen, client):
    """Invalid requisition ID -> 404."""
    resp = client.get("/api/requisitions/99999/pdf")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@patch("app.services.document_service.generate_rfq_summary_pdf", side_effect=RuntimeError("render failed"))
def test_rfq_pdf_generation_error(mock_gen, client):
    """Service throws RuntimeError -> 500."""
    resp = client.get("/api/requisitions/1/pdf")
    assert resp.status_code == 500
    assert "PDF generation failed" in resp.json()["detail"]


# ── Quote PDF ────────────────────────────────────────────────────────


@patch("app.services.document_service.generate_quote_report_pdf", return_value=b"%PDF-quote-content")
def test_quote_pdf_success(mock_gen, client, test_quote):
    """Valid quote -> returns PDF bytes."""
    resp = client.get(f"/api/quotes/{test_quote.id}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-quote-content"


@patch("app.services.document_service.generate_quote_report_pdf", side_effect=ValueError("Quote not found"))
def test_quote_pdf_not_found(mock_gen, client):
    """Invalid quote ID -> 404."""
    resp = client.get("/api/quotes/99999/pdf")
    assert resp.status_code == 404


@patch("app.services.document_service.generate_quote_report_pdf", side_effect=RuntimeError("render failed"))
def test_quote_pdf_generation_error(mock_gen, client):
    """Service throws RuntimeError -> 500."""
    resp = client.get("/api/quotes/1/pdf")
    assert resp.status_code == 500
