# CRM Redesign — Remaining Work, Phased Roadmap

**Created:** 2026-06-18
**Status:** Active roadmap. **(2026-06-29 note:** many listed items have since shipped — cross-check current code / this session's crash-recovery audit before starting. Known still-open: Phase 5 Reporting page (forecast engine built but unwired — see `docs/superpowers/plans/2026-06-18-crm-phase5b-pipeline-forecast.md`), AI-suggest-a-tier.)**
**Parent spec:** `docs/superpowers/specs/2026-06-17-crm-redesign-design.md` (the approved design — locked decisions live there)

## Where we are

**Shipped + live on staging (2026-06-18):**
- **Plan 1 — Cadence Data Foundation** (migration `110`): two clocks (`last_outbound_at`/`last_reply_at`) + `tier` on Company/Site/Contact/Vendor, `cadence_state`, materialization, real-time forward-only bump, nightly recompute + backfill, stalest sort.
- **Plan 3 — Two-pane Cockpit UI** (P3-1…P3-5): cadence call-list (left), account-detail cadence hero + commercial strip, contacts accordion + roles + per-contact clocks, unified activity timeline (+ `q.total_amount` bug fix), vendor cadence mirror.
- **Capture-reliability #1**: Graph subscription survives transient failures.
- **Reqs → Sales Hub** rename.

**The north-star rubric (from the parent spec) is unchanged:** invisible capture · single trustworthy view · fits the requisition-driven process · surfaces next action · integrates with everything · honest real-time reporting · fast & easy · customizable not overwhelming. AVAIL replaces Salesforce.

This roadmap sequences everything **left**. Each phase is delivered the same way Plan 1/Plan 3 were: its own spec→plan→subagent-TDD-build→review→merge→deploy cycle. Phases are ordered by **value × independence × (no external credential dependency)**.

---

## Phase 1 — Capture Completeness *(do next; mostly no external creds)*

**Goal:** make the two clocks **honest and complete** for the channels we already have, and close the known data-attribution gap. This is the highest-leverage next work because the entire cockpit's value rests on the clocks being trustworthy.

**Scope:**
1. **Contact-level email attribution** *(unblocks the cockpit's open caveat)* — inbound/outbound email currently matches by email-string and sets `company_id`/`customer_site_id` but **not** `site_contact_id`, so per-contact reply clocks stay NULL for email. Resolve the matched email to a `SiteContact` (by verified email) and set `site_contact_id`, so per-contact reply clocks populate from real email, not just click-to-contact.
2. **Send-time outbound clock** (probe #7) — write the outbound `ActivityLog` synchronously when `send_batch_rfq` sendMail succeeds (currently the outbound clock is up to 30 min late and lost if the sent-folder scan never matches). Coordinate dedup with the 30-min `scan_sent_folder` writer via an idempotency key. *(Now unblocked — Plan 1's clocks/bump are on main.)*
3. **`Contact.sent_at` column** (probe #8) — durable true send-time; migration.
4. **Subscription-health observability** (probe #2/#3/#4) — `GraphSubscription` health columns (`last_renewed_at`, `renew_fail_count`, `last_error`) + migration; surface repeated failures to `User.m365_error_reason`; a read-only subscription-health endpoint (+ optional cockpit badge).
5. **Sent-folder recovery** (probe #5/#6) — a job that backfills NULL `graph_message_id` for recent Contacts (re-runs the sent lookup once the message surfaces via delta), restoring Tier-1 conversation-id reply matching; widen the `_find_sent_message` `$top` window.
6. **AI grading for call + Teams** — expand `_AI_SCORED_TYPES` (or rule-based meaningful-classification) to `call_logged`/`teams_message` so a connected call / attended meeting feeds the **reply** clock honestly on those channels (today only email is graded).

**Dependencies:** none external (all Graph-mail + activity layer we already run). **Migrations:** yes (`Contact.sent_at`, subscription-health columns) — coordinate numbers via `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
**Value:** High — honest clocks across channels + per-contact email attribution. **Effort:** Medium.
**Open decisions:** the dedup idempotency key for send-time vs scan-folder writes; the "meaningful call" rule (connected + min-duration vs AI).

---

## Phase 2 — Account Governance & Functions *(Plan 4; high daily-use value, no external deps)*

**Goal:** the fields & functions that make reps trust and shape the CRM — "customizable but not overwhelming."

**Scope:**
- **Tags / segmentation** — reuse the existing `Tag` + `EntityTag` infrastructure (tag_type e.g. `segment`, manual = always-visible) for account labels; expose in the left-list filter alongside the AI brand/commodity tags. Drives "OEM accounts", "at-risk", ICP slices.
- **Account tier UI** — set/change `tier` (the cadence target driver) per account; AI-suggest a tier.
- **Buying-role taxonomy** — extend `SiteContact.contact_role` to the relationship-map taxonomy (Specifier · Buyer/PO · AP/Payer · Logistics · Exec · Other); enrichment can infer it. (Cockpit P3-3 already displays the existing role; this enriches the values.)
- **Inline edit** — edit-site / edit-contact endpoints (missing today) via a drawer/modal pattern (not page-reflow); edit company fields.
- **Do-not-contact flag** (SiteContact) — surfaced + respected by outreach.
- **Merge duplicates** — account + contact dedup (one-record-per-entity governance).

**Dependencies:** Phase 1's role grading helps populate roles. **Migrations:** likely (DNC flag, tag association, role values). **Value:** High. **Effort:** Medium-High.
**Open decisions:** tag model shape (reuse EntityTag vs lightweight label table); merge-conflict resolution UX.

---

## Phase 3 — Deal Consolidation *(Plan 5; unifies the account = the single-source-of-truth payoff)*

**Goal:** make the account the one place for the whole relationship — bring Quotes & Buy-Plans into the account workspace; demote the standalone top-level pages to cross-account list/report views.

**Scope:**
- Account "Deals" surface: the account's **requisitions-as-opportunities** rollup (req → sourcing → quoted → won/lost → PO) + quotes + buy-plans, launched/viewed from the account.
- Demote `/v2/quotes` & `/v2/buy-plans` from primary nav to cross-account list/report destinations.
- **Coordinate with the in-flight `feat/quotes-relocation` work** (check if still active; align rather than collide).

**Dependencies:** coordinate with concurrent quotes-relocation; reuses the existing quote/buy-plan builders (launched from the account). **Migrations:** unlikely. **Value:** High (the #1 "single view" trait). **Effort:** Medium-High.
**Open decisions:** how much of the heavy quote/buy-plan authoring moves *into* the account vs launches from it; nav reorganization specifics.

---

## Phase 4 — Enrichment Hybrid *(Plan 6; gated on credentials you provide)*

**Goal:** keep account/contact data fresh and the buying-role roster populated — "lower the cost of keeping good data."

**Scope:**
- **Server-side Clay connector** (worker-safe — NOT the interactive claude.ai connector) for contact-discovery (auto-populate buying-role contacts) + **quarterly re-verify** (job-change/decay detection).
- **Lusha** kept as a fast in-app direct-dial/email tier (already partly wired); verified dials feed click-to-call.
- Enrich-on-create + quarterly re-verify cadence; all through the existing 98%-confidence / no-hallucination gates.

**Dependencies / external prerequisites:** **Clay API key** (new) + **Lusha API key** (confirm present). **Migrations:** maybe (enrichment provenance). **Value:** High (data freshness + relationship-map roster). **Effort:** Medium (Clay connector is a real but small build).
**Open decisions:** Clay orchestration boundary (already decided hybrid: Clay orchestrates discovery + re-verify, Lusha in-app tier).

---

## Phase 5 — Reporting *(manager view; honest forecast + performance)*

**Goal:** "honest, real-time reporting" — the trait we deferred out of the daily hub.

**Scope:**
- **Performance dashboard** (relocated out of the CRM shell): team & individual coverage / quality / responsiveness / outcome — now feedable by the real cadence + quality-graded activity data that's live.
- **Pipeline / forecast** — requisition-as-opportunity rollup (value × win-probability), account & team level, honest and real-time. Lives in Reporting, **never in the daily hub** (locked decision).
- Outcome correlation: interactions → RFQs → quotes → orders.

**Dependencies:** the cadence/activity data (live) + Phase 1's complete capture (for honest coverage/responsiveness). **Migrations:** maybe (materialized metrics). **Value:** Medium-High (management visibility). **Effort:** Medium.

---

## Phase 6 — Teams + 8x8 Auto-Capture *(credential-gated; the original spec's Phase 2)*

**Goal:** remove the last manual step — full passive capture on phone + Teams, so "zero manual logging" becomes true.

**Scope:**
- **Teams:** Azure admin-consent for Chat/CallRecords/OnlineMeetings scopes; flip `mvp_mode` off **(needs deliberate sign-off — it also re-enables Dashboard/Analytics/Enrichment/Task-Manager)**; build the two genuinely-missing pieces — call-direction detection (`/callRecords/{id}/sessions`) and meeting capture (`onlineMeetings` participants/duration, the strongest "real reply" signal).
- **8x8:** provision Analytics API creds + `eight_by_eight_enabled=true`; optional click-to-dial (Lusha dials → 8x8 places + auto-logs).

**Dependencies / external prerequisites:** **Azure Teams admin consent**, **8x8 Analytics creds**, the **`mvp_mode` flip sign-off**. **Value:** High (true zero-manual) but **blocked on external credentials you control**. **Effort:** Medium-High. Defer until creds are available so the build is testable against real data.

---

## Milestone — Salesforce Cutover *(gated on rollout readiness)*

**Goal:** turn Salesforce/Legendary off; AVAIL is the sole system of record.

**Scope:** one-time migration seed from the **SFDC Weekly Export** (accounts, contacts, material/inventory history); a "can we turn Salesforce off?" readiness bar (the above phases give AVAIL enough to replace what the team uses SFDC for). Tracked in `docs/PRE_ROLLOUT_CHECKLIST.md`.

**Dependencies:** Phases 1–5 feature-complete enough to replace SFDC's actual usage; rollout readiness (no near-term date per current state — don't sequence other work around it).

---

## Recommended order & rationale

1. **Phase 1 (Capture Completeness)** — unblocks honest clocks + per-contact email attribution (the cockpit's open caveat); no external deps. **Start here.**
2. **Phase 2 (Governance & Functions)** — highest daily-use value; no external deps.
3. **Phase 3 (Deal Consolidation)** — the single-source-of-truth payoff; coordinate with concurrent quotes-relocation.
4. **Phase 4 (Enrichment Hybrid)** — when Clay/Lusha keys are provisioned (can run in parallel with 2/3 once keyed).
5. **Phase 5 (Reporting)** — after capture is complete (honest inputs).
6. **Phase 6 (Teams + 8x8)** — when Azure consent + 8x8 creds + the `mvp_mode` sign-off land.
- **Cutover** — when 1–5 make AVAIL a full SFDC replacement.

**Cross-cutting discipline (every phase):** TDD subagent build + adversarial review + GitHub-CI-gated merge; migration-number coordination via `MIGRATION_NUMBERS_IN_FLIGHT.txt`; verify DB-specific queries on live PG; update `docs/APP_MAP*` after each phase.
