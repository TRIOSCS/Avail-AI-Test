"""tests/test_quote_builder_nightly.py — Coverage tests for quote_builder_modal_multi
(lines 78-103).

The /v2/partials/quote-builder/multi route is shadowed by /{req_id} because FastAPI
registers routes in declaration order and "multi" is not a valid int (yields 422).
We call the async function directly to exercise all branches in lines 78-103.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, test_requisition, test_customer_site, test_company)
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.models import CustomerSite, Requisition, User
from app.routers.quote_builder import quote_builder_modal_multi


def _make_request() -> MagicMock:
    """Return a minimal mock Request object accepted by TemplateResponse."""
    req = MagicMock()
    req.headers = {}
    return req


def _run(coro):
    """Run an async coroutine synchronously (event loop already exists via
    nest_asyncio)."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _call_modal_multi(requisition_ids, user, db):
    """Invoke the async quote_builder_modal_multi function synchronously."""
    return _run(
        quote_builder_modal_multi(
            request=_make_request(),
            requisition_ids=requisition_ids,
            user=user,
            db=db,
        )
    )


def _call_modal_multi_capturing(requisition_ids, user, db, found_req):
    """Run modal_multi with get_req_for_user stubbed to found_req and template_response
    captured.

    Returns (result, captured_context).
    """
    mock_response = HTMLResponse("<html>modal</html>")
    captured_ctx: dict = {}

    def _fake_template_response(template_name, context):
        captured_ctx.update(context)
        return mock_response

    with patch("app.dependencies.get_req_for_user", return_value=found_req):
        with patch(
            "app.template_env.template_response",
            side_effect=_fake_template_response,
        ):
            result = _call_modal_multi(requisition_ids, user, db)

    return result, mock_response, captured_ctx


class TestQuoteBuilderModalMultiInvalidIds:
    """Lines 80-83: ValueError branch when IDs contain non-numeric text."""

    @pytest.mark.parametrize(
        "requisition_ids",
        ["abc,def", "1,bad,3"],
        ids=["non_numeric", "mixed_valid_invalid"],
    )
    def test_invalid_ids_raise_400(self, requisition_ids, db_session: Session, test_user: User):
        with pytest.raises(HTTPException) as exc_info:
            _call_modal_multi(requisition_ids, test_user, db_session)
        assert exc_info.value.status_code == 400


class TestQuoteBuilderModalMultiEmptyIds:
    """Lines 84-85: empty list branch when requisition_ids is blank or whitespace."""

    @pytest.mark.parametrize(
        "requisition_ids",
        ["", "  ,  "],
        ids=["empty_string", "whitespace_only"],
    )
    def test_empty_ids_raise_400(self, requisition_ids, db_session: Session, test_user: User):
        with pytest.raises(HTTPException) as exc_info:
            _call_modal_multi(requisition_ids, test_user, db_session)
        assert exc_info.value.status_code == 400


class TestQuoteBuilderModalMultiNotFound:
    """Lines 88-90: get_req_for_user returns None → 404."""

    def test_req_not_found_raises_404(self, db_session: Session, test_user: User):
        with patch("app.dependencies.get_req_for_user", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                _call_modal_multi("99999", test_user, db_session)
        assert exc_info.value.status_code == 404


class TestQuoteBuilderModalMultiNoCustomerSite:
    """Lines 92-93, 101-113: valid req without a customer_site_id → 200, empty
    customer_name."""

    def test_valid_req_no_customer_site_returns_html(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        test_requisition.customer_site_id = None

        result, mock_response, _ = _call_modal_multi_capturing(
            str(test_requisition.id), test_user, db_session, test_requisition
        )

        assert result is mock_response


class TestQuoteBuilderModalMultiWithCustomerSite:
    """Lines 94-99: has_customer_site=True branch — looks up company name."""

    def test_with_customer_site_and_company_name(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
    ):
        test_requisition.customer_site_id = test_customer_site.id
        db_session.flush()

        result, mock_response, captured_ctx = _call_modal_multi_capturing(
            str(test_requisition.id), test_user, db_session, test_requisition
        )

        assert result is mock_response
        assert captured_ctx.get("has_customer_site") is True
        # Company name comes from test_company ("Acme Electronics") via test_customer_site
        assert captured_ctx.get("customer_name") == "Acme Electronics"

    def test_with_customer_site_missing_from_db(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """customer_site_id set but db.get returns None → customer_name stays empty."""
        test_requisition.customer_site_id = 99999  # non-existent

        result, mock_response, captured_ctx = _call_modal_multi_capturing(
            str(test_requisition.id), test_user, db_session, test_requisition
        )

        assert result is mock_response
        assert captured_ctx.get("has_customer_site") is True
        assert captured_ctx.get("customer_name") == ""

    def test_multi_req_ids_passed_through_to_context(
        self,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """multi_req_ids in the template context must equal the raw requisition_ids
        param."""
        test_requisition.customer_site_id = None
        raw_ids = f"{test_requisition.id},{test_requisition.id + 1}"

        _, _, captured_ctx = _call_modal_multi_capturing(raw_ids, test_user, db_session, test_requisition)

        assert captured_ctx.get("multi_req_ids") == raw_ids
