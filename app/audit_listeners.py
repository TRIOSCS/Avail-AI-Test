# SQLAlchemy event listeners — CRM audit trail (created_by / modified_by).
#
# What: Registers before_insert / before_update listeners on Company, CustomerSite,
#       and SiteContact that stamp created_by_id / modified_by_id from the
#       request-scoped contextvar.  Background jobs have no request → contextvar
#       stays None → audit columns stay NULL (correct behaviour).
# Called by: app/main.py (imported at module load so listeners register once)
# Depends on: app/request_context.py, app/models/crm.py

from sqlalchemy import event

from .models.crm import Company, CustomerSite, SiteContact
from .request_context import current_user_id_var

_AUDIT_MODELS = (Company, CustomerSite, SiteContact)


def _stamp_created(mapper, connection, target) -> None:  # noqa: ARG001
    """Set created_by_id and modified_by_id on INSERT if contextvar is set."""
    uid = current_user_id_var.get()
    if uid is None:
        return
    if target.created_by_id is None:
        target.created_by_id = uid
    if target.modified_by_id is None:
        target.modified_by_id = uid


def _stamp_modified(mapper, connection, target) -> None:  # noqa: ARG001
    """Set modified_by_id on UPDATE if contextvar is set."""
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
