"""approvals/__init__.py — Approval Engine service package.

Purpose: Exposes routing and (future) orchestration services for the approval workflow.

Called by: routers/approvals.py (Task 5+), other services that need to trigger approvals.
Depends on: app.services.approvals.routing
"""

from .routing import NoEligibleApproverError, route_request

__all__ = ["route_request", "NoEligibleApproverError"]
