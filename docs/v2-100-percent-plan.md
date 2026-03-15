# V2 100% Parity Plan

All 189 V1 API endpoints already exist. The work is building HTMX UI surfaces
that call them. Organized into 10 sprints, roughly 1-2 days each.

---

## Sprint 1: Requisition Power Features
**What:** Inline editing, row actions, clone, bulk ops
**Why:** Requisitions are the hub — every workflow starts here

| Task | V1 API it calls | Template work |
|---|---|---|
| Inline field editing (click cell → input → save) | `PATCH /requisitions2/{id}/inline` | Alpine.js `x-data` toggle on cells in `list.html` |
| Row actions dropdown (archive, claim, unclaim, won, lost) | `POST /requisitions2/{id}/action/{action}` | Dropdown menu on each row |
| Bulk actions bar (select rows → archive/activate/assign) | `POST /requisitions2/bulk/{action}` | Already have template in `requisitions2/_bulk_bar.html` |
| Clone requisition button | `POST /api/requisitions/{id}/clone` | Button on detail page, redirects to new req |
| Sortable table columns | `GET /requisitions2/table` | Add `hx-get` with sort param to column headers |

**Files:** `list.html`, `detail.html` (requisitions), `htmx_views.py`
**Estimated new routes:** 3-4

---

## Sprint 2: Offer Management Completion
**What:** Full offer CRUD, attachments, review queue, mark-sold
**Why:** Can't run sourcing without managing offers properly

| Task | V1 API it calls | Template work |
|---|---|---|
| Edit offer (inline or modal) | `PUT /api/offers/{id}` | Edit form/modal from offers tab |
| Delete offer | `DELETE /api/offers/{id}` | Trash icon on offer row with confirm |
| Mark offer sold | `PATCH /api/offers/{id}/mark-sold` | "Sold" button on active offers |
| Offer review queue page | `GET /api/offers/review-queue` | New page: list of medium-confidence AI-parsed offers |
| Promote/reject from review queue | `POST /api/offers/{id}/promote` | Approve/Reject buttons on review cards |
| Offer attachment upload | `POST /api/offers/{id}/attachments` | File input on offer detail |
| Change history view | `GET /api/changelog/offer/{id}` | Expandable audit trail on offer |

**Files:** `offers.html` tab, new `review_queue.html`, `htmx_views.py`
**Estimated new routes:** 6-7

---

## Sprint 3: Vendor CRUD + Contact Management
**What:** Edit vendors, manage contacts, contact timeline, nudges
**Why:** Vendor contacts drive RFQ targeting

| Task | V1 API it calls | Template work |
|---|---|---|
| Edit vendor form (name, emails, phones, website) | `PUT /api/vendors/{id}` | Inline edit on detail header |
| Toggle blacklist | `POST /api/vendors/{id}/blacklist` | Toggle switch on vendor detail |
| Add/edit/delete vendor contacts | `POST/PUT/DELETE /api/vendors/{id}/contacts` | CRUD forms on contacts tab |
| Contact activity timeline | `GET /api/vendors/{id}/contacts/{cid}/timeline` | Expandable timeline on contact row |
| Contact nudge suggestions | `GET /api/vendors/{id}/contact-nudges` | Badge/panel on contacts tab |
| AI relationship summary per contact | `GET /api/vendors/{id}/contacts/{cid}/summary` | Popover or expandable section |
| Email performance metrics | `GET /api/vendors/{id}/email-metrics` | Stats panel on analytics tab |
| Click-to-call logging | `POST /api/vendors/{id}/contacts/{cid}/log-call` | `tel:` links fire hx-post |
| Quick-add email → contact | `POST /api/vendor-card/add-email` | Small form on contacts tab |
| Vendor reviews | `POST/DELETE /api/vendors/{id}/reviews` | Reviews section on detail |
| Name autocomplete | `GET /api/autocomplete/names` | Shared autocomplete component |

**Files:** vendor `detail.html`, `contacts.html` tab, `htmx_views.py`
**Estimated new routes:** 10-12

---

## Sprint 4: Company CRUD + Site Contacts
**What:** Create/edit companies, site contact notes, enrichment
**Why:** Companies are the customer side of the CRM

| Task | V1 API it calls | Template work |
|---|---|---|
| Create company form | `POST /api/companies` | Modal or inline form on list page |
| Edit company fields | `PUT /api/companies/{id}` | Inline edit on detail header |
| AI strategic account summary | `POST /api/companies/{id}/summarize` | Button → panel on detail |
| AI brand/commodity tags | `POST /api/companies/{id}/analyze-tags` | Tags section on detail |
| Company typeahead for forms | `GET /api/companies/typeahead` | Shared component for req create, etc |
| Duplicate check on create | `GET /api/companies/check-duplicate` | Pre-submit validation |
| Update site fields | `PUT /api/sites/{id}` | Inline edit on site card |
| Site contact notes | `POST/GET /api/sites/{id}/contacts/{cid}/notes` | Notes section on contact card |
| Contact notes timeline | `GET /api/sites/{id}/contacts/{cid}/notes` | Chronological note list |
| Company enrichment button | `POST /api/enrich/company/{id}` | Button on company detail |
| Vendor enrichment button | `POST /api/enrich/vendor/{id}` | Button on vendor detail |

**Files:** company `detail.html`, `list.html`, site templates, `htmx_views.py`
**Estimated new routes:** 8-10

---

## Sprint 5: Quote Workflow Completion
**What:** Preview, delete, reopen, revise, pricing history, terms
**Why:** Close the quote lifecycle loop

| Task | V1 API it calls | Template work |
|---|---|---|
| Quote HTML preview before send | `POST /api/quotes/{id}/preview` | Modal with rendered email HTML |
| Delete draft quote | `DELETE /api/quotes/{id}` | Trash button on draft quotes |
| Reopen sent quote | `POST /api/quotes/{id}/reopen` | Button on closed quotes |
| Create revision from existing | `POST /api/quotes/{id}/revise` | "Revise" button → new draft |
| Recent terms autocomplete | `GET /api/quotes/recent-terms` | Autocomplete on payment/shipping fields |
| MPN pricing history | `GET /api/pricing-history/{mpn}` | Chart or table on material detail |
| Update quote metadata | `PUT /api/quotes/{id}` | Inline edit on quote detail header |

**Files:** quote `detail.html`, `list.html`, material `detail.html`, `htmx_views.py`
**Estimated new routes:** 5-6

---

## Sprint 6: RFQ Workflow Depth
**What:** Prepare, retry, phone log, batch follow-up, nav badge
**Why:** Complete the vendor outreach loop

| Task | V1 API it calls | Template work |
|---|---|---|
| RFQ prepare (vendor data + exhaustion) | `POST /api/requisitions/{id}/rfq-prepare` | Pre-send info panel in compose view |
| Retry failed RFQ | `POST /api/contacts/{id}/retry` | Retry button on failed contacts |
| Log phone contact | `POST /api/contacts/phone` | Form in activity tab |
| Batch follow-up send | `POST /api/follow-ups/send-batch` | "Send All" button on follow-ups page |
| Follow-up count nav badge | `GET /api/follow-ups/summary` | Badge on sidebar link |
| Response status update (reviewed/rejected) | `PATCH /api/vendor-responses/{id}/status` | Buttons on response cards |

**Files:** `rfq_compose.html`, `follow_ups/list.html`, `base.html` sidebar, `htmx_views.py`
**Estimated new routes:** 4-5

---

## Sprint 7: Email Integration
**What:** Thread viewer, reply, AI summary, intelligence dashboard
**Why:** Email is the primary vendor communication channel

| Task | V1 API it calls | Template work |
|---|---|---|
| Email thread viewer | `GET /api/emails/thread/{id}` | New thread detail template |
| Requirement email list | `GET /api/requirements/{id}/emails` | Tab on requisition detail |
| Vendor email threads | `GET /api/vendors/{id}/emails` | Enhanced emails tab |
| In-thread reply | `POST /api/emails/reply` | Reply form at bottom of thread |
| AI thread summary | `GET /api/email-intelligence/thread-summary/{id}` | Summary card at top of thread |
| Email intelligence list | `GET /api/email-intelligence` | New page or section |
| Email intelligence dashboard | `GET /api/email-intelligence/dashboard` | Dashboard widget or page |

**Files:** New `emails/` template directory, vendor emails tab, `htmx_views.py`
**Estimated new routes:** 6-7

---

## Sprint 8: Proactive Selling + Prospecting Completion
**What:** Draft/send offers, convert wins, scorecard, prospect submit
**Why:** Revenue generators

| Task | V1 API it calls | Template work |
|---|---|---|
| AI-draft proactive offer email | `POST /api/proactive/draft` | Compose form on match card |
| Send proactive offer | `POST /api/proactive/send` | Send button after draft |
| Convert proactive win → req+quote+buyplan | `POST /api/proactive/convert/{id}` | "Won" flow on sent offers |
| Proactive scorecard | `GET /api/proactive/scorecard` | Stats panel on proactive page |
| Contact picker for site | `GET /api/proactive/contacts/{site_id}` | Dropdown in compose flow |
| Do-not-offer suppression | `POST /api/proactive/do-not-offer` | "Don't offer again" button |
| Match count nav badge | `GET /api/proactive/count` | Badge on sidebar |
| Submit domain for prospecting | `POST /api/prospects/add` | Form on prospecting page |
| Free enrichment | `POST /api/prospects/suggested/{id}/enrich-free` | Button on prospect card |
| Poll enrichment status | `GET /api/prospects/suggested/{id}/enrichment` | Progress indicator |
| Prospect stats | `GET /api/prospects/suggested/stats` | Stats bar on list page |
| Discovery batches (admin) | `GET /api/prospects/batches` | Admin section |

**Files:** proactive templates, prospecting templates, `htmx_views.py`
**Estimated new routes:** 8-10

---

## Sprint 9: Materials + Activity + Knowledge
**What:** Enrichment, merge, stock import, call logging, knowledge CRUD
**Why:** Data quality and CRM completeness

| Task | V1 API it calls | Template work |
|---|---|---|
| AI material enrichment | `POST /api/materials/{id}/enrich` | Button on material detail |
| Merge duplicate materials | `POST /api/materials/merge` | Admin tool or material detail |
| Import vendor stock list | `POST /api/materials/import-stock` | Upload form in settings or materials |
| Soft-delete/restore material | `DELETE/POST /api/materials/{id}` | Toggle on material detail |
| Backfill manufacturers | `POST /materials/backfill-manufacturers` | Admin button |
| Click-to-call event | `POST /api/activity/call-initiated` | Fire from `tel:` links globally |
| Contact activity timeline | `GET /api/activity/contact/{id}` | Expandable on contact cards |
| Vendor last-call | `GET /api/activity/vendors/{id}/last-call` | Display on vendor detail |
| Knowledge entry CRUD UI | `GET/POST/PUT/DELETE /api/knowledge` | Knowledge tab or panel |
| Q&A interface | `POST /api/knowledge/question` + answer | Q&A section on knowledge panel |
| MPN insights panel | `GET/POST /api/materials/insights` | Insights panel on material detail |

**Files:** material templates, activity components, knowledge panel, `htmx_views.py`
**Estimated new routes:** 8-10

---

## Sprint 10: Admin & Import Completion
**What:** Remaining admin tools, import UIs, buy plan edge cases
**Why:** Operational completeness

| Task | V1 API it calls | Template work |
|---|---|---|
| Connector health dashboard | `GET /api/admin/api-health/dashboard` | Settings system tab or dedicated page |
| Material integrity check | `GET /api/admin/integrity` | Admin data-ops tab |
| CSV vendor import | `POST /api/admin/import/vendors` | Upload form in data-ops |
| CSV customer import UI | `POST /api/customers/import` | Upload form in data-ops |
| Site ownership transfer | `POST /api/admin/transfer/execute` | Admin tool |
| Data cleanup scan | `POST /api/admin/data-cleanup/scan` | Admin tool |
| Suggested contacts from enrichment | `GET /api/suggested-contacts` | Panel on vendor/company detail |
| Sync logs viewer | `GET /api/admin/sync-logs` | Admin page |
| User list for dropdowns | `GET /api/users/list` | Shared component |
| Buy plan verification group mgmt | `GET/POST /api/buy-plans/verification-group` | Settings section |
| Token-based approval (email links) | `GET/PUT /api/buy-plans/token/{token}` | Standalone approval page |
| Buyer favoritism detection | `GET /api/buy-plans/favoritism/{user_id}` | Manager-only panel |
| Buy plan resubmit | `POST /api/buy-plans/{id}/resubmit` | Button on rejected plans |
| Scan Outlook for PO emails | `GET /api/buy-plans/{id}/verify-po` | Button on active plan |

**Files:** settings templates, buy plan templates, admin pages, `htmx_views.py`
**Estimated new routes:** 10-12

---

## Totals

| Sprint | New Routes | Templates | Focus |
|---|---|---|---|
| 1. Req Power Features | 3-4 | 2 modified | Inline edit, bulk, clone |
| 2. Offer Management | 6-7 | 2 new, 1 modified | Full CRUD, review queue |
| 3. Vendor + Contacts | 10-12 | 3 modified, 1 new | Contact lifecycle |
| 4. Company + Sites | 8-10 | 3 modified, 1 new | CRM completion |
| 5. Quote Completion | 5-6 | 2 modified | Preview, revise, history |
| 6. RFQ Depth | 4-5 | 3 modified | Retry, batch, badge |
| 7. Email Integration | 6-7 | 4 new | Thread viewer, reply, AI |
| 8. Proactive + Prospects | 8-10 | 4 modified | Draft, send, convert |
| 9. Materials + Activity + KB | 8-10 | 3 modified, 1 new | Enrichment, CRUD |
| 10. Admin + Import | 10-12 | 4 modified, 2 new | Ops tools |
| **TOTAL** | **~75** | **~30** | |

Key insight: All backend logic exists. Each sprint is purely
**template + thin HTMX route** work, calling existing V1 APIs.
No new services, models, or migrations needed.
