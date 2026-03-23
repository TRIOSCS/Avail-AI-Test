"""test_htmx_xss.py — Verify XSS-safe JavaScript escaping in customer lookup.

The customer_lookup endpoint embeds AI-returned company data into inline
JavaScript.  Values must be escaped with json.dumps() so that single quotes,
double quotes, backslashes, and HTML tags cannot break out of JS string
literals.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views.customer_lookup
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class TestCustomerLookupXSS:
    """Ensure company names with special chars don't break embedded JS."""

    @pytest.fixture(autouse=True)
    def _setup(self, client: TestClient):
        self.client = client

    def _post_lookup(self, mock_result: dict, company_name: str = "TestCo"):
        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            return self.client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": company_name, "location": ""},
                headers={"HX-Request": "true"},
            )

    def test_single_quote_in_company_name(self):
        """O'Brien Corp must not break JS string literals."""
        resp = self._post_lookup(
            {"company_name": "O'Brien Corp", "website": "", "phone": ""},
            company_name="O'Brien Corp",
        )
        assert resp.status_code == 200
        body = resp.text
        # The HTML display should show the escaped name
        assert "O&#x27;Brien Corp" in body or "O'Brien Corp" in body or "O\\u0027Brien" in body
        # The JS must use json.dumps output — a quoted string, never raw single-quote
        # json.dumps("O'Brien Corp") => '"O\'Brien Corp"' — safe in JS context
        js_safe = json.dumps("O'Brien Corp")
        assert js_safe in body, f"Expected json-escaped value {js_safe!r} in response body"
        # Must NOT contain the raw unescaped value inside fd.append
        assert "fd.append('company_name','O'Brien Corp')" not in body

    def test_double_quote_in_company_name(self):
        """Double quotes must be escaped in JS context."""
        resp = self._post_lookup(
            {"company_name": 'Foo "Bar" Inc', "website": "", "phone": ""},
            company_name='Foo "Bar" Inc',
        )
        assert resp.status_code == 200
        body = resp.text
        js_safe = json.dumps('Foo "Bar" Inc')
        assert js_safe in body

    def test_html_injection_in_company_name(self):
        """<script> tags must be escaped in HTML display context."""
        resp = self._post_lookup(
            {
                "company_name": "<script>alert(1)</script>",
                "website": "",
                "phone": "",
            },
            company_name="<script>alert(1)</script>",
        )
        assert resp.status_code == 200
        body = resp.text
        # HTML display context must use html-escaped entities
        assert "&lt;script&gt;" in body
        # JS context must use json.dumps (produces a JSON-quoted string)
        js_safe = json.dumps("<script>alert(1)</script>")
        assert js_safe in body

    def test_backslash_in_values(self):
        """Backslashes must be escaped to prevent JS string breakout."""
        resp = self._post_lookup(
            {
                "company_name": "Back\\slash Co",
                "website": "https://example.com",
                "phone": "555-0100",
                "address_line1": "123 Main\\St",
                "city": "Test City",
                "state": "TX",
                "zip": "75001",
                "country": "US",
            },
        )
        assert resp.status_code == 200
        body = resp.text
        # json.dumps will double-escape the backslash
        assert json.dumps("Back\\slash Co") in body
        assert json.dumps("123 Main\\St") in body

    def test_all_fields_json_escaped(self):
        """Every field embedded in JS uses json.dumps escaping."""
        result = {
            "company_name": "A'B",
            "website": "http://a'b.com",
            "phone": "555'123",
            "address_line1": "1'st Ave",
            "city": "O'Fallon",
            "state": "MO'",
            "zip": "63'66",
            "country": "U'S",
        }
        resp = self._post_lookup(result, company_name="A'B")
        assert resp.status_code == 200
        body = resp.text
        for key, value in result.items():
            js_safe = json.dumps(value)
            assert js_safe in body, (
                f"Field {key!r} value {value!r} not properly json-escaped (expected {js_safe!r} in body)"
            )

    def test_null_result_returns_error(self):
        """When claude_json returns None, show error message."""
        resp = self._post_lookup(None, company_name="Unknown")
        assert resp.status_code == 200
        assert "Could not look up company" in resp.text
