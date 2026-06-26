"""approvals/__init__.py — Approval Engine service package.

Purpose: Groups routing and orchestration services for the approval workflow.
         Import concrete entrypoints from their submodules (.service, .events,
         .routing) — this package exposes no re-exports (codebase convention).

Called by: routers/approvals.py, other services that trigger approvals — each
           imports from the concrete submodule it needs.
Depends on: app.services.approvals.routing, app.services.approvals.service,
            app.services.approvals.events
"""
