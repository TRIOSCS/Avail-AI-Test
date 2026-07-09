"""test_c1_buyplan_gate.py — QP Phase C1: the approvals engine OWNS the buy-plan gate.

Covers the C1 contract + its three reviewed risk-mitigations:
  - submit_buy_plan (above auto-approve threshold) creates a BUY_PLAN ApprovalRequest
    routed to can_approve_buy_plans holders; plan stays PENDING.
  - decide(approve) drives the buy-plan side effects in the SAME session before commit:
    plan ACTIVE + buyer tasks generated + approved_by set + request APPROVED (RISK 1 —
    atomic dispatch inside decide()).
  - decide(reject) drives the reject side effects: plan → DRAFT, request REJECTED.
  - resubmit cancels the stale open request so exactly ONE REQUESTED request remains
    (RISK 2 — no double request).
  - the approve router falls back to legacy approve_buy_plan when no open engine request
    exists (RISK 3 — pre-C1 transition window) and does not crash.
  - NoEligibleApproverError (no approver configured) leaves the plan PENDING with no
    orphan engine state.
  - ApprovalRequestActionSource counts open requests the user must decide (nav badge).

Called by: pytest
Depends on: conftest (db_session), app.services.buyplan_workflow,
            app.services.approvals.service, app.models.{approvals,buy_plan,auth}.
"""

import contextlib
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_user
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User
from app.models.buy_plan import BuyPlan, VerificationGroupMember
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.service import decide as svc_decide
from app.services.buyplan_workflow import (
    approve_buy_plan,
    cancel_buy_plan,
    halt_plan,
    resubmit_buy_plan,
    submit_buy_plan,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_approver(db: Session) -> User:
    u = User(
        email=f"c1-approver-{uuid.uuid4().hex[:6]}@test.com",
        name="C1 Approver",
        role="admin",
        azure_id=f"azure-c1-appr-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=True,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_draft_plan(db: Session, user: User, *, total_cost: float = 10_000.0) -> BuyPlan:
    """A draft buy plan over the auto-approve threshold (so submit → PENDING, not
    ACTIVE)."""
    req = Requisition(
        name=f"REQ-C1-{uuid.uuid4().hex[:6]}",
        customer_name="C1Co",
        status="active",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QC1-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(quote)
    db.flush()

    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status="pending",
        total_cost=total_cost,
    )
    db.add(bp)
    db.flush()
    return bp


def _open_requests(db: Session, plan_id: int) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
        ).scalars()
    )


# ── submit → engine request ──────────────────────────────────────────────


def test_submit_creates_buy_plan_request(db_session: Session) -> None:
    """A non-auto-approved submit creates ONE BUY_PLAN ApprovalRequest, plan stays
    PENDING."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)

    submit_buy_plan(plan.id, "SO-1001", approver, db_session)

    assert plan.status == BuyPlanStatus.PENDING.value
    reqs = _open_requests(db_session, plan.id)
    assert len(reqs) == 1
    ar = reqs[0]
    assert ar.gate_type == ApprovalGateType.BUY_PLAN
    assert ar.subject_type == ApprovalSubjectType.BUY_PLAN
    assert ar.subject_id == plan.id
    # Routed to the approver as a PENDING recipient.
    recip = db_session.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(ApprovalStep.request_id == ar.id, ApprovalStepRecipient.user_id == approver.id)
    ).scalar_one()
    assert recip.status == "pending"


# ── RISK 1: decide() drives side effects atomically, same session ─────────


def test_decide_approve_drives_side_effects_same_session(db_session: Session) -> None:
    """RISK 1: after decide(approve), plan is ACTIVE *and* request APPROVED in the SAME
    session before any commit, with buyer-task gen run + approver stamped."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1002", approver, db_session)
    ar = _open_requests(db_session, plan.id)[0]

    resolved = svc_decide(db_session, ar.id, approver, "approve", comment="LGTM")

    # No commit has happened — assert end-state in the live session.
    assert resolved.status == ApprovalRequestStatus.APPROVED
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    assert plan.approved_by_id == approver.id
    assert plan.approved_at is not None
    assert plan.approval_notes == "LGTM"


def test_decide_reject_sends_plan_to_draft(db_session: Session) -> None:
    """Decide(reject) → request REJECTED + plan back to DRAFT."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1003", approver, db_session)
    ar = _open_requests(db_session, plan.id)[0]

    resolved = svc_decide(db_session, ar.id, approver, "reject", comment="price too high")

    assert resolved.status == ApprovalRequestStatus.REJECTED
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.DRAFT.value
    assert plan.approval_notes == "price too high"


def test_decide_approve_rolls_back_on_side_effect_failure(db_session: Session, monkeypatch) -> None:
    """RISK 1 (atomicity): if a side effect raises, decide() propagates — the caller's
    transaction rolls back so the request can't land APPROVED while the plan stays
    PENDING."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1004", approver, db_session)
    # Commit the submission so it is its own transaction (mirrors production: submit and
    # decide are separate requests). The rollback below then reverts ONLY the decide work.
    db_session.commit()
    ar = _open_requests(db_session, plan.id)[0]

    # _generate_buyer_tasks is called from WITHIN buyplan_approval (same module, plain
    # intra-module call) — the patch must target that submodule directly, not the
    # `app.services.buyplan_workflow` package (P4.3 split).
    import app.services.buyplan_workflow.buyplan_approval as bw

    def _boom(*args, **kwargs):
        raise RuntimeError("buyer task generation blew up")

    monkeypatch.setattr(bw, "_generate_buyer_tasks", _boom)

    raised = False
    try:
        svc_decide(db_session, ar.id, approver, "approve", comment="ok")
    except RuntimeError:
        raised = True
        db_session.rollback()
    assert raised, "decide() must NOT swallow the side-effect failure"

    # After rollback: request still REQUESTED and plan still PENDING (no split-brain) —
    # the engine never moved the request to APPROVED because the dispatch raised inline.
    db_session.expire_all()
    ar2 = db_session.get(ApprovalRequest, ar.id)
    plan2 = db_session.get(BuyPlan, plan.id)
    assert ar2.status == ApprovalRequestStatus.REQUESTED
    assert plan2.status == BuyPlanStatus.PENDING.value


# ── RISK 2: resubmit cancels the stale request ────────────────────────────


def test_resubmit_leaves_exactly_one_open_request(db_session: Session) -> None:
    """RISK 2: resubmit cancels the stale open request before creating a new one, so
    exactly ONE REQUESTED request exists for the plan."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1005", approver, db_session)
    first = _open_requests(db_session, plan.id)
    assert len(first) == 1
    first_id = first[0].id

    # Manager rejects → plan back to DRAFT, request resolved.
    ar = db_session.get(ApprovalRequest, first_id)
    svc_decide(db_session, ar.id, approver, "reject", comment="redo it")
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.DRAFT.value

    # Salesperson resubmits.
    resubmit_buy_plan(plan.id, "SO-1005R", approver, db_session)
    assert plan.status == BuyPlanStatus.PENDING.value

    open_now = _open_requests(db_session, plan.id)
    assert len(open_now) == 1, f"expected exactly 1 open request, got {[r.id for r in open_now]}"
    assert open_now[0].id != first_id, "resubmit must create a fresh request"


def test_resubmit_after_unresolved_submit_cancels_stale(db_session: Session) -> None:
    """RISK 2 (direct): even if the first request was never decided, a second submit-
    style open cancels it — never two live REQUESTED rows.

    Forced by submitting, resetting the plan to DRAFT directly, then resubmitting.
    """
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1006", approver, db_session)
    stale_id = _open_requests(db_session, plan.id)[0].id

    # Bypass the manager decision: force the plan back to DRAFT (stale request still open).
    plan.status = BuyPlanStatus.DRAFT.value
    db_session.flush()

    resubmit_buy_plan(plan.id, "SO-1006R", approver, db_session)

    open_now = _open_requests(db_session, plan.id)
    assert len(open_now) == 1
    assert open_now[0].id != stale_id
    stale = db_session.get(ApprovalRequest, stale_id)
    assert stale.status == ApprovalRequestStatus.CANCELLED


# ── No eligible approver: plan PENDING, no orphan engine state ─────────────


def test_submit_with_no_approver_leaves_plan_pending(db_session: Session) -> None:
    """If no user holds can_approve_buy_plans, routing raises NoEligibleApproverError;
    the submit logs a WARNING and leaves the plan PENDING with no open request."""
    # Submitter does NOT have the approval right, and no other approver exists.
    submitter = User(
        email=f"c1-noappr-{uuid.uuid4().hex[:6]}@test.com",
        name="No Approver",
        role="buyer",
        azure_id=f"azure-c1-noappr-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=False,
        created_at=datetime.now(UTC),
    )
    db_session.add(submitter)
    db_session.flush()
    plan = _make_draft_plan(db_session, submitter)

    submit_buy_plan(plan.id, "SO-1007", submitter, db_session)

    assert plan.status == BuyPlanStatus.PENDING.value
    assert _open_requests(db_session, plan.id) == []


# ── RISK 3: router fallback when no open request (pre-C1 plan) ─────────────


@contextlib.contextmanager
def _build_client(db: Session, user: User):
    """A TestClient with db + auth (buy-plan approver) overrides, cleaned up on exit
    even if the request raises (contextmanager guarantees the finally block runs)."""
    from app.main import app

    def _db():
        yield db

    def _user():
        return user

    overrides = {get_db: _db, require_user: _user, require_buyplan_approver: _user}
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


def test_approve_router_falls_back_when_no_open_request(db_session: Session) -> None:
    """RISK 3: a plan that is PENDING with NO open engine request (submitted pre-C1) is
    approved via the legacy approve_buy_plan fallback — the action does not crash."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    # Put the plan straight into PENDING WITHOUT opening an engine request (pre-C1 state).
    plan.status = BuyPlanStatus.PENDING.value
    plan.submitted_by_id = approver.id
    db_session.flush()
    db_session.commit()
    assert _open_requests(db_session, plan.id) == []

    with _build_client(db_session, approver) as client:
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve"},
        )

    assert resp.status_code == 200
    db_session.expire_all()
    plan2 = db_session.get(BuyPlan, plan.id)
    assert plan2.status == BuyPlanStatus.ACTIVE.value
    assert plan2.approved_by_id == approver.id


def test_approve_router_uses_engine_when_request_open(db_session: Session) -> None:
    """Happy path: with an open engine request, the router resolves it via decide()."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1008", approver, db_session)
    db_session.commit()
    ar_id = _open_requests(db_session, plan.id)[0].id

    with _build_client(db_session, approver) as client:
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve", "notes": "engine path"},
        )

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.ACTIVE.value
    assert db_session.get(ApprovalRequest, ar_id).status == ApprovalRequestStatus.APPROVED


# ── Nav badge alert source ────────────────────────────────────────────────


def test_approval_action_source_counts_my_open_requests(db_session: Session) -> None:
    """ApprovalRequestActionSource counts open requests where the user is a PENDING
    recipient, and drops the count once the request is decided."""
    from app.services.alerts.sources.approvals import ApprovalRequestActionSource

    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-1009", approver, db_session)
    db_session.flush()

    source = ApprovalRequestActionSource()
    assert source.count_for_user(db_session, approver) == 1

    ar = _open_requests(db_session, plan.id)[0]
    svc_decide(db_session, ar.id, approver, "approve", comment="done")
    db_session.flush()
    assert source.count_for_user(db_session, approver) == 0


# ── Side-effect helper parity (extraction was behavior-neutral) ───────────


def test_legacy_approve_and_engine_decide_reach_same_active_state(db_session: Session) -> None:
    """approve_buy_plan and decide() both call the shared _run_approve_side_effects, so
    a plan approved either way lands in the same ACTIVE end-state."""
    approver = _make_approver(db_session)

    # Legacy path (no engine request): force PENDING then approve directly.
    legacy_plan = _make_draft_plan(db_session, approver)
    legacy_plan.status = BuyPlanStatus.PENDING.value
    db_session.flush()
    approve_buy_plan(legacy_plan.id, "approve", approver, db_session, notes="legacy")
    assert legacy_plan.status == BuyPlanStatus.ACTIVE.value
    assert legacy_plan.approved_by_id == approver.id

    # Engine path.
    engine_plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(engine_plan.id, "SO-1010", approver, db_session)
    ar = _open_requests(db_session, engine_plan.id)[0]
    svc_decide(db_session, ar.id, approver, "approve", comment="engine")
    db_session.refresh(engine_plan)
    assert engine_plan.status == BuyPlanStatus.ACTIVE.value
    assert engine_plan.approved_by_id == approver.id


# ── Cancel / halt cascade the open engine request (no orphan, no resurrection) ──


def _ops_member(db: Session, *, role: str = "buyer") -> User:
    """An active ops verification-group member (defaults to a plain buyer — NEITHER the
    plan submitter NOR a manager/admin — to prove the engine cancel authz still
    holds)."""
    u = User(
        email=f"c1-ops-{uuid.uuid4().hex[:6]}@test.com",
        name="C1 Ops",
        role=role,
        azure_id=f"azure-c1-ops-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=False,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    db.add(VerificationGroupMember(user_id=u.id, is_active=True))
    db.flush()
    return u


def test_cancel_pending_plan_cancels_open_request(db_session: Session) -> None:
    """(a) Cancelling a PENDING plan with an open engine request CANCELS that request
    (it no longer sits REQUESTED in the queue/badge), and a later approve is
    impossible."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-C1CANCEL", approver, db_session)
    ar_id = _open_requests(db_session, plan.id)[0].id

    cancel_buy_plan(plan.id, approver, db_session, reason="customer pulled the order")

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.CANCELLED.value
    # The request is CANCELLED, not REQUESTED — no orphan in the approvals queue/badge.
    assert db_session.get(ApprovalRequest, ar_id).status == ApprovalRequestStatus.CANCELLED
    assert _open_requests(db_session, plan.id) == []

    # Deciding the now-CANCELLED request is impossible (engine rejects a non-REQUESTED req).
    raised = False
    try:
        svc_decide(db_session, ar_id, approver, "approve", comment="too late")
    except ValueError:
        raised = True
    assert raised, "approving a cancelled engine request must raise"
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.CANCELLED.value


def test_orphan_request_approval_does_not_resurrect_cancelled_plan(db_session: Session, monkeypatch) -> None:
    """(b) THE proven bug, closed: if a stale REQUESTED request survives (cancel cascade
    suppressed) on a CANCELLED plan, approving it must NOT resurrect the plan.

    The state guard in ``_run_approve_side_effects`` raises ValueError → the request stays
    open and the plan stays CANCELLED with no buyer tasks generated.
    """
    # _cancel_open_engine_requests_for_plan and _generate_buyer_tasks are both called from
    # WITHIN buyplan_approval (same module, plain intra-module calls) — the patch must
    # target that submodule directly, not the `app.services.buyplan_workflow` package
    # (P4.3 split).
    import app.services.buyplan_workflow.buyplan_approval as bw

    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-C1ORPHAN", approver, db_session)
    ar_id = _open_requests(db_session, plan.id)[0].id

    # Simulate the PRE-FIX orphan: cancel the plan WITHOUT cascading the request (patch the
    # cascade to a no-op), leaving the request stuck REQUESTED on a CANCELLED plan.
    monkeypatch.setattr(bw, "_cancel_open_engine_requests_for_plan", lambda *a, **k: 0)
    cancel_buy_plan(plan.id, approver, db_session, reason="cancelled")
    monkeypatch.undo()
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.CANCELLED.value
    assert _open_requests(db_session, plan.id)  # the orphan still sits REQUESTED

    # An approver picks the orphan out of the queue and approves it.
    task_calls: list = []
    monkeypatch.setattr(bw, "_generate_buyer_tasks", lambda *a, **k: task_calls.append(1))

    raised = False
    try:
        svc_decide(db_session, ar_id, approver, "approve", comment="resurrect?")
    except ValueError:
        raised = True
    assert raised, "the state guard must reject approving an orphan on a cancelled plan"

    # No resurrection: plan stays CANCELLED, never went ACTIVE, no buyer tasks generated.
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.CANCELLED.value
    assert task_calls == [], "buyer tasks must NOT be generated for a cancelled plan"


def test_halt_plan_cancels_open_request(db_session: Session) -> None:
    """(c) HALTing a PENDING plan (via halt_plan) cancels its open engine request, even
    when the halting ops member is a plain buyer (not submitter, not manager/admin) —
    the helper cancels on behalf of the request's own requester/owner so authz holds."""
    approver = _make_approver(db_session)
    ops = _ops_member(db_session)  # plain buyer, distinct from the approver/submitter
    plan = _make_draft_plan(db_session, approver)
    submit_buy_plan(plan.id, "SO-C1HALT", approver, db_session)
    ar_id = _open_requests(db_session, plan.id)[0].id
    assert plan.status == BuyPlanStatus.PENDING.value

    halt_plan(plan.id, ops, db_session, reason="SO mismatch in Acctivate")

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.HALTED.value
    assert db_session.get(ApprovalRequest, ar_id).status == ApprovalRequestStatus.CANCELLED
    assert _open_requests(db_session, plan.id) == []


# ── Badge: post-C1 plan counts on approvals only; pre-C1 plan counts on buy-plans ──


def test_buyplan_badge_excludes_plans_with_open_engine_request(db_session: Session) -> None:
    """(d) A post-C1 PENDING plan (open engine request) is NOT counted by the buy-plans
    ACTION badge (it surfaces on the approvals badge instead — no double-count); a
    pre-C1 PENDING plan (NO engine request) still counts on the buy-plans badge so it
    stays visible."""
    from app.services.alerts.sources.approvals import ApprovalRequestActionSource
    from app.services.alerts.sources.buyplan import BuyplanActionSource

    bp_source = BuyplanActionSource()
    appr_source = ApprovalRequestActionSource()

    approver = _make_approver(db_session)

    # Post-C1 plan: PENDING with an open engine request routed to the approver.
    post_c1 = _make_draft_plan(db_session, approver)
    submit_buy_plan(post_c1.id, "SO-C1BADGE", approver, db_session)
    db_session.flush()

    # The post-C1 plan counts on the approvals badge, NOT the buy-plans badge.
    assert appr_source.count_for_user(db_session, approver) == 1
    assert bp_source.count_for_user(db_session, approver) == 0

    # Pre-C1 plan: PENDING with NO engine request (transition window). It must stay visible
    # on the buy-plans badge.
    pre_c1 = _make_draft_plan(db_session, approver)
    pre_c1.status = BuyPlanStatus.PENDING.value
    pre_c1.so_status = SOVerificationStatus.PENDING.value
    db_session.flush()

    assert bp_source.count_for_user(db_session, approver) == 1
    # The approvals badge is unchanged by the pre-C1 plan (it has no engine request).
    assert appr_source.count_for_user(db_session, approver) == 1
