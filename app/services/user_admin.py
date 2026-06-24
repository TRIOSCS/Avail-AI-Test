"""User-admin audit service.

Append-only audit logging for admin actions against users (invite, role change,
activate/deactivate, access grant/revoke — see constants.UserAuditAction).

Functions accept a db Session and do NOT commit — the caller owns the commit.

Called by: admin user-management routes/services (later phases)
Depends on: app.models.UserAdminAudit
"""

from ..models import UserAdminAudit


def record_user_audit(db, *, actor_id, target_user_id, action, detail=None):
    """Append a UserAdminAudit row (caller commits)."""
    db.add(
        UserAdminAudit(
            actor_id=actor_id,
            target_user_id=target_user_id,
            action=str(action),
            detail=detail or {},
        )
    )
