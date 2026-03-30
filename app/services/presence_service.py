"""Teams presence detection service with bounded cache.

Called by: vendor contact templates, customer contact templates
Depends on: app/utils/graph_client.py
"""

import time

from loguru import logger

_presence_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX = 500


async def get_presence(email: str, gc) -> str | None:
    """Get Teams presence status for a user by email.

    Returns: 'Available', 'Away', 'BeRightBack', 'Busy', 'DoNotDisturb', 'Offline', or None on error.
    """
    now = time.monotonic()
    cached = _presence_cache.get(email)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    try:
        data = await gc.get_json(
            f"/users/{email}/presence",
            params={"$select": "availability"},
        )
        status = data.get("availability", "Offline")

        if len(_presence_cache) >= _CACHE_MAX:
            _presence_cache.clear()
        _presence_cache[email] = (status, now)

        return status
    except Exception as e:
        logger.debug(f"Presence lookup failed for {email}: {e}")
        return None


def presence_color(status: str | None) -> str:
    """Return Tailwind CSS class for presence status dot."""
    if status in ("Available",):
        return "bg-emerald-400"
    if status in ("Away", "BeRightBack"):
        return "bg-amber-400"
    if status in ("Busy", "DoNotDisturb"):
        return "bg-rose-400"
    return "bg-gray-300"
