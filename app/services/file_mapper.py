"""File mapper — maps URL routes to source files for diagnosis context.

Scans app/routers/ for FastAPI route decorators and builds a mapping from
URL patterns to the relevant router, service, model, and template files.

Called by: services/diagnosis_service.py
Depends on: app/routers/ (file scan)
"""

import os
import re
from pathlib import Path
from functools import lru_cache

from loguru import logger

APP_ROOT = Path(__file__).parent.parent

# Files that must NEVER be auto-modified by the self-heal pipeline
STABLE_FILES = frozenset([
    "app/main.py",
    "app/database.py",
    "app/dependencies.py",
    "app/config.py",
    "app/startup.py",
    "app/models/base.py",
    "app/models/__init__.py",
    "alembic/env.py",
])

_ROUTE_RE = re.compile(
    r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']'
)


@lru_cache(maxsize=1)
def scan_routers() -> dict[str, str]:
    """Scan app/routers/ and return {route_pattern: router_file_path}.

    Example: {"/api/trouble-tickets": "app/routers/trouble_tickets.py"}
    """
    routers_dir = APP_ROOT / "routers"
    if not routers_dir.is_dir():
        logger.warning("Routers directory not found: {}", routers_dir)
        return {}

    route_map = {}
    for py_file in sorted(routers_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        rel_path = f"app/routers/{py_file.name}"
        try:
            content = py_file.read_text(errors="replace")
        except OSError:
            continue
        for match in _ROUTE_RE.finditer(content):
            route_pattern = match.group(2)
            # Normalize parametric segments: {id} → {param}
            normalized = re.sub(r'\{[^}]+\}', '{param}', route_pattern)
            route_map[normalized] = rel_path
    return route_map


def _singularize(name: str) -> str:
    """Naive singularization: strip trailing 's'."""
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def get_relevant_files(
    route_pattern: str | None = None,
    error_context: str | None = None,
) -> list[dict]:
    """Return files relevant to a route or error, with role and confidence.

    Each entry: {"path": str, "role": str, "confidence": float, "stable": bool}
    Roles: "router", "service", "model", "template", "static"
    """
    files = []
    route_map = scan_routers()

    # Find matching router file
    router_file = None
    if route_pattern:
        normalized = re.sub(r'\{[^}]+\}', '{param}', route_pattern)
        # Try exact match first
        router_file = route_map.get(normalized)
        # Try prefix match (require at least /api/X prefix to avoid false matches)
        if not router_file:
            for pattern, rfile in route_map.items():
                prefix = pattern.rsplit('/', 1)[0]
                if len(prefix) > 5 and normalized.startswith(prefix):
                    router_file = rfile
                    break

    if router_file:
        files.append({
            "path": router_file,
            "role": "router",
            "confidence": 0.9,
            "stable": router_file in STABLE_FILES,
        })
        # Infer service file from router name
        base = Path(router_file).stem  # e.g. "trouble_tickets"
        singular = _singularize(base)  # e.g. "trouble_ticket"
        service_candidates = [
            f"app/services/{base}_service.py",
            f"app/services/{singular}_service.py",
            f"app/services/{base}.py",
            f"app/services/{singular}.py",
        ]
        for svc in service_candidates:
            if (APP_ROOT.parent / svc).is_file():
                files.append({
                    "path": svc,
                    "role": "service",
                    "confidence": 0.8,
                    "stable": svc in STABLE_FILES,
                })
                break

        # Infer model file
        model_candidates = [
            f"app/models/{singular}.py",
            f"app/models/{base}.py",
        ]
        for mp in model_candidates:
            if (APP_ROOT.parent / mp).is_file():
                files.append({
                    "path": mp,
                    "role": "model",
                    "confidence": 0.6,
                    "stable": mp in STABLE_FILES,
                })
                break

    # If error_context mentions specific files, add them
    if error_context:
        mentioned = re.findall(r'app/\S+\.py', error_context)
        for m in mentioned:
            if not any(f["path"] == m for f in files):
                files.append({
                    "path": m,
                    "role": "mentioned",
                    "confidence": 0.7,
                    "stable": m in STABLE_FILES,
                })

    return files


def has_stable_files(file_list: list[dict]) -> bool:
    """Check if any files in the list are in STABLE_FILES."""
    return any(f.get("stable") for f in file_list)
