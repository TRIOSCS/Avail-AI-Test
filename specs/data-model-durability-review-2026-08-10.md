# Data-Model Durability Review — 2026-08-10

Scope: full-schema durability audit (naming/ERP-neutrality, normalization,
indexes/scale, migrations, conventions). 8 findings survived independent
verification at HIGH severity; 0 were refuted. Every claim below was
re-checked against source at the cited file:line.

---

## 1. Executive read

**Overall health: sound skeleton, unevenly applied.** The core spine is
right: buy_plans_v3 / buy_plan_lines carry the canonical ERP-neutral trio
(`sales_order_number`, `customer_po_number`, `po_number` —
app/models/buy_plan.py:86,87,224), good in-model patterns exist
(@validates derivation, snapshot-on-create, deleted_at tombstones), and
migrations are numbered and CI-gated. The failures are all the same
shape: **a good pattern exists, and specific tables were left out of it.**

- Sighting missed the @validates normalization remediation that
  Requirement, Offer, and VendorPartUnavailability got.
- qp_serial_entries missed the canonical-naming retirement that
  migration 164 performed on the QP header (`tso` still lives there).
- BuyPlanLine missed the identity-snapshot pattern that
  Prepayment.vendor_name was created for.
- Quote grew a structured quote_lines table but the JSON blob was never
  demoted, so both are live.

**Three themes across the 8 findings:**

1. **Deletes the database does not refuse.** Deleting a quote DB-cascades
   through the whole approved deal including PAID prepayment wire records
   (only app-level count-guards stand in the way, and one unguarded ORM
   path exists today). Deleting a requirement NULLs the part and vendor
   identity off live buy-plan lines.
2. **Two truths for one fact.** Quote lines exist as JSON and as rows;
   the SO number exists canonically on the buy plan and as free-text
   `tso` on serial entries; soft-delete exists as three different
   tombstone shapes.
3. **Safety nets that prove less than they look.** CI's full downgrade
   replay only passes with a CASCADE escape hatch production never uses;
   startup backfills self-heal missing normalized keys, masking that
   three write paths never set them.

**How it will age:** each theme compounds. Every new writer rolls the
dice on the JSON/rows split, every release widens the unproven rollback
window, every new query site is a coin-flip on the deleted_at filter.
None of these are hard to fix today; all of them get harder every month.

**2027 Dynamics 365 BC readiness: moderate.** The neutral-naming rule
held where it was applied — nothing here blocks the move. But the
mapping work will trip on exactly four things: qp_serial_entries jargon
columns (tso/customer_po/purchase_order/TPO), `companies.sf_account_id`
(the last vendor-named column, and a precedent that invites a
`dynamics_account_id` sibling), quote lines with no single canonical
source to export from, and any deal rows a CASCADE has already deleted —
reconciliation cannot recover rows that no longer exist. Fix the delete
paths first; renames second; the export-canonical question before any
mapping spec is written.

---

## 2. Findings by dimension

### 2a. Naming / ERP-neutrality (2 findings)

**N1 — qp_serial_entries re-keys ERP doc numbers under jargon names.**
app/models/quality_plan.py:255 `purchase_order`, :259 `tso`,
:260 `customer_po` — all String(255) free text (created by
alembic/versions/161_qp_native_sections.py:92,96,97). The codebase's own
comment declares the canonical home: quality_plan.py:68 "SO# is
canonical on BuyPlan.sales_order_number", and migration 164 retired the
QP header's duplicate SO column — but left the serial-entry copy behind.
Drift is mechanical, not theoretical: app/routers/quality_plans.py:509
and :533 accept `tso` as raw Form text with no validation against the
buy plan two hops up, so two independently-typed copies of one SO number
can disagree. Also: `purchasing_tpo_ship_complete` /
`purchasing_tpo_notes` (quality_plan.py:97-98) bake the undocumented
"TPO" acronym into quality_plans (never expanded anywhere in app/), and
quality_plan.py:258 `seagate_sn` hardcodes a customer name into a
column. Context: the table deliberately mirrors the old Excel serial
table (docstring, quality_plan.py:239) and TSO/TPO are house vocabulary,
not ERP vendor names — so this is a CLAUDE.md new-column-convention
miss, not a hard-rule breach.
*Fix:* one rename migration — tso→sales_order_number,
customer_po→customer_po_number, purchase_order→po_number,
purchasing_tpo_*→purchasing_po_*. Better: stop re-keying SO/customer-PO
per serial row entirely; derive both from the parent QP's buy plan and
keep only po_number per entry (serials genuinely vary by PO).

**N2 — companies.sf_account_id: the legacy CRM vendor's name in a
unique-constrained column on the core Company table.**
app/models/crm.py:105 (unique index crm.py:189; baseline migration 001,
so grandfathered, not a new-code violation). The unique constraint
structurally assumes exactly one external system per company — the exact
assumption 2027 breaks. The name also sits in the API schema layer
(app/schemas/prospect_pool.py:23), though that schema is currently
orphaned (tests only), so the leak is latent.
*Fix:* neutral pair `external_account_ref` + `external_account_source`,
unique on the pair, backfill source='salesforce_import'; update the
prospect_pool schema in the same PR. Minimum-churn version: add the
source column and freeze sf_account_id as legacy-import-only in the
model docstring.

### 2b. Normalization / relational integrity (3 findings)

**R1 — Deleting a quote DB-cascades through the whole approved deal,
including paid money records.** Root: buy_plans_v3.quote_id
ondelete=CASCADE (app/models/buy_plan.py:82; DB FK from
alembic/versions/001_initial_schema.py:2432, never altered). Chain:
buy_plan_lines CASCADE (buy_plan.py:201) → quality_plans CASCADE
(quality_plan.py:55) → qp_serial_entries CASCADE (quality_plan.py:248),
plus buy_plan_attachments (buy_plan.py:350-352) and prepayments CASCADE
(quality_plan.py:170) — which carry `wire_reference` (:205) and
`paid_amount` (:206). The only protection is app-level count-guards on
the two quote-delete routes (app/routers/htmx/quotes.py:160-170,
app/routers/crm/quotes.py:373-375). An unguarded writer exists today:
Requisition.quotes has ORM cascade="all, delete-orphan"
(app/models/sourcing.py:96) with no guard. Prepayment's own docstring
contradicts the schema: "a prepayment record outlives the line it
prepaid — audit trail" (quality_plan.py:171-173, buy_plan_line_id is
SET NULL) while buy_plan_id one line up is CASCADE. ApprovalRequest rows
survive (polymorphic subject, no FK — quality_plan.py:15-17) and end up
pointing at dead subject_ids.
*Fix:* one migration — buy_plans_v3.quote_id → SET NULL (column already
nullable); prepayments.buy_plan_id → RESTRICT. Keep the route guards as
UX; let the DB refuse the destructive path. Optionally RESTRICT
quality_plans.buy_plan_id once any QP section is reviewed-stamped.

**R2 — BuyPlanLine snapshots no part or vendor identity; one
requirement-delete leaves an amnesiac line.** BuyPlanLine
(app/models/buy_plan.py:192-278) reaches part/vendor only via
requirement_id (SET NULL, :204) and offer_id (SET NULL, :205).
delete_requirement (app/routers/requisitions/requirements.py:615-625)
hard-deletes with only an ownership check — no buy-plan guard — and the
requirement's Offers die with it via BOTH ORM cascade
(app/models/sourcing.py:173) and DB CASCADE (app/models/offers.py:35).
Result: a line in AWAITING_PO or PO_CONFIRMED keeps qty, cost, and PO
number but loses WHAT part and WHICH vendor — both identity-resolution
paths (app/services/buyplan_hub.py:66-79,
app/services/buyplan_workflow/buyplan_approval.py:737-741) resolve
exclusively through those two now-NULL FKs. The codebase already learned
this lesson once: Prepayment.vendor_name (quality_plan.py:179-182)
exists "even if the line/offer later changes."
*Fix:* (1) snapshot mpn + vendor_name (+ vendor_card_id) onto
BuyPlanLine at creation, mirroring the Prepayment pattern; (2) guard
delete_requirement against any BuyPlanLine referencing the requirement
or its offers.

**R3 — Quote lines live in two representations and neither is
complete.** The NOT NULL line_items JSON (app/models/quotes.py:37) and
structured QuoteLine rows (:97, docstring "replaces JSON line_items for
querying") are both live. Sync is convention-only:
_recalc_quote_totals (app/routers/htmx/quotes.py:57-99) rebuilds JSON
from rows and every mutation handler must remember to call it. Already
broken one way: app/services/proactive_service.py:906-922 creates quotes
with JSON and zero QuoteLine rows. Migration 027 created quote_lines
with no backfill, so legacy quotes are row-less too. Meanwhile six
readers consume only the JSON — app/services/vendor_score.py:271,400;
buyer_leaderboard.py:53; avail_score_service.py:138;
multiplier_score_service.py:76; pricing_history.py:127;
quote_send.py:246 (the emailed rows render from JSON) — and
quote_preflight.py:62-73 needs an explicit rows-then-JSON fallback. A
second hand-rolled dual-write exists outside the arbitration point
(app/routers/htmx/offers/crud.py:261-301). Concrete drift today:
pricing_history.py:138 reads material_card_id from JSON, but _recalc's
rebuild omits that key — edited quotes silently lose card-keyed price
history; and pricing_history reads rows at :51 but JSON at :127, so
proactive quotes appear in one code path and not the other.
*Fix:* make quote_lines canonical — add QuoteLine writes to the
proactive path, backfill rows from legacy JSON, repoint the six JSON
readers, then demote line_items to a nullable render-snapshot.

### 2c. Indexes / scale

No high-severity findings survived verification on this dimension.
(Targeted composite indexes like ix_sightings_mpn_vendor_norm,
app/models/sourcing.py:351, exist where matching needs them — the
problem found there is missing key VALUES, filed under Conventions C1,
not missing indexes.)

### 2d. Migrations / reversibility (1 finding)

**M1 — Multi-step rollback is unproven; the CI proof uses a CASCADE
escape hatch production never has.** CI's own comment admits it:
.github/workflows/ci.yml:231 "the chain has no clean reverse drop
ordering." The nightly full downgrade-base replay (ci.yml:526-541)
passes only because ALEMBIC_ALLOW_CASCADE=1 (ci.yml:537) makes
alembic/env.py rewrite drops as DROP TABLE ... CASCADE
(env.py:196-201) and default if_exists=True on drop_index
(env.py:151-153) — env.py:178-188's own docstring calls it the hatch
that can "silently destroy dependent FK rows." The only no-cascade
downgrade CI runs is single-step on an empty DB (ci.yml:236-252). But
production's real rollback is deploy.sh:151 `alembic downgrade
$PREV_DB_REVISION` — multi-revision whenever a deploy carries 2+
migrations (normal, per MIGRATION_NUMBERS_IN_FLIGHT.txt) — with no
ALEMBIC_ALLOW_CASCADE anywhere in deploy.sh or compose, and
deploy.sh:152's failure branch is "manual intervention required."
Precise phrasing: a 2+ revision rollback is *unproven and liable to
fail* (it fails if the window contains a mis-ordered drop), stranding
alembic_version mid-chain; forcing it through with the var deletes
dependent rows. The trap widens every release.
*Fix:* grep the chain-replay job output for
"[alembic-idempotent] CASCADE drop_table" to enumerate exactly which
downgrades drop out of dependency order; fix those downgrade()
functions to drop dependents first. Then add a CI step running
`alembic downgrade -10 && alembic upgrade head` WITHOUT the env var, so
the realistic rollback window is continuously proven under production
semantics.

### 2e. Conventions (2 findings)

**C1 — Sighting skips the in-model normalization convention; three
write paths ship NULL match keys.** Requirement (app/models/
sourcing.py:192-200), Offer (app/models/offers.py:111-118), and
VendorPartUnavailability (app/models/vendor_part_unavailability.py:110-115,
"an un-normalized write is unrepresentable") all derive normalized keys
via @validates. Sighting does not — sourcing.py:254
(vendor_name_normalized) and :258 (normalized_mpn) rely on every writer
remembering. Three already miss: app/services/sighting_ingest.py:22-49
(shortlist + quick-source) sets neither; app/routers/sources.py:166-186
(email_attachment) and app/routers/requisitions/requirements.py:1086-1104
(stock-list import) set vendor but not normalized_mpn; bonus fourth:
app/services/excess_mirror.py:255 sets mpn but never vendor. Matching is
part-number-only and keys on these columns (app/search_service.py:1985,
2203; description_service.py:62; vendor_affinity_service.py:105; index
sourcing.py:351). Caveat that caps severity: startup backfills
(app/startup.py:243-246, :875-907) self-heal the NULLs on every
non-TESTING boot — so the real exposure is rows invisible to the
matcher from write until next restart/deploy. The codebase's own comment
calls exactly that window a defect (sourcing.py:195-197: "a NULL would
hide the ask until the next startup backfill") — Sighting was simply
left out of that remediation.
*Fix:* @validates("mpn_matched") and @validates("vendor_name") on
Sighting deriving both keys (exact Requirement/Offer precedent), plus a
one-shot backfill migration. Writers keep their explicit assignments;
the hook makes the miss unrepresentable.

**C2 — Three soft-delete conventions coexist, and the timestamp one is
only spot-enforced.** MaterialCard.deleted_at
(app/models/intelligence.py:147), VendorCard.is_active
(app/models/vendors.py:49), Contact.is_archived alongside
Contact.is_active (app/models/crm.py:325,332); everything else
hard-deletes. Seven MaterialCard lookups omit the deleted_at filter —
app/services/enrichment.py:170 (then WRITES enrichment onto the deleted
card), tagging_ai_triage.py:309, vendor_affinity_service.py:57,95,352,
stock_list_ingest.py:173,185 (attaches vendor history to a deleted
card), description_service.py:42-45 — while siblings filter correctly
(datasheet_capture.py:180; search_service.py:528,1209,2513). So
"deleted" cards silently keep absorbing enrichment and stock-list
writes. Two corrections from verification: part_history_service.py:223
DOES filter (original cite was wrong), and recreation is NOT blocked on
live PG — migration 045 made the unique index partial
(WHERE deleted_at IS NULL), so resurrection happens only in the
deliberate audited get-or-create (app/search_service.py:2699-2727) and
the admin restore endpoint (app/routers/materials.py:594). Note the
side-finding: the model's unique=True (intelligence.py:30) no longer
matches the live partial index — real model-vs-prod drift.
*Fix:* make the filter unforgettable — either
with_loader_criteria(MaterialCard, deleted_at.is_(None)) on the
session, or one get_card_by_mpn(db, key, include_deleted=False) helper
that all lookup sites route through. Then standardize on deleted_at as
the single tombstone convention (it carries when-info the booleans
lack) and document/rename is_active / is_archived toward it.

---

## 3. Fix-once items that lift many tables

1. **DB-level delete policy for financial/audit records** (R1+R2): one
   migration flipping destructive CASCADEs to SET NULL / RESTRICT makes
   every current and FUTURE writer safe — route guards only protect the
   routes that remembered.
2. **@validates derivation as the house rule** (C1): extend the
   existing pattern to Sighting and the "must remember to normalize"
   class of bug becomes unrepresentable across all four match-keyed
   tables.
3. **Snapshot-on-create for identity fields** (R2): Prepayment already
   proves the pattern; applying it to BuyPlanLine sets the precedent
   for any future row that must outlive its source.
4. **One unforgettable soft-delete filter** (C2): a session-level
   with_loader_criteria or single lookup helper fixes 7+ query sites at
   once and de-risks every future one.
5. **Neutral external-ref pair** (N2): external_account_source +
   external_account_ref serves Salesforce history AND Dynamics 2027
   AND anything after — kills the one-vendor-column-per-system pattern
   before it starts.
6. **No-var downgrade window in CI** (M1): a 10-revision no-CASCADE
   round-trip continuously proves the only rollback an operator would
   actually run, for every future migration free.

---

## 4. Top-6 fix order

| # | Fix | Size | Why this order |
|---|-----|------|----------------|
| 1 | R1: quote_id → SET NULL, prepayments.buy_plan_id → RESTRICT | Small (1 migration) | **Cheap now, catastrophic later.** Paid wire records are one unguarded delete from gone (the ORM path at sourcing.py:96 exists today); deleted rows can never be reconciled at 2027 cutover. |
| 2 | R2: BuyPlanLine mpn/vendor snapshots + delete_requirement guard | Small | Corrupts LIVE purchasing state today, not history. Two small changes, proven in-house pattern. |
| 3 | C1: Sighting @validates + backfill | Very small | Exact precedent exists 3 tables over; closes the matcher-invisibility window for near-free. |
| 4 | C2: unforgettable deleted_at filter on MaterialCard | Medium | Stops deleted cards absorbing enrichment/stock writes now; every new query site is currently a coin-flip. |
| 5 | M1: fix flagged downgrades + no-var window test in CI | Small-medium | **Cheap now, costly later** — the unproven rollback window widens every single release; the bill arrives during an outage. |
| 6 | R3: quote_lines becomes canonical | Large | Biggest job, so start it last but soon: every new quote writer adds drift, and the 2027 export needs ONE canonical source. Costs grow monotonically. |

**Just behind (7/8):** the naming batch — N1 (qp_serial_entries rename
to the canonical trio + TPO) and N2 (sf_account_id neutral pair) as one
rename PR. Both are classic cheap-now-costly-later (renames get pricier
as code accrues on the names) but neither destroys data; land the batch
before any Dynamics field-mapping spec is written.
