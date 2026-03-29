# CRM Phase 2b: AI Interaction Quality Scoring + Performance Dashboard

**Created:** 2026-03-29
**Status:** Approved
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)

## Goal

Classify every non-email ActivityLog entry with AI for quality scoring, extend the existing Avail Score framework with an Interaction Quality metric, add a Performance tab to the CRM shell, and enrich activity timeline views with quality badges and clean summaries.

## Design Decisions

- **Don't re-classify emails** — `email_intelligence_service.py` already classifies them. Only score calls, notes, meetings, Teams messages.
- **Add columns to ActivityLog, not JSON** — need indexed queries for batch job and template filtering.
- **15-minute batch job, not per-commit hooks** — simpler, same effective speed at 50-150 activities/day. Avoids wiring hooks into every commit site.
- **Extend avail_score_service.py** — add Interaction Quality as a new sub-metric, don't create a parallel scoring system.
- **Performance tab in CRM shell** — third tab alongside Customers | Vendors, shows team scores from existing avail_score snapshots.

## Part 1: ActivityLog Quality Columns

### New Columns (Migration)

Add to `activity_log` table:

| Column | Type | Notes |
|--------|------|-------|
| `quality_score` | Float, nullable | 0-100 interaction quality |
| `quality_classification` | String(30), nullable | conversation/voicemail/auto_reply/ooo/bounce/follow_up/quote/negotiation |
| `quality_assessed_at` | DateTime, nullable | When AI scored this entry |
| `is_meaningful` | Boolean, nullable | True = real interaction, False = noise (voicemail, auto-reply, OOO) |

Add partial index: `ix_activity_unscored` on `(quality_assessed_at)` WHERE `quality_assessed_at IS NULL` — enables efficient batch job queries.

Update `ActivityLog` model in `app/models/intelligence.py` with the 4 new columns.

### AI Summary

Store AI-generated clean summary in existing `ActivityLog.summary` column (String 500). The current auto-generated summaries ("Email to John Smith") are low-value placeholders that the AI summary improves upon. Overwriting is intentional.

## Part 2: Batch AI Quality Scorer

### New APScheduler Job

**File:** `app/jobs/quality_jobs.py`

**Schedule:** Every 15 minutes.

**Logic:**
1. Query `ActivityLog WHERE quality_assessed_at IS NULL AND event_type NOT IN ('email') AND created_at > now() - interval '7 days'`
2. For each unscored entry, call `claude_structured()` with `model_tier="fast"` (Haiku)
3. Write results back: `quality_score`, `quality_classification`, `is_meaningful`, `summary` (clean summary), `quality_assessed_at`
4. Process in batches of 50 to avoid long-running transactions

**AI Classification Schema:**

```python
QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_meaningful": {"type": "boolean", "description": "True if this is a real interaction (not voicemail, auto-reply, OOO, bounce)"},
        "quality_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Interaction quality: 0=noise, 50=routine, 100=high-value negotiation or deal-closing conversation"},
        "classification": {
            "type": "string",
            "enum": ["conversation", "voicemail", "auto_reply", "ooo", "bounce", "follow_up", "quote", "negotiation"]
        },
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "clean_summary": {"type": "string", "description": "1-2 sentence summary of what happened, stripped of noise. Max 100 words."}
    },
    "required": ["is_meaningful", "quality_score", "classification", "sentiment", "clean_summary"]
}
```

**System prompt:** Provide the activity's subject, notes, duration, channel, event_type, and direction. Ask the AI to classify whether this was a real meaningful business interaction and summarize it.

**Cost estimate:** ~50-150 non-email activities/day at Haiku pricing (~$0.01/call) = $0.50-1.50/day = $15-45/month.

## Part 3: Extend Avail Score Framework

### New Sub-Metric: Interaction Quality

Add to `app/services/avail_score_service.py`:

**For sales roles:** Add metric S6 "Interaction Quality"
- Score 0-10 derived from average `quality_score` of meaningful ActivityLog entries for this user in the scoring window
- Mapping: avg quality >= 80 → 10, >= 60 → 8, >= 40 → 6, >= 20 → 4, < 20 → 2

**For buyer roles:** Add metric B6 "Interaction Quality" (same formula)

This integrates naturally with the existing `compute_buyer_avail_score()` and `compute_sales_avail_score()` functions, adding one more sub-score to the total.

## Part 4: Performance Tab in CRM Shell

### Route

**File:** `app/routers/crm/views.py`

New endpoint: `GET /v2/partials/crm/performance`

Queries the most recent `AvailScoreSnapshot` (or `UnifiedScoreSnapshot`) per active user, returns a table partial.

### Template

**File:** `app/templates/htmx/partials/crm/performance_tab.html`

Table with one row per salesperson:

| Column | Data |
|--------|------|
| Name | User display name |
| Coverage | Score 0-100 with emerald/amber/rose pill |
| Quality | Score 0-100 with pill |
| Responsiveness | Score 0-100 with pill |
| Outcome | Score 0-100 with pill |
| Overall | Total avail score |

Color coding follows existing staleness pattern:
- >= 70: `bg-emerald-50 text-emerald-700`
- >= 40: `bg-amber-50 text-amber-700`
- < 40: `bg-rose-50 text-rose-700`

### CRM Shell Update

Add third tab button to `app/templates/htmx/partials/crm/shell.html`:
- Label: "Performance"
- `hx-get="/v2/partials/crm/performance"`
- `hx-target="#crm-tab-content"`
- `hx-trigger="click"`
- Pattern C style (same as Customers/Vendors)

## Part 5: Activity Timeline Enrichment

### Customer Activity Tab

**File:** `app/templates/htmx/partials/customers/tabs/activity_tab.html`

Changes to the Activity Log section (lines 107-143):
- Add quality badge pill after the activity type icon: `bg-emerald-50 text-emerald-700` for meaningful, `bg-gray-100 text-gray-500` for noise
- Display `a.summary` (AI clean summary) below the subject line in `text-xs text-gray-500`
- Add "Hide noise" toggle at the top of the activity section: Alpine.js `x-data="{ showNoise: false }"`, each row uses `x-show="showNoise || {{ 'true' if a.is_meaningful != false else 'false' }}"`

### Vendor Contact Timeline

**File:** `app/templates/htmx/partials/vendors/contact_timeline.html`

Same pattern — add quality badge dot and summary. This template is simpler (colored dots + text), so the enrichment is lighter.

### Hide Noise Toggle

Server-side implementation via query parameter `?hide_noise=1`:
- Route handler adds `WHERE is_meaningful != False` filter when `hide_noise=1`
- Toggle is an Alpine.js boolean that re-triggers the HTMX `hx-get` with the parameter
- Reduces result count, improves performance

## Technical Architecture

### New Files

| File | Responsibility |
|------|---------------|
| `app/services/activity_quality_service.py` | AI quality scoring logic — `score_activity(activity_id, db)` |
| `app/jobs/quality_jobs.py` | APScheduler job — batch score unscored entries every 15 min |
| `app/templates/htmx/partials/crm/performance_tab.html` | Performance dashboard table |
| `alembic/versions/XXX_activity_quality_scoring.py` | Add 4 columns + partial index to activity_log |
| `tests/test_activity_quality.py` | Quality scoring + performance tab tests |

### Modified Files

| File | Change |
|------|--------|
| `app/models/intelligence.py` | Add 4 columns to ActivityLog model |
| `app/services/avail_score_service.py` | Add Interaction Quality sub-metric (S6/B6) |
| `app/routers/crm/views.py` | Add `/v2/partials/crm/performance` route |
| `app/templates/htmx/partials/crm/shell.html` | Add Performance tab button |
| `app/templates/htmx/partials/customers/tabs/activity_tab.html` | Quality badges + summaries + hide noise |
| `app/templates/htmx/partials/vendors/contact_timeline.html` | Quality badges + summaries |
| `app/jobs/__init__.py` | Register quality jobs |
| `app/scheduler.py` | Register quality job module |

## What This Does NOT Include

- Email re-classification (already handled by email_intelligence_service)
- Per-commit real-time scoring hooks (batch job handles it)
- New aggregate scoring tables (extends existing avail_score snapshots)
- Vendor activity tab creation (Phase 3 scope)
- Responsiveness window function queries (deferred — coverage + quality + outcome first)
