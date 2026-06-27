"""test_buyplan_approvals_lens.py — Approvals folded into the Buy Plans hub.

Covers:
  - the hub renders the "Approvals" lens pill ONLY for approvers (can_approve_any);
  - the lens-body route /v2/partials/buy-plans/approvals is approver-gated (403 otherwise)
    and renders the four-tab queue into #bp-hub-body;
  - the approvals alert count merges onto the Buy Plans nav badge (the source is
    re-registered under the "buy-plans" tab).

Called by: pytest
Depends on: conftest (client, test_user, db_session), app.routers.htmx_views,
            app.services.alerts, app.models.approvals.
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


class TestApprovalsLensGating:
    def test_pill_hidden_for_non_approver(self, client, test_user, db_session):
        """A user with no approver toggle does not see the Approvals lens pill."""
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200
        assert "lens=approvals" not in resp.text

    def test_pill_shown_for_approver(self, client, test_user, db_session):
        """An approver sees the Approvals lens pill in the hub switcher."""
        test_user.can_approve_buy_plans = True
        db_session.flush()
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200
        assert "lens=approvals" in resp.text

    def test_lens_body_403_for_non_approver(self, client, test_user, db_session):
        """The lens body is approver-gated."""
        resp = client.get("/v2/partials/buy-plans/approvals")
        assert resp.status_code == 403

    def test_lens_body_renders_four_tabs_into_hub_body(self, client, test_user, db_session):
        """An approver gets all four tab labels and sub-tabs targeting #bp-hub-body."""
        test_user.can_approve_sales_orders = True
        db_session.flush()
        resp = client.get("/v2/partials/buy-plans/approvals?tab=buy_plans")
        assert resp.status_code == 200
        for label in ("Buy Plans", "Sales Orders", "Purchase Orders", "Vendor Prepayments"):
            assert label in resp.text
        assert 'hx-target="#bp-hub-body"' in resp.text

    def test_full_page_threads_lens_to_lazy_partial(self, nonadmin_client):
        """A full-page load of /v2/buy-plans?lens=approvals threads ?lens= into the lazy
        partial URL, so a deep link / reload / the /v2/approvals/queue redirect lands on
        the requested lens instead of the role default.

        (v2_page authenticates via the session cookie, so this needs nonadmin_client,
        not the require_user-only client.)
        """
        resp = nonadmin_client.get("/v2/buy-plans?lens=approvals")
        assert resp.status_code == 200
        assert "/v2/partials/buy-plans?lens=approvals" in resp.text

    def test_full_page_ignores_unknown_lens(self, nonadmin_client):
        """An unknown ?lens= value is dropped (no query string), not echoed verbatim."""
        resp = nonadmin_client.get("/v2/buy-plans?lens=bogus")
        assert resp.status_code == 200
        assert "lens=bogus" not in resp.text


class TestBadgeMerge:
    def test_approval_action_registered_under_buy_plans(self):
        """The approvals alert source now lives on the buy-plans tab (folded nav)."""
        import app.services.alerts.sources  # noqa: F401  — import triggers registration
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
