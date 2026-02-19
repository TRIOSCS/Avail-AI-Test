"""Tests for app.services.document_service and app.routers.documents.

WeasyPrint requires system libraries (pango, etc.) not available on the test
host, so we inject a fake weasyprint module into sys.modules before any
imports. Tests verify DB queries, template rendering, and router behaviour.
"""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# ── Fake weasyprint module ───────────────────────────────────────────
# Must be installed before importing document_service or the router.

_mock_html_cls = MagicMock()
_fake_weasyprint = MagicMock()
_fake_weasyprint.HTML = _mock_html_cls

# Insert into sys.modules so `from weasyprint import HTML` resolves
sys.modules.setdefault("weasyprint", _fake_weasyprint)

from app.models import (  # noqa: E402
    Company, CustomerSite, Offer, Quote, Requisition, Requirement, User,
)
from app.services.document_service import (  # noqa: E402
    generate_rfq_summary_pdf,
    generate_quote_report_pdf,
)


@pytest.fixture(autouse=True)
def _reset_html_mock():
    """Reset the shared HTML mock between tests."""
    _mock_html_cls.reset_mock()
    _mock_html_cls.return_value.write_pdf.reset_mock()
    _mock_html_cls.return_value.write_pdf.return_value = b"%PDF-1.4 fake"
    _mock_html_cls.return_value.write_pdf.side_effect = None


# ── generate_rfq_summary_pdf ────────────────────────────────────────


class TestGenerateRfqSummaryPdf:
    def test_raises_for_missing_requisition(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            generate_rfq_summary_pdf(99999, db_session)

    def test_renders_template_and_calls_weasyprint(
        self, db_session, test_requisition, test_offer
    ):
        result = generate_rfq_summary_pdf(test_requisition.id, db_session)

        assert result == b"%PDF-1.4 fake"
        _mock_html_cls.assert_called_once()
        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert test_requisition.name in html_string

    def test_includes_requirements_in_html(self, db_session, test_requisition):
        generate_rfq_summary_pdf(test_requisition.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "LM317T" in html_string

    def test_includes_offers_in_html(self, db_session, test_requisition, test_offer):
        generate_rfq_summary_pdf(test_requisition.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "Arrow Electronics" in html_string

    def test_works_with_no_offers(self, db_session, test_requisition):
        """Requisition with requirements but no offers should still render."""
        result = generate_rfq_summary_pdf(test_requisition.id, db_session)
        assert result == b"%PDF-1.4 fake"

    def test_html_contains_generated_timestamp(self, db_session, test_requisition):
        generate_rfq_summary_pdf(test_requisition.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "UTC" in html_string


# ── generate_quote_report_pdf ────────────────────────────────────────


class TestGenerateQuoteReportPdf:
    def test_raises_for_missing_quote(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            generate_quote_report_pdf(99999, db_session)

    def test_renders_template_and_calls_weasyprint(
        self, db_session, test_quote, test_customer_site, test_company
    ):
        result = generate_quote_report_pdf(test_quote.id, db_session)

        assert result == b"%PDF-1.4 fake"
        _mock_html_cls.assert_called_once()
        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert test_quote.quote_number in html_string

    def test_includes_customer_and_company(
        self, db_session, test_quote, test_customer_site, test_company
    ):
        generate_quote_report_pdf(test_quote.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert test_customer_site.site_name in html_string
        assert test_company.name in html_string

    def test_renders_line_items(self, db_session, test_customer_site, test_company, test_user, test_requisition):
        """Quote with line items should include them in the HTML."""
        quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-LI-01",
            status="draft",
            line_items=[
                {"mpn": "LM317T", "manufacturer": "TI", "qty": 1000, "cost_each": 0.45, "sell_each": 0.75, "margin_pct": 40.0},
                {"mpn": "NE555P", "manufacturer": "TI", "qty": 500, "cost_each": 0.20, "sell_each": 0.35, "margin_pct": 42.9},
            ],
            subtotal=975.0,
            total_cost=550.0,
            total_margin_pct=43.6,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.commit()

        generate_quote_report_pdf(quote.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "LM317T" in html_string
        assert "NE555P" in html_string

    def test_quote_with_empty_line_items(
        self, db_session, test_customer_site, test_user, test_requisition
    ):
        """Quote with empty line_items list should still render cleanly."""
        quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-EMPTY",
            status="draft",
            line_items=[],
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.commit()

        result = generate_quote_report_pdf(quote.id, db_session)
        assert result == b"%PDF-1.4 fake"

    def test_includes_notes_when_present(self, db_session, test_customer_site, test_user, test_requisition):
        quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-NOTES",
            status="draft",
            line_items=[],
            notes="Special pricing for bulk order",
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.commit()

        generate_quote_report_pdf(quote.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "Special pricing for bulk order" in html_string

    def test_includes_payment_and_shipping_terms(
        self, db_session, test_customer_site, test_user, test_requisition
    ):
        quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-TERMS",
            status="draft",
            line_items=[],
            payment_terms="Net 30",
            shipping_terms="FOB Origin",
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.commit()

        generate_quote_report_pdf(quote.id, db_session)

        html_string = _mock_html_cls.call_args.kwargs["string"]
        assert "Net 30" in html_string
        assert "FOB Origin" in html_string


# ── Router endpoint tests ────────────────────────────────────────────


class TestDocumentRouterRfqPdf:
    def test_404_for_missing_requisition(self, client):
        resp = client.get("/api/requisitions/99999/pdf")
        assert resp.status_code == 404

    def test_returns_pdf_on_success(self, client, db_session, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert "attachment" in resp.headers["content-disposition"]
        assert f"rfq-{test_requisition.id}.pdf" in resp.headers["content-disposition"]

    def test_500_on_unexpected_error(self, client, db_session, test_requisition):
        _mock_html_cls.return_value.write_pdf.side_effect = RuntimeError("Rendering failed")
        resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
        assert resp.status_code == 500


class TestDocumentRouterQuotePdf:
    def test_404_for_missing_quote(self, client):
        resp = client.get("/api/quotes/99999/pdf")
        assert resp.status_code == 404

    def test_returns_pdf_on_success(self, client, db_session, test_quote):
        resp = client.get(f"/api/quotes/{test_quote.id}/pdf")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert "attachment" in resp.headers["content-disposition"]
        assert f"quote-{test_quote.id}.pdf" in resp.headers["content-disposition"]

    def test_500_on_unexpected_error(self, client, db_session, test_quote):
        _mock_html_cls.return_value.write_pdf.side_effect = RuntimeError("Rendering failed")
        resp = client.get(f"/api/quotes/{test_quote.id}/pdf")
        assert resp.status_code == 500
