# Apollo.io Phase 2 — Full Integration Design

## Date: 2026-03-01

## Context

Apollo.io account (mkhoury@trioscs.com) is fully provisioned but empty:
- 0 contacts, 0 sequences, 0 email accounts linked
- 95 lead credits, 160 direct dial credits, 5,000 AI credits, 0 export credits
- Team ID: `699ab10cc1b756001d4172c3`, User ID: `699ab10dc1b756001d417435`

Existing AvailAI code already has:
- `app/connectors/apollo_client.py` — search_contacts(), enrich_person(), enrich_company()
- `app/services/prospect_discovery_apollo.py` — check_people_signals(), run_people_check_batch()
- `app/services/prospect_contacts.py` — full contact enrichment pipeline (Apollo search → Hunter verify → classify → import)
- `app/services/credit_manager.py` — credit tracking service
- Apollo in enrichment waterfall: `enrichment_service.py` (company enrichment Phase 1)

## Design Decision: Hybrid Integration

**Cold prospecting** (new companies) → Apollo sequences (deliverability, tracking, analytics)
**Warm vendor RFQs** (known vendors) → AvailAI M365 email (existing workflow)
**Contact enrichment** → Apollo API via code (deepens existing pipeline)
**Company enrichment** → Apollo API via code (already in waterfall)

## Part 1: Apollo Account Bootstrap

### Manual Steps (Apollo UI — cannot be automated)
1. Update profile: First name "M", Last name "Khoury", Title "Owner"
2. Link email account (mkhoury@trioscs.com or dedicated outreach address)
3. Create 3 sequence templates:
   - **Intro Sequence**: Initial outreach to procurement contacts (3-step: intro → value prop → soft close)
   - **Follow-up Sequence**: Re-engage after no response (2-step: check-in → final)
   - **Re-engage Sequence**: Revive old contacts (1-step: "still looking for components?")

### Automated via MCP/API
- Create labels: `electronics-oem`, `distributor`, `broker`, `procurement-contact`
- Create accounts for top AvailAI companies (via apollo_accounts_create)

## Part 2: Bidirectional Contact Sync (Code)

### New Router: `app/routers/apollo_sync.py`

**Endpoints:**

#### `POST /api/apollo/sync-contacts`
Push AvailAI vendor contacts into Apollo as contacts.
- Reads from `vendor_contacts` table (existing contacts with emails)
- Creates Apollo contacts with `run_dedupe=true`
- Tags with appropriate labels based on company type
- Returns: `{synced: int, skipped: int, errors: int}`
- Rate limited: 5 req/min (Apollo free tier)

#### `GET /api/apollo/discover/{domain}`
Search Apollo for procurement contacts at a company domain.
- Calls `mixed_people_api_search` with procurement title filters
- Returns preview list (masked emails) for user review
- Does NOT auto-import — user selects which contacts to enrich

#### `POST /api/apollo/enrich-selected`
Enrich user-selected contacts from discover results.
- Calls `people/match` for each selected person (costs 1 lead credit each)
- Stores enriched data in `vendor_contacts` table
- Hunter verification on returned emails
- Returns: `{enriched: int, verified: int, credits_used: int, credits_remaining: int}`

#### `POST /api/apollo/enroll-sequence`
Add contacts to an Apollo sequence for outreach.
- Requires: sequence_id, contact_ids (Apollo IDs), email_account_id
- Validates contacts have verified emails before enrolling
- Returns: `{enrolled: int, skipped_no_email: int}`

#### `GET /api/apollo/credits`
Show current credit usage and remaining.
- Calls Apollo profile endpoint with `include_credit_usage=true`
- Returns formatted credit summary

### New Service: `app/services/apollo_sync_service.py`

Core logic separated from routes:
- `sync_contacts_to_apollo(db, label)` — bulk push
- `discover_contacts(domain, title_keywords)` — search + preview
- `enrich_contacts(person_ids)` — batch enrich with credit tracking
- `enroll_in_sequence(sequence_id, contact_ids)` — sequence enrollment

## Part 3: Frontend Integration

### Companies Drawer — New "Apollo" Tab
- Show Apollo enrichment data for company
- "Find Contacts" button → calls discover endpoint → shows review table
- Checkbox select contacts → "Enrich Selected" → shows results with email/phone
- "Add to Sequence" dropdown → enroll in Apollo sequence

### Credit Usage Widget
- Small badge in header or settings page
- Shows: lead credits remaining / direct dial remaining
- Warning when < 10% remaining

## Credit Budget

With 95 lead credits and 160 direct dial credits:
- Each people/match enrichment = 1 lead credit → ~95 contact enrichments
- Each phone reveal = 1 direct dial credit → ~160 phone lookups
- Prioritize: enrich decision makers first, then influencers

## What We're NOT Building
- No duplicate email system (Apollo for cold, M365 for warm)
- No real-time Apollo webhook listener (overkill at current scale)
- No Apollo analytics dashboard in AvailAI (use Apollo's built-in)
- No automatic sequence enrollment (always human-in-the-loop)

## Files to Create/Modify

### New Files
- `app/routers/apollo_sync.py` — 4 endpoints
- `app/services/apollo_sync_service.py` — sync/discover/enrich/enroll logic
- `app/schemas/apollo.py` — request/response models
- `tests/test_apollo_sync.py` — full test coverage

### Modified Files
- `app/main.py` — register apollo_sync router
- `app/static/crm.js` — Apollo tab in company drawer
- `app/templates/index.html` — Apollo tab markup
- `app/config.py` — add any new Apollo config vars if needed

### No Migration Needed
- Enrichment data stored in existing `enrichment_data` JSON columns
- Contact data stored in existing `vendor_contacts` table
- Credit tracking via in-memory + Apollo API (no new DB tables)
