# CRM Redesign — Relationship-Cadence Cockpit

**Created:** 2026-06-17
**Status:** Design approved (pending final user review) — supersedes the 2026-03-29 4-phase CRM roadmap
**Goal:** A cleaner, more automated CRM that helps **sales and buyers manage relationships** — the account is the single source of truth, the daily job is "who haven't I touched, and for how long," and keeping the data current is nearly invisible.

---

## 1. North Star — the rubric this design is measured against

The user's definition of a great CRM (the bar every decision below is held to):

1. **Invisible data entry** — auto-capture email/calls/meetings; enrich automatically; manual logging near zero. If keeping it current is work, it rots.
2. **Single trustworthy view** — every interaction in one timeline; no one asks "what's the latest with this account?"
3. **Fits the real process** — requisition-driven brokerage, not a SaaS lead→opp→close funnel.
4. **Surfaces next action** — who's overdue, who hasn't been followed up with, the highest-value touch today.
5. **Integrates with everything** — email, Teams, phone, enrichment as the hub of the stack.
6. **Honest real-time reporting** — accurate pipeline/forecast without spreadsheet cleanup.
7. **Fast & easy** — adoption beats features; clean, quick, intuitive.
8. **Customizable, not overwhelming** — shape it to the workflow without a six-month engagement.

**Throughline:** lower the cost of keeping good data; raise the value you get back from it.

---

## 2. Locked Decisions

1. **AVAIL replaces Salesforce/Legendary entirely** — AVAIL is the sole system of record; no sync, no split brain. The SFDC Weekly Export is a **one-time migration seed**, not an ongoing feed. Cutover ("can we turn Salesforce off?") is a milestone, gated on rollout readiness.
2. **Unified account hub** — the account page is the single source of truth. Standalone Quotes & Buy-Plans **demote** to cross-account list/report views, launched from the account.
3. **Symmetric customer/vendor hub** — both sides share one cockpit model (contacts-with-roles, two-clock cadence, unified timeline); the vendor side adds discovery on top. Role-based default: sales → Customers, sourcing → Vendors.
4. **Requisition = the opportunity** — no new "deal" object. A requisition's stage (new → sourcing → quoted → won/lost → PO) *is* the pipeline. The account rolls open requisitions into a pipeline value + honest forecast, **in Reporting — never in the daily hub.**
5. **Two-pane cockpit** — left: your owned accounts (scrollable, click-to-open, sortable); right: the full account. Identical skeleton on both sides.
6. **Hero metric = days since contact** — sortable on both panes; **no pipeline panel in the hub.**
7. **Two clocks** — `last_outbound_at` (attention given) and `last_reply_at` (real connection), shown side by side, each sortable.
8. **Cadence = tier target inside a universal envelope** — weekly ideal, 30-day hard max for *every* account; tier sets where amber begins.
9. **Contacts front-and-center** — buying roles, per-contact clocks, click-to-contact.
10. **Disciplines** — invisible auto-capture, enrich-on-create + quarterly re-verify, one owner per account, closed-loop outcomes.
11. **Scope = Foundation + full email auto-capture now**; Teams + 8x8 are a **credential-gated Phase 2**. **No outbound/Broadcast-Send engine** (deferred indefinitely).
12. **Capture maximized at every stage** — frictionless click-to-log on all channels now; full passive email now; full passive Teams/8x8 when creds land; AI grades *genuine* activity on every channel.
13. **Enrichment = Hybrid** — build a **server-side Clay connector** for contact-discovery (buying roles) + quarterly re-verify; keep **Lusha as a fast in-app direct-dial/email tier** (already partly wired). Both run through existing confidence/no-hallucination gates (98% target).

---

## 3. Information Architecture & Layout

- `/v2/crm` keeps a tab bar, now **Customers · Vendors**. The **Performance** scoreboard moves to **Reporting** (manager view), alongside the demoted pipeline/forecast.
- **Quotes & Buy-Plans** become cross-account list/report views (reachable under Reporting), launched from the account.

### Two-pane cockpit (identical skeleton both sides)

```
LEFT  — your book                    RIGHT — the open account
┌────────────────────────┐          ┌──────────────────────────────────┐
│ [My ▾] 🔍  sort:[📤/💬▾]│          │ NAME · type · tier · owner        │
│ ● Stark    🔴 31d       │   click  │ 📤 outbound 9d   💬 reply 31d ⚠   │
│ ● Wayne    🟠 18d  ───▶ │  ──────▶ │ ⚠ NEXT: <next best touch> [Log][✉]│
│ ○ Globex   🟢  2d       │          │ CONTACTS · TIMELINE · data        │
└────────────────────────┘          └──────────────────────────────────┘
```

- **Left:** owned accounts; default filter **"My accounts"** with a toggle for **All / a teammate's book**; sortable by either clock; colored by cadence state (🟢 on-target · 🟠 due · 🔴 overdue/past-30d).
- **Right:** header (name · type · **tier** · owner · **two clocks** · next-best-touch + quick actions), then **Contacts**, **unified Timeline**, account-data sections. **No pipeline panel.**
- **Vendor side:** same skeleton; right pane leads with **vendor score + parts/commodities covered**, **offer & RFQ history** as the core timeline; the existing **Find-by-Part discovery** is preserved as a left-pane mode. Same two clocks + cadence so sourcing keeps IBM/Celestica-type relationships warm.

---

## 4. The Account Record — Fields & Functions

Legend: **[KEEP]** exists · **[SURFACE]** exists but hidden today · **[NEW]** to add.

### Account header (Company)
- Name, type (Customer/Vendor) **[KEEP]**
- **Tier** — Key / Core / Standard / Prospect **[NEW]** (drives cadence; AI may suggest)
- Owner — one per account **[KEEP]** (`account_owner_id`)
- **Two clocks** (last outbound / last reply + days + cadence state) **[NEW]**
- **Tags / labels** for manual segmentation **[NEW]**; AI brand/commodity tags **[SURFACE]** (chips)
- Website, industry, HQ, employees, firmographics **[KEEP]**
- **Commercial context** — compact, secondary line: LTV / 90-day revenue / win-rate / last order **[SURFACE]** (facts, not pipeline)

### Contacts (front-and-center)
- Name, title, email, phone(s) **[KEEP]**
- **Buying role** — Specifier · Buyer/PO · AP/Payer · Logistics · Exec · Other **[NEW]**
- **Per-contact clocks** (last outbound / last reply) **[NEW]**
- **Key-contact star / primary** **[NEW]**, **do-not-contact flag** **[NEW]**
- Status, site link, LinkedIn, notes **[KEEP]/[SURFACE]**
- Functions: add/**edit** contact (**[NEW]** — no edit endpoint today), set role, set primary, mark DNC, click-to-contact

### Sites
- Address, **payment terms / credit** **[KEEP]**, primary-site flag, contacts-per-site
- **edit-site** function **[NEW]** (missing today) — via **drawer/modal**, not the page-reflow inline form

### Right-pane data sections
1. **Contacts** — grouped by site (accordion), each with role + clocks
2. **Unified Timeline** — auto + manual merged, channel icons + direction, **quality-graded** with a **hide-noise** toggle
3. **Parts they buy** **[NEW]** — part-level buy history, repeat SKUs, **EOL-exposure flags** (the moat)
4. **Quotes & deals (history)** — read view + launch the quote builder (history, not pipeline)
5. **Notes** · 6. **AI Insights** (situation/development/next-steps) **[KEEP]** · 7. **Files** (company-level attachments, reuse OneDrive pattern)

### Account-level functions
Log activity (manual fallback) · edit company/contact/site · set tier/owner/tags · mark next-best-touch done · launch quote · clone requisition · enrich / re-verify · **mark DNC** · **merge duplicates**.

**Resolved judgment calls:** commercial context = keep but compact/secondary; DNC + Merge + company Files = all in; role taxonomy = the six above. **Snooze = cut.** Next-best-touch = kept.

---

## 5. Cadence & the Two Clocks (the engine)

### Clock definitions
- **📤 Outbound / attention clock** (`last_outbound_at`) — resets on **any** logged outbound touch (email sent · call placed · Teams msg · meeting set · manual outbound log). Drives colors & sort.
- **💬 Reply / connection clock** (`last_reply_at`) — resets **only** on a **meaningful inbound**: an email reply graded *meaningful* by the AI scorer, an answered/inbound call, or an attended meeting. Voicemail · OOO · auto-reply **do not** reset it.

### Cadence: tier target inside a universal envelope

| Tier | Green (on-target) | 🟠 Amber | 🔴 Red |
|---|---|---|---|
| **Key** | ≤ 7d | 8–30d | **> 30d** |
| **Core** | ≤ 14d | 15–30d | **> 30d** |
| **Standard** | ≤ 30d | — | **> 30d** |
| **Prospect** | ≤ 30d | — | **> 30d** |

- **Red at 30 days for everyone** — the universal backstop; no account falls through.
- Default left-list sort = **stalest outbound first** (so green-but-aging accounts still surface for a weekly touch).
- Colors computed off the **outbound** clock; the reply clock rides alongside as the honesty check.
- **"Needs a touch" chip** → everything amber/red in your book.
- **Next-best-touch** — when amber/red, the header names *which* contact to reach (key/PO contact, or most-overdue) + suggested channel + one-click Log/Email.
- **Coverage badge** per account (`📧 auto · 📞 💬 manual`) so each clock is trustworthy.

---

## 6. Automation & Capture — maximizing real activity

Capture is **layered**, so the clocks are always as complete and honest as the available channels allow.

1. **Zero-friction click-to-log — all channels, Phase 1.** Every outreach action (Call · Email · Teams) logs an outbound touch on click (contact + channel + timestamp) and stamps the outbound clock. Hardened so a click **always** logs.
2. **Email — full passive, Phase 1.** Inbound webhook + outbound send-tracking + AI reply-classification already work. Add: a **synchronous send-timestamp anchor** and a **scheduled Graph-subscription renewal job**. Email drives both clocks for real on day one.
3. **Teams + 8x8 — full passive, Phase 2 (credential-gated).** When Azure Teams consent + 8x8 creds land: Teams chat + **call-direction** (build) + **meeting capture** (build); 8x8 CDR polling + **8x8 click-to-dial** (Call button places + auto-logs the call).
4. **"Real genuine activity" grading — every channel.** Expand the AI quality scorer (today email + sightings only) to **calls and Teams**, so a connected call/attended meeting counts toward the reply clock and voicemail/missed/OOO/auto do not.
5. **Honesty rail.** Coverage badge per account; **no "zero-manual" claim** until Teams meetings + 8x8 are live; frictionless click-to-log fills the gap meanwhile.

---

## 7. Enrichment — Clay (orchestrator) + Lusha (in-app tier)

**Current state:** Lusha is already partially wired into the contact-enrichment path (`customer_enrichment_service.py`); Clay has **zero** server-side integration (only the interactive claude.ai connector, which is **not available in background workers**).

**Design (Hybrid):**
- **Build a server-side Clay API connector** (worker-safe, credentialed) to drive:
  - **Relationship-map contact discovery** — *find-and-enrich-contacts-at-company* auto-populates the buying-role roster (Specifier / Buyer-PO / AP / Logistics).
  - **Quarterly re-verify / decay management** — job-change detection on a cadence (contact data rots 25–30%/yr).
- **Keep Lusha as a fast in-app direct-dial/email tier** — verified dials feed the Call button and (Phase 2) 8x8 click-to-dial.
- Both run through existing **confidence/no-hallucination gates** (98% target). **Enrich-on-create + quarterly re-verify**, fired automatically.

**Credential prerequisites:** Clay API key (new), Lusha API key (confirm — not in `.env.example`).

---

## 8. Data Model & Migrations

One migration adds:

| Change | On | Why |
|---|---|---|
| `last_outbound_at`, `last_reply_at` | Company, CustomerSite, **SiteContact**, VendorCard, VendorContact | the two clocks, account + contact level |
| `last_activity_at` | **SiteContact** (missing today) | contact-level staleness |
| `tier` (key/core/standard/prospect) | Company | cadence target |
| `role` (Specifier/Buyer-PO/AP/Logistics/Exec/Other) | SiteContact | relationship map |
| `is_key`, `do_not_contact` | SiteContact | primary contact + DNC |
| **Tag** table + account association | new | normalized, filterable segmentation (not JSON) |

**Migration discipline:** revision id **≤ 32 chars** (PG `VARCHAR(32)`; SQLite won't catch it); coordinate via `MIGRATION_NUMBERS_IN_FLIGHT.txt`; verify on **live PG** (SQLite masks PG-specific SQL).

**Write paths & materialization:**
- Direction-aware updates: outbound touch → `last_outbound_at`; meaningful inbound → `last_reply_at`. **Audit every caller of `_update_last_activity`** (webhook_service, activity_service, OOO-repair) so one clock can't clobber the other — the #1 correctness risk.
- **Nightly materialization + idempotent backfill** from historical `ActivityLog`.
- Cadence state derived from `tier` + outbound clock; **never-contacted = NULL clock** is a distinct "new / no touch yet" state, sorted as most-overdue (avoids the NULL-sort 500 class).

---

## 9. Bug Fixes Folded In

- `q.total_amount` (blank-value bug in the account Activity tab) → real Quote fields (subtotal/total_cost/won_revenue).
- Merge the two duplicate offer-reject routes.
- Add **edit-site / edit-contact** endpoints (drawer pattern).
- Enrichment **dedup guard** (sync/async race).
- Pick **`QuoteLine`** as the single quote line-item source; retire the legacy `line_items` JSON.

---

## 10. Structure, Error Handling, Testing

**Structure:** new cadence/clock logic in `crm_service` + the dedicated **CRM router** (never `htmx_views.py`). Templates reuse the macros from the recent simplify pass; HTMX+Alpine with the known static-guard rules (single-quoted attrs for `tojson`, `hx-vals` object literals, Dockerfile cache order). **Update the APP_MAP docs** after.

**Error handling (no silent failures):** capture lapses **alert, not fail silently** (Graph subscription-expiry observability); materialization + backfill idempotent; click-to-log failures surface to the user.

**Testing:** unit — clock derivation (outbound vs reply), per-tier cadence state, meaningful-grading rules, NULL-clock sort. Verify clock sort/filter queries on **live PG**. Full **xdist** suite (parallel-state regressions). Bump line-keyed static-analysis guards. **Live-drive the deployed app** after shipping (catches SQLite-masked 500s).

---

## 11. Build Sequence

**Phase 1 (now):**
1. Migration: clock columns + `tier` + `role` + Tag table + flags.
2. Direction-aware write paths + clobber audit.
3. Materialization + idempotent backfill job.
4. Cadence/clock service + helpers + NULL handling.
5. Email reliability fixes (sync send-timestamp, subscription-renewal job).
6. AI quality scoring expanded to call/Teams.
7. Two-pane UI (customer + vendor): contacts accordion (roles + clocks), unified timeline, parts-they-buy, header clocks + next-best-touch + coverage badge.
8. Functions: edit contact/site (drawer), tags, tier, DNC, merge.
9. Demote Quotes/Buy-Plans to cross-account lists; Performance → Reporting. **Coordinate with the in-flight `feat/quotes-relocation` worktree.**
10. Bug fixes (§9).
11. Enrichment: server-side Clay connector + Lusha tier; enrich-on-create + quarterly re-verify.

**Phase 2 (credential-gated):** Teams call-direction + meeting capture + 8x8 enable + click-to-dial. Flipping `mvp_mode` off also re-enables Dashboard/Analytics/Enrichment/Task-Manager → **needs explicit sign-off** before that flip.

---

## 12. External Dependencies & Risks (user-owned)

- **Azure app** with Teams scopes (Chat.Read.All / CallRecords.Read.All / OnlineMeetings) + admin consent — gates Teams capture (Phase 2).
- **8x8 Analytics API credentials** — gate 8x8 capture (Phase 2).
- **Clay API key** (new server-side connector) and **Lusha API key** (confirm present).
- **`mvp_mode` flip** has broad side-effects (re-enables Dashboard/Analytics/Enrichment/Task-Manager) — deliberate sign-off required.
- **Single-timestamp clobbering**, **email outbound-timestamp loss**, **Graph subscription silent expiry** — all addressed in §8/§6 but must be verified, not assumed.

---

## 13. Out of Scope (this redesign)

- Outbound / Broadcast-Send engine, ICP scoring, multi-touch cadences (deferred indefinitely).
- Teams/8x8 go-live (Phase 2, gated on credentials above).
- The Salesforce data migration itself (separate cutover effort, gated on rollout readiness).

---

## 14. Coordination Notes

Concurrent worktrees in flight at design time (do **not** touch; coordinate): `feat/quotes-relocation` (overlaps §11.9), `feat+auto-datasheet-capture`, `proactive-buyplan-cph-feed`, `buy-plan-audit-fixes`.
