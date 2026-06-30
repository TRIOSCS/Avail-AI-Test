"""Route + render tests for the Pipeline surface (Approvals rework Phase C).

Covers GET /v2/partials/approvals/pipeline → _render_pipeline_body → _surface_pipeline.html:
- 200 for a buyer fixture, with the empty all-columns state;
- the three visible stage columns (Build / Approve / Purchase) render the right plans by
  status (DRAFT→Build, PENDING→Approve, ACTIVE→Purchase);
- the Done summary is collapsed by default (x-show on the cards, default closed);
- the Mine/All scope toggle fires ?scope=all with hx-target="#bp-hub-body" + push-url off;
- the signature 4-pip stepper renders the correct "ball" position (the single accent node)
  for a plan in each of the four stages.

Reuses the buy-plan builders from tests/test_buyplan_hub_board.py and conftest fixtures
(client, db_session, test_user, test_quote, test_requisition).

Depends on: app/routers/htmx/buy_plans (pipeline lens dispatch),
            app/templates/htmx/partials/approvals/_surface_pipeline.html + _pipeline_macros.html.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus
from tests.test_buyplan_hub_board import _make_plan

PIPELINE_URL = "/v2/partials/approvals/pipeline"


# ── Empty state ─────────────────────────────────────────────────────────────


def test_pipeline_surface_empty_state(client: TestClient):
    """A buyer with no deals gets 200 + the three stage columns and an empty Done
    bar."""
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    # Three visible stage columns, canonical labels only (no Draft/Pending/Active vocab).
    assert "Build" in body
    assert "Approve" in body
    assert "Purchase" in body
    assert "Done" in body
    # Empty → no card carries a pip ball.
    assert "bg-accent-500" not in body


# ── Column bucketing by status ──────────────────────────────────────────────


def test_pipeline_columns_bucket_by_status(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """A DRAFT / PENDING / ACTIVE plan each render under the right column.

    The buyer sees all deals (scope defaults to ``all``); each card links to its detail and
    carries its stage's pip ball, so the aggregate pip counts pin the bucketing:
    balls = 3 (one per card), done pips = 0 (Build) + 1 (Approve) + 2 (Purchase) = 3.
    """
    kw = dict(quote_id=test_quote.id, requisition_id=test_requisition.id)
    draft = _make_plan(db_session, status=BuyPlanStatus.DRAFT, **kw)
    pending = _make_plan(db_session, status=BuyPlanStatus.PENDING, **kw)
    active = _make_plan(db_session, status=BuyPlanStatus.ACTIVE, **kw)

    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text

    # Column headers present.
    assert "Build" in body and "Approve" in body and "Purchase" in body
    # Every plan's card links to its detail.
    for plan in (draft, pending, active):
        assert f'hx-get="/v2/partials/buy-plans/{plan.id}"' in body
    # One ball per card; done-pip total fixes each ball position (0 + 1 + 2 = 3).
    assert body.count("bg-accent-500") == 3
    assert body.count("bg-brand-500") == 3
    # Canonical stage captions appear (one per card).
    assert ">Build</span>" in body
    assert ">Approve</span>" in body
    assert ">Purchase</span>" in body


# ── 4-pip stepper: ball position per stage ──────────────────────────────────


@pytest.mark.parametrize(
    "status,expected_ball_index",
    [
        (BuyPlanStatus.DRAFT, 0),  # Build
        (BuyPlanStatus.PENDING, 1),  # Approve
        (BuyPlanStatus.ACTIVE, 2),  # Purchase
        (BuyPlanStatus.COMPLETED, 3),  # Done
    ],
)
def test_pipeline_pip_ball_position(
    client: TestClient,
    db_session: Session,
    test_user,
    test_quote,
    test_requisition,
    status,
    expected_ball_index,
):
    """A single plan in each stage renders exactly one accent ball, with the number of
    filled (done) pips before it equal to the stage index — proving the ball lands on
    the plan's current stage.

    COMPLETED is server-rendered inside the collapsed Done section.
    """
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=status,
    )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    # Exactly one ball (accent node) for the one card on the page.
    assert body.count("bg-accent-500") == 1
    # Filled done pips precede the ball → their count is the ball's stage index.
    assert body.count("bg-brand-500") == expected_ball_index


# ── Done collapsed by default ───────────────────────────────────────────────


def test_pipeline_done_collapsed_by_default(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """The Done section is collapsed by default — its cards live under x-show with the
    toggle starting closed."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
    )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    # The collapsible starts closed and the completed cards are gated on it.
    assert "doneOpen: false" in body
    assert 'x-show="doneOpen"' in body
    # The Done summary bar shows the count.
    assert ">Done</span>" in body


# ── Scope toggle ────────────────────────────────────────────────────────────


def test_pipeline_scope_toggle_targets_hub_body(client: TestClient):
    """A can-see-all viewer (buyer) gets the Mine/All toggle: ?scope=all reloads the
    body in place (hx-target #bp-hub-body) and never pushes a URL (R6)."""
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "/v2/partials/approvals/pipeline?scope=all" in body
    assert "/v2/partials/approvals/pipeline?scope=mine" in body
    assert 'hx-target="#bp-hub-body"' in body
    assert 'hx-push-url="false"' in body


def test_pipeline_sales_locked_to_mine_no_toggle(client: TestClient, sales_user):
    """A sales user (no cross-owner visibility) gets no scope toggle and scope=all is
    refused, so no other rep's deals leak — exactly like the standalone board."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get(f"{PIPELINE_URL}?scope=all")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "?scope=all" not in resp.text
    assert "?scope=mine" not in resp.text
