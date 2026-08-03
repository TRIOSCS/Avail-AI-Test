"""Notification outbound links resolve to real pages.

Every URL the app puts into an email or push notification must land on a registered
route — a renamed page must fail here, not in a user's inbox. Born from the ownership-
warning email linking to /companies/{id}, a path that never existed as a page route (the
account page is /v2/customers/{id}).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.routing import Match

from app.models.auth import User
from app.models.crm import Company
from app.services.ownership_service import _send_warning_alert


def _route_exists(path: str) -> bool:
    """True if a GET for ``path`` matches any registered route (incl.

    redirects).
    """
    from app.main import app

    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in app.router.routes)


@pytest.mark.parametrize(
    "path",
    [
        "/v2/customers/1",  # ownership-warning email button
        "/v2/buy-plans/1",  # buy-plan Teams deep link (308 shim into approvals)
        "/v2/proactive",  # proactive Teams push card
        "/p/confirm/some-token",  # prepayment tokenized pay link
    ],
)
def test_notification_link_paths_resolve(path):
    assert _route_exists(path), f"notification links to unregistered path: {path}"


@pytest.mark.asyncio
async def test_ownership_warning_email_links_to_customer_page(db_session, test_company: Company, sales_user: User):
    """The warning email button must target /v2/customers/{id}, not the dead
    /companies/{id}."""
    test_company.account_owner_id = sales_user.id
    db_session.flush()

    gc = MagicMock()
    gc.post_json = AsyncMock(return_value={})
    with (
        patch("app.scheduler.get_valid_token", AsyncMock(return_value="tok")),
        patch("app.utils.graph_client.GraphClient", MagicMock(return_value=gc)),
    ):
        await _send_warning_alert(test_company, days_inactive=40, inactivity_limit=45, db=db_session)

    assert gc.post_json.await_count == 1
    body = gc.post_json.await_args.args[1]["message"]["body"]["content"]
    assert f"/v2/customers/{test_company.id}" in body
    assert f"/companies/{test_company.id}" not in body
