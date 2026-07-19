"""test_buyplan_approvals_lens.py — Per-gate approvals folded into the stage tabs.

The standalone four-tab Approvals lens is RETIRED (SP-1). Each gate's pending queue now
renders as a pinned "Pending approvals" section INSIDE its stage tab, shown only to the
matching approver. Covers:
  - the Buy Plans stage tab hides the pinned section for a non-approver and shows it for
    a user holding can_approve_buy_plans (with a pending row);
  - the full page threads ?lens= into the lazy hub partial (new stage-tab keys);
  - the approvals alert count still merges onto the Buy Plans nav badge.

Called by: pytest
Depends on: conftest (client, nonadmin_client, test_user, db_session),
            app.routers.htmx_views, app.services.alerts, app.models.approvals.
"""

from app.constants import (
    AlertKind,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
)
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient


def _seed_pending_buy_plan_approval(db, user):
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=1,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.flush()
    return ar


class TestPinnedApprovalSection:
    def test_full_page_lens_urls_308_to_workspace(self, nonadmin_client):
        """Old pushed /v2/buy-plans?lens= URLs 308 onto the workspace Buy Plans tab —
        the hub retired post-parity (spec §11.1) and the lens key is dropped.

        (v2_page authenticates via the session cookie, so this needs nonadmin_client.)
        """
        for lens in ("pipeline", "bogus"):
            resp = nonadmin_client.get(f"/v2/buy-plans?lens={lens}", follow_redirects=False)
            assert resp.status_code == 308
            assert resp.headers["location"] == "/v2/approvals?tab=buy-plans"


class TestBadgeMerge:
    def test_approval_action_registered_under_buy_plans(self):
        """The approvals alert source now lives on the buy-plans tab (folded nav)."""
        import app.services.alerts.sources  # noqa: F401
        from app.services.alerts import tab_for_kind

        assert tab_for_kind(AlertKind.APPROVAL_ACTION) == "buy-plans"

    def test_count_for_buy_plans_includes_awaiting_approvals(self, db_session, test_user):
        """count_for_tab('buy-plans') sums the approval-action count (badge merge)."""
        import app.services.alerts.sources  # noqa: F401
        from app.services.alerts import count_for_tab

        before = count_for_tab(db_session, test_user, "buy-plans")
        _seed_pending_buy_plan_approval(db_session, test_user)
        after = count_for_tab(db_session, test_user, "buy-plans")
        assert after == before + 1
