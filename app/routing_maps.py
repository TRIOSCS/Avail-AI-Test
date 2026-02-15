"""
routing_maps.py — Configurable routing inference maps loader.

Loads brand→commodity and country→region mappings from a JSON config file.
Maps are cached in memory at startup and can be reloaded via reload_routing_maps().

Business Rules:
- All map keys are stored and compared lowercase
- Missing config file falls back to empty dicts (graceful degradation)
- Invalid JSON logs error and keeps previous maps in memory

Called by: services/routing_service.py, routers/v13_features.py (reload endpoint)
Depends on: app/config/routing_maps.json
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

_CONFIG_PATH = Path(__file__).parent / "config" / "routing_maps.json"

_brand_commodity_map: dict[str, str] = {}
_country_region_map: dict[str, str] = {}


def _load_from_file(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read and parse the routing maps JSON file."""
    if not path.exists():
        logger.warning(f"Routing maps config not found at {path}, using empty maps")
        return {}, {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to parse routing maps config: {exc}")
        return {}, {}

    brand_map = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in raw.get("brand_commodity_map", {}).items()
    }
    country_map = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in raw.get("country_region_map", {}).items()
    }
    return brand_map, country_map


def load_routing_maps(path: Path | None = None) -> None:
    """Load (or reload) routing maps from the config file into memory."""
    global _brand_commodity_map, _country_region_map
    target = path or _CONFIG_PATH
    brand, country = _load_from_file(target)
    _brand_commodity_map = brand
    _country_region_map = country
    logger.info(
        f"Routing maps loaded: {len(_brand_commodity_map)} brands, "
        f"{len(_country_region_map)} countries"
    )


def get_brand_commodity_map() -> dict[str, str]:
    """Return the cached brand→commodity map."""
    return _brand_commodity_map


def get_country_region_map() -> dict[str, str]:
    """Return the cached country→region map."""
    return _country_region_map


# Auto-load on import
load_routing_maps()
