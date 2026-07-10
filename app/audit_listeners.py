# SQLAlchemy event listeners — CRM audit trail (created_by / modified_by).
#
# What: Registers before_insert / before_update listeners on Company, CustomerSite,
#       and SiteContact that stamp created_by_id / modified_by_id from the
#       request-scoped contextvar.  Background jobs have no request → contextvar
#       stays None → audit columns stay NULL (correct behaviour).
# Called by: app/main.py (imported at module load so listeners register once)
# Depends on: app/request_context.py, app/models/crm.py
#
# BULK-UPDATE CAVEAT: bulk query().update() calls on these models bypass ORM
# before_update events entirely, so modified_by_id is NOT stamped on those
# paths.  The current callers that do bulk updates (cadence_service,
# activity_service, company_merge_service) only update timestamp columns, so
# missing attribution is acceptable there.  Any NEW bulk-update path that
# requires attribution must set modified_by_id manually — the listener will
# not fire for it.

from sqlalchemy import event

from .models.crm import Company, CustomerSite, SiteContact
from .request_context import current_user_id_var

_AUDIT_MODELS = (Company, CustomerSite, SiteContact)


def _stamp_created(mapper, connection, target) -> None:
    """Set created_by_id and modified_by_id on INSERT if contextvar is set."""
    uid = current_user_id_var.get()
    if uid is None:
        return
    if target.created_by_id is None:
        target.created_by_id = uid
    if target.modified_by_id is None:
        target.modified_by_id = uid


def _stamp_modified(mapper, connection, target) -> None:
    """Set modified_by_id on UPDATE if contextvar is set.

    Intentional asymmetry with _stamp_created:
    - created_by_id is set ONCE on insert (guarded by ``if created_by_id is None``).
    - modified_by_id always reflects the LATEST request user on every update
      (no guard — latest modifier wins).
    - Both are no-ops when the contextvar is None (background writes, CLI, jobs).
    """
    uid = current_user_id_var.get()
    if uid is None:
        return
    target.modified_by_id = uid


def register_audit_listeners() -> None:
    """Register before_insert / before_update listeners on the three CRM entities.

    Idempotent — calling twice does not double-register (SQLAlchemy deduplicates by
    (event_name, listener_fn) identity).
    """
    for model in _AUDIT_MODELS:
        event.listen(model, "before_insert", _stamp_created)
        event.listen(model, "before_update", _stamp_modified)
