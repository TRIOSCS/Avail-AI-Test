# Integrations Bundle: Charts, Apollo, Presence, ACS, Call Records

**Created:** 2026-03-30
**Status:** Approved

## Goal

Five independent integrations that enhance CRM functionality: Chart.js performance dashboards, Apollo/LinkedIn enrichment, Teams presence detection, Azure Communication Services click-to-call, and Teams call records sync.

## 1. Chart.js Performance Dashboards

**Replace** the HTML table in the Performance tab with interactive charts.

### Charts
- **Bar chart:** Avail Scores by user, sorted descending, color-coded (emerald >= 70, amber >= 40, rose < 40)
- **Stacked bar:** Behaviors (blue) + Outcomes (green) breakdown per user

### Technical
- Add `chart.js` to `package.json` and import via Vite
- New JSON endpoint: `GET /api/crm/performance-metrics` returns `{names, scores, behaviors, outcomes}`
- Alpine.js component initializes Chart.js on `<canvas>` elements
- Keep existing HTML table below charts as detail view
- Route handler in `app/routers/crm/views.py`

### Files
- Modify: `package.json` (add chart.js)
- Modify: `app/routers/crm/views.py` (add JSON endpoint)
- Modify: `app/templates/htmx/partials/crm/performance_tab.html` (add canvas + Alpine component)
- Run: `npm install && npm run build`

## 2. Apollo/LinkedIn Enrichment

**Reconnect** Apollo as an enrichment provider alongside Explorium and AI.

### Architecture
- New `app/connectors/apollo.py`:
  - `search_company(domain)` → company data (industry, employee_size, linkedin_url, legal_name)
  - `search_contacts(domain, limit)` → contact list (name, email, phone, title, linkedin_url)
  - Uses `APOLLO_API_KEY` header authentication
  - API base: `https://api.apollo.io/v1`

- Integrate into `app/enrichment_service.py`:
  - Call Apollo in parallel with Explorium (Phase 1)
  - Merge results: Apollo fills gaps left by Explorium
  - Source tracking: `enrichment_source = "apollo"`

### Config
- Add `APOLLO_API_KEY` to `.env.example` and `app/config.py`
- Feature gated: skip Apollo if key not set

### Files
- Create: `app/connectors/apollo.py`
- Modify: `app/enrichment_service.py` (add Apollo phase)
- Modify: `app/config.py` (add apollo_api_key setting)
- Modify: `.env.example` (add APOLLO_API_KEY)
- Create: `tests/test_apollo_connector.py`

## 3. Teams Presence Detection

**Show** online/away/offline status dots next to contacts.

### Architecture
- Add `Presence.Read.All` to `GRAPH_SCOPES` in `app/config.py`
- New `app/services/presence_service.py`:
  - `get_presence(email, token)` → calls `GET /users/{email}/presence`
  - Returns: `"Available"`, `"Away"`, `"Busy"`, `"DoNotDisturb"`, `"Offline"`
  - In-memory cache with 5-minute TTL (bounded to 500 entries, same pattern as Teams email cache)

### UI Integration
- Vendor contact list: green/amber/gray dot next to each contact with an email
- Customer site contacts: same dots
- HTMX lazy-load: presence dots loaded after initial page render via separate partial to avoid blocking

### Files
- Create: `app/services/presence_service.py`
- Modify: `app/config.py` (add Presence.Read.All to GRAPH_SCOPES)
- Modify: `app/templates/htmx/partials/vendors/tabs/contacts.html` (add presence dots)
- Modify: `app/templates/htmx/partials/customers/tabs/site_contacts.html` (add presence dots)
- Create: `tests/test_presence_service.py`

### Note
Adding `Presence.Read.All` to GRAPH_SCOPES requires users to re-authenticate (scope change triggers re-consent). Plan a migration window.

## 4. Azure Communication Services — Click-to-Call

**Enable** one-click PSTN calling from the app with automatic activity logging.

### Architecture
- New `app/services/acs_service.py`:
  - `initiate_call(user_id, to_phone, db)` → creates ACS call, returns call_connection_id
  - Uses Azure Communication Services SDK (`azure-communication-callautomation`)
  - Config: `ACS_CONNECTION_STRING` in `.env`

- New webhook endpoint: `POST /api/webhooks/acs`
  - Receives `CallCompleted` events from ACS
  - Calls existing `log_call_activity()` with duration, direction, external_id
  - Auto-matches phone to CRM entity via existing `match_phone_to_entity()`

- New API endpoint: `POST /api/calls/initiate`
  - Accepts: `{to_phone, contact_name, company_id/vendor_card_id}`
  - Calls `acs_service.initiate_call()`
  - Returns: `{status: "calling", call_id: "..."}`

### UI
- Phone number links in contact cards change from `<a href="tel:...">` to HTMX `hx-post="/api/calls/initiate"` with the phone number as payload
- Show "Calling..." toast on initiation
- Activity auto-logged when call completes (via webhook)

### Config
- Add `ACS_CONNECTION_STRING` to `.env.example` and `app/config.py`
- Add `ACS_CALLBACK_URL` (webhook endpoint URL)
- Feature gated: phone links stay as `tel:` if ACS not configured

### Files
- Create: `app/services/acs_service.py`
- Modify: `app/routers/v13_features/activity.py` (add webhook + initiate endpoints)
- Modify: `app/main.py` (add CSRF exempt for ACS webhook)
- Modify: `app/config.py` (add ACS settings)
- Modify: `.env.example` (add ACS vars)
- Add: `azure-communication-callautomation` to `requirements.txt`
- Create: `tests/test_acs_service.py`

## 5. Teams Call Records Sync

**Auto-log** all Teams calls to ActivityLog.

### Architecture
- Add `CallRecords.Read.All` to `GRAPH_SCOPES` in `app/config.py`
- New `app/jobs/teams_call_jobs.py`:
  - Polls `GET /communications/callRecords` via Graph API
  - Runs daily (or configurable interval)
  - Watermark stored in `system_config` table (key: `teams_calls_last_poll`)
  - For each call record:
    1. Extract: participants, start/end time, call type, direction
    2. Match participants to CRM entities via email
    3. Call `log_call_activity()` with `channel="teams_call"`, `external_id=callRecord.id`
    4. Dedup via external_id (existing pattern)

### Scope Note
`CallRecords.Read.All` is an application permission (not delegated). Requires app-only token via client credentials flow. However, since we decided against app tokens in Phase 2a, we'll use the delegated `CallRecords.Read` scope instead, which reads only the authenticated user's call records. The daily job iterates over connected users (same pattern as `ensure_all_users_subscribed`).

### Files
- Create: `app/jobs/teams_call_jobs.py`
- Modify: `app/jobs/__init__.py` (register job)
- Modify: `app/config.py` (add CallRecords.Read to GRAPH_SCOPES)
- Create: `tests/test_teams_call_jobs.py`

## Build Order

These are independent — build in parallel where possible:

1. **Chart.js** (1 day, no external dependencies)
2. **Apollo enrichment** (2 days, needs API key)
3. **Teams Presence** (1-2 days, needs scope change)
4. **ACS click-to-call** (3-4 days, needs ACS resource + connection string)
5. **Teams Call Records** (3-4 days, needs scope change, same as #3)

Items 3 and 5 share the same scope change deployment — do them together.

## What This Does NOT Include

- 8x8 OAuth migration (not possible via Azure AD)
- Power BI embedding (Chart.js sufficient, no licensing cost)
- Meeting transcripts (low coverage, high effort)
- Salesforce bidirectional sync (deferred, largest effort)
