"""test_requisition_service_bulk.py — Characterization tests for the bulk requisition
service helpers.

Pins the batch-assign response invariant and the TERMINAL status constant.

Called by: pytest
Depends on: app.services.requisition_service, app.constants, conftest fixtures
"""

import pytest
from pydantic import ValidationError

from app.constants import RequisitionStatus
from app.schemas.responses import BatchAssignResponse


def test_terminal_constant_values_and_not_a_member():
    """TERMINAL holds exactly the three done-statuses and is not an enum member."""
    assert RequisitionStatus.TERMINAL == frozenset(
        {
            RequisitionStatus.WON,
            RequisitionStatus.LOST,
            RequisitionStatus.CANCELLED,
        }
    )
    assert "TERMINAL" not in RequisitionStatus.__members__


def test_batch_assign_response_requires_assigned_to_and_count():
    """assigned_to is required; count must match ids."""
    ok = BatchAssignResponse(assigned_count=1, assigned_ids=[5], assigned_to="Jane")
    assert ok.assigned_to == "Jane"
    with pytest.raises(ValidationError):
        BatchAssignResponse(assigned_count=1, assigned_ids=[5])  # missing assigned_to
    with pytest.raises(ValidationError):
        BatchAssignResponse(assigned_count=2, assigned_ids=[5], assigned_to="Jane")
