"""approvals/__init__.py — Approval Engine service package.

Purpose: Exposes routing and orchestration services for the approval workflow.

Called by: routers/approvals.py (Task 5+), other services that need to trigger approvals.
Depends on: app.services.approvals.routing, app.services.approvals.service,
            app.services.approvals.events
"""

from .events import cancel, reassign
from .events import record as record_event
from .routing import NoEligibleApproverError, route_request
from .service import create_request, decide

__all__ = [
    "route_request",
    "NoEligibleApproverError",
    "create_request",
    "decide",
    "reassign",
    "cancel",
    "record_event",
]
