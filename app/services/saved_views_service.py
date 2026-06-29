"""saved_views_service.py — per-user CRM list filter presets (Saved Views).

Backs the Saved Views control on the customers (accounts) and contacts lists.
A saved view is a named, whitelisted snapshot of a list's filter query params
stored per (user, list_key). Re-saving a name overwrites in place (upsert).

Called by: app/routers/htmx/companies.py (saved-views routes)
Depends on: app/models/crm.SavedView
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import SavedView, User

# Filter query-param keys we will persist, per list surface. Anything outside
# the whitelist (e.g. offset/limit, CSRF, stray fields) is dropped so a saved
# view can only ever reconstruct a legitimate filter state.
ALLOWED_FILTER_KEYS: dict[str, frozenset[str]] = {
    "customers": frozenset(
        {"search", "staleness", "account_type", "my_only", "sort", "segment", "disposition", "has_open_reqs"}
    ),
    "contacts": frozenset({"search", "company_id", "contact_role", "cadence_state"}),
}

# "All …" sentinels that mean "no filter" — never stored (apply resets to default).
_EMPTY_SENTINELS = frozenset({"", "0"})

_NAME_MAX = 80
_MAX_VIEWS_PER_LIST = 50


def valid_list_key(list_key: str) -> bool:
    """True if *list_key* names a supported list surface."""
    return list_key in ALLOWED_FILTER_KEYS


def clean_filters(list_key: str, raw: dict) -> dict[str, str]:
    """Whitelist + normalize a raw filter dict for *list_key*.

    Keeps only allowed keys whose stripped value is meaningful (non-empty and not an
    "all" sentinel). Values are coerced to stripped strings — the apply step writes them
    straight back onto the filter form fields.
    """
    allowed = ALLOWED_FILTER_KEYS.get(list_key, frozenset())
    out: dict[str, str] = {}
    for key in allowed:
        val = raw.get(key)
        if val is None:
            continue
        sval = str(val).strip()
        if sval in _EMPTY_SENTINELS:
            continue
        out[key] = sval
    return out


def list_saved_views(db: Session, user: User, list_key: str) -> list[SavedView]:
    """Return *user*'s saved views for *list_key*, name-sorted."""
    if not valid_list_key(list_key):
        return []
    return (
        db.query(SavedView)
        .filter(SavedView.user_id == user.id, SavedView.list_key == list_key)
        .order_by(SavedView.name)
        .all()
    )


def create_saved_view(db: Session, user: User, list_key: str, name: str, raw_filters: dict) -> SavedView:
    """Create or overwrite a saved view (upsert on user_id+list_key+name).

    Raises ValueError for an unknown list_key, a blank name, or when the per-list cap is
    reached on a brand-new name.
    """
    if not valid_list_key(list_key):
        raise ValueError("Unknown list_key")
    clean_name = (name or "").strip()[:_NAME_MAX]
    if not clean_name:
        raise ValueError("A view name is required")

    filters = clean_filters(list_key, raw_filters)

    existing = (
        db.query(SavedView)
        .filter(
            SavedView.user_id == user.id,
            SavedView.list_key == list_key,
            SavedView.name == clean_name,
        )
        .first()
    )
    if existing:
        existing.filters = filters
        db.commit()
        db.refresh(existing)
        return existing

    count = db.query(SavedView).filter(SavedView.user_id == user.id, SavedView.list_key == list_key).count()
    if count >= _MAX_VIEWS_PER_LIST:
        raise ValueError(f"You can save at most {_MAX_VIEWS_PER_LIST} views for this list")

    view = SavedView(user_id=user.id, list_key=list_key, name=clean_name, filters=filters)
    db.add(view)
    db.commit()
    db.refresh(view)
    return view


def delete_saved_view(db: Session, user: User, view_id: int) -> bool:
    """Delete *user*'s saved view by id.

    Returns False if not found / not owned.
    """
    view = db.query(SavedView).filter(SavedView.id == view_id, SavedView.user_id == user.id).first()
    if not view:
        return False
    db.delete(view)
    db.commit()
    return True
