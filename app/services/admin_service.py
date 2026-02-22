"""Admin service — user management, system config, health."""

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import APP_VERSION, settings
from ..models import (
    ApiSource,
    Company,
    MaterialCard,
    Offer,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    SystemConfig,
    User,
    VendorCard,
)

log = logging.getLogger(__name__)


# ── User Management ──────────────────────────────────────────────────

VALID_ROLES = ("buyer", "sales", "trader", "manager", "admin", "dev_assistant")


def list_users(db: Session) -> list[dict]:
    """Return all users with role, active status, and M365 info."""
    users = db.query(User).order_by(User.name).all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role or "buyer",
            "is_active": getattr(u, "is_active", True),
            "m365_connected": u.m365_connected,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


def update_user(db: Session, user_id: int, updates: dict, admin_user: User) -> dict:
    """Update a user's role or active status. Guards against self-modification."""
    target = db.get(User, user_id)
    if not target:
        return {"error": "User not found", "status": 404}

    if "is_active" in updates and updates["is_active"] is not None:
        if target.id == admin_user.id and not updates["is_active"]:
            return {"error": "Cannot deactivate yourself", "status": 400}
        target.is_active = updates["is_active"]
        log.info(
            f"Admin {admin_user.email} set user {target.email} is_active={updates['is_active']}"
        )

    if "role" in updates and updates["role"] is not None:
        if target.id == admin_user.id:
            return {"error": "Cannot change your own role", "status": 400}
        if updates["role"] not in VALID_ROLES:
            return {
                "error": f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}",
                "status": 400,
            }
        old_role = target.role
        target.role = updates["role"]
        log.info(
            f"Admin {admin_user.email} changed {target.email} role: {old_role} -> {updates['role']}"
        )

    if "name" in updates and updates["name"] is not None:
        target.name = updates["name"].strip()

    db.commit()
    return {
        "id": target.id,
        "name": target.name,
        "email": target.email,
        "role": target.role,
        "is_active": getattr(target, "is_active", True),
    }


# ── System Config (with in-memory cache) ────────────────────────────

_config_cache: dict[str, str] = {}
_config_cache_ts: float = 0
_CONFIG_CACHE_TTL = 0 if os.environ.get("TESTING") else 300  # 5 minutes (disabled in tests)


def _load_config_cache(db: Session) -> dict[str, str]:
    """Load all config into memory cache."""
    global _config_cache, _config_cache_ts
    rows = db.query(SystemConfig).all()
    _config_cache = {r.key: r.value for r in rows}
    _config_cache_ts = time.time()
    return _config_cache


def get_config_value(db: Session, key: str) -> str | None:
    """Get a single config value with in-memory caching (5-min TTL)."""
    global _config_cache, _config_cache_ts
    if time.time() - _config_cache_ts > _CONFIG_CACHE_TTL:
        _load_config_cache(db)
    return _config_cache.get(key)


def get_config_values(db: Session, keys: list[str]) -> dict[str, str]:
    """Get multiple config values with in-memory caching."""
    global _config_cache, _config_cache_ts
    if time.time() - _config_cache_ts > _CONFIG_CACHE_TTL:
        _load_config_cache(db)
    return {k: _config_cache[k] for k in keys if k in _config_cache}


def get_all_config(db: Session) -> list[dict]:
    """Return all system_config rows."""
    rows = db.query(SystemConfig).order_by(SystemConfig.key).all()
    return [
        {
            "key": r.key,
            "value": r.value,
            "description": r.description,
            "updated_by": r.updated_by,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def set_config_value(db: Session, key: str, value: str, admin_email: str) -> dict:
    """Update a config value. Creates if missing."""
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if not row:
        return {"error": f"Config key '{key}' not found", "status": 404}
    old_value = row.value
    row.value = value
    row.updated_by = admin_email
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    # Invalidate in-memory cache so next read picks up the change
    global _config_cache_ts
    _config_cache_ts = 0
    log.info(f"Config {key} changed: {old_value} -> {value} by {admin_email}")
    return {"key": row.key, "value": row.value, "updated_by": row.updated_by}



# ── System Health ────────────────────────────────────────────────────


def get_system_health(db: Session) -> dict:
    """System health: version, DB stats, scheduler status, connector health."""
    # Row counts
    counts = {}
    for label, model in [
        ("users", User),
        ("requisitions", Requisition),
        ("requirements", Requirement),
        ("sightings", Sighting),
        ("companies", Company),
        ("vendor_cards", VendorCard),
        ("material_cards", MaterialCard),
        ("offers", Offer),
        ("quotes", Quote),
    ]:
        try:
            counts[label] = db.query(sqlfunc.count(model.id)).scalar() or 0
        except Exception:
            counts[label] = -1

    # Per-user scheduler status
    users = db.query(User).all()
    scheduler_status = []
    for u in users:
        scheduler_status.append(
            {
                "id": u.id,
                "email": u.email,
                "m365_connected": u.m365_connected,
                "has_refresh_token": bool(u.refresh_token),
                "token_expires_at": u.token_expires_at.isoformat()
                if u.token_expires_at
                else None,
                "last_inbox_scan": u.last_inbox_scan.isoformat()
                if u.last_inbox_scan
                else None,
                "last_contacts_sync": u.last_contacts_sync.isoformat()
                if u.last_contacts_sync
                else None,
            }
        )

    # Connector health from api_sources
    connectors = []
    try:
        sources = db.query(ApiSource).order_by(ApiSource.name).all()
        for s in sources:
            connectors.append(
                {
                    "name": s.name,
                    "display_name": s.display_name,
                    "status": s.status,
                    "category": s.category,
                    "last_success": s.last_success.isoformat()
                    if s.last_success
                    else None,
                    "last_error": s.last_error,
                    "total_searches": s.total_searches,
                    "total_results": s.total_results,
                }
            )
    except Exception:
        pass

    return {
        "version": APP_VERSION,
        "db_stats": counts,
        "scheduler": scheduler_status,
        "connectors": connectors,
    }
