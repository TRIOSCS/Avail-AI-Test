"""Health check utilities for the /health endpoint.

Called by: main.py health endpoint
Depends on: config.py settings
"""

from loguru import logger

BACKUP_TIMESTAMP_FILE = "/app/uploads/.last_backup"


def check_backup_freshness() -> str:
    """Check if the last backup timestamp is recent enough.

    Returns "ok", "stale", or "unknown".
    """
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from ..config import settings

    ts_path = Path(BACKUP_TIMESTAMP_FILE)
    if not ts_path.exists():
        return "unknown"

    try:
        raw = ts_path.read_text().strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        backup_time = datetime.fromisoformat(raw)
        if backup_time.tzinfo is None:
            backup_time = backup_time.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - backup_time
        if age < timedelta(hours=settings.backup_max_age_hours):
            return "ok"
        return "stale"
    except (ValueError, OSError) as e:
        logger.warning("Backup freshness check failed: %s", e)
        return "unknown"
