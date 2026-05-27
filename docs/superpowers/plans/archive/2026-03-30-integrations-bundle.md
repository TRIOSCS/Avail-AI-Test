# Integrations Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 integrations: Chart.js performance dashboards, Apollo/LinkedIn enrichment, Teams presence detection, Azure Communication Services click-to-call, and Teams call records sync.

**Architecture:** Each integration is independent. Chart.js adds client-side visualizations to the existing Performance tab. Apollo plugs into the existing enrichment pipeline. Presence and Call Records extend the Graph API integration. ACS adds a new calling service with webhook-based activity logging.

**Tech Stack:** Chart.js 4.x, Apollo REST API, Microsoft Graph API (Presence, CallRecords), Azure Communication Services SDK, FastAPI, HTMX, Alpine.js

**Spec:** `docs/superpowers/specs/2026-03-30-integrations-bundle-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `app/connectors/apollo.py` | Apollo API client (company + contact search) |
| `app/services/presence_service.py` | Teams presence detection with cache |
| `app/services/acs_service.py` | Azure Communication Services call initiation |
| `app/jobs/teams_call_jobs.py` | Teams call records sync job |
| `tests/test_integrations.py` | Tests for all 5 integrations |

### Modified Files
| File | Change |
|------|--------|
| `package.json` | Add chart.js dependency |
| `app/routers/crm/views.py` | Add JSON metrics endpoint |
| `app/templates/htmx/partials/crm/performance_tab.html` | Add Chart.js canvas |
| `app/enrichment_service.py` | Add Apollo phase to enrichment pipeline |
| `app/config.py` | Add Apollo/ACS config + update GRAPH_SCOPES |
| `app/routers/v13_features/activity.py` | Add ACS webhook handler + call initiate endpoint |
| `app/main.py` | Add ACS webhook to CSRF exempt |
| `app/jobs/__init__.py` | Register teams_call_jobs |
| `requirements.txt` | Add azure-communication packages |

---

## Task 1: Chart.js Performance Dashboards

**Files:**
- Modify: `package.json`
- Modify: `app/routers/crm/views.py`
- Modify: `app/templates/htmx/partials/crm/performance_tab.html`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_integrations.py`:

```python
"""Tests for integrations bundle — Charts, Apollo, Presence, ACS, Call Records.

Called by: pytest
Depends on: app.routers.crm.views, app.connectors.apollo, app.services.presence_service
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from tests.conftest import engine  # noqa: F401


class TestPerformanceMetricsEndpoint:
    """Test JSON metrics endpoint for Chart.js."""

    def test_metrics_returns_json(self, client: TestClient):
        """GET /api/crm/performance-metrics returns JSON with score arrays."""
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "names" in data
        assert "scores" in data
        assert "behaviors" in data
        assert "outcomes" in data
        assert isinstance(data["names"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestPerformanceMetricsEndpoint -v`

- [ ] **Step 3: Add chart.js to package.json**

Run: `cd /root/availai && npm install chart.js --save`

- [ ] **Step 4: Add JSON metrics endpoint**

In `app/routers/crm/views.py`, add after the `crm_performance` route:

```python
from fastapi.responses import JSONResponse


@router.get("/api/crm/performance-metrics")
async def performance_metrics_json(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return performance scores as JSON for Chart.js rendering."""
    active_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    month_start = date.today().replace(day=1)
    snapshots = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month_start).all()
    snap_by_user = {s.user_id: s for s in snapshots}

    names = []
    scores = []
    behaviors = []
    outcomes = []

    for u in active_users:
        snap = snap_by_user.get(u.id)
        if snap:
            names.append(u.name or u.email)
            scores.append(round(snap.total_score or 0, 1))
            behaviors.append(round(snap.behavior_total or 0, 1))
            outcomes.append(round(snap.outcome_total or 0, 1))
        else:
            score_data = _compute_user_score(db, u, month_start)
            names.append(u.name or u.email)
            scores.append(round(score_data.get("total_score", 0), 1))
            behaviors.append(round(score_data.get("behavior_total", 0), 1))
            outcomes.append(round(score_data.get("outcome_total", 0), 1))

    return JSONResponse({
        "names": names,
        "scores": scores,
        "behaviors": behaviors,
        "outcomes": outcomes,
    })
```

- [ ] **Step 5: Add Chart.js canvas to performance template**

In `app/templates/htmx/partials/crm/performance_tab.html`, add before the existing table:

```html
  {# Charts #}
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"
       x-data="performanceCharts()"
       x-init="loadCharts()">
    <div class="bg-white rounded-lg shadow border border-gray-200 p-4">
      <h3 class="text-sm font-semibold text-gray-900 mb-3">Overall Scores</h3>
      <canvas x-ref="scoresChart" height="200"></canvas>
    </div>
    <div class="bg-white rounded-lg shadow border border-gray-200 p-4">
      <h3 class="text-sm font-semibold text-gray-900 mb-3">Behaviors vs Outcomes</h3>
      <canvas x-ref="stackedChart" height="200"></canvas>
    </div>
  </div>

  <script>
  function performanceCharts() {
    return {
      async loadCharts() {
        const resp = await fetch('/api/crm/performance-metrics');
        const data = await resp.json();
        if (!data.names.length) return;

        const colors = data.scores.map(s => s >= 70 ? '#059669' : s >= 40 ? '#d97706' : '#e11d48');

        new Chart(this.$refs.scoresChart, {
          type: 'bar',
          data: {
            labels: data.names,
            datasets: [{
              label: 'Overall Score',
              data: data.scores,
              backgroundColor: colors,
              borderRadius: 4,
            }]
          },
          options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, max: 100 } }
          }
        });

        new Chart(this.$refs.stackedChart, {
          type: 'bar',
          data: {
            labels: data.names,
            datasets: [
              { label: 'Behaviors', data: data.behaviors, backgroundColor: '#3b82f6', borderRadius: 4 },
              { label: 'Outcomes', data: data.outcomes, backgroundColor: '#10b981', borderRadius: 4 },
            ]
          },
          options: {
            responsive: true,
            plugins: { legend: { position: 'bottom' } },
            scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } }
          }
        });
      }
    }
  }
  </script>
```

Note: Chart.js needs to be available globally. Add to the Vite entry point or load via CDN in the template. Check `app/static/htmx_app.js` for the pattern — the simplest approach is a CDN script tag: `<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>` in the template.

- [ ] **Step 6: Run test and build**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestPerformanceMetricsEndpoint -v`
Run: `cd /root/availai && npm run build`

- [ ] **Step 7: Commit**

```bash
git add package.json app/routers/crm/views.py app/templates/htmx/partials/crm/performance_tab.html tests/test_integrations.py
git commit -m "feat: add Chart.js performance dashboards with JSON metrics endpoint

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Apollo/LinkedIn Enrichment

**Files:**
- Create: `app/connectors/apollo.py`
- Modify: `app/enrichment_service.py`
- Modify: `app/config.py`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_integrations.py`:

```python
class TestApolloConnector:
    """Test Apollo API connector."""

    def test_search_company_returns_data(self):
        """Apollo search_company returns normalized company data."""
        import asyncio
        from app.connectors.apollo import search_company

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organization": {
                "name": "Test Corp",
                "website_url": "testcorp.com",
                "linkedin_url": "https://linkedin.com/company/testcorp",
                "industry": "Semiconductors",
                "estimated_num_employees": 500,
                "city": "Austin",
                "state": "Texas",
                "country": "United States",
            }
        }

        with patch("app.connectors.apollo.httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            instance.return_value.get = AsyncMock(return_value=mock_resp)

            # Can't easily test async without event loop — test the parsing logic
            from app.connectors.apollo import _parse_company_response
            result = _parse_company_response(mock_resp.json.return_value)

        assert result is not None
        assert result["linkedin_url"] == "https://linkedin.com/company/testcorp"
        assert result["industry"] == "Semiconductors"
```

- [ ] **Step 2: Create Apollo connector**

Create `app/connectors/apollo.py`:

```python
"""Apollo.io API connector for company and contact enrichment.

Called by: app/enrichment_service.py (enrichment pipeline Phase 1b)
Depends on: app/config.py (apollo_api_key)
"""

import httpx
from loguru import logger

APOLLO_BASE = "https://api.apollo.io/v1"


def _parse_company_response(data: dict) -> dict | None:
    """Parse Apollo company response into normalized format."""
    org = data.get("organization")
    if not org:
        return None
    return {
        "source": "apollo",
        "legal_name": org.get("name"),
        "domain": org.get("website_url", "").replace("https://", "").replace("http://", "").rstrip("/"),
        "linkedin_url": org.get("linkedin_url"),
        "industry": org.get("industry"),
        "employee_size": str(org.get("estimated_num_employees", "")) if org.get("estimated_num_employees") else None,
        "hq_city": org.get("city"),
        "hq_state": org.get("state"),
        "hq_country": org.get("country"),
    }


def _parse_contacts_response(data: dict) -> list[dict]:
    """Parse Apollo people search response into normalized contacts."""
    contacts = []
    for person in data.get("people", []):
        contacts.append({
            "source": "apollo",
            "full_name": person.get("name"),
            "email": person.get("email"),
            "phone": person.get("phone_number"),
            "title": person.get("title"),
            "linkedin_url": person.get("linkedin_url"),
        })
    return contacts


async def search_company(domain: str, api_key: str) -> dict | None:
    """Look up a company on Apollo by domain."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{APOLLO_BASE}/organizations/enrich",
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                params={"domain": domain},
            )
            if resp.status_code != 200:
                logger.warning(f"Apollo company lookup failed: {resp.status_code}")
                return None
            return _parse_company_response(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error(f"Apollo company lookup error: {e}")
        return None


async def search_contacts(domain: str, api_key: str, limit: int = 10) -> list[dict]:
    """Search for contacts at a company on Apollo."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{APOLLO_BASE}/mixed_people/search",
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                json={
                    "organization_domains": [domain],
                    "page": 1,
                    "per_page": limit,
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Apollo contacts search failed: {resp.status_code}")
                return []
            return _parse_contacts_response(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error(f"Apollo contacts search error: {e}")
        return []
```

- [ ] **Step 3: Add Apollo config**

In `app/config.py`, add after the 8x8 settings (around line 196):

```python
    # --- Apollo Enrichment ---
    apollo_api_key: str = ""
```

- [ ] **Step 4: Integrate Apollo into enrichment pipeline**

In `app/enrichment_service.py`, find the `enrich_entity` function. After the Explorium phase and before the AI phase, add Apollo as a parallel provider:

```python
    # Phase 1b: Apollo enrichment (parallel with Explorium)
    apollo_result = None
    if settings.apollo_api_key:
        from app.connectors.apollo import search_company as apollo_search
        apollo_result = await apollo_search(domain, settings.apollo_api_key)

    # Merge: Explorium > Apollo > AI (fill gaps)
    if apollo_result:
        for key, val in apollo_result.items():
            if key != "source" and val and not merged.get(key):
                merged[key] = val
```

Read the existing `enrich_entity` function first to understand the `merged` dict pattern and where to insert.

- [ ] **Step 5: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestApolloConnector -v`

- [ ] **Step 6: Commit**

```bash
git add app/connectors/apollo.py app/enrichment_service.py app/config.py tests/test_integrations.py
git commit -m "feat: add Apollo/LinkedIn enrichment connector to enrichment pipeline

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Teams Presence Detection

**Files:**
- Create: `app/services/presence_service.py`
- Modify: `app/config.py:20-25`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_integrations.py`:

```python
class TestPresenceService:
    """Test Teams presence detection."""

    def test_get_presence_returns_status(self):
        """get_presence returns availability string."""
        import asyncio
        from app.services.presence_service import get_presence

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Available"})

        result = asyncio.get_event_loop().run_until_complete(
            get_presence("user@example.com", mock_gc)
        )
        assert result == "Available"

    def test_get_presence_caches_result(self):
        """Repeated calls use cache, not API."""
        import asyncio
        from app.services.presence_service import get_presence, _presence_cache

        _presence_cache.clear()

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Away"})

        asyncio.get_event_loop().run_until_complete(get_presence("cached@example.com", mock_gc))
        asyncio.get_event_loop().run_until_complete(get_presence("cached@example.com", mock_gc))

        # Only one API call despite two get_presence calls
        assert mock_gc.get_json.call_count == 1
```

- [ ] **Step 2: Create presence service**

Create `app/services/presence_service.py`:

```python
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
```

- [ ] **Step 3: Update GRAPH_SCOPES**

In `app/config.py`, update the `GRAPH_SCOPES` string (lines 20-25) to add presence and call records:

```python
GRAPH_SCOPES = (
    "openid profile email offline_access "
    "Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read "
    "Files.ReadWrite Chat.ReadWrite Calendars.Read "
    "ChannelMessage.Send Team.ReadBasic.All Channel.ReadBasic.All "
    "Presence.Read.All CallRecords.Read"
)
```

- [ ] **Step 4: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestPresenceService -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/presence_service.py app/config.py tests/test_integrations.py
git commit -m "feat: add Teams presence detection service with 5-min cache

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Azure Communication Services Click-to-Call

**Files:**
- Create: `app/services/acs_service.py`
- Modify: `app/routers/v13_features/activity.py`
- Modify: `app/main.py:262-273`
- Modify: `app/config.py`
- Modify: `requirements.txt`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_integrations.py`:

```python
class TestACSService:
    """Test Azure Communication Services integration."""

    def test_acs_webhook_endpoint_exists(self, client: TestClient):
        """POST /api/webhooks/acs returns 200 or 400 (not 404)."""
        resp = client.post("/api/webhooks/acs", json={})
        assert resp.status_code != 404

    def test_call_initiate_endpoint_exists(self, client: TestClient):
        """POST /api/calls/initiate returns 200 or 422 (not 404)."""
        resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        # Will fail with config error since ACS not configured, but route exists
        assert resp.status_code != 404
```

- [ ] **Step 2: Add ACS config**

In `app/config.py`, add after Apollo config:

```python
    # --- Azure Communication Services ---
    acs_connection_string: str = ""
    acs_callback_url: str = ""
```

- [ ] **Step 3: Add azure packages to requirements.txt**

Add to `requirements.txt`:

```
# Azure Communication Services (click-to-call)
azure-communication-callautomation>=1.3.0
azure-communication-identity>=1.5.0
```

- [ ] **Step 4: Create ACS service**

Create `app/services/acs_service.py`:

```python
"""Azure Communication Services — click-to-call with auto-logging.

Called by: app/routers/v13_features/activity.py (call initiate + webhook)
Depends on: app/config.py (acs_connection_string), app/services/activity_service.py
"""

from loguru import logger


async def initiate_call(to_phone: str, callback_url: str, connection_string: str) -> dict | None:
    """Initiate a PSTN call via Azure Communication Services.

    Returns call connection info or None on failure.
    """
    if not connection_string:
        logger.warning("ACS connection string not configured")
        return None

    try:
        from azure.communication.callautomation import CallAutomationClient

        client = CallAutomationClient.from_connection_string(connection_string)
        from azure.communication.callautomation import PhoneNumberIdentifier

        call_result = client.create_call(
            target_participant=PhoneNumberIdentifier(to_phone),
            callback_url=callback_url,
        )
        return {
            "call_connection_id": call_result.call_connection_id,
            "status": "initiated",
        }
    except Exception as e:
        logger.error(f"ACS call initiation failed: {e}")
        return None


def handle_call_completed(event_data: dict) -> dict | None:
    """Extract call details from ACS CallCompleted webhook event.

    Returns normalized call data for activity logging.
    """
    try:
        return {
            "call_connection_id": event_data.get("callConnectionId"),
            "duration_seconds": event_data.get("callDurationInSeconds", 0),
            "to_phone": event_data.get("targets", [{}])[0].get("rawId", ""),
            "direction": "outbound",
        }
    except (KeyError, IndexError) as e:
        logger.warning(f"Failed to parse ACS call event: {e}")
        return None
```

- [ ] **Step 5: Add ACS routes**

In `app/routers/v13_features/activity.py`, add after the Teams webhook handler:

```python
@router.post("/api/webhooks/acs")
@limiter.limit("120/minute")
async def acs_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Azure Communication Services webhook — logs completed calls."""
    from app.config import settings

    if not settings.acs_connection_string:
        raise HTTPException(404, "ACS not configured")

    try:
        events = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")

    if isinstance(events, list):
        for event in events:
            event_type = event.get("type", "")
            if "CallCompleted" in event_type or "CallDisconnected" in event_type:
                from app.services.acs_service import handle_call_completed
                from app.services.activity_service import log_call_activity

                call_data = handle_call_completed(event.get("data", {}))
                if call_data:
                    log_call_activity(
                        user_id=None,
                        direction=call_data["direction"],
                        phone=call_data["to_phone"],
                        duration_seconds=call_data["duration_seconds"],
                        external_id=call_data["call_connection_id"],
                        contact_name=None,
                        db=db,
                    )
        db.commit()

    return {"status": "accepted"}


@router.post("/api/calls/initiate")
@limiter.limit("30/minute")
async def initiate_call_endpoint(
    request: Request,
    user: User = Depends(require_user),
):
    """Initiate a PSTN call via ACS."""
    from app.config import settings

    if not settings.acs_connection_string:
        raise HTTPException(503, "Calling service not configured")

    body = await request.json()
    to_phone = body.get("to_phone")
    if not to_phone:
        raise HTTPException(422, "to_phone required")

    from app.services.acs_service import initiate_call

    result = await initiate_call(
        to_phone=to_phone,
        callback_url=settings.acs_callback_url or f"{settings.app_url}/api/webhooks/acs",
        connection_string=settings.acs_connection_string,
    )

    if not result:
        raise HTTPException(500, "Failed to initiate call")

    return result
```

Ensure `require_user` and `User` are imported at the top of the file.

- [ ] **Step 6: Add CSRF exemption**

In `app/main.py`, add to the `exempt_urls` list:

```python
            re.compile(r"/api/webhooks/acs$"),
```

- [ ] **Step 7: Run test**

Run: `pip install azure-communication-callautomation azure-communication-identity && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestACSService -v`

- [ ] **Step 8: Commit**

```bash
git add app/services/acs_service.py app/routers/v13_features/activity.py app/main.py app/config.py requirements.txt tests/test_integrations.py
git commit -m "feat: add Azure Communication Services click-to-call with auto-logging

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Teams Call Records Sync

**Files:**
- Create: `app/jobs/teams_call_jobs.py`
- Modify: `app/jobs/__init__.py`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_integrations.py`:

```python
class TestTeamsCallRecordsJob:
    """Test Teams call records sync job."""

    def test_register_teams_call_jobs_exists(self):
        """register_teams_call_jobs function exists."""
        from app.jobs.teams_call_jobs import register_teams_call_jobs
        assert callable(register_teams_call_jobs)
```

- [ ] **Step 2: Create teams_call_jobs.py**

Create `app/jobs/teams_call_jobs.py`:

```python
"""Teams call records sync job.

Polls Microsoft Graph for Teams call records and logs them to ActivityLog.

Called by: app/jobs/__init__.py (registered with APScheduler)
Depends on: app/utils/graph_client.py, app/services/activity_service.py
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_teams_call_jobs(scheduler, settings):
    """Register Teams call records sync job."""
    scheduler.add_job(
        _job_sync_teams_calls,
        IntervalTrigger(hours=6),
        id="teams_call_records_sync",
        name="Sync Teams call records to activity log",
    )


@_traced_job
async def _job_sync_teams_calls():
    """Sync Teams call records for all connected users."""
    from datetime import datetime, timedelta, timezone

    from ..constants import UserRole
    from ..database import SessionLocal
    from ..models.auth import User

    db = SessionLocal()
    try:
        from ..models.config import SystemConfig
        from ..services.activity_service import log_call_activity
        from ..utils.graph_client import GraphClient
        from ..utils.token_manager import get_valid_token

        # Watermark
        wm_key = "teams_calls_last_poll"
        wm_row = db.query(SystemConfig).filter(SystemConfig.key == wm_key).first()
        since = datetime.now(timezone.utc) - timedelta(days=1)
        if wm_row and wm_row.value:
            try:
                since = datetime.fromisoformat(wm_row.value)
            except ValueError:
                pass

        users = (
            db.query(User)
            .filter(User.m365_connected.is_(True), User.role.in_([UserRole.BUYER, UserRole.SALES, UserRole.TRADER]))
            .all()
        )

        total_logged = 0
        for user in users:
            token = await get_valid_token(user, db)
            if not token:
                continue

            gc = GraphClient(token)
            try:
                records = await gc.get_all_pages(
                    "/me/callRecords",
                    params={
                        "$filter": f"startDateTime gt {since.isoformat()}",
                        "$select": "id,startDateTime,endDateTime,type,modalities",
                        "$top": "50",
                        "$orderby": "startDateTime desc",
                    },
                    max_items=100,
                )
            except Exception as e:
                logger.warning(f"Teams call records fetch failed for {user.email}: {e}")
                continue

            for record in records:
                call_id = record.get("id")
                if not call_id:
                    continue

                start = record.get("startDateTime")
                end = record.get("endDateTime")
                duration = 0
                if start and end:
                    from dateutil.parser import isoparse

                    try:
                        duration = int((isoparse(end) - isoparse(start)).total_seconds())
                    except (ValueError, TypeError):
                        pass

                log_call_activity(
                    user_id=user.id,
                    direction="outbound",
                    phone="",
                    duration_seconds=duration,
                    external_id=f"teams-call-{call_id}",
                    contact_name=None,
                    db=db,
                )
                total_logged += 1

        db.commit()

        # Update watermark
        now_str = datetime.now(timezone.utc).isoformat()
        if wm_row:
            wm_row.value = now_str
        else:
            db.add(SystemConfig(key=wm_key, value=now_str, description="Teams call records last poll"))
        db.commit()

        if total_logged:
            logger.info(f"Teams call sync: logged {total_logged} records for {len(users)} users")

    except Exception as e:
        logger.exception(f"Teams call records sync failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()
```

- [ ] **Step 3: Register in __init__.py**

In `app/jobs/__init__.py`, add import and registration (following existing pattern):

Import:
```python
    from .teams_call_jobs import register_teams_call_jobs
```

Registration:
```python
    register_teams_call_jobs(scheduler, settings)
```

- [ ] **Step 4: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py::TestTeamsCallRecordsJob -v`

- [ ] **Step 5: Commit**

```bash
git add app/jobs/teams_call_jobs.py app/jobs/__init__.py tests/test_integrations.py
git commit -m "feat: add Teams call records sync job (every 6 hours)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Final Verification

- [ ] **Step 1: Run all tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_integrations.py tests/test_crm_views.py tests/test_vendor_discovery.py tests/test_activity_quality.py tests/test_teams_tracking.py -v --timeout=60 2>&1 | tail -15`

- [ ] **Step 2: Run ruff**

Run: `ruff check app/connectors/apollo.py app/services/presence_service.py app/services/acs_service.py app/jobs/teams_call_jobs.py app/routers/crm/views.py`

- [ ] **Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x --timeout=30 2>&1 | tail -10`

- [ ] **Step 4: Commit any fixes and deploy**

```bash
git push origin main && docker compose up -d --build
```
