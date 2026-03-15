# V1 vs V2 Feature Gap Analysis

Generated: 2026-03-15

## Summary

- **V2 coverage:** ~8% of V1 endpoints (15 of ~190)
- **V1 routes:** ~190 endpoints across 15 router files + CRM module (76 endpoints)
- **V2 routes:** 15 endpoints in `htmx_views.py`
- **V2 strength:** Buy Plans HTMX workflow (new UI, not in V1), server-rendered HTML

---

## What V2 Covers (Detailed)

### Requisitions — 3 endpoints

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/requisitions` | List with search (name/customer), status filter, pagination |
| `POST /v2/partials/requisitions/create` | Create with name, customer, deadline, urgency, bulk parts text |
| `GET /v2/partials/requisitions/{id}` | Detail view with requirements table, inline add requirement |
| `POST /v2/partials/requisitions/{id}/requirements` | Add single requirement (MPN, qty, brand) |

**What works:** Browse, create, view detail, add parts, search from detail view.

**What's missing vs V1 (9 endpoints in `requisitions2.py`):**
- `GET /requisitions2/stream` — SSE real-time table updates
- `GET /requisitions2/table` — Sortable table fragment with column sorting
- `GET /requisitions2/{id}/modal` — Detail modal popup
- `GET /requisitions2/{id}/edit/{field}` — Inline cell editing (click-to-edit any field)
- `PATCH /requisitions2/{id}/inline` — Save inline edit
- `POST /requisitions2/{id}/action/{action}` — Row actions: archive, activate, claim, unclaim, won, lost, assign
- `POST /requisitions2/bulk/{action}` — Bulk actions on selected rows (archive, activate, assign)
- `POST /api/requisitions/{id}/clone` — Clone requisition with requirements and reference offers

---

### Part Search — 2 endpoints

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/search` | Search form with MPN input |
| `POST /v2/partials/search/run` | Execute search, return results table (vendor, MPN, qty, price, source badge) |

**What works:** Search by MPN, view results with source-colored badges, search from requisition detail.

**What's missing vs V1 (`materials.py` — 11 endpoints):**
- `GET /api/materials` — Material card list with search/pagination
- `GET /api/materials/{id}` — Material card detail (vendor history, sightings, offers)
- `GET /api/materials/by-mpn/{mpn}` — Lookup card by MPN
- `PUT /api/materials/{id}` — Edit material card (manufacturer, description, enrichment)
- `POST /api/materials/{id}/enrich` — AI enrichment (auto-fill specs)
- `DELETE /api/materials/{id}` — Soft-delete material card
- `POST /api/materials/{id}/restore` — Restore soft-deleted card
- `POST /api/materials/merge` — Merge duplicate material cards
- `POST /materials/backfill-manufacturers` — Bulk manufacturer backfill
- `POST /api/materials/import-stock` — Import vendor stock list as cards
- `GET /api/pricing-history/{mpn}` — Price history across all quotes

---

### Vendors — 2 endpoints

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/vendors` | Card grid with live search, score badges, pagination |
| `GET /v2/partials/vendors/{id}` | Detail: stats (sightings, win rate, POs, response time), contacts table, recent sightings |

**What works:** Browse vendors, search, view detail with stats and recent sightings.

**What's missing vs V1 (`vendors_crud.py` — 9 endpoints):**
- `GET /api/vendors/check-duplicate` — Duplicate check (exact + fuzzy)
- `GET /api/autocomplete/names` — Name autocomplete across vendors + companies
- `PUT /api/vendors/{id}` — Update vendor (emails, phones, website, display name)
- `POST /api/vendors/{id}/blacklist` — Toggle blacklist status
- `DELETE /api/vendors/{id}` — Delete vendor (admin)
- `POST /api/vendors/{id}/reviews` — Add vendor review
- `DELETE /api/vendors/{id}/reviews/{review_id}` — Delete review

---

### Companies — 2 endpoints

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/companies` | Table with live search, type badges, owner, site/req counts |
| `GET /v2/partials/companies/{id}` | Detail: quick info grid, sites table, notes section |

**What works:** Browse companies, search, view detail with sites.

**What's missing vs V1 (`crm/companies.py` — 8 endpoints):**
- `GET /api/companies/typeahead` — Lightweight typeahead for forms
- `GET /api/companies/check-duplicate` — Duplicate name check
- `POST /api/companies` — Create company with auto-enrichment + default HQ site
- `PUT /api/companies/{id}` — Update company fields
- `POST /api/companies/{id}/summarize` — AI strategic account summary
- `POST /api/companies/{id}/analyze-tags` — AI brand/commodity tag generation

---

### Buy Plans — 12 endpoints (V2 only, no V1 UI equivalent)

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/buy-plans` | List with status tabs, "My Only" toggle, search, role-based filtering |
| `GET /v2/partials/buy-plans/{id}` | Full detail: summary cards, AI flags, line items, workflow buttons |
| `POST /v2/partials/buy-plans/{id}/submit` | Submit with SO#, customer PO, notes |
| `POST /v2/partials/buy-plans/{id}/approve` | Manager approve/reject with notes |
| `POST /v2/partials/buy-plans/{id}/verify-so` | Ops verify/reject/halt sales order |
| `POST /v2/partials/buy-plans/{id}/lines/{line_id}/confirm-po` | Buyer confirms PO number + ship date |
| `POST /v2/partials/buy-plans/{id}/lines/{line_id}/verify-po` | Ops verifies PO entry |
| `POST /v2/partials/buy-plans/{id}/lines/{line_id}/issue` | Flag line issue (out of stock, price change) |
| `POST /v2/partials/buy-plans/{id}/cancel` | Cancel plan with reason |
| `POST /v2/partials/buy-plans/{id}/reset` | Reset halted/cancelled to draft |

**What works:** Full multi-step workflow: draft → submit → approve → PO confirm → verify → complete. Status tabs, margin color-coding, AI summary/flags, role-based actions.

**V1 API has more (`crm/buy_plans.py` — 20 endpoints) that V2 doesn't expose:**
- `GET /api/buy-plans/verification-group` — List ops verification group members
- `POST /api/buy-plans/verification-group` — Add/remove ops group members
- `GET /api/buy-plans/token/{token}` — Public token-based plan view (email links)
- `PUT /api/buy-plans/token/{token}/approve` — Token-based approval (from email)
- `PUT /api/buy-plans/token/{token}/reject` — Token-based rejection (from email)
- `GET /api/buy-plans/favoritism/{user_id}` — Buyer favoritism detection (manager)
- `POST /api/buy-plans/{id}/case-report` — Regenerate case report
- `POST /api/quotes/{quote_id}/buy-plan/build` — AI-build buy plan from won quote
- `POST /api/buy-plans/{id}/resubmit` — Resubmit rejected plan
- `GET /api/buy-plans/{id}/verify-po` — Scan Outlook for PO emails
- `GET /api/buy-plans/{id}/offers/{req_id}` — Available offers for plan line

---

### Dashboard — 1 endpoint

| V2 Endpoint | What It Does |
|---|---|
| `GET /v2/partials/dashboard` | Stats cards: open reqs, active vendors, active companies |

**Note:** Route exists but not linked in sidebar navigation.

---

## What V2 Is Missing Entirely

### 1. RFQ & Email Workflow — 13 endpoints (`rfq.py`)

The core business process for sending quotes to vendors and tracking responses.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/requisitions/{id}/rfq` | Send batch RFQ emails to vendors via Graph API |
| `POST /api/requisitions/{id}/rfq-prepare` | Get vendor data + exhaustion info before sending |
| `POST /api/requisitions/{id}/poll` | Manually poll inbox for vendor responses |
| `GET /api/requisitions/{id}/contacts` | List outbound RFQ email contacts |
| `GET /api/requisitions/{id}/responses` | List vendor responses (filterable) |
| `GET /api/requisitions/{id}/activity` | Combined view: contacts + responses + tracking by vendor |
| `PATCH /api/vendor-responses/{id}/status` | Mark response reviewed/rejected |
| `POST /api/contacts/phone` | Log phone contact event |
| `POST /api/contacts/{id}/retry` | Re-send failed RFQ email |
| `GET /api/follow-ups` | List stale contacts needing follow-up |
| `GET /api/follow-ups/summary` | Cross-req follow-up counts for nav badge |
| `POST /api/follow-ups/{id}/send` | Send follow-up email |
| `POST /api/follow-ups/send-batch` | Batch send follow-ups |

---

### 2. Quotes & Offers — 28 endpoints (`crm/quotes.py` + `crm/offers.py`)

Quote creation, sending, revision, and offer management.

**Quotes (12 endpoints):**

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/requisitions/{id}/quote` | Fetch latest quote |
| `GET /api/requisitions/{id}/quotes` | List all quotes including revisions |
| `GET /api/quotes/recent-terms` | Recent payment/shipping terms for autocomplete |
| `POST /api/requisitions/{id}/quote` | Create quote from offers or manual lines |
| `PUT /api/quotes/{id}` | Update draft quote |
| `DELETE /api/quotes/{id}` | Delete draft quote |
| `POST /api/quotes/{id}/preview` | HTML email preview |
| `POST /api/quotes/{id}/send` | Send quote via Graph API |
| `POST /api/quotes/{id}/result` | Mark won/lost with reason |
| `POST /api/quotes/{id}/revise` | Create new revision |
| `POST /api/quotes/{id}/reopen` | Reopen as sent or create revision |
| `GET /api/pricing-history/{mpn}` | MPN pricing history across quotes |

**Offers (16 endpoints):**

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/requisitions/{id}/offers` | List offers grouped by requirement |
| `POST /api/requisitions/{id}/offers` | Create offer with vendor fuzzy matching |
| `PUT /api/offers/{id}` | Update offer with change tracking |
| `DELETE /api/offers/{id}` | Delete offer |
| `PUT /api/offers/{id}/reconfirm` | Reconfirm historical offer |
| `PUT /api/offers/{id}/approve` | Approve pending offer |
| `PUT /api/offers/{id}/reject` | Reject pending offer |
| `PATCH /api/offers/{id}/mark-sold` | Mark stock as sold |
| `GET /api/changelog/{type}/{id}` | Change history for entity |
| `POST /api/offers/{id}/attachments` | Upload to OneDrive + attach |
| `POST /api/offers/{id}/attachments/onedrive` | Attach existing OneDrive file |
| `DELETE /api/offer-attachments/{id}` | Delete attachment |
| `GET /api/onedrive/browse` | Browse OneDrive for picker |
| `GET /api/offers/review-queue` | Medium-confidence offers needing review |
| `POST /api/offers/{id}/promote` | Promote reviewed offer |
| `POST /api/offers/{id}/reject` | Reject reviewed offer |

---

### 3. Vendor Contacts — 12 endpoints (`vendor_contacts.py`)

Contact discovery, management, and relationship tracking.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/vendor-contact` | 3-tier lookup: cache → website scrape → AI web search |
| `GET /api/vendor-contacts/bulk` | All contacts in single query (avoids N+1) |
| `GET /api/vendors/{id}/contacts` | List contacts for vendor |
| `GET /api/vendors/{id}/contacts/{cid}/timeline` | Contact activity timeline |
| `GET /api/vendors/{id}/contact-nudges` | Nudge suggestions for dormant contacts |
| `GET /api/vendors/{id}/contacts/{cid}/summary` | AI relationship summary |
| `POST /api/vendors/{id}/contacts/{cid}/log-call` | Log click-to-call event |
| `POST /api/vendors/{id}/contacts` | Add contact manually |
| `PUT /api/vendors/{id}/contacts/{cid}` | Update contact |
| `DELETE /api/vendors/{id}/contacts/{cid}` | Delete contact |
| `GET /api/vendors/{id}/email-metrics` | Email performance metrics |
| `POST /api/vendor-card/add-email` | Quick-add email + create contact |

---

### 4. Sites & Customer Contacts — 10 endpoints (`crm/sites.py`)

Customer site management and contact CRUD.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/companies/{id}/sites` | Add site to company |
| `PUT /api/sites/{id}` | Update site |
| `GET /api/sites/{id}` | Site detail with contacts and recent reqs |
| `GET /api/customer-contacts` | All customer contacts across sites |
| `GET /api/sites/{id}/contacts` | List contacts for site |
| `POST /api/sites/{id}/contacts` | Create site contact (with dedup) |
| `PUT /api/sites/{id}/contacts/{cid}` | Update site contact |
| `DELETE /api/sites/{id}/contacts/{cid}` | Delete site contact |
| `POST /api/sites/{id}/contacts/{cid}/notes` | Log timestamped note |
| `GET /api/sites/{id}/contacts/{cid}/notes` | Note history |

---

### 5. AI Assistant — 15 endpoints (`ai.py`)

AI-powered parsing, enrichment, and intelligence.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/ai/find-contacts` | Find contacts using AI web search |
| `GET /api/ai/prospect-contacts` | List enriched contacts |
| `POST /api/ai/prospect-contacts/{id}/save` | Keep prospect contact |
| `DELETE /api/ai/prospect-contacts/{id}` | Delete prospect contact |
| `POST /api/ai/prospect-contacts/{id}/promote` | Promote to vendor/site contact |
| `POST /api/ai/parse-email` | Parse vendor reply into structured quotes |
| `POST /api/ai/normalize-parts` | Normalize part numbers (manufacturer, package, base) |
| `POST /api/ai/parse-response/{id}` | Re-parse vendor response with upgraded parser |
| `POST /api/ai/save-parsed-offers` | Save AI-parsed draft offers |
| `GET /api/ai/company-intel` | Intelligence brief for company (cached 7d) |
| `POST /api/ai/draft-rfq` | Generate personalized RFQ email body |
| `POST /api/ai/parse-freeform-rfq` | Parse free-form customer text into RFQ |
| `POST /api/ai/parse-freeform-offer` | Parse free-form vendor text into offers |
| `POST /api/ai/apply-freeform-rfq` | Create requisition from parsed RFQ |
| `POST /api/ai/save-freeform-offers` | Save freeform-parsed offers |

---

### 6. Proactive Selling — 11 endpoints (`proactive.py`)

Auto-matching inventory to customer needs and outbound offers.

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/proactive/matches` | List matches grouped by customer |
| `POST /api/proactive/refresh` | Trigger matching scan |
| `GET /api/proactive/count` | Match count for nav badge |
| `POST /api/proactive/dismiss` | Dismiss matches |
| `POST /api/proactive/do-not-offer` | Permanently suppress MPNs for customer |
| `POST /api/proactive/draft` | AI-draft offer email |
| `POST /api/proactive/send` | Send offer email |
| `GET /api/proactive/offers` | List sent offers |
| `POST /api/proactive/convert/{id}` | Convert to won req + quote + buy plan |
| `GET /api/proactive/scorecard` | Performance scorecard |
| `GET /api/proactive/contacts/{site_id}` | Contact picker for site |

---

### 7. Prospect Finder — 9 endpoints (`prospect_suggested.py`)

New account discovery and enrichment pipeline.

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/prospects/suggested` | List prospects with filters/sort |
| `GET /api/prospects/suggested/stats` | Aggregate stats |
| `GET /api/prospects/suggested/{id}` | Full prospect detail |
| `POST /api/prospects/suggested/{id}/claim` | Claim with deep enrichment |
| `POST /api/prospects/suggested/{id}/dismiss` | Dismiss with reason |
| `GET /api/prospects/suggested/{id}/enrichment` | Poll enrichment status |
| `POST /api/prospects/suggested/{id}/enrich-free` | Free enrichment (SAM.gov + Google News) |
| `POST /api/prospects/add` | Submit domain for prospecting |
| `GET /api/prospects/batches` | Discovery batch history (admin) |

---

### 8. Email Integration — 7 endpoints (`emails.py`)

Email thread viewing, reply, and intelligence.

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/requirements/{id}/emails` | Threads linked to requirement |
| `GET /api/emails/thread/{conversation_id}` | All messages in thread |
| `GET /api/vendors/{id}/emails` | Threads with vendor |
| `POST /api/emails/reply` | Send reply in thread |
| `GET /api/email-intelligence/thread-summary/{id}` | AI thread summary |
| `GET /api/email-intelligence` | Recent classified emails |
| `GET /api/email-intelligence/dashboard` | Aggregated email dashboard |

---

### 9. Activity Tracking — 4 endpoints (`activity.py`)

Call logging and timeline views.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/activity/call-initiated` | Log click-to-call event |
| `GET /api/activity/account/{company_id}` | Company activity timeline |
| `GET /api/activity/contact/{contact_id}` | Contact activity timeline |
| `GET /api/activity/vendors/{vendor_id}/last-call` | Most recent vendor call |

---

### 10. Knowledge Base — 19 endpoints (`knowledge.py`)

Knowledge entries, Q&A, and AI insights across entities.

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/knowledge` | List entries with filters |
| `POST /api/knowledge` | Create entry |
| `PUT /api/knowledge/{id}` | Update entry |
| `DELETE /api/knowledge/{id}` | Delete entry |
| `GET /api/knowledge/quota` | Daily question quota |
| `GET /api/knowledge/config` | Config values |
| `PUT /api/knowledge/config` | Update config (admin) |
| `POST /api/knowledge/question` | Post Q&A question |
| `POST /api/knowledge/{id}/answer` | Post answer |
| `GET /api/requisitions/{id}/insights` | Cached AI insights for req |
| `POST /api/requisitions/{id}/insights/refresh` | Generate fresh req insights |
| `GET /api/vendors/{id}/insights` | Cached vendor insights |
| `POST /api/vendors/{id}/insights/refresh` | Fresh vendor insights |
| `GET /api/companies/{id}/insights` | Cached company insights |
| `POST /api/companies/{id}/insights/refresh` | Fresh company insights |
| `GET /api/dashboard/pipeline-insights` | Pipeline-level insights |
| `POST /api/dashboard/pipeline-insights/refresh` | Fresh pipeline insights |
| `GET /api/materials/insights` | MPN insights |
| `POST /api/materials/insights/refresh` | Fresh MPN insights |

---

### 11. Data Sources & Email Mining — 12 endpoints (`sources.py`)

API source management and email inbox mining.

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/sources` | All API sources grouped by status |
| `POST /api/sources/{id}/test` | Test API source with known part |
| `PUT /api/sources/{id}/toggle` | Enable/disable source (admin) |
| `PUT /api/sources/{id}/activate` | Toggle is_active flag |
| `GET /api/sources/health-summary` | Active sources with errors |
| `GET /api/system/alerts` | Active API alerts |
| `POST /api/email-mining/scan` | Scan inbox for vendor contacts/offers |
| `GET /api/email-mining/status` | Mining status |
| `POST /api/email-mining/scan-outbound` | Scan Sent Items for RFQ metrics |
| `POST /api/email-mining/compute-engagement` | Recompute vendor scores |
| `GET /api/vendors/{id}/engagement` | Vendor score breakdown |
| `POST /api/email-mining/parse-response-attachments/{id}` | Parse response attachments |

---

### 12. Enrichment & Import — 8 endpoints (`crm/enrichment.py`)

External data enrichment and bulk import.

| V1 Endpoint | What It Does |
|---|---|
| `POST /api/enrich/company/{id}` | Enrich company with external data + contacts |
| `POST /api/enrich/vendor/{id}` | Enrich vendor with external data |
| `GET /api/suggested-contacts` | Find suggested contacts from enrichment |
| `POST /api/suggested-contacts/add-to-vendor` | Add suggested contact to vendor |
| `POST /api/suggested-contacts/add-to-site` | Set suggested contact as site primary |
| `GET /api/admin/sync-logs` | View sync log entries (admin) |
| `GET /api/users/list` | User list for dropdowns |
| `POST /api/customers/import` | Bulk import customers/sites (admin) |

---

### 13. Admin — System & Data Ops — 28 endpoints (`admin/system.py` + `admin/data_ops.py`)

**System Config (10 endpoints):**

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/admin/config` | All system config |
| `PUT /api/admin/config/{key}` | Update config value |
| `GET /api/admin/health` | System health status |
| `GET /api/admin/connector-health` | Per-connector health metrics |
| `GET /api/admin/api-health/dashboard` | Full API health dashboard |
| `GET /api/admin/sources/{id}/credentials` | Get masked credentials |
| `PUT /api/admin/sources/{id}/credentials` | Set credentials |
| `DELETE /api/admin/sources/{id}/credentials/{var}` | Remove credential |
| `GET /api/admin/integrity` | Material card integrity checks |
| `GET /api/admin/material-audit` | Material card audit log |

**Data Operations (18 endpoints):**

| V1 Endpoint | What It Does |
|---|---|
| `GET /api/admin/vendor-dedup-suggestions` | Fuzzy duplicate vendor detection |
| `POST /api/admin/vendor-merge` | Merge duplicate vendors |
| `GET /api/admin/company-dedup-suggestions` | Duplicate company detection |
| `GET /api/admin/company-merge-preview` | Preview merge impact |
| `POST /api/admin/company-merge` | Merge companies |
| `POST /api/admin/import/customers` | CSV customer import |
| `POST /api/admin/import/vendors` | CSV vendor import |
| `GET /api/admin/teams/config` | Teams integration config |
| `POST /api/admin/teams/config` | Save Teams config |
| `GET /api/admin/teams/channels` | List Teams channels |
| `POST /api/admin/teams/channel-routing` | Save event → channel routing |
| `GET /api/admin/teams/channel-routing` | Get event → channel routing |
| `GET /api/admin/transfer/preview` | Preview site ownership transfer |
| `POST /api/admin/transfer/execute` | Transfer site ownership |
| `POST /api/admin/data-cleanup/scan` | Scan for test/junk records |
| `POST /api/admin/data-cleanup/fix-dates` | Fix sentinel dates |

---

## Endpoint Count Summary

| Area | V1 Endpoints | V2 Endpoints | Coverage |
|---|---|---|---|
| Requisitions | 9 | 4 | 44% |
| Part Search / Materials | 11 | 2 | 18% |
| Vendors CRUD | 9 | 2 | 22% |
| Companies | 8 | 2 | 25% |
| Buy Plans | 20 | 10 | 50% |
| Quotes | 12 | 0 | 0% |
| Offers | 16 | 0 | 0% |
| RFQ & Email Workflow | 13 | 0 | 0% |
| Vendor Contacts | 12 | 0 | 0% |
| Sites & Customer Contacts | 10 | 0 | 0% |
| AI Assistant | 15 | 0 | 0% |
| Proactive Selling | 11 | 0 | 0% |
| Prospect Finder | 9 | 0 | 0% |
| Email Integration | 7 | 0 | 0% |
| Activity Tracking | 4 | 0 | 0% |
| Knowledge Base | 19 | 0 | 0% |
| Sources & Email Mining | 12 | 0 | 0% |
| Enrichment & Import | 8 | 0 | 0% |
| Admin (System + Data Ops) | 28 | 0 | 0% |
| **TOTAL** | **~233** | **20** | **~8%** |

---

## What V2 Does Better Than V1

1. **Buy Plans** — Complete multi-step approval workflow (sales → manager → ops) with modals, PO confirmation, issue flagging, AI flags
2. **Server-rendered HTML** — Faster initial load, less JS, no large bundle downloads
3. **HTMX partial swaps** — Smoother UX than full page reloads without SPA complexity
4. **Mobile-responsive** — Sidebar collapses to hamburger menu on mobile

---

## V1 Source Files

| Module | File | Endpoints |
|---|---|---|
| Requisitions | `app/routers/requisitions2.py` | 9 |
| RFQ | `app/routers/rfq.py` | 13 |
| Vendors CRUD | `app/routers/vendors_crud.py` | 9 |
| Vendor Contacts | `app/routers/vendor_contacts.py` | 12 |
| Materials | `app/routers/materials.py` | 11 |
| AI Services | `app/routers/ai.py` | 15 |
| Activity | `app/routers/activity.py` | 4 |
| Proactive | `app/routers/proactive.py` | 11 |
| Prospects | `app/routers/prospect_suggested.py` | 9 |
| Email | `app/routers/emails.py` | 7 |
| Knowledge | `app/routers/knowledge.py` | 19 |
| Sources | `app/routers/sources.py` | 12 |
| CRM/Companies | `app/routers/crm/companies.py` | 8 |
| CRM/Quotes | `app/routers/crm/quotes.py` | 12 |
| CRM/Offers | `app/routers/crm/offers.py` | 16 |
| CRM/Sites | `app/routers/crm/sites.py` | 10 |
| CRM/Buy Plans | `app/routers/crm/buy_plans.py` | 20 |
| CRM/Clone | `app/routers/crm/clone.py` | 1 |
| CRM/Enrichment | `app/routers/crm/enrichment.py` | 8 |
| Admin/System | `app/routers/admin/system.py` | 10 |
| Admin/Data Ops | `app/routers/admin/data_ops.py` | 18 |

## V2 Source Files

| Module | File | Endpoints |
|---|---|---|
| All views + partials | `app/routers/htmx_views.py` | 20 |
| Templates | `app/templates/htmx/` | 14 templates |

---

## Priority Order for Porting (Suggested)

1. **Quotes & Offers** — Can't close deals without quoting (28 endpoints)
2. **RFQ & Email Workflow** — Core business process, vendor outreach (13 endpoints)
3. **Sites & Customer Contacts** — Needed for quote delivery and RFQ contact selection (10 endpoints)
4. **Vendor Contacts** — Needed for RFQ contact selection (12 endpoints)
5. **Activity Tracking** — Call/email logging for CRM (4 endpoints)
6. **AI Services** — Parsing, enrichment, intelligence (15 endpoints)
7. **Email Integration** — Inbox, threads, reply (7 endpoints)
8. **Material Management** — Enrichment, merge, stock import (11 endpoints)
9. **Proactive Selling** — Revenue generator but not blocking (11 endpoints)
10. **Prospect Finder** — Growth feature (9 endpoints)
11. **Knowledge Base** — Nice to have (19 endpoints)
12. **Admin/Config** — Internal tools (28 endpoints)
13. **Sources & Email Mining** — Background processes (12 endpoints)
14. **Enrichment & Import** — Bulk operations (8 endpoints)
