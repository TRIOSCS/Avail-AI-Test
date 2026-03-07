# AI Sprinkles Across All Views — Phase 3 Design

**Date**: 2026-03-07
**Status**: Approved
**Parent**: `docs/plans/2026-03-07-ai-intelligence-layer-design.md`

## Goal

Add four pre-computed AI insight widgets across Material Card, Vendor Card, Dashboard, and Company drawer using existing `knowledge_entries` infrastructure.

## Decisions

| Decision | Choice |
|----------|--------|
| Scope | 4 sprinkles: sourcing history, vendor inline, pipeline health, activity summary |
| Skipped | Offer outliers, contact priority (deferred) |
| Compute model | Pre-computed only (background job), no on-demand AI calls |
| Storage | Existing `knowledge_entries` table with `ai_insight` type, linked via FKs |
| UI pattern | Collapsible card (reuse Phase 1 pattern) + inline badges for MPNs |
| New tables | None — no migration needed |

## Sprinkle 1: Sourcing History

**Placement:** Material Card popup + inline badge next to part rows in req drill-down

**Shows:** "Quoted 3x before, avg $2.40, last quoted 2026-02-15 at $2.55"

**Data source:** Knowledge facts from Phase 1 auto-capture + historical offers/quotes

**Storage:** `ai_insight` entries with `mpn` set, no `requisition_id` (cross-req)

**Generator:** `generate_mpn_insights(db, mpn)` — gathers all knowledge for this MPN, summarizes pricing/availability history

**Job scope:** Top 50 most-quoted MPNs per refresh run

## Sprinkle 2: Vendor Inline

**Placement:** Collapsible insights card at top of Vendor Card popup

**Shows:** 3-5 insights — response patterns, pricing trends, reliability, part specialization

**Data source:** Knowledge entries linked to vendor + offer/quote history + RFQ response rate

**Storage:** `ai_insight` entries with `vendor_card_id` set

**Generator:** `generate_vendor_insights(db, vendor_card_id)` — vendor-specific context engine

**Job scope:** Top 20 most active vendors per refresh run

## Sprinkle 3: Pipeline Health

**Placement:** New "Pipeline Health" card on Dashboard view

**Shows:** AI summary — stalling deals, coverage gaps, win/loss trends, deals needing attention

**Data source:** Active requisitions, recent quotes, offer coverage, deal ages

**Storage:** `ai_insight` entries with `mpn = '__pipeline__'` (special marker, no FK)

**Generator:** `generate_pipeline_insights(db)` — pipeline-wide analysis

**Job scope:** 1 per refresh run (pipeline-wide)

## Sprinkle 4: Activity Summary

**Placement:** Collapsible insights card on Company drawer Overview tab

**Shows:** "Last contacted 12 days ago, 3 open deals worth $45K, response time trending slower"

**Data source:** Company activity log, open requisitions, quote history, contact engagement

**Storage:** `ai_insight` entries with `company_id` set

**Generator:** `generate_company_insights(db, company_id)` — company engagement analysis

**Job scope:** Top 20 most active companies per refresh run

## Backend

### New generator functions in `knowledge_service.py`

- `generate_mpn_insights(db, mpn)` — sourcing history for a part number
- `generate_vendor_insights(db, vendor_card_id)` — vendor intelligence
- `generate_pipeline_insights(db)` — pipeline-wide health summary
- `generate_company_insights(db, company_id)` — company activity summary

All follow the same pattern: build context string -> Claude Sonnet with thinking budget -> store as `ai_insight` entries with appropriate FKs.

### Context builders (new functions in `knowledge_service.py`)

- `build_mpn_context(db, mpn)` — all knowledge + offers + quotes for this MPN
- `build_vendor_context(db, vendor_card_id)` — vendor knowledge + offer history + response rate
- `build_pipeline_context(db)` — active reqs, quote coverage, deal ages, win/loss stats
- `build_company_context(db, company_id)` — company knowledge + activity + open reqs

### Expanded background job

The existing 6h `_job_refresh_insights` expands to include:
- Pipeline insights: 1 per run
- Company insights: top 20 most active companies
- Vendor insights: top 20 most active vendors
- MPN insights: top 50 most-quoted MPNs
- Existing req insights: top 50 recently active reqs (unchanged)

### Cost control

- Cap entity counts per run (20 companies, 20 vendors, 50 MPNs)
- Use Haiku for context extraction, Sonnet only for insight synthesis
- Skip entities unchanged since last insight generation
- Each insight set expires in 30 days (existing `EXPIRY_AI_INSIGHT`)

## API Endpoints

### New convenience endpoints (read-only + refresh)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/vendors/{id}/insights` | Cached vendor insights |
| POST | `/api/vendors/{id}/insights/refresh` | Re-generate vendor insights |
| GET | `/api/companies/{id}/insights` | Cached company insights |
| POST | `/api/companies/{id}/insights/refresh` | Re-generate company insights |
| GET | `/api/dashboard/pipeline-insights` | Cached pipeline health |
| POST | `/api/dashboard/pipeline-insights/refresh` | Re-generate pipeline health |
| GET | `/api/materials/insights` | Cached MPN insights (`?mpn=X`) |
| POST | `/api/materials/insights/refresh` | Re-generate MPN insights (`?mpn=X`) |

All follow the same pattern as `GET /api/requisitions/{id}/insights`.

## Frontend

### Material Card popup
- Add sourcing history badge below MPN heading
- Fetch from `/api/materials/insights?mpn=X`
- Show inline: "Quoted 3x, avg $2.40" or "No history"

### Req drill-down parts tab
- Add inline sourcing badge next to each part row
- Batch fetch: `/api/materials/insights?mpn=X` for each MPN in the req
- Subtle inline text, not a card

### Vendor Card popup
- Add collapsible insights card at top (same `_renderInsightsCard` pattern)
- Fetch from `/api/vendors/{id}/insights`
- Refresh button

### Dashboard
- Add "Pipeline Health" card in the overview area
- Fetch from `/api/dashboard/pipeline-insights`
- Refresh button
- Show 3-5 bullet insights

### Company drawer Overview tab
- Add collapsible insights card (same pattern)
- Fetch from `/api/companies/{id}/insights`
- Refresh button

## Files to Create/Modify

### Modified files
- `app/services/knowledge_service.py` — 4 new context builders + 4 new generators
- `app/routers/knowledge.py` — 8 new convenience endpoints
- `app/jobs/knowledge_jobs.py` — expand refresh job scope
- `app/static/app.js` — Material Card + Vendor Card + Dashboard + req parts badges
- `app/static/crm.js` — Company drawer insights card

## Future Phases (not in scope)

- Offer outlier detection (flag unusual pricing)
- Contact priority scoring (who to engage next)
- Real-time insight generation (on-demand AI calls)
