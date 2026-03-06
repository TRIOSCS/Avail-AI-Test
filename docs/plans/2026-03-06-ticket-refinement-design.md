# Trouble Ticket System Refinements — Design Document

Date: 2026-03-06
Branch: deep-cleaning
Status: Approved

## Overview

Six refinements to the existing trouble ticket system. Goal: make the ticket module exhaustive enough that a clean sweep means the site works perfectly.

## 1. Vinod Admin User

Idempotent seed in `startup.py`: create user `vinod@trioscs.com`, role `admin`, skip if exists. No schema changes, no new role.

## 2. AI Thread Consolidation

Group duplicate bug reports using the existing `parent_ticket_id` field.

**On submission:** After ticket creation, query all open tickets (excluding new one). Send titles + descriptions to Claude Haiku with the new ticket's info. Prompt: "Is this the same underlying issue as any of these? Return the ticket ID or 'new'. Confidence 0.0-1.0." If confidence >0.9, set `parent_ticket_id` to the matched ticket.

**Daily batch job:** Scheduler scans unlinked open tickets, runs pairwise similarity via Haiku in batches, links any >0.9 matches.

**Dashboard:** Show child count badge on parent tickets. Expand to see linked tickets.

**New column:** `similarity_score` (Float, nullable) on `trouble_tickets` — records AI confidence when linking.

**No notifications** for linking — silent, dashboard shows counts.

## 3. Report Issue Templates

Add "Common Issues" quick-select to the submit modal in `tickets.js`:

| Template | Pre-filled title | Description hint |
|----------|-----------------|-----------------|
| Search not working | "Search returns no/wrong results for [part]" | "What part number? What did you expect?" |
| Page won't load | "Page fails to load: [which page]" | "Which page? Any error message?" |
| Data looks wrong | "Incorrect data on [what]" | "What's wrong? What should it be?" |
| Slow performance | "Slow response on [where]" | "Which page? How long does it take?" |
| Email/RFQ issue | "Email or RFQ problem: [describe]" | "Which RFQ? What happened?" |
| Other | "" | "Describe what happened" |

Frontend-only change. No backend changes.

## 4. Disable Auto-Close

Remove the auto-close job from `selfheal_jobs.py`. Keep the weekly report job.

## 5. Proactive Prompt Quality

Update file mappings in `ai_trouble_prompt.py` and `diagnosis_service.py`:

- Expand view-to-file mappings (add prospecting, tagging, apollo, notifications, tickets)
- Include CLAUDE.md rules in generated prompts
- Structure prompts as: Context -> Diagnosis -> Files to Read -> Fix Instructions -> Test Instructions
- Use actual file paths from current codebase

## 6. "Find Trouble" — Exhaustive Automated Site Testing

### Button
"Find Trouble" button in ticket dashboard header (admin only). Always runs full audit.

### Phase A — Playwright Mechanical Sweep (server-side subprocess)

Headless Chromium navigates every page and clicks every interactive element.

Checks for:
- JS console errors
- 500/4xx HTTP responses
- Broken links / dead routes
- Slow responses (>3s)
- Unhandled exceptions
- Empty data states that shouldn't be empty
- Form validation failures
- Modal/drawer open/close
- Pagination
- All buttons clickable without crash

Authenticates using admin session. Crawl strategy: main nav -> every sidebar link -> every drawer/modal -> every button -> form submissions with test data.

Each failure auto-creates a ticket with `source: 'playwright'`, full context (URL, element selector, error, screenshot, network log, timing).

Progress streamed to UI via polling: "Testing Search... Testing CRM... 3 issues found..."

Runs as background subprocess. ~4-7 minutes for full sweep.

### Phase B — Claude Agent Intelligent Testing

After Playwright finishes, generates prompt cards for every app area. Auto-opens browser tabs via `window.open()` to each area. Prompts displayed in a floating panel for copy-paste into Claude in Chrome.

Each prompt includes:
- What to test in this area
- What "correct" looks like
- Known data to verify against
- Key workflows to exercise
- How to submit findings via the ticket API

Agents coordinate via:
- `GET /api/trouble-tickets/active-areas` — areas currently being tested (avoid overlap)
- `GET /api/trouble-tickets/similar?title=...&description=...` — check before submitting duplicates

### Test Coverage Map (exhaustive)

| Area | Playwright | Claude Agent |
|------|-----------|-------------|
| Search | All connectors return data, results render, no errors | Results correct for known parts, scoring right, filters logical |
| Requisitions | CRUD, clone, add parts, status changes, pagination | Data integrity, counts match, workflow sensible |
| RFQ | Create, send, view responses, status transitions | Email content correct, parsing logical, pricing reasonable |
| CRM - Companies | List loads, pagination, drawer tabs, filters | Data accuracy, counts match, owner filter works |
| CRM - Contacts | Bulk load, create, edit, enrichment buttons | Contact data correct, enrichment returns real data |
| CRM - Quotes | Create, line items, status changes | Pricing calculations, workflow logical |
| Prospecting | Discovery pool, filters, cards render | Suggestions relevant, enrichment quality, scoring sensible |
| Vendors | Vendor cards, offers list, intelligence tab | Offer data fresh, vendor scores reasonable |
| Email Mining | Inbox results, attachment parsing | Parsed data accurate, confidence scores sensible |
| Tagging | Tag list, material card tags, admin ops | Tags accurate for known parts, confidence reasonable |
| Admin - API Health | Dashboard loads, status indicators | Statuses reflect reality, alerts fire correctly |
| Admin - Tickets | List, detail, diagnose, execute, verify | AI diagnosis quality, prompt usefulness |
| Admin - Settings | Config panels load, save works | Settings actually take effect |
| Auth | Login, logout, session refresh, role enforcement | Can't access admin as buyer, tokens refresh |
| Notifications | Bell icon, list, mark read | Notifications fire for right events |
| Upload | File upload (BOM, stock lists) | Parsed data correct, error handling for bad files |
| Apollo | Discover, enrich, sync contacts | Data quality, credit usage shown |

### New Files
- `app/services/site_tester.py` — Playwright sweep orchestrator
- `app/services/test_prompts.py` — generates Claude agent prompts per area
- `tests/test_site_tester.py` — unit tests

### New Endpoints
- `POST /api/trouble-tickets/find-trouble` — kicks off full audit, returns job ID
- `GET /api/trouble-tickets/find-trouble/{job_id}` — progress polling
- `GET /api/trouble-tickets/active-areas` — areas currently under test
- `GET /api/trouble-tickets/similar` — duplicate check (title + description similarity)

### New Migration
Add to `trouble_tickets` table:
- `similarity_score` Float, nullable (for consolidation)
- `tested_area` String(50), nullable
- `dom_snapshot` Text, nullable
- `network_errors` JSON, nullable
- `performance_timings` JSON, nullable
- `reproduction_steps` JSON, nullable

### Agent Coordination
- Source types: `'ticket_form'`, `'report_button'`, `'playwright'`, `'agent'`
- Agents call `/active-areas` before starting to claim an area
- Agents call `/similar` before submitting to avoid duplicates
- Thread consolidation catches any remaining duplicates at >0.9 confidence
