"""Teams presence detection service with bounded LRU cache.

Called by: not yet wired (planned for vendor/customer contact templates)
Depends on: caller-supplied graph client (duck-typed, needs .get_json())
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

        # LRU eviction: remove oldest 20% when at capacity (avoid cache stampede)
        if len(_presence_cache) >= _CACHE_MAX:
            sorted_entries = sorted(_presence_cache.items(), key=lambda x: x[1][1])
            for key, _ in sorted_entries[: _CACHE_MAX // 5]:
                del _presence_cache[key]
        _presence_cache[email] = (status, now)

        return status
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "403" in err_msg:
            logger.error("Presence API auth failure for %s — check Presence.Read.All permission: %s", email, e)
        else:
            logger.warning("Presence lookup failed for %s: %s", email, e)
        return None


def presence_color(status: str | None) -> str:
    """Return Tailwind CSS class for presence status dot."""
    if status == "Available":
        return "bg-emerald-400"
    if status in ("Away", "BeRightBack"):
        return "bg-amber-400"
    if status in ("Busy", "DoNotDisturb"):
        return "bg-rose-400"
    return "bg-gray-300"
