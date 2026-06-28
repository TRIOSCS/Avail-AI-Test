"""Tests for Approvals Engine + Quality Plan StrEnum constants.

Verifies that all gate types, status enums, payment, sourcing, and QP enums
are present with the correct string values.

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
    SourcingType,
)


def test_gate_types_are_strenum_values() -> None:
    """Brief from task-1: verbatim assertions required by the spec."""
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert set(ApprovalGateType) >= {"buy_plan", "prepayment", "qp_sales", "purchase_order"}
    assert ApprovalRequestStatus.REQUESTED == "requested"
    assert PaymentMethod.WIRE == "wire"


def test_approval_gate_type_members() -> None:
    """All four gate-type members present with correct values."""
    assert ApprovalGateType.BUY_PLAN == "buy_plan"
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert ApprovalGateType.QP_SALES == "qp_sales"
    assert ApprovalGateType.PURCHASE_ORDER == "purchase_order"
    assert len(ApprovalGateType) == 4


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
    """All three payment-method members present."""
    assert PaymentMethod.CC == "cc"
    assert PaymentMethod.PAYPAL == "paypal"
    assert PaymentMethod.WIRE == "wire"
    assert len(PaymentMethod) == 3


def test_sourcing_type_members() -> None:
    """All four sourcing-type members present."""
    assert SourcingType.SPOT == "spot"
    assert SourcingType.CONTRACT == "contract"
    assert SourcingType.COMMODITY == "commodity"
    assert SourcingType.PREFERRED == "preferred"
    assert len(SourcingType) == 4


def test_quality_plan_status_members() -> None:
    """All four QP-status members present."""
    assert QualityPlanStatus.DRAFT == "draft"
    assert QualityPlanStatus.IN_REVIEW == "in_review"
    assert QualityPlanStatus.APPROVED == "approved"
    assert QualityPlanStatus.REJECTED == "rejected"
    assert len(QualityPlanStatus) == 4


def test_qp_order_type_members() -> None:
    """Both QP-order-type members present."""
    assert QPOrderType.NEW == "new"
    assert QPOrderType.REVISION == "revision"
    assert len(QPOrderType) == 2


def test_all_enums_are_str_comparable() -> None:
    """StrEnum members compare equal to plain strings (StrEnum contract)."""
    assert ApprovalGateType.BUY_PLAN == "buy_plan"
    assert ApprovalRequestStatus.APPROVED == "approved"
    assert ApprovalRecipientStatus.PENDING == "pending"
    assert ApprovalStepRule.ALL == "all"
    assert PaymentMethod.CC == "cc"
    assert SourcingType.PREFERRED == "preferred"
    assert QualityPlanStatus.IN_REVIEW == "in_review"
    assert QPOrderType.REVISION == "revision"
