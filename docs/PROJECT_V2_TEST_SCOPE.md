# AvailAI v2 — Complete Test Scope

> Full end-to-end testing guide for AvailAI v2. Every page, feature, flow, edge case, background job, and integration that must be verified before shipping.

---

## 0. How to Use This Document

- Work through each section top-to-bottom. Each section lists: **what to test**, **how to test it**, **expected result**, and **edge cases**.
- Every page route is prefixed `/v2/...` (the HTMX app). Partials are under `/v2/partials/...`.
- "Pass" means the expected result matched AND no errors appear in `docker compose logs -f app`.
- Track failures by feature area (Section 2–15) and report with a screenshot + request_id from logs.

### Environments
| Env | URL | Purpose |
|-----|-----|---------|
| Local | `http://localhost:8000` | Dev smoke |
| Staging | (set via env) | Pre-prod QA |
| Production | `https://app.yourdomain.com` | Live data, read-only testing unless explicit |

### Pre-Flight
1. `docker compose ps` → all 6 containers (`app`, `db`, `redis`, `caddy`, `db-backup`, `enrichment-worker`) healthy.
2. `docker compose exec app alembic current` → matches latest in `alembic/versions/`.
3. `.env` has: `ANTHROPIC_API_KEY`, `AZURE_CLIENT_ID/SECRET/TENANT`, at least 3 supplier keys, `DATABASE_URL`, `REDIS_URL`.
4. `curl -I https://app.yourdomain.com/healthz` → `200 OK`.
5. `npm run build` → no errors; `app/static/dist/` refreshed.
6. Browser: Chrome latest + Safari mobile viewport (iPhone 14).

### Test Accounts
- **Admin role:** full access (settings, users, diagnostics).
- **Buyer role:** search, send RFQ, build quotes.
- **User role:** read-only on CRM, no send.

---

## 1. Authentication & Session

### 1.1 OAuth Login (Azure AD)
- **URL:** `/auth/login` → redirects to Microsoft → callback `/auth/callback`.
- **Verify:** session cookie set (HTTP-only, 15-min expiry), landing page loads at `/v2/requisitions`.
- **Edge:** deny consent → error page with "try again" CTA; expired token → auto-refresh via `require_fresh_token`; tampered cookie → kicked to `/auth/login`.
- **Logout:** `/auth/logout` clears session, redirects to login.

### 1.2 Permission Gates
- Hit `/v2/settings` as non-admin → 403 or redirect.
- Hit `/v2/partials/requisitions/{id}/rfq-send` as `user` role → blocked.
- Fresh-token gate: wait >15 min idle → next mutation prompts re-auth.

### 1.3 CSRF
- POST without CSRF cookie/header → 403.
- Double-submit token present on all forms (inspect DOM).

---

## 2. Navigation & App Shell

### 2.1 Topbar
- Logo → `/v2/requisitions`.
- Global search input (if enabled) → typeahead via `/v2/partials/search/global`.
- User menu: profile, settings (admin), logout.

### 2.2 Sidebar / Bottom Nav (mobile)
Tabs must route correctly:
| Label | Route | Partial |
|-------|-------|---------|
| Reqs | `/v2/requisitions` | partials/requisitions/list.html |
| Sightings | `/v2/sightings` | partials/sightings/list.html |
| Materials | `/v2/materials` | partials/materials/workspace.html |
| Search | `/v2/search` | partials/search/page.html |
| Vendors | `/v2/vendors` | partials/vendors/list.html |
| Customers | `/v2/customers` | partials/customers/list.html |
| Quotes | `/v2/quotes` | partials/quotes/list.html |
| Buy Plans | `/v2/buy-plans` | partials/buy_plans/list.html |
| Excess | `/v2/excess` | partials/excess/list.html |
| Prospecting | `/v2/prospecting` | partials/prospecting/list.html |
| Proactive | `/v2/proactive` | partials/proactive/list.html |
| Follow-ups | `/v2/follow-ups` | partials/follow_ups/list.html |
| Tickets | `/v2/trouble-tickets` | partials/tickets/list.html |
| CRM | `/v2/crm` | partials/crm/... |
| Settings | `/v2/settings` | partials/settings/... |

- Sidebar persists open/collapsed state across reloads (`$store.sidebar` with `@persist`).
- Active tab is visually highlighted.
- Mobile: bottom nav visible <768px; topbar collapses to hamburger.

### 2.3 Base Page Load
- Every top-level route returns the app shell + spinner, then lazy-loads partial via `hx-get`.
- No double renders; no flash of unstyled content.

### 2.4 Toast Store
- After a successful mutation, HX-Trigger `{"showToast": "..."}` → `$store.toast.show = true` with message + type (success/error/info).
- Toast auto-dismisses after ~4s.

### 2.5 SSE Stream
- DevTools → Network → `/api/events/stream` open, text/event-stream.
- Trigger: complete a search → browser receives `search_complete`. Parse an offer → `offer_parsed`.

---

## 3. Requisitions (core workflow)

### 3.1 List `/v2/requisitions`
- Columns: ID, Customer, Status, Parts count, Owner, Created, Updated.
- Filters: status (open, in-progress, quoted, won, archived, cancelled), owner, customer, date range.
- Sort: each column.
- Pagination: 25/50/100 per page; correct total count.
- Empty state: CTA "Create your first requisition."

### 3.2 Create `/v2/partials/requisitions/create-form`
- Manual: customer picker (typeahead + "quick create"), reference number, due date, notes.
- AI intake: paste freeform text → `ai_intake_parser.py` → structured parts list shown for confirmation.
- CSV/Excel import: `/v2/partials/requisitions/import-form` → preview (`import-parse`) → save (`import-save`).
- **Edge:** malformed CSV → field-level errors; duplicate MPNs merged; missing quantity defaults to 1 with warning.

### 3.3 Detail `/v2/requisitions/{id}`
Tabs (each loads via `/v2/partials/requisitions/{id}/tab/{tab}`):
- **Overview** — summary, customer, owner.
- **Parts** — requirement rows with MPN chips (primary + substitutes via `_mpn_chips.html`).
- **Offers** — all sightings/offers, sortable by price/qty/vendor/T-tier.
- **Leads** — AI-ranked sourcing leads.
- **Responses** — parsed vendor email replies.
- **Activity** — timeline of events.
- **Quote** — draft quote if any.
- **Attachments** — uploaded docs.

### 3.4 Inline Editing
- Click header field (customer, reference, notes, due date) → edit form swaps in → save via `hx-patch` → display swaps back.
- Description field on Part header is AI-generated and inline-editable.
- **Edge:** empty value validation; optimistic lock; concurrent edit shows toast warning.

### 3.5 Parts Management
- Add part: MPN, qty, target price, notes; `@validates` uppercases MPN.
- Delete part: confirm modal → `hx-delete`.
- Substitute MPNs: add/remove; format `[{mpn, manufacturer}]`; legacy string format still rendered.
- **Edge:** MPN <3 chars rejected; pasted multi-line list splits rows.

### 3.6 Search-All (the big one)
- Click "Search All" → POST `/v2/partials/requisitions/{id}/search-all` → SSE progress bar → results populate.
- All 10 connectors fire in parallel (`nexar`, `brokerbin`, `digikey`, `mouser`, `element14`, `ebay`, `oemsecrets`, `sourcengine`, `email_mining`, `ai_live_web`, plus local `source_stocks`).
- Sightings dedupe by requirement + vendor + MPN.
- Each sighting scored T1–T7.
- Vendor cards auto-upserted; material cards auto-upserted and linked to requirement.
- **Edge:** one connector fails → others still return; 0 results → empty state with retry CTA; stale data (>24h) triggers refresh.
- After search: `db.expire(requirement)` before re-render — verify stale data not shown.

### 3.7 Bulk Actions
`/v2/partials/requisitions/bulk/{action}` — archive, cancel, assign owner, change status.
- Select multiple rows → action toolbar appears → execute → list refreshes.

### 3.8 Paste Offer / Parse Email
- `/v2/partials/requisitions/{id}/paste-offer-form` → paste raw email → `parse-offer` → Claude extracts → preview → save via `save-parsed-offers`.
- Confidence ≥0.8 auto-saves; 0.5–0.8 flags for review; <0.5 blocked.

### 3.9 RFQ Compose & Send
- `/v2/partials/requisitions/{id}/rfq-compose` → select vendors, edit subject/body.
- "AI cleanup" (`ai-cleanup-email`) → rewrites for tone.
- Send → `rfq-send` → Graph API sends emails; each gets `[AVAIL-{req_id}]` subject tag.
- **Verify:** `contacts` row per vendor with `graph_message_id`; activity log entry; vendor card `total_outreach++`.
- **Edge:** missing email on vendor → warning; rate limit hit → retry with backoff; Graph API 401 → re-auth prompt.

### 3.10 Poll Inbox (manual)
- `/v2/partials/requisitions/{id}/poll-inbox` → immediate pull of replies → new responses appear in Responses tab.

### 3.11 Create Quote from Requisition
- `/v2/partials/requisitions/{id}/create-quote` → navigates to quote builder with selected offers.

### 3.12 Offers within Req
- Review: `/offers/{offer_id}/review` — accept/reject.
- Edit: `edit-form` → `edit`.
- Delete: `DELETE /offers/{offer_id}`.
- Mark sold: `/offers/{offer_id}/mark-sold`.
- Reconfirm: `/offers/{offer_id}/reconfirm` — refreshes from source.
- Changelog: `/offers/{offer_id}/changelog` — full audit trail.

---

## 4. Search (global workspace)

### 4.1 `/v2/search`
- Single MPN, batch MPN paste, natural-language AI search (`/v2/partials/search/ai`).
- Filters (`/v2/partials/search/filter`): source, stock, price, lead time, authorized only, date range.
- Results stream via `/v2/partials/search/stream` (SSE) — partial results render as each connector returns.
- Lead detail: `/v2/partials/search/lead-detail` → drill-in.
- "Add to requisition": pick via `requisition-picker` → `add-to-requisition`.
- **Edge:** >50 MPNs warning; connector timeout (60s) → skipped with banner.

### 4.2 Global Search Typeahead
- `/v2/partials/search/global` — 500ms debounce, matches across reqs, parts, vendors, companies, material cards.
- Keyboard: arrow keys navigate, Enter selects.

---

## 5. Sightings `/v2/sightings`

- List of all vendor quotes (non-archived requisitions only).
- Columns: MPN chips, vendor, qty, price, lead time, T-tier badge, source, age.
- Filters: T-tier, vendor, source, requisition.
- MPN chip click → opens material card modal (via `link_map`).
- Batch RFQ from sightings: select rows → send.
- **Edge:** archived/cancelled reqs excluded; empty state renders correctly.

---

## 6. Materials `/v2/materials`

### 6.1 Workspace
- Full-text search (`faceted_search_service`): multi-word → PostgreSQL FTS (tsvector + pg_trgm typo tolerance); single token → ILIKE MPN prefix.
- Facets: category, manufacturer, lifecycle status, commodity specs.
- Sort: relevance, recency, most-sourced.

### 6.2 Detail `/v2/materials/{card_id}`
- Header: MPN, manufacturer, description, category, lifecycle.
- **Inline-edit description** (AI-generated) — click, edit, save via `hx-patch`.
- Substitutes list.
- Cross-references (`/find_crosses`): cached in `material_cards.cross_references`; `?refresh=1` forces Claude re-fetch.
- Stock history, price history, vendor history.
- "Enrich" button → triggers Claude Haiku re-classify.

### 6.3 Enrichment Pipeline
- Hourly job `_job_material_enrichment` enriches pending cards.
- Startup backfill ensures every requirement MPN has a material card.
- **Verify:** `search_vector` TSVECTOR auto-updates after description/category changes (trigger test: edit → re-search within seconds).

---

## 7. Vendors

### 7.1 List `/v2/vendors`
- Filters: score tier, tags, recency of interaction, region.
- Columns: name, domain, score, last contact, reliability, open offers.

### 7.2 Detail `/v2/vendors/{id}`
- Tabs: Overview, Contacts, Offers, Activity, Stock history, Reviews, Tags.
- Reliability score breakdown: response rate, on-time delivery, cancellation rate, quote conversion.
- Add/edit/delete contact; merge duplicate vendors; tag edits.
- "Find by part": `/v2/partials/vendors/find-by-part` → vendors who ever quoted this MPN.

### 7.3 Fuzzy Matching
- New vendor with similar name → `fuzzy_score_vendor()` suggests merge; threshold test at ~85%.

---

## 8. Customers / CRM `/v2/customers`, `/v2/crm`

- CRUD companies, sites, contacts.
- Enrichment: Apollo + Explorium + Claude analysis → `enrichment_queue` review queue.
- Import CSV/Excel.
- Customer detail `/v2/customers/{id}`: purchase history, quotes, open reqs, proactive matches, contacts.
- Merge duplicates (`company_merge` service).
- **Edge:** junk domains (`JUNK_DOMAINS`) excluded from enrichment auto-save; junk email prefixes skipped.

---

## 9. Offers & Quotes

### 9.1 Offers review queue `/v2/partials/offers/review-queue`
- Low-confidence parsed offers pending human decision.
- Promote (`/offers/{id}/promote`) or Reject (`/offers/{id}/reject`).

### 9.2 Quotes `/v2/quotes`
- List: status (draft, sent, accepted, rejected, expired).
- Create via quote builder: select offers → margin % → line items → save.
- Send: Graph API; attach PDF (Jinja2 `documents/quote_report.html`).
- E-signature link flow.
- Pricing history per MPN.
- Accept → triggers Buy Plan creation.

---

## 10. Buy Plans `/v2/buy-plans`

### 10.1 List & Detail
- Status progression: DRAFT → SUBMITTED → APPROVED → PO_SENT → COMPLETE (or REJECTED / HALTED).
- Each line: assigned buyer (`ownership_service`), AI score (`buyplan_scoring`), PO number, expected delivery.
- External approval token link (emailed to approver).
- Teams notification on state change.

### 10.2 State Machine
- Verify every transition triggers: `activity_log` insert, `notifications` insert, Teams webhook, email.
- Invalid transition (e.g., COMPLETE → DRAFT) rejected with error.

---

## 11. Excess `/v2/excess`

- Lists, line items, bids, solicitations, import.
- CRUD full lifecycle: create list → add lines → open solicitation → vendors bid → award → convert to offers.

---

## 12. Proactive Matching `/v2/proactive`

- Daily job compares active offers to customer purchase history.
- Matches scored by `match_score = f(purchase_count, recency, margin)`.
- User review: dismiss, send, scorecard breakdown.
- Throttle table prevents re-offering same MPN to same customer within window.
- `proactive_do_not_offer` blacklist respected.
- Send → Graph API email + activity log + throttle insert.

---

## 13. Prospecting `/v2/prospecting`

- AI-suggested prospects (daily job, Explorium + web search).
- Claim, dismiss, enrich.
- Detail `/v2/prospecting/{id}` — fit score, readiness score, contacts, signals.

---

## 14. Follow-ups `/v2/follow-ups`

- `/v2/partials/follow-ups` — list of overdue contacts/tasks.
- Send follow-up via `/v2/partials/follow-ups/{contact_id}/send`.

---

## 15. Trouble Tickets `/v2/trouble-tickets`

- User-filed error reports + AI-analyzed.
- Detail `/v2/trouble-tickets/{id}` — conversation, status, resolution.

---

## 16. Settings `/v2/settings` (admin only)

- User management (add, remove, role change).
- API sources config (`connector_status`): enable/disable each connector, test endpoint, last health.
- Feature flags (`MVP_MODE`, `EMAIL_MINING_ENABLED`, etc.).
- Diagnostics dashboard: DB, Redis, Graph, Anthropic, each connector.
- Stocklist upload (local vendor inventory).
- Webhooks config.

---

## 17. Background Jobs (APScheduler)

Verify each runs on schedule and logs success/failure:

| Job | Frequency | Verification |
|-----|-----------|--------------|
| `inbox_monitor` | 30 min | Send test RFQ → reply → confirm offer appears |
| `requirement_refresh` | 4 hours | Stale req gets new sightings |
| `proactive_matcher` | Daily | New offer matches customer history |
| `vendor_scorer` | Daily | Score changes visible on vendor detail |
| `health_check` | 5 min | `/v2/settings` → Diagnostics shows fresh timestamps |
| `backup` | 6 hours | `pg_dump` file in backup volume |
| `tagging_auto` | Hourly | New MPN gets commodity/brand tag |
| `material_enrichment` | Hourly | Pending card gets description/category |
| `task_reminder` | 2 hours | Overdue task → notification |
| `teams_sync` | 6 hours | New 8x8 calls appear in activity |
| `prospecting_refresh` | Daily | New suggested prospects |
| `maintenance` | Daily | ANALYZE runs, cache cleanup logged |
| `quality` | Daily | Vendor scorecards recompute |

---

## 18. Integrations

### 18.1 Microsoft Graph
- Send RFQ → email delivered.
- Reply → `inbox_monitor` picks up, matches via `graph_conversation_id`.
- Webhook renewal (every 5 min).
- Calendar/contacts sync if enabled.

### 18.2 Anthropic Claude
- Email parsing (Sonnet): confidence returned, extracted fields populated.
- MPN normalization (Sonnet).
- Material enrichment (Haiku): description, category, lifecycle.
- Cross-reference discovery.
- Tagging classification.
- **Quota edge:** 429 → retry with backoff; failure → cached fallback or manual review flag.

### 18.3 Supplier APIs
For each of Nexar, BrokerBin, DigiKey, Mouser, Element14, eBay, OEMSecrets, SourceEngine:
- Test from Settings → Diagnostics → "Test connector" button → returns sample result.
- Key rotation: invalid key → clear error banner, connector auto-disabled.

### 18.4 Apollo & Explorium
- Enrichment pulls company data.
- Rate limit handling.

### 18.5 Redis
- `/v2/settings` diagnostics shows Redis connected.
- Cache invalidation on mutation (e.g., edit vendor → list cache cleared).
- Fallback to PG JSONB `intel_cache` if Redis down.

### 18.6 Sentry + Loguru
- Trigger 500 error → Sentry event captured with `request_id`.
- Logs show structured JSON with request_id, user_id, timing.

---

## 19. Frontend UX

### 19.1 HTMX Behaviors
- All navigation via `hx-get`, no full page reloads.
- `data-loading-disable` disables button during flight.
- `hx-indicator` spinner shown.
- Errors: server 4xx/5xx → `_oob_toast()` renders error toast via OOB swap.

### 19.2 Alpine Components
- `$store.toast`, `$store.sidebar`, modal dispatch.
- Do NOT call `$store.toast.show()` as function — it's boolean.
- `@persist` restores sidebar state after reload.

### 19.3 Responsive
- Mobile (<768px): bottom nav, hamburger menu, full-width tables scroll horizontally.
- Tablet (768–1024px): sidebar collapsed by default.
- Desktop (>1024px): sidebar expanded.

### 19.4 Dark Mode
- Toggle in user menu → `dark:` classes applied.
- Every page readable in both modes (no invisible text).

### 19.5 Tailwind Safelist
- Dynamic color classes (status pills, T-tier badges) render correctly post-build.
- After deploy, verify no MISSING classes warning from `deploy.sh` Step 6.

### 19.6 Build Artifacts
- `app/static/dist/*.js` and `*.css` content-hashed.
- `npm run build` → no console errors; smoke test via `scripts/smoke-test-bundles.mjs`.

---

## 20. Data Integrity & Edge Cases

### 20.1 Concurrency
- Two users edit same requisition → last-write-wins with toast warning on overwritten field.
- Search-all fired twice in parallel → second call detects in-flight state, returns cached partial.

### 20.2 MPN Normalization
- Input: lowercase → stored uppercase.
- Input with whitespace, dashes, reel suffixes (`-TR`, `-REEL`) → canonical stored via `strip_packaging_suffixes`.
- <3 chars → rejected.

### 20.3 Substitute MPN Formats
- Legacy string list `["ABC"]` and canonical dict list `[{"mpn":"ABC","manufacturer":"TI"}]` both render via `|sub_mpns` filter.

### 20.4 Empty States
Every list view has an empty state with a CTA:
- Reqs → "Create requisition"
- Materials → "Run a search"
- Vendors → "Add vendor"
- etc. Verify none render blank.

### 20.5 Error Paths
- `/api/...` errors → JSON `{"error", "status_code", "request_id"}` (NOT `detail`).
- HTMX errors → HTML partial with error banner + retry.
- Invalid route → 404 page (not white screen).

### 20.6 Rate Limiting
- Rapid-fire 20+ RFQ sends → throttled after threshold; clear error.

### 20.7 Junk Data
- Email from `JUNK_DOMAINS` → not enriched, not added to contacts.
- Email prefix in `JUNK_EMAIL_PREFIXES` (e.g., `noreply@`) → skipped.

### 20.8 Timezone
- All timestamps stored UTC (`UTCDateTime` type); displayed in user local time.

### 20.9 Large Datasets
- Req with 500 parts → loads via pagination or virtual scroll within 3s.
- Vendor with 5,000 offers history → filterable.

---

## 21. Performance

- Page load (cold): <2s to first partial paint.
- Search-all (10 connectors): <30s total for 1 MPN; results stream progressively.
- HTMX swap: <200ms after response.
- Material workspace FTS on 100k rows: <500ms.
- N+1 queries: run with SQLAlchemy echo → no obvious loops over rows triggering queries.

---

## 22. Security

- SQL injection: submit `'; DROP TABLE requisitions; --` in search/form → parameterized, no effect.
- XSS: submit `<script>alert(1)</script>` in notes/description → escaped in render.
- Auth bypass: strip cookie, hit `/v2/requisitions` → redirected to login.
- CSRF: submit POST without token → 403.
- Secret leakage: view page source → no API keys, no tokens.
- CSP header present on all responses.

---

## 23. Deployment & Rollback

### 23.1 Deploy
- `./deploy.sh` → commits, pushes, builds `--no-cache`, `--force-recreate`, health checks, CSS verification.
- Build tag matches commit SHA (visible at `/healthz` or footer).

### 23.2 Migrations
- `alembic upgrade head` idempotent.
- `alembic heads` → single head (merge if multiple).
- Test downgrade → upgrade roundtrip on staging copy.

### 23.3 Rollback
- `git revert` last deploy → redeploy.
- Migration rollback: `alembic downgrade -1`.
- DB restore: `scripts/restore.sh <backup_file>`.

---

## 24. Automated Tests

Run before any sign-off:

```bash
# Unit + integration
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v

# Full with coverage
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing

# E2E workflows
npx playwright test --project=workflows

# Dead-ends (error paths)
npx playwright test --project=dead-ends

# Accessibility
npx playwright test --project=accessibility

# Visual regression
npx playwright test --project=visual

# Frontend bundle smoke
node scripts/smoke-test-bundles.mjs

# Linting
ruff check app/
mypy app/
npm run lint
```

All must pass. Coverage should not drop vs. main.

---

## 25. Manual Smoke Test (30-minute golden path)

Minimum end-to-end before any release:

1. Login → land on Reqs.
2. Create new requisition with 3 parts (manual).
3. Run Search All → sightings appear from ≥3 sources.
4. Review offers → accept 2.
5. Compose RFQ → send to 2 vendors → toast confirms.
6. Manually paste a vendor reply → Parse → confidence ≥0.8 auto-creates offer.
7. Create Quote from requisition → send → PDF attaches.
8. Mark Quote accepted → Buy Plan auto-created in DRAFT.
9. Submit → Approve Buy Plan → Teams notification fires.
10. Visit Vendors list → score updated for the 2 vendors emailed.
11. Visit Materials → search one MPN → card detail loads → description editable.
12. Visit Sightings → MPN chip click → material modal opens.
13. Logout → session cleared.

All 13 steps pass with no console errors, no 500s in logs, no missing CSS classes.

---

## 26. Sign-Off Checklist

- [ ] All 25 sections above verified on staging.
- [ ] Automated test suite green.
- [ ] `ruff`, `mypy`, `npm run lint` clean.
- [ ] No open P0/P1 tickets against release branch.
- [ ] Deploy script CSS verification: zero MISSING classes.
- [ ] Sentry: no new unresolved issues in last 24h.
- [ ] Backup confirmed within last 6 hours.
- [ ] Rollback plan documented with migration downgrade target.

---

**Owner:** QA lead
**Last updated:** 2026-04-16
**Related:** `docs/APP_MAP_ARCHITECTURE.md`, `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_DATABASE.md`, `docs/12_HUMAN_QA_CHECKLIST.md`
