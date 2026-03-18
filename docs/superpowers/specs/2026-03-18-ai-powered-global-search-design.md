# AI-Powered Global Search — Design Spec

## Goal

Replace the limited global search bar (3 entities, ILIKE only) with a two-tier universal search that covers 7 entity types with fuzzy matching (Tier 1) and Claude Haiku intent parsing for natural language queries (Tier 2).

## Architecture

```
Tier 1 (type-ahead, <100ms):
  User types → 300ms debounce → pg_trgm fuzzy + ILIKE across 7 entities
  → dropdown with smart best-match + grouped sections

Tier 2 (AI search, <2s):
  User presses Enter → Claude Haiku structured output → intent JSON
  → targeted SQLAlchemy queries → smart answer in dropdown
  → "View all results" link → full search results page

Fallback: If Claude API fails/times out → silently fall back to Tier 1
```

## Tech Stack

- **pg_trgm** PostgreSQL extension for fuzzy/similarity matching
- **Claude Haiku** (`claude_structured()` with `model_tier="fast"`) for intent parsing
- **Redis** for AI search result caching (5-min TTL)
- **HTMX** for dropdown updates + full page navigation
- **Alpine.js** for dropdown state management

---

## Tier 1: Fast SQL Search (Type-Ahead)

### Entities & Fields Searched

| Entity | Table | Fields | Display |
|--------|-------|--------|---------|
| Requisitions | `requisitions` | `name`, `customer_name` | Name + customer |
| Companies | `companies` | `name`, `domain` | Name |
| Vendors | `vendor_cards` | `display_name`, `normalized_name`, `domain` | Display name |
| Vendor Contacts | `vendor_contacts` | `full_name`, `email`, `phone` | Name + email |
| Site Contacts | `site_contacts` | `full_name`, `email`, `phone` | Name + email |
| Parts | `requirements` | `primary_mpn`, `normalized_mpn`, `brand` | MPN + brand |
| Offers | `offers` | `vendor_name`, `mpn` | MPN + vendor |

### Matching Strategy

1. **pg_trgm extension**: `CREATE EXTENSION IF NOT EXISTS pg_trgm;` (in Alembic migration)
2. **GIN indexes**: On all searched columns for fast similarity lookups
3. **Query logic**: Use `ILIKE` for exact substring match + `similarity()` for fuzzy ranking
4. **Result limit**: 5 per entity type in dropdown (35 max total), ordered by similarity score (matches existing behavior)
5. **Minimum query length**: 2 characters (unchanged)

### Dropdown UX

- **Smart answer**: The single highest-similarity result across all entities is shown prominently at the top with a larger card (entity type badge, key details)
- **Grouped sections**: Below the smart answer, remaining results grouped by entity type (only show groups that have results)
- **"View all results" link**: At the bottom of the dropdown, navigates to `/v2/search/results?q=<query>`
- Each result is clickable, navigates to the entity's detail page via HTMX

### New Service: `app/services/global_search_service.py`

Single function that runs all 7 entity queries and returns structured results:

```python
def fast_search(query: str, db: Session) -> dict:
    """Search all entities with pg_trgm fuzzy matching.

    NOTE: This is a sync function (db is a sync SQLAlchemy Session).
    The async route handler calls it directly — FastAPI runs sync functions
    in a thread pool automatically.

    In test mode (TESTING=1 / SQLite), falls back to plain ILIKE matching
    since pg_trgm is PostgreSQL-only. The similarity() ranking is skipped
    and results are ordered by id desc instead.

    Returns:
        {
            "best_match": {"type": "vendor_contact", "id": 42, ...} | None,
            "groups": {
                "requisitions": [...],
                "companies": [...],
                "vendors": [...],
                "vendor_contacts": [...],
                "site_contacts": [...],
                "parts": [...],
                "offers": [...],
            },
            "total_count": int,
        }
    """
```

This decouples search logic from the route handler. The existing `global_search()` route in `htmx_views.py` will call this service instead of inline queries.

All queries use `escape_like()` from `app/utils/sql_helpers.py` for ILIKE parameters. For `similarity()` calls, use SQLAlchemy's `func.similarity(column, bindparam)` — never string interpolation.

---

## Tier 2: AI-Powered Search (Enter to Search)

### Intent Parsing

When user presses Enter, the query is sent to Claude Haiku via `claude_structured()` with this schema:

```python
SEARCH_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "searches": {
            "type": "array",
            "description": "One or more search operations to perform",
            "items": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": [
                            "requisition", "company", "vendor",
                            "vendor_contact", "site_contact",
                            "part", "offer"
                        ],
                    },
                    "text_query": {
                        "type": "string",
                        "description": "Free-text to search/match against",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Structured filters to apply",
                        "properties": {
                            "status": {"type": "string"},
                            "customer_name": {"type": "string"},
                            "vendor_name": {"type": "string"},
                            "brand": {"type": "string"},
                            "email_domain": {"type": "string"},
                            "is_blacklisted": {"type": "boolean"},
                        },
                    },
                },
                "required": ["entity_type", "text_query"],
            },
        },
    },
    "required": ["searches"],
}
```

### System Prompt

```
You are a search intent parser for an electronic component sourcing platform.
Given a user's search query, determine which entities they want to find and what
filters to apply.

Available entities:
- requisition: Purchase requests. Fields: name, customer_name, status (active/closed/cancelled)
- company: Customer/prospect companies. Fields: name, domain, account_type (Customer/Prospect/Partner/Competitor)
- vendor: Component suppliers. Fields: display_name, domain, is_blacklisted
- vendor_contact: People at vendor companies. Fields: full_name, email, phone, title
- site_contact: People at customer companies. Fields: full_name, email, phone, title, contact_role (buyer/technical/decision_maker)
- part: Component requirements. Fields: primary_mpn, normalized_mpn, brand, sourcing_status (open/sourcing/offered/quoted/won/lost)
- offer: Vendor price quotes. Fields: mpn, vendor_name, status (active/sold)

Rules:
- If the query looks like an email address, search vendor_contacts and site_contacts by email
- If the query looks like a phone number, search vendor_contacts and site_contacts by phone
- If the query looks like a part number (alphanumeric with dashes), search parts and offers by MPN
- If the query mentions a company by name, search companies and vendors
- If the query is ambiguous, return multiple searches to cover likely intents
- Always set text_query to the relevant search term extracted from the natural language

Examples:
- "LM358" → search parts (text_query="LM358") + offers (text_query="LM358")
- "john@acme.com" → search vendor_contacts (text_query="john@acme.com") + site_contacts (text_query="john@acme.com")
- "open reqs for Raytheon" → search requisitions (text_query="Raytheon", filters={status:"active", customer_name:"Raytheon"})
- "who sells LM317?" → search parts (text_query="LM317") + offers (text_query="LM317") + vendors (text_query="LM317")
```

### AI Search Flow

1. User presses Enter in the search bar
2. HTMX sends `POST /v2/partials/search/ai` with `q=<query>`
3. **Redis cache check**: Hash the query, check for cached result (5-min TTL)
4. **Rate limit check**: 10 AI searches per user per minute (use existing rate limit middleware)
5. Call `claude_structured()` with `model_tier="fast"`, `timeout=5`
6. Parse intent JSON, execute targeted SQLAlchemy queries per search operation
7. Cache result in Redis
8. Return HTMX partial with smart answer + grouped results + "View all results" link
9. **On Claude failure**: Fall back to `fast_search()` silently

### New Service Function

```python
async def ai_search(query: str, db: Session) -> dict:
    """AI-powered intent search using Claude Haiku.

    This function IS async because it awaits claude_structured().
    The sync DB queries within it are fine — FastAPI handles the mix.

    Returns same structure as fast_search() for template compatibility.
    Falls back to fast_search() on Claude failure.
    """
```

---

## Full Search Results Page

### Route

`GET /v2/search/results?q=<query>` → renders `htmx/partials/search/full_results.html`

Also accessible via HTMX: `GET /v2/partials/search/results?q=<query>`

### Layout

- Reuses existing page patterns (table-based lists)
- Tab bar at top: All | Requisitions | Companies | Vendors | Vendor Contacts | Customer Contacts | Parts | Offers
- Each tab shows a table with relevant columns for that entity type
- "All" tab shows grouped sections (similar to dropdown but with more results — 10 per entity)
- Search input at top of page (pre-filled with query) for re-searching
- Empty state: "No results found for '<query>'" with suggestion text "Try a part number, company name, email, or phone number"

---

## JSON Field Searching (Vendors)

VendorCard stores `emails` and `phones` as JSON arrays. To search these:

```python
# Cast JSON array to text for ILIKE matching
from sqlalchemy import cast, String
VendorCard.emails.cast(String).ilike(f"%{safe}%")
```

This handles searching within JSON arrays without needing to unnest. Simple and effective for the ILIKE case.

---

## Database Migration

Single Alembic migration that:

1. Enables `pg_trgm` extension: `CREATE EXTENSION IF NOT EXISTS pg_trgm;`
2. Adds GIN trigram indexes on high-value search columns:
   - `requisitions.name`, `requisitions.customer_name`
   - `companies.name`, `companies.domain`
   - `vendor_cards.display_name`, `vendor_cards.normalized_name`, `vendor_cards.domain`
   - `vendor_contacts.full_name`, `vendor_contacts.email`
   - `site_contacts.full_name`, `site_contacts.email`
   - `requirements.primary_mpn`, `requirements.normalized_mpn`
   - `offers.mpn`, `offers.vendor_name`

Index syntax: `CREATE INDEX ix_<table>_<col>_trgm ON <table> USING gin (<col> gin_trgm_ops);`

Downgrade: Drop all indexes, drop extension.

---

## Rate Limiting

AI search endpoint gets its own rate limit: `10/minute` per user. This is separate from the existing `rate_limit_search` setting.

Add to `config.py`:
```python
rate_limit_ai_search: str = "10/minute"
```

---

## Redis Caching

AI search results are cached in Redis:
- **Key format**: `ai_search:<md5(query.lower().strip())>`
- **TTL**: 5 minutes (300 seconds)
- **Value**: JSON-serialized search results dict
- Uses existing Redis connection from `app/cache/intel_cache.py`

Fast search (Tier 1) is NOT cached — it's already sub-100ms.

---

## Template Changes

### Modified: `app/templates/htmx/base.html`

- Add `@keydown.enter.prevent` to search input to trigger AI search
- Add `hx-post="/v2/partials/search/ai"` on Enter
- Keep existing `hx-get="/v2/partials/search/global"` for type-ahead (unchanged trigger)
- Add a small "AI" indicator/spinner that shows when AI search is in progress
- Add Alpine.js `aiSearching` flag to prevent double-submit on rapid Enter presses
- When AI search is in flight (`aiSearching=true`), force `searchOpen=true` to keep dropdown visible (overrides `@blur` timeout)
- On AI search response, reset `aiSearching=false`

### New: `app/templates/htmx/partials/shared/search_results.html` (replace existing)

- Smart best-match card at top (larger, with entity type badge)
- Grouped sections below (7 entity types)
- "View all results" link at bottom
- Loading state for AI search

### New: `app/templates/htmx/partials/search/full_results.html`

- Full page search results with tabs per entity type
- Table layout per entity type
- Pre-filled search bar at top

---

## File Changes Summary

| Action | File | Purpose |
|--------|------|---------|
| Create | `app/services/global_search_service.py` | Search logic (fast_search + ai_search) |
| Create | `app/templates/htmx/partials/search/full_results.html` | Full search results page |
| Create | `alembic/versions/xxx_add_pg_trgm_search_indexes.py` | pg_trgm extension + GIN indexes |
| Modify | `app/templates/htmx/base.html` | Add Enter-to-AI-search behavior |
| Modify | `app/templates/htmx/partials/shared/search_results.html` | Expanded dropdown with 7 entities + smart answer |
| Modify | `app/routers/htmx_views.py` | Update global_search route, add ai_search + full_results routes |
| Modify | `app/config.py` | Add `rate_limit_ai_search` setting |
| Create | `tests/test_global_search_service.py` | Unit tests for search service |
| Create | `tests/test_ai_search.py` | Tests for AI search endpoint (mocked Claude) |

---

## Testing Strategy

### pg_trgm / SQLite Compatibility

The test suite uses SQLite in-memory (`TESTING=1`). pg_trgm and `similarity()` are PostgreSQL-only. To handle this:

- `fast_search()` detects the DB dialect at call time: `db.bind.dialect.name`
- **PostgreSQL**: Uses `func.similarity()` for ranking + ILIKE for matching
- **SQLite (tests)**: Falls back to plain ILIKE matching, ordered by `id desc`
- This means tests verify the query logic and result structure but NOT fuzzy ranking. Fuzzy ranking is implicitly tested via the deployed app on PostgreSQL.

### Test Files

- **`tests/test_global_search_service.py`**: Tests `fast_search()` with SQLite (ILIKE path). Seeds test DB with requisitions, companies, vendors, contacts, parts, offers. Verifies correct entities returned, result structure, limit enforcement, empty query handling.
- **`tests/test_ai_search.py`**: Tests `ai_search()` with mocked `claude_structured()`. Verifies intent parsing → query execution flow, fallback on Claude failure, Redis caching behavior (mock Redis), rate limiting.

### What to Mock

- `claude_structured()` — mock at `app.utils.claude_client.claude_structured`
- Redis — mock `get_cached` / `set_cached` from `app.cache.intel_cache`
- Do NOT mock the database — use real SQLite test DB with seeded data

---

## What's NOT in Scope

- Aggregation queries ("how many vendors responded this week?")
- Conversational follow-ups
- Vector/embedding search
- ParadeDB (future upgrade path if pg_trgm ranking insufficient)
- Typesense (future upgrade if search becomes core product differentiator)
