# Open-Items Triage — user decisions (2026-07-16)

Live log of decisions as we walk the 56 items one by one. Companion to
`2026-07-16-open-items-triage.md` (the items) and `-master-execution-roadmap.md`.

## Decisions
1. **4 built-but-hidden queues** (offer-review, follow-ups, unmatched-activity + click-to-call, cross-req buyer-leads) → **WIRE ALL 4 into nav.** One small PR each, add entry point, live-verify.

## Pending (asking one by one)
- BUILD? (9), FINISH-or-REMOVE (4), RE-ENABLE tests (3), RECOVER/WIRE/VERIFY (3), DROP (15), FIX-doc (1), + 12 first-report items.
2. **Supplier connectors** (Future/Heilind/Rochester/Verical/LCSC/FindChips) → **BUILD ONLY THE ONES TRIO USES** — user to name the suppliers; drop others.
3. **15 dead-code/dead-data items** → **VERIFY + DELETE as one cleanup PR** (confirm each truly orphaned first).
4. **3 disabled tests** (OpenAPI contract, performance-API, migration-170 index) → **RE-ENABLE ALL 3** (restore coverage; install schemathesis in CI).
5. **Proactive re-match drop** (approved-out-of-pending offers silently skipped by matching) → **FIX** (reproduce → fix + test). Covers 2 leads.
6. **QA/test-infra** → **DO ALL 3**: nightly-suite real alerting, browser E2E into CI, facet-accuracy audit harness.
7. **Remaining features**: enrichment review queue → **BUILD**; sighting scoring-weight knobs → **BUILD (wire)**; self-heal subsystem → **DROP**.
8. **Teams Q&A routing** (schema-only) → **BUILD the workflow**.
   - Auto-do cleanups (clear, no decision needed): add pagination to Proactive Matches; finish removing the abandoned offer-attribution lifecycle.
9. **4 small items** → **DO ALL**: fix Dependabot 'dependencies' label; investigate empty tag-threshold config (possible live bug); fix dead env-var toggles; recover dossier price-sanity signal from tag.

--- All 39 deep-sweep items triaged. Now the 17 first-report items. ---
10. **First-report cleanups** → **DO**: fix stale CLAUDE.md keys, add companies.account_type index, port deploy.sh rollback. **enrichment_credit_usage table → KEEP** (not dropped).
11. **Bigger security/infra** → **DO ALL 3** (plan+confirm each, higher blast-radius): webhook edge IP allowlist, concurrency fix (workers+pool+load-test), set ENCRYPTION_SALT (after canary-gap fix).
12. **Features/UI** → **DO ALL 4**: Buy Plans nav slot, CRM reporting page, QP vendor share link (needs redacted-field whitelist confirm), calendar near-real-time (delta approach).
13. **Debt** → **DO BOTH**: SQLAlchemy 2.0 lint-guard now (mass migration stays gated); revive vendor reply-ranking (data-gated — inert until email_health_score populates).

## Summary
DROPPED (only): self-heal subsystem. KEPT (not dropped): enrichment_credit_usage table.
Everything else: build / fix / wire / re-enable / delete-dead-code. ~all 56 items actioned.
Still needs user input: (a) which supplier connectors Trio uses; (b) QP share-link redacted-field whitelist.

## Corrections (during one-by-one input gathering)
- **QP vendor-share link → DROPPED (reverses decision 12).** User: the QP is INTERNAL ONLY — the vendor doesn't need it. QP items go onto the Acctivate PO; the manager approves by logging into Acctivate and checking against Avail. (Context: Acctivate is the live ERP/PO system used in the approval workflow.)

## Wave-A nav placements + product context (from one-by-one)
- **KEY CONTEXT:** the **Sightings tab is the buyers' HOME SCREEN** — where they find parts and get confirmed offers to salespeople. Buyer-facing queues belong here.
- **Offer-review queue → surface in the SIGHTINGS tab** (not the offers area). Buyers work medium-confidence offers there while confirming.
- **Follow-ups queue → SIGHTINGS tab** too (buyer sourcing loop). Both buyer queues live in Sightings.
