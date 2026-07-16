"""Connector-health dashboard assembly — real per-connector rows for the admin UI.

Reads the ApiSource telemetry columns that health_monitor.ping_source /
deep_test_source and the search path maintain (status, last_success, last_error,
error_count_24h, avg_response_ms, total_searches) and applies the shared
auto-degrade heuristic: >=4 errors in the last 24h AND more failures than
successes reports DEGRADED regardless of the stored status.

Called by: app.routers.htmx.settings.admin_api_health (HTMX partial, embedded in the
    settings System tab) and app.routers.admin.system api_connector_health +
    api_health_dashboard (JSON — effective_status only).
Depends on: app.models.config.ApiSource, app.constants.ApiSourceStatus.
"""

from datetime import datetime
from typing import TypedDict

from sqlalchemy.orm import Session

from ..constants import ApiSourceStatus
from ..models.config import ApiSource

# Auto-degrade threshold: minimum 24h error count before the heuristic applies.
DEGRADE_MIN_ERRORS_24H = 4


class ConnectorRow(TypedDict):
    """One per-connector row of the admin health dashboard."""

    name: str
    status: str
    is_active: bool
    last_success: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    error_count_24h: int
    avg_response_ms: int
    total_searches: int


class HealthDashboard(TypedDict):
    """Context for htmx/partials/admin/api_health.html."""

    connectors: list[ConnectorRow]
    overall_status: str


def effective_status(src: ApiSource) -> str:
    """Stored status with the auto-degrade heuristic applied.

    A source that logged >= DEGRADE_MIN_ERRORS_24H errors in the last 24h and failed
    more often than it succeeded is reported as DEGRADED even if its stored status is
    still 'live'.
    """
    total = src.total_searches or 0
    errors_24h = src.error_count_24h or 0
    if errors_24h >= DEGRADE_MIN_ERRORS_24H and total > 0:
        recent_success = max(0, total - errors_24h)
        if errors_24h > recent_success:
            return ApiSourceStatus.DEGRADED.value
    return str(src.status or ApiSourceStatus.PENDING)


def _overall_status(connectors: list[ConnectorRow]) -> str:
    """Roll active connectors up to one dashboard state.

    healthy = every active connector live; down = at least one ERROR source and none
    serving (no live AND no degraded source — a real outage); degraded = any other
    error/degraded presence while something still serves — including an all-degraded
    fleet or an error-plus-degraded mix, whose degraded sources are still returning
    results (heuristic-degraded, stored status live) and so must not read as an outage;
    unknown = no active sources or none checked yet (pending/disabled only).
    """
    active = [c["status"] for c in connectors if c["is_active"]]
    if not active:
        return "unknown"
    has_live = ApiSourceStatus.LIVE.value in active
    has_degraded = ApiSourceStatus.DEGRADED.value in active
    has_error = ApiSourceStatus.ERROR.value in active
    # A degraded source (heuristic-flagged, stored status live) is still returning
    # results, so it counts as serving — only a fleet with NOTHING serving is an outage.
    serving = has_live or has_degraded
    if has_error and not serving:
        return "down"
    if has_error or has_degraded:
        return "degraded"
    return "healthy" if has_live else "unknown"


def get_health_dashboard(db: Session) -> HealthDashboard:
    """Assemble the connector-health dashboard context from api_sources telemetry."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    connectors: list[ConnectorRow] = [
        ConnectorRow(
            name=src.display_name or src.name,
            status=effective_status(src),
            is_active=bool(src.is_active),
            last_success=src.last_success,
            last_error=src.last_error,
            last_error_at=src.last_error_at,
            error_count_24h=src.error_count_24h or 0,
            avg_response_ms=src.avg_response_ms or 0,
            total_searches=src.total_searches or 0,
        )
        for src in sources
    ]
    return HealthDashboard(connectors=connectors, overall_status=_overall_status(connectors))
