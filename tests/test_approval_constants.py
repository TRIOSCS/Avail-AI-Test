"""Tests for Approvals Engine + Quality Plan StrEnum constants.

Verifies that all gate types, status enums, payment, and QP enums are present
with the correct string values.

Called by: pytest
Depends on: app.constants
"""

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalStepRule,
    PaymentMethod,
    QPOrderType,
    QualityPlanStatus,
)


def test_gate_types_are_strenum_values() -> None:
    """Brief from task-1: verbatim assertions required by the spec."""
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert set(ApprovalGateType) >= {"buy_plan", "prepayment", "qp_sales", "qp_purchasing", "purchase_order"}
    assert ApprovalRequestStatus.REQUESTED == "requested"
    assert PaymentMethod.WIRE == "wire"


def test_approval_gate_type_members() -> None:
    """All five gate-type members present with correct values (SP-3 added QP_PURCHASING,
    de-collided from the deal-level PURCHASE_ORDER gate)."""
    assert ApprovalGateType.BUY_PLAN == "buy_plan"
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert ApprovalGateType.QP_SALES == "qp_sales"
    assert ApprovalGateType.QP_PURCHASING == "qp_purchasing"
    assert ApprovalGateType.PURCHASE_ORDER == "purchase_order"
    assert len(ApprovalGateType) == 5


def test_approval_request_status_members() -> None:
    """All five request-status members present."""
    assert ApprovalRequestStatus.REQUESTED == "requested"
    assert ApprovalRequestStatus.APPROVED == "approved"
    assert ApprovalRequestStatus.REJECTED == "rejected"
    assert ApprovalRequestStatus.CANCELLED == "cancelled"
    assert ApprovalRequestStatus.EXPIRED == "expired"
    assert len(ApprovalRequestStatus) == 5


def test_approval_recipient_status_members() -> None:
    """All four recipient-status members present."""
    assert ApprovalRecipientStatus.PENDING == "pending"
    assert ApprovalRecipientStatus.APPROVED == "approved"
    assert ApprovalRecipientStatus.REJECTED == "rejected"
    assert ApprovalRecipientStatus.REASSIGNED == "reassigned"
    assert len(ApprovalRecipientStatus) == 4


def test_approval_step_rule_members() -> None:
    """Both step-rule members present."""
    assert ApprovalStepRule.ANY == "any"
    assert ApprovalStepRule.ALL == "all"
    assert len(ApprovalStepRule) == 2


def test_payment_method_members() -> None:
    """All five payment-method members present (ACH + COD added by the Approvals
    Workspace, migration 192)."""
    assert PaymentMethod.CC == "cc"
    assert PaymentMethod.PAYPAL == "paypal"
    assert PaymentMethod.WIRE == "wire"
    assert PaymentMethod.ACH == "ach"
    assert PaymentMethod.COD == "cod"
    assert len(PaymentMethod) == 5


def test_quality_plan_status_members() -> None:
    """Only the live 'draft' QP-status member remains (the submit/review lifecycle was
    never built — see QualityPlanStatus docstring)."""
    assert QualityPlanStatus.DRAFT == "draft"
    assert len(QualityPlanStatus) == 1


def test_qp_order_type_members() -> None:
    """Only the live 'new' QP-order-type member remains (the 'revision' supersede-flow
    was never built)."""
    assert QPOrderType.NEW == "new"
    assert len(QPOrderType) == 1


def test_all_enums_are_str_comparable() -> None:
    """StrEnum members compare equal to plain strings (StrEnum contract)."""
    assert ApprovalGateType.BUY_PLAN == "buy_plan"
    assert ApprovalRequestStatus.APPROVED == "approved"
    assert ApprovalRecipientStatus.PENDING == "pending"
    assert ApprovalStepRule.ALL == "all"
    assert PaymentMethod.CC == "cc"
    assert QualityPlanStatus.DRAFT == "draft"
    assert QPOrderType.NEW == "new"
