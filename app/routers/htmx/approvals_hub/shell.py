"""Approvals Workspace shell + tab body — per-viewer badges, the 3-pill shell route, the
tab GET route, and the shared ``render_tab_body``.

W4.8 split of app/routers/htmx/approvals_hub.py — pure structural move: URLs and
behavior unchanged; routes attach to the shared router imported from .common.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....constants import (
    AccessKey,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    BuyPlanLineStatus,
)
from ....database import get_db
from ....dependencies import can_verify_po_line, require_access, require_user
from ....models import BuyPlanLine, User
from ....models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ....template_env import template_response
from .._shared import _base_ctx
from .common import _TAB_LABELS, _TABS, DEFAULT_TAB, _resolve_tab, router

# ── Per-viewer badges ───────────────────────────────────────────────────


def _decidable_gate_counts(db: Session, user: User) -> dict[str, int]:
    """Open engine requests the viewer can decide RIGHT NOW, counted per gate type.

    Mirrors the engine's decide() eligibility (REQUESTED + a PENDING recipient slot),
    same join as queue._actionable_request_ids but grouped by gate for the tab badges.
    """
    rows = db.execute(
        select(ApprovalRequest.gate_type, func.count(func.distinct(ApprovalRequest.id)))
        .join(ApprovalStep, ApprovalStep.request_id == ApprovalRequest.id)
        .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
        .group_by(ApprovalRequest.gate_type)
    ).all()
    return {str(gate): int(cnt) for gate, cnt in rows}


def _po_waiting_on_viewer(db: Session, user: User) -> int:
    """PO-tab badge: lines waiting on THIS viewer.

    = PENDING_VERIFY lines the viewer may approve (can_verify_po_line — right + dollar
    limit) + the viewer's own assigned AWAITING_PO lines (their confirm-PO work).
    """
    pending = (
        db.execute(select(BuyPlanLine).where(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY.value))
        .scalars()
        .all()
    )
    verifiable = sum(1 for line in pending if can_verify_po_line(user, line))
    own_awaiting = int(
        db.execute(
            select(func.count(BuyPlanLine.id)).where(
                BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO.value,
                BuyPlanLine.buyer_id == user.id,
            )
        ).scalar_one()
    )
    return verifiable + own_awaiting


def _viewer_badges(db: Session, user: User) -> dict[str, int]:
    """Per-viewer tab badges (spec §5: tab badges = items waiting on the viewer)."""
    gates = _decidable_gate_counts(db, user)
    return {
        "deals": gates.get(ApprovalGateType.BUY_PLAN.value, 0),
        "purchase-orders": _po_waiting_on_viewer(db, user),
        "prepayments": gates.get(ApprovalGateType.PREPAYMENT.value, 0),
    }


# ── Shell + tab body ────────────────────────────────────────────────────


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
async def approvals_hub_shell(
    request: Request,
    tab: str = "",
    select: int | None = None,
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals Workspace shell (3-pill tab switcher + a lazy tab body).

    The shell renders the three tab pills with per-viewer "waiting on you" badges + a
    lazy body that loads the active tab's split view into ``#ap-hub-body``. ``?tab=``
    threads a deep-link / pushed tab URL; legacy tab keys alias onto the new tabs.
    ``?select=<plan id>`` (the retired /v2/buy-plans/{id} deep-link redirect) threads
    into the tab body so the SO/BP list preselects that plan's pane.
    """
    active_tab = _resolve_tab(tab) or DEFAULT_TAB
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "active_tab": active_tab,
            "tabs": [(key, _TAB_LABELS[key]) for key in _TABS],
            "badges": _viewer_badges(db, user),
            "select": select,
        }
    )
    return template_response("htmx/partials/approvals/approvals_hub.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_hub_tab(
    request: Request,
    tab: str,
    scope: str = "all",
    select: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one workspace tab body (the split view) into ``#ap-hub-body``.

    ``tab`` is one of the three workspace keys (legacy keys alias); any other
    value 404s. ``scope`` seeds the list's Mine/All toggle. ``select`` (a plan id from
    a /v2/buy-plans/{id} deep link) threads into the list's lazy URL so the list
    preselects that plan.
    """
    if _resolve_tab(tab) is None:
        raise HTTPException(404, "Unknown approvals tab")
    return render_tab_body(request, user, db, tab, scope, select=select)


def render_tab_body(
    request: Request, user: User, db: Session, tab: str, scope: str = "all", select: int | None = None
) -> HTMLResponse:
    """Build + render one workspace tab body (shared by the tab GET route and the decide
    handlers' origin=approvals_hub / legacy re-render branches).

    The body is the split view: the left list lazy-loads ``/{tab}/list`` (so a decide
    re-render always repaints a FRESH list), the right pane fills on row selection.
    ``select`` rides the list's lazy URL for deep-link preselection (decide re-renders
    never pass it — a decision must not steal the selection back).
    """
    resolved = _resolve_tab(tab)
    if resolved is None:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update({"tab": resolved, "tab_label": _TAB_LABELS[resolved], "scope": scope, "select": select})
    return template_response("htmx/partials/approvals/_workspace_split.html", ctx)
