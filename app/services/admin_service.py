"""Admin service — user management, system config, health."""

import os
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import APP_VERSION
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

# ── User Management ──────────────────────────────────────────────────

VALID_ROLES = ("buyer", "sales", "trader", "manager", "admin")


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
    """Update a user's role or active status.

    Guards against self-modification.
    """
    target = db.get(User, user_id)
    if not target:
        return {"error": "User not found", "status": 404}

    if "is_active" in updates and updates["is_active"] is not None:
        if target.id == admin_user.id and not updates["is_active"]:
            return {"error": "Cannot deactivate yourself", "status": 400}
        target.is_active = updates["is_active"]
        logger.info(f"Admin {admin_user.email} set user {target.email} is_active={updates['is_active']}")

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
        logger.info(f"Admin {admin_user.email} changed {target.email} role: {old_role} -> {updates['role']}")

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


def _ensure_config_cache_fresh(db: Session) -> None:
    """Reload the config cache if it has gone stale (TTL expired)."""
    if time.time() - _config_cache_ts > _CONFIG_CACHE_TTL:
        _load_config_cache(db)


def get_config_value(db: Session, key: str) -> str | None:
    """Get a single config value with in-memory caching (5-min TTL)."""
    _ensure_config_cache_fresh(db)
    return _config_cache.get(key)


def get_config_values(db: Session, keys: list[str]) -> dict[str, str]:
    """Get multiple config values with in-memory caching."""
    _ensure_config_cache_fresh(db)
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
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


def set_config_value(db: Session, key: str, value: str, admin_email: str) -> dict:
    """Update a config value.

    Creates if missing.
    """
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if not row:
        return {"error": f"Config key '{key}' not found", "status": 404}
    old_value = row.value
    row.value = value
    row.updated_by = admin_email
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    # Invalidate the in-memory cache so the next resolver read reflects this write
    # immediately rather than serving the stale value until the 5-min TTL lapses.
    _invalidate_config_cache()
    logger.info(f"Config {key} changed: {old_value} -> {value} by {admin_email}")
    return {"key": row.key, "value": row.value, "updated_by": row.updated_by}


def _invalidate_config_cache() -> None:
    """Force the next config read to reload from the DB.

    Resetting the cache timestamp makes _ensure_config_cache_fresh treat the cache as
    stale on the next access, so a freshly-written value is picked up promptly.
    """
    global _config_cache_ts
    _config_cache_ts = 0


# ── Effective-flag resolver (DB row overrides env default) ──────────────
#
# The System settings tab edits system_config DB rows; these resolvers make that row
# authoritative for the 4 feature flags. The env-backed Pydantic setting is the
# fallback default used only when the DB row is absent or unparseable. Consumers pass
# their existing settings.<flag> value as env_default, so behaviour is unchanged until
# an admin deliberately flips a toggle (see app/startup.py no-surprise reconcile).

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def _safe_config_value(db: Session | None, key: str) -> str | None:
    """Read a config value, returning None on a missing session or any DB read error.

    The resolver contract is "DB row wins, else env default" — a transient DB failure or
    an unprovisioned schema must degrade to the env default, never crash a consumer
    (e.g. scheduler registration reading flags before the table exists in some
    contexts).
    """
    if db is None:
        return None
    try:
        return get_config_value(db, key)
    except SQLAlchemyError as e:
        logger.warning("Config read for '{}' failed; falling back to env default: {}", key, e)
        # Clear any aborted-transaction state so later reads on this session can proceed.
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        return None


def get_effective_flag(db: Session | None, key: str, env_default: bool) -> bool:
    """Resolve a boolean feature flag: DB row wins, else env_default.

    Returns the parsed DB value (case-insensitive "true"/"false" family) when the
    system_config row exists and holds a valid bool string; otherwise env_default.
    A missing session (db is None), a DB read error, or a malformed/absent row falls
    back to env_default and never raises.
    """
    raw = _safe_config_value(db, key)
    if raw is None:
        return env_default
    normalized = raw.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    return env_default


def get_effective_int(db: Session | None, key: str, env_default: int) -> int:
    """Resolve an integer config value: DB row wins, else env_default.

    Returns int(DB value) when the system_config row exists and parses as an int;
    otherwise env_default. A missing session, a DB read error, an absent row, or a
    non-integer value falls back to env_default and never raises.
    """
    raw = _safe_config_value(db, key)
    if raw is None:
        return env_default
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return env_default


# ── System Health ────────────────────────────────────────────────────


def get_system_health(db: Session) -> dict:
    """System health: version, DB stats, scheduler status, connector health."""
    # Row counts — use display-friendly labels
    TABLE_LABELS = {
        "users": "Users",
        "requisitions": "Requisitions",
        "requirements": "Requirements",
        "sightings": "Sightings",
        "companies": "Customers",
        "vendor_cards": "Vendor Cards",
        "material_cards": "Material Cards",
        "offers": "Offers",
        "quotes": "Quotes",
    }
    counts = {}
    for key, model in [
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
        label = TABLE_LABELS.get(key, key.replace("_", " ").title())
        try:
            counts[label] = db.query(sqlfunc.count(model.id)).scalar() or 0
        except Exception:
            counts[label] = -1

    # Per-user scheduler status
    scheduler_status = [
        {
            "id": u.id,
            "email": u.email,
            "m365_connected": u.m365_connected,
            "has_refresh_token": bool(u.refresh_token),
            "token_expires_at": u.token_expires_at.isoformat() if u.token_expires_at else None,
            "last_inbox_scan": u.last_inbox_scan.isoformat() if u.last_inbox_scan else None,
            "last_contacts_sync": u.last_contacts_sync.isoformat() if u.last_contacts_sync else None,
        }
        for u in db.query(User).all()
    ]

    # Connector health from api_sources
    connectors = []
    try:
        connectors = [
            {
                "name": s.name,
                "display_name": s.display_name,
                "status": s.status,
                "category": s.category,
                "last_success": s.last_success.isoformat() if s.last_success else None,
                "last_error": s.last_error,
                "total_searches": s.total_searches,
                "total_results": s.total_results,
            }
            for s in db.query(ApiSource).order_by(ApiSource.name).all()
        ]
    except Exception as e:
        logger.warning("Admin health: connector stats query failed: {}", e)

    return {
        "version": APP_VERSION,
        "db_stats": counts,
        "scheduler": scheduler_status,
        "connectors": connectors,
    }
