# V1 vs V2 Feature Gap Analysis

Generated: 2026-03-15

## Summary

- **V2 coverage:** ~22% of V1 features
- **V2 routes:** 15 | **V1 routes:** 130+
- **V2 strength:** Buy Plans workflow (new, not in V1)

## Feature Coverage

| Feature | V1 Status | V2 Status | Gap |
|---------|-----------|-----------|-----|
| **Buy Plans Workflow** | Not built | Full (16 endpoints) | V2 only |
| **Requisitions** | Full (8 routes) | Partial (3 routes) | Missing: inline edit, bulk actions, advanced filters, SSE updates, export/import, copy, templates |
| **Part Search** | Full (7 routes) | Partial (2 routes) | Missing: material card detail, enrichment UI, merge, stock import, price/lead time history |
| **Vendors** | Full (9 routes) | Partial (2 routes) | Missing: contact CRUD, contact timeline, email metrics, 3-tier lookup, reviews/ratings, blacklist, portfolio |
| **Companies** | Full | Partial (2 routes) | Missing: activity timeline, contact management, custom fields, notes |
| **RFQ & Email Workflow** | Full (13 routes) | None | Batch RFQ sending, contact selection, inbox polling, response parsing, follow-ups, activity tracking |
| **Activity Tracking** | Full (4 routes) | None | Call logging, email tracking, timeline view, activity feed filtering |
| **Proactive Selling** | Full (11 routes) | None | Standing offer generation, match discovery, offer drafting/sending, conversion tracking, scorecards |
| **AI Prospect Finder** | Full (15+ routes) | None | Contact discovery, enrichment (free & paid), prospect claims, batch import |
| **AI Assistant** | Full (15 routes) | None | Email parsing, RFQ/offer freeform parsing, part normalization, auto-extraction, company intel, draft RFQ |
| **Email Integration** | Full (6 routes) | None | Inbox list, thread view, reply, email intel dashboard, thread summaries, attachment parsing |
| **Vendor Contacts** | Full (11 routes) | None | CRUD, 3-tier lookup (cache > scrape > AI), contact timeline, email metrics, bulk ops |
| **Material Management** | Full (11 routes) | None | Material card list/detail/edit, enrichment, merge, stock import, bulk manufacturer backfill |
| **Knowledge Base** | Full (7 routes) | None | CRUD, tag-based search, Q&A format, quota tracking |
| **Admin/Config** | Full (30+ routes) | None | Source credentials, API key testing, tagging/backfill, error reporting, system config |

## V1 Source Files

| Module | File | Route Count |
|--------|------|-------------|
| Requisitions | `app/routers/requisitions2.py` | 8 |
| RFQ | `app/routers/rfq.py` | 13 |
| Vendors CRUD | `app/routers/vendors_crud.py` | 9 |
| Vendor Contacts | `app/routers/vendor_contacts.py` | 11 |
| Materials | `app/routers/materials.py` | 11 |
| AI Services | `app/routers/ai.py` | 15 |
| Activity | `app/routers/activity.py` | 4 |
| Proactive | `app/routers/proactive.py` | 11 |
| Prospects | `app/routers/prospect_suggested.py` | 9 |
| Email | `app/routers/emails.py` | 6 |
| Knowledge | `app/routers/knowledge.py` | 7 |
| Sources/Admin | `app/routers/sources.py`, `app/routers/admin/` | 30+ |

## V2 Source Files

| Module | File | Route Count |
|--------|------|-------------|
| All views + partials | `app/routers/htmx_views.py` | 15 |
| Templates | `app/templates/htmx/` | - |

## What V2 Does Better Than V1

1. **Buy Plans** — Complete multi-step approval workflow (sales > manager > ops) with modals, PO confirmation, issue flagging
2. **Server-rendered HTML** — Faster initial load, less JS, no large bundle downloads
3. **HTMX partial swaps** — Smoother UX than full page reloads without SPA complexity

## Priority Order for Porting (Suggested)

1. RFQ & Email Workflow (core business process)
2. Vendor Contacts (needed for RFQ contact selection)
3. Activity Tracking (call/email logging)
4. AI Services (parsing, enrichment)
5. Email Integration (inbox, threads)
6. Material Management (enrichment, merge)
7. Proactive Selling
8. Prospect Finder
9. Knowledge Base
10. Admin/Config
