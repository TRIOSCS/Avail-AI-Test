# Healthy CRM — Foundational Details Audit & Plan

**Date:** 2026-06-23
**Purpose:** Step back from reactive fixes and lay out *all* the small, basic, deep details a healthy
B2B CRM needs — grounded in an audit of what AvailAI already has (file-referenced) vs. what's missing.
**Method:** 5 parallel code audits (fields · activity/notes · tasks/ownership/cadence · list & detail
UX · data integrity).

---

## 0. What's already strong (the foundation — don't rebuild)

These are genuinely good and ahead of generic CRMs — they're the base everything else builds on:

- **Two-clock cadence** (last_outbound_at / last_reply_at) at account + site + contact, tier targets
  (key 7d / core 14d / standard 30d), states new/on_target/due/overdue. *Best-in-class for a call list.*
- **Zero-touch auto-logging** — M365 Sent/Received email, RFQ sends, and click-to-contact all write to
  one `ActivityLog`; a unified RFQ+Quote+Activity timeline with a noise filter.
- **Contact-first management** — cards with role chip, priority/archive/DNC, set-primary, suggested-
  contacts enrichment; split-panel detail that doesn't lose scroll.
- **Dedup spine** — `normalized_name` (pg_trgm), AI auto-dedup (92–98%), possible-duplicate banner +
  merge flow, per-site email unique constraint, phone E.164 on Company.
- **Enrichment provenance** tracked per field (Explorium/Clay/Apollo/Hunter).
- **Global typeahead** across 7 entity types; invitational empty states everywhere.

> Implication: most of the work below is *completing details on a strong base*, not rebuilding.

---

## 1. The gap inventory (Have / Partial / Missing, by layer)

### A. Record fields — the "deep details" on every account/contact

**Cheapest win — columns that EXIST but have no form input (just surface them):**
`Company.legal_name`, `employee_size`, `revenue_range`, `tier`, `source`, `phone`, `credit_terms`,
`tax_id`; `Company.hq_city/state/country` (add `hq_street`+`hq_zip`). These are already stored/used —
they just need to appear in the create/edit forms. **(P0, low effort.)**

| Field | Entity | Verdict | Pri |
|---|---|---|---|
| first_name / last_name (split from `full_name`) | Contact | MISSING | **P0** |
| primary_contact pointer (Account → Contact FK) | Account | MISSING | **P0** |
| parent/child company hierarchy (self-FK) | Account | MISSING | **P0** |
| contact owner (own, not just site/account) | Contact | MISSING | **P0** |
| general user **tags** | Account + Contact | MISSING | **P0** |
| full structured HQ address (street+zip) | Account | PARTIAL | **P0** |
| lifecycle stage (prospect/lead/customer/churned) | Account | MISSING | P1 |
| department · seniority | Contact | MISSING | P1 |
| phone variants (mobile/office/direct) | Contact | PARTIAL (1) | P1 |
| per-channel opt-out (email vs call vs sms) | Contact | PARTIAL (DNC only) | P1 |
| preferred contact method · timezone | Contact | MISSING | P2 |
| reports-to (org chart self-FK) | Contact | MISSING | P2 |

### B. Activity, timeline & notes

| Detail | Verdict | Pri |
|---|---|---|
| Call **outcome** (connected/voicemail/declined/no-answer) | MISSING | **P0** |
| Calendar **meetings logged to the timeline** (detected today, not surfaced) | MISSING | **P0** |
| Per-activity **follow-up flag** (+ due date) | MISSING | **P0** |
| **Notes as a feed** on Account/Site (contact notes already a feed; account/site are a single blob) | PARTIAL | P1 |
| **Pin/star** a note | MISSING | P1 |
| Compose & send **email from the contact** (auto-logged) | MISSING | P1 |
| Email **thread grouping** (one row per reply today) | MISSING | P2 |

### C. Tasks, ownership & the daily worklist

| Detail | Verdict | Pri |
|---|---|---|
| **Account/contact-level tasks** (today only requisition-scoped `RequisitionTask`) | MISSING | **P0** |
| **"My Day"** queue (overdue follow-ups + due tasks + next actions in one place) | MISSING | **P0** |
| **Explicit next-step** field (what + when) on account/contact | MISSING | **P0** |
| **Due/overdue follow-up worklist** (cadence computes it; no list to work from) | MISSING | **P0** |
| Reminders / notifications on due tasks | MISSING | P1 |
| Assignment history (who owned when) | MISSING | P1 |
| Task snooze / recurring / comments | MISSING | P2 |
| Deal **forecasting** on Requisition (stage · probability · expected-close) | MISSING | P1 (own project) |

### D. List & detail UX

| Detail | Verdict | Pri |
|---|---|---|
| Account-page **tabs don't switch** (htmx filter bug) | **FIXED 2026-06-23** | — |
| **Bulk select + bulk actions** (assign owner / tag / export) | MISSING | **P0** |
| **Deep-linkable tabs** (`?tab=`) + tab keyboard nav | PARTIAL/MISSING | **P0** |
| Disposition + has-open-reqs **filters** | PARTIAL | **P0** |
| **Saved views / filters** | MISSING | P1 |
| **Inline edit** in the list | MISSING | P1 |
| **CSV export** | MISSING | P1 |
| Recently-viewed / favorites / pins | MISSING | P2 |
| Column customization / density | MISSING | P2 |
| *(Strong already: search, 6-way sort, global typeahead, empty states, split-panel)* | HAVE | — |

### E. Data trust & integrity

| Detail | Verdict | Pri |
|---|---|---|
| **`updated_by` on CRM records** — who changed an account/contact (no audit trail today; ChangeLog only covers offers/reqs) | MISSING | **P0** |
| Validation errors **surface cleanly** as a toast | PARTIAL | **P0** |
| Phone normalization on **site/contact** phones (E.164 only on Company today) | MISSING | P1 |
| **Industry** standardized (pick-list, not freeform) | MISSING | P1 |
| **Data-quality / completeness** indicator + "enrich to fill" prompts (provenance tracked but hidden) | MISSING | P1 |
| Wire ChangeLog to CRM entities → a **field-history view** | PARTIAL | P2 |
| Record-level access / role gating | MISSING | *(defer: single-user staging today; P1 when multi-user)* |
| File **attachments** on account/contact/material/offer/requirement | **DONE 2026-06-23** (this branch) | — |

---

## 2. Recommended build sequence (phased)

**Phase 0 — Quick wins (days, mostly UI):**
1. Surface the already-existing columns in the account create/edit forms (legal_name, employee_size,
   revenue_range, tier, source, phone, credit_terms, tax_id) + add `hq_street`/`hq_zip`.
2. Deep-linkable tabs (`?tab=`) + tab arrow-key nav. (Tab-switch bug already fixed.)
3. Disposition + has-open-reqs list filters.

**Phase 1 — Record fidelity (the "deep details"):** migration-backed —
first/last name split (back-compat), `parent_company_id`, `primary_contact_id`, `contact_owner_id`,
**tags** on account+contact, lifecycle stage. One CRM "fields" migration + form/UX wiring.

**Phase 2 — The daily worklist (highest rep-usability leverage):**
account/contact-level **Tasks** + explicit **next-step** + a **"My Day"** view that unions overdue
follow-ups (cadence) + due tasks + next actions. This is the single biggest "feels like a real CRM"
upgrade.

**Phase 3 — Activity completeness:** call outcome, meetings→timeline, per-activity follow-up flag,
account/site notes as a feed + pinning, compose-email-from-contact.

**Phase 4 — Power-user UX:** bulk select + bulk actions, saved views, inline edit, CSV export.

**Phase 5 — Trust & quality:** `updated_by` + CRM field-history, validation→toast polish, phone
normalization everywhere, industry pick-list, data-completeness score + enrich-to-fill, surface
provenance.

**Parallel track (own project):** Requisition deal forecasting (stage/probability/expected-close +
a pipeline dashboard) — from the earlier competitive analysis; larger and somewhat independent.

---

## 3. Notes
- The app is **single-user staging** today, so record-level access control is deferred (not a P0 now);
  it becomes P1 when multi-user.
- Many P0 items are cheap because the **data model is already richer than the UI exposes** — surfacing
  beats building.
- Each phase is independently shippable and testable; Phase 0 needs no migration.
