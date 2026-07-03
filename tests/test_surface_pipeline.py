"""Route + render tests for the Pipeline surface (Approvals rework Phase C).

Covers GET /v2/partials/buy-plans/pipeline → _render_pipeline_body → _surface_pipeline.html:
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

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from tests.test_buyplan_hub_board import _make_line, _make_plan

PIPELINE_URL = "/v2/partials/buy-plans/pipeline"


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
    assert "/v2/partials/buy-plans/pipeline?scope=all" in body
    assert "/v2/partials/buy-plans/pipeline?scope=mine" in body
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


# ── Halted column (buyer HALTED visibility, Phase F-1) ───────────────────────


def test_pipeline_halted_column_for_can_see_all(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """A can-see-all viewer (buyer) gets a 4th Halted column rendering halted plans."""
    halted = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
    )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "lg:grid-cols-4" in body  # the grid widens to 4 columns
    assert "Halted" in body  # the column header
    assert f'hx-get="/v2/partials/buy-plans/{halted.id}"' in body  # the halted card links to detail


def test_pipeline_halted_column_hidden_for_sales(
    client: TestClient, sales_user, db_session, test_quote, test_requisition
):
    """A sales user (no cross-owner visibility) gets the 3-column grid (no Halted
    lane)."""
    from app.dependencies import require_user
    from app.main import app

    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=sales_user.id,
    )
    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get(PIPELINE_URL)
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    body = resp.text
    assert "lg:grid-cols-4" not in body
    assert "lg:grid-cols-3" in body


# ── Metric strip avg margin (parity, Phase F-1) ──────────────────────────────


def test_pipeline_metric_strip_shows_avg_margin(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """The Pipeline metric strip surfaces the open-book avg margin."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_margin_pct=22,
    )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    assert "avg margin" in resp.text


# ── Richer deal card (parity, Phase F-1) ─────────────────────────────────────


def test_pipeline_deal_card_returned_badge(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """A returned (rejected-resubmit) DRAFT card shows a Returned badge."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        so_status=SOVerificationStatus.REJECTED,
    )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "Returned" in body
    assert "badge-danger" in body


def test_pipeline_deal_card_po_progress_and_blocker(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """An ACTIVE card shows the PO-progress bar (verified/total) and the blocker
    text."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.AWAITING_PO)
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "bg-emerald-500" in body  # PO-progress fill
    assert "1/2" in body  # verified/total
    assert "1 POs to cut" in body  # the blocker text


# ── Done section uses the shared archive-rows include (Phase F-1) ────────────


def test_pipeline_done_uses_archive_rows_include(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """The Done section renders completed deals via the shared archive-rows partial;
    with more than one page it offers a Load older button hitting pipeline-archive."""
    from datetime import datetime, timedelta, timezone

    from app.services.buyplan_hub import ARCHIVE_PAGE_SIZE

    now = datetime.now(timezone.utc)
    for i in range(ARCHIVE_PAGE_SIZE + 1):
        _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.COMPLETED,
            completed_at=now - timedelta(hours=i),
        )
    resp = client.get(PIPELINE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "/v2/partials/buy-plans/pipeline-archive?scope=" in body
    assert "Load older" in body
