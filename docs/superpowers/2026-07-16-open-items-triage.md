# Open-Items Triage — notes for relevance decisions (2026-07-16)

_Everything the two sweeps found, with a note + relevance question per item so you can mark keep / drop / defer. **Section 1** = the 39 deep-sweep leads (⚠️ mostly un-adversarially-verified — the verify workflow hung; I inline-verified the top cluster and flagged the rest as leads). **Section 2** = the 17 items from the first report (already adversarially verified; full detail in `2026-07-15-missing-incomplete-projects-report.md`)._

---

# Section 1 — Deep-sweep leads (39)


## A. Built but unreachable (no UI entry) — highest-value, cheapest to recover

**1. offer-review-queue-unreachable**
The global 'review queue' console for medium-confidence AI-parsed vendor offers (PENDING_REVIEW) — a purpose-built cross-requisition triage page with promote/reject actions — has no nav item, badge, or link anywhere. Users can't find it.
_✅ VERIFIED unreachable — route GET /v2/partials/offers/review-queue (crud.py:718) has a rendering template but NO nav/loader links to it._
Impact: A reviewer with APPROVE_OFFERS cannot open the built queue of all AI-flagged offers awaiting sign-off; the cross-requisition review workflow is invisible even though 451 offers cur
→ Relevance: do you still want this feature reachable? → wire nav (small) OR delete the route.

**2. follow-ups-queue-unreachable**
The 'Follow-ups' queue — chase stale RFQ email replies, with per-contact send, AI-draft, and batch-send — is a working page reachable only by typing /v2/follow-ups. A live amber count badge shows how many replies need chasing, but nothing links to the actual queue.
_✅ VERIFIED unreachable — only a count-badge on the Requisitions nav icon (mobile_nav.html:95); no click-through to the /v2/follow-ups page._
Impact: Users see 'N follow-ups to chase' on the Sales Hub icon but have no way to open the follow-up queue; the individual + batch reply-chasing workflow is effectively unreachable.
→ Relevance: do you still want this feature reachable? → wire nav (small) OR delete the route.

**3. unmatched-activity-review-and-click-to-call-unwired**
The CRM 'unmatched activities' review queue (call/email touches the system couldn't auto-attribute to a contact, so a human assigns or dismisses them) and the 8x8 click-to-call 'initiate call' action are built server-side but have no UI.
_✅ VERIFIED unreachable — /api/activities/unmatched + /api/calls/initiate have ZERO template/static consumers._
Impact: Activities that can't be auto-linked to a contact accumulate with no review surface to attribute/dismiss them, and the click-to-call capability can't be invoked from the app.
→ Relevance: do you still want this feature reachable? → wire nav (small) OR delete the route.

**4. cross-req-buyer-lead-queue-unwired**
A cross-requisition 'buyer follow-up' leads queue (all SourcingLeads filterable by buyer_status, with per-lead status transitions and thumbs-up/down feedback) is exposed as a JSON API but has no front-end; leads never leave buyer_status='new'.
_✅ VERIFIED unreachable — /api/leads/queue etc. have ZERO template/static consumers._
Impact: There is no place for a buyer to work leads across requisitions or record lead-quality feedback; the buyer_status pipeline the backend supports is inert.
→ Relevance: do you still want this feature reachable? → wire nav (small) OR delete the route.


## B. Requested but never built

**5. proactive-rematch-on-offer-approval**
Vendor offers that are still pending manager approval when the proactive matching scan runs, and are approved afterward, are silently never proactively matched to customers — the broker never sees the match and the sales lead is lost.
_State: lead (unverified). Evidence: app/services/proactive_matching.py:476-478 documents the gap verbatim ('an offer created as pending_review (excluded here) that is later approved won't be re-scanned because the watermark has already _
Impact: Silent loss of proactive sales matches for any offer that required approval — likely a large fraction, since PENDING_REVIEW is a normal initial offer state (status_machine.py:25).
→ Relevance: do you still want this built? → build OR drop from backlog.

**6. nightly-suite-crash-class-and-real-alerting**
The nightly full-test-suite run has no real alerting: a FAIL or xdist CRASH only appends an 'ALERT:' line to a per-day log file nobody watches, so a broken nightly suite rots silently. Root-causing the xdist worker-crash class was also deferred.
_State: lead (unverified). Evidence: scripts/nightly_tests.sh:70-73 — non-PASS status just `echo "ALERT: ${STATUS}" >> "$LOG_FILE"` (to /var/log/avail/nightly_tests/); no email/Slack/webhook/GH-issue anywhere. Merged PR #634 body: 'Remai_
Impact: Nightly regressions/coverage drops/worker crashes go unnoticed until someone manually reads the log; the 'alerting' is a dead write.
→ Relevance: do you still want this built? → build OR drop from backlog.

**7. enrichment-review-queue-never-wired**
The human-in-the-loop enrichment review queue (accept/reject proposed enrichment field-values) plus its batch-job tracker were fully scaffolded in the schema but never wired — no code ever writes a proposed enrichment to review, and there is no review UI.
_State: lead (unverified). Evidence: Models app/models/enrichment.py:23 (EnrichmentJob, 'Tracks bulk enrichment runs') and :50 (EnrichmentQueue, 'Pending enrichment results for review or auto-apply', 7 indexes, polymorphic vendor_card/co_
Impact: The promised 'review proposed enrichment before it is applied' workflow does not exist; enrichment either auto-applies or is dropped, with no queue for a human to accept/reject, an
→ Relevance: do you still want this built? → build OR drop from backlog.

**8. facet-audit-accuracy-harness-never-built**
The Phase-2.2 volume-weighted facet-accuracy audit harness — records a correct/wrong/unverifiable verdict per decoded material-spec facet — has its table, CHECK constraint and validator shipped, but the writer that performs the audits was never built.
_State: lead (unverified). Evidence: app/models/telemetry.py:45 FacetAudit (table facet_audits, closed verdict vocabulary + ck_facet_audits_verdict CHECK + @validates guard) created in alembic/versions/104_trust_telemetry.py. The model's_
Impact: There is no facet-accuracy audit capability; the decoded-spec-facet trust reporting the table was built to support cannot run, so facet accuracy is unmeasured despite the 55,409-ro
→ Relevance: do you still want this built? → build OR drop from backlog.

**9. browser-e2e-suite-runs-in-no-automated-context**
The entire browser/end-to-end layer — a Python pytest-playwright suite plus a separate TypeScript Playwright suite — runs in no CI job, no deploy step, and not in the nightly host suite, despite recent investment to make it deterministic and CI-ready.
_State: lead (unverified). Evidence: Two unrun suites: (1) tests/e2e/*.py (7 test files: test_app_deep, test_core_pages_render, test_deep_dive, test_navigation_smoke, test_connectors_settings_e2e, test_sightings_workspace_e2e, test_spec__
Impact: Every navigation/auth/workflow/accessibility/visual regression the browser suites were built to catch ships unguarded; the substantial deterministic-seed work is stranded and rots
→ Relevance: do you still want this built? → build OR drop from backlog.

**10. planned-supplier-connectors-never-built**
Six supplier integrations (FindChips/Supplyframe, Future Electronics, Heilind, LCSC, Rochester, Verical/Arrow Marketplace) are advertised to the user in Settings -> Connectors as 'Planned' cards but have no implementation at all.
_State: lead (unverified). Evidence: app/services/connector_service.py:67 `_PLANNED = {"findchips","future","heilind","lcsc","rochester","verical"}`; rendered as read-only 'Planned' cards via app/templates/htmx/partials/settings/_connect_
Impact: Operator sees six named supplier sources promised as 'Planned' on the Connectors page that will never search — no way to enable them, and no backlog item tracks building them.
→ Relevance: do you still want this built? → build OR drop from backlog.

**11. self-heal-subsystem-configured-but-never-built**
An autonomous self-healing / auto-diagnose subsystem (auto-diagnose issues, auto-execute low-risk fixes, file tickets against a budget, backed by a GitHub repo/token) is fully configured in the deployed staging environment but has zero implementing code in the repo.
_State: lead (unverified). Evidence: Staging /root/availai/.env:92-96 sets SELF_HEAL_ENABLED, SELF_HEAL_AUTO_DIAGNOSE, SELF_HEAL_AUTO_EXECUTE_LOW, SELF_HEAL_TICKET_BUDGET, SELF_HEAL_WEEKLY_BUDGET; :114-115 GITHUB_REPO=TRIOSCS/Avail-AI-Te_
Impact: The product owner set SELF_HEAL_ENABLED=true (and a $500 weekly / 10-ticket budget) believing an auto-diagnose/auto-fix loop is running; it is a complete no-op — no self-healing oc
→ Relevance: do you still want this built? → build OR drop from backlog.

**12. configurable-sighting-scoring-weights-never-wired**
Six scoring-weight tuning knobs (WEIGHT_RECENCY, WEIGHT_QUANTITY, WEIGHT_VENDOR_RELIABILITY, WEIGHT_DATA_COMPLETENESS, WEIGHT_SOURCE_CREDIBILITY, WEIGHT_PRICE) are set in the deployed .env to make sighting-score weights operator-tunable, but the scorer ignores them and uses a hardcoded weight table.
_State: lead (unverified). Evidence: Staging /root/availai/.env:25-30 sets all six WEIGHT_* vars. Whole-repo grep for WEIGHT_* / weight_price etc. returns 0 code matches. app/scoring.py:31 `SIGHTING_V2_WEIGHTS: dict[str,float] = {...}` i_
Impact: Changing any WEIGHT_* value on the server has no effect on sighting scores; an operator tuning them is silently ignored, and the promised 'tune the scoring model' capability doesn'
→ Relevance: do you still want this built? → build OR drop from backlog.

**13. supplier-connectors-future-heilind-rochester-verical**
Four supplier sourcing connectors the user explicitly asked to build — Future Electronics, Heilind, Rochester (direct API) and Verical (browser-worker) — were never built; they exist only as disabled placeholder rows on the Connectors page.
_State: lead (unverified). Evidence: Live DB (docker exec availai-db-1 psql) api_sources: future / heilind / rochester / verical all is_active=f, status=disabled; rendered as read-only 'planned' rows via app/services/connector_service.py_
Impact: Sourcing (the product's core function per CLAUDE.md) cannot query Future, Heilind, Rochester (an authorized EOL/obsolete distributor — genuinely distinct coverage), or the Verical
→ Relevance: do you still want this built? → build OR drop from backlog.


## C. Partially built / half-removed

**14. proactive-matches-unbounded-no-pagination**
The Proactive Matches page loads every match for the user in a single unbounded query with no pagination; the fix was explicitly deferred when the N+1 perf fixes shipped.
_State: lead (unverified). Evidence: app/services/proactive_service.py:72 `matches = query.all()` — no LIMIT/OFFSET; the page (routers/htmx/proactive.py) renders all of them. Merged PR #662 body: 'The unbounded match query (3rd sub-findi_
Impact: A power user with many open matches loads/renders the entire set on every /v2/proactive hit — slow page + memory pressure that grows with match volume.
→ Relevance: finish it OR fully remove the half? → decide direction.

**15. offer-attribution-lifecycle-half-collapsed**
The Offer "attribution lifecycle" (active → expired → converted) was decided-against and half-removed, but left in a stuck middle state: the enum was trimmed to a single value while the column, its stale 3-state comment, and a never-rendered serialization all remain — dead-data that always reads "active".
_State: lead (unverified). Evidence: app/models/offers.py:108 `attribution_status = Column(String(20), default="active")  # active, expired, converted` (stale comment promises 3 states). app/constants.py:75-78 `AttributionStatus` StrEnum_
Impact: Effectively none today (the field is never displayed), so this is latent dead-data / stale-schema, not a user-visible gap. The concrete residue: a stale inline comment on offers.py
→ Relevance: finish it OR fully remove the half? → decide direction.

**16. teams-qa-knowledge-routing-schema-only**
The Teams knowledge Q&A 'routing / delivery / nudge' workflow (deliver a question, nudge for an answer, record how it was answered, enforce a daily-question cap) shipped its DB schema in one migration but the application logic and its config were never built/wired.
_State: lead (unverified). Evidence: alembic/versions/064_teams_qa_routing.py:26-28 adds knowledge_entries.{nudged_at, delivered_at, answered_via}; :35-45 creates knowledge_config and seeds ('daily_question_cap','10'). app/models/knowled_
Impact: Knowledge questions are never tracked as delivered/nudged, 'answered via' is never recorded, and the daily-question cap is unenforced; the intended Teams Q&A routing behaviour is s
→ Relevance: finish it OR fully remove the half? → decide direction.

**17. proactive-approved-offer-not-rematched**
Proactive matching silently drops vendor offers that were approved *after* landing in pending_review — they never surface as customer matches on the Proactive tab.
_State: lead (unverified). Evidence: app/services/proactive_matching.py run_proactive_scan (def ~L466) gates to _LIVE_STATUSES=[ACTIVE, APPROVED] but advances a persisted SystemConfig watermark past each offer's created_at; the in-code c_
Impact: A vendor offer that a buyer reviews and approves (the normal happy path for mined/unsolicited offers) is silently never matched to open customer requirements — the exact buying opp
→ Relevance: finish it OR fully remove the half? → decide direction.


## D. Built but not on main

**18. dossier-price-sanity-market-baseline-strip**
Part-dossier price-sanity signal — the market panel used to compute a market-median baseline and flag price outliers (offers ~20x+ above the fresh-result median), so buyers were warned when an offer's price was wildly out of line. It was built, shipped, then reverted and never re-landed.
_State: lead (unverified). Evidence: Removed from main by commit 7804d47b (PR #401, 'revert(dossier): remove market-baseline strip (price-sanity dropped — data trust)'; branch commit 478f0106) — merge-base confirms 7804d47b IS on main, i_
Impact: The part dossier surfaces no automatic price-sanity warning. When a supplier offer is priced far out of line with the market (e.g. an order of magnitude above the median of fresh r
→ Relevance: recover the work from the tag OR abandon?


## E. Dead code (candidates to delete)

**19. buyplan-pre-c1-approve-fallback-cleanup**
The buy-plan approve/reject route keeps a legacy pre-C1 fallback (approve_buy_plan) that its own comment says will be 'removed in a follow-up once no pre-C1 plans remain'; that cleanup never happened.
_State: lead (unverified). Evidence: app/routers/htmx/buy_plans.py:705-707 ('If NO open request exists ... we fall back to the legacy approve_buy_plan and log a WARNING (RISK 3, transition window; the fallback is removed in a follow-up o_
Impact: Two divergent approval code paths (engine decide vs legacy approve_buy_plan) coexist; the legacy path bypasses the engine's atomic side-effect guarantees and only logs a WARNING, a
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**20. sync-log-admin-viewer-read-never-written**
The admin 'sync logs' viewer endpoint reads a table that nothing ever writes and that no UI ever calls — a dead observability surface that always returns an empty list.
_State: lead (unverified). Evidence: app/models/sync.py:11 SyncLog (table sync_logs). Admin endpoint app/routers/crm/enrichment.py:438 GET /api/admin/sync-logs queries db.query(SyncLog) (:448). Repo-wide there is NO writer of SyncLog (no_
Impact: An admin who hits /api/admin/sync-logs always sees [] regardless of sync activity; there is no working sync-history view.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**21. intel-cache-orm-model-redundant**
The IntelCache ORM model is dead — the intel_cache table is written and read exclusively via raw SQL, so the mapped class is a redundant, never-instantiated, never-queried definition.
_State: lead (unverified). Evidence: app/models/enrichment.py:165 defines the IntelCache ORM model (table intel_cache). All actual access is raw SQL: app/cache/intel_cache.py:133 (INSERT INTO intel_cache) and :248 (delete), app/cache/dec_
Impact: None functional; it is dead-code clutter that misleads readers into thinking IntelCache is accessed via the ORM and adds a maintenance/drift surface.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**22. legacy-proactive-json-router-dead**
The entire legacy /api/proactive/* JSON API router (matches, count, offers, scorecard, send, refresh, dismiss, do-not-offer, convert, contacts, draft) is still mounted but fully superseded by the HTMX proactive router; nothing calls it.
_State: lead (unverified). Evidence: app/routers/proactive.py (router at :26, 11 endpoints from :29) is registered at app/main.py:826/854. The live Proactive UI uses /v2/partials/proactive/* and /v2/proactive/* from app/routers/htmx/proa_
Impact: None functionally (dead code), but the app publicly serves 11 duplicate authenticated endpoints that widen attack surface and mislead maintainers into thinking there are two proact
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**23. legacy-tags-rest-api-dead**
The /api/tags/* tag REST API (list all tags, entity tags, material-card tags, tag→entities) has no consumer; tag display/edit in the UI goes through the HTMX company-tag partials instead.
_State: lead (unverified). Evidence: app/routers/tags.py routes at :24 GET /api/tags/, :51 /{tag_id}/entities, :88 /entities/{type}/{id}, :120 /material-cards/{id}. grep across app/templates + app/static/*.js + app/**/*.py (excl the rout_
Impact: Dead code: 4 authenticated read endpoints exposing tag/entity relationships with no caller.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**24. legacy-ai-freeform-endpoints-dead**
A cluster of AI JSON endpoints in ai.py (draft-rfq, parse/apply-freeform-rfq, parse-freeform-offer, save-freeform-offers, company-intel, find-contacts, prospect-contacts list/save/promote/delete, parse-response/{id}) is superseded by the HTMX paste/parse and vendor find-contacts flows and is called by nothing.
_State: lead (unverified). Evidence: app/routers/ai.py endpoints /api/ai/{draft-rfq, parse-freeform-rfq, apply-freeform-rfq, parse-freeform-offer, save-freeform-offers, company-intel, find-contacts, prospect-contacts…, parse-response/{id_
Impact: Dead code: ~10 authenticated AI endpoints (some invoking Claude/enrichment) with no caller — cost/attack-surface risk and maintainer confusion.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**25. activity-timeline-json-parallels-dead**
The JSON timeline endpoints /api/activity/account/{id}, /api/activity/contact/{id}, and /api/activity/vendors/{id}/last-call are dead parallels of the server-rendered Activity tab / vendor card and are called by nothing.
_State: lead (unverified). Evidence: app/routers/activity.py:321 get_account_timeline_endpoint, :356 get_contact_timeline_endpoint, :439 get_vendor_last_call. grep templates+static for 'activity/account','activity/contact','/last-call' =_
Impact: Dead code: 3 authenticated endpoints returning per-account/contact/vendor activity with no consumer.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**26. misc-legacy-json-parallels-and-unwired-triggers**
A tail of individually-orphaned endpoints left mounted after HTMX/feature reworks or never wired to a control: an unwired OneDrive file-picker, a duplicate-company checker superseded by entity-specific ones, the /api/error-reports alias, manual email-mining triggers, materials bulk ops, and several JSON parallels.
_State: lead (unverified). Evidence: All verified 0 UI refs (templates+static) and non-webhook/non-OAuth: app/routers/crm/offers.py browse_onedrive (/api/onedrive/browse — 'Browse user's OneDrive files for the picker', no picker UI exist_
Impact: Mostly dead code (attack surface + maintainer confusion); the OneDrive picker is a partially-built feature (backend browse endpoint with no picker UI).
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**27. stale-ignore-refs-to-deleted-test_browser_e2e**
Two test-config files still special-case a Playwright test file that no longer exists in the repo.
_State: lead (unverified). Evidence: pytest.ini addopts includes `--ignore=tests/test_browser_e2e.py` and scripts/ci_shard.py:38 has EXCLUDED_FILE_NAMES = {"test_browser_e2e.py"}, but `ls tests/test_browser_e2e.py` -> 'No such file or di_
Impact: None functional; a small maintenance smell that misleads a reader into thinking an active browser-e2e file is being deliberately excluded.
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.

**28. redundant-dead-supplier-key-settings-fields**
Three supplier API-key Settings fields exist in config.py but are never read; the connectors resolve those keys exclusively through the credential store / env, so the Settings fields are dead.
_State: lead (unverified). Evidence: app/config.py:96/99/102 declare oemsecrets_api_key, sourcengine_api_key, element14_api_key. Zero `settings.<field>` / `getattr(settings, ...)` reads anywhere (grep returns 0 for each). The connectors _
Impact: Benign but misleading: the fields imply these keys flow through Settings when they do not; low-risk config drift that a future reader can misuse (setting settings.* expecting it to
→ Relevance: safe to delete (removes confusion/attack surface)? → almost always yes unless you plan to revive.


## F. Dead data layer (columns/tables never used)

**29. dead-site-contact-owner-column-and-index**
site_contacts.contact_owner_id is a dead column (contact ownership was moved to site -> account owner) documented as always-NULL and 'retained to avoid a migration (Phase 1 cleanup)'; the column, its index, and its relationship were never dropped.
_State: lead (unverified). Evidence: app/models/crm.py:307-309 ('DEPRECATED / UNUSED ... Column is retained to avoid a migration; it will always be NULL for new contacts'). Still carries a relationship (crm.py:361) and a dedicated index _
Impact: Dead column + unused index on a live table; a decision-gated cleanup (drop vs formally keep) that never reached the user, so it never resurfaces on its own.
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.

**30. buyer-vendor-stats-no-populator**
The per-buyer x per-vendor performance stats table (RFQs sent, response rate, offers won, win rate, avg response hours) is documented as 'Auto-populated' but no code ever populates or reads it — a dead table and a stale docstring claim.
_State: lead (unverified). Evidence: app/models/performance.py:267 BuyerVendorStats with docstring line 270 'Auto-populated.' (table buyer_vendor_stats, unique (user_id, vendor_card_id) + 3 indexes), created in alembic/versions/001_initi_
Impact: Per-buyer/vendor relationship analytics (which buyer works best with which vendor, personal win-rate/response-time) are unavailable; the leaderboard/affinity story that would use t
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.

**31. vendor-quality-metrics-decorative-columns**
A cluster of vendor delivery-quality metric columns (on-time delivery, RMA rate, quote accuracy, lead-time accuracy) were schema-designed on the vendor snapshot and vendor card but the computation was never implemented — the columns are never written and never read.
_State: lead (unverified). Evidence: app/models/performance.py:30-35 VendorMetricsSnapshot defines quote_accuracy, on_time_delivery, rma_rate, lead_time_accuracy. The SOLE writer app/services/vendor_scorecard.py:293-307 sets response_rat_
Impact: Vendor scorecards can never surface on-time-delivery %, RMA rate, quote accuracy, or lead-time accuracy (and always show 0 POs-in-window), so the vendor performance picture is perm
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.

**32. tag-threshold-config-seed-missing-on-live**
The tag-visibility threshold config table is seeded by migrations but is empty on the deployed DB, which makes the entity-tag two-gate visibility system force every non-segment tag to hidden.
_State: lead (unverified). Evidence: Seed rows are inserted unconditionally by alembic/versions/042_add_tagging_tables.py:152,165 (op.bulk_insert) and again by 046_fix_threshold_entity_types.py:33; live alembic head is 189 (well past bot_
Impact: Brand/commodity tags on companies, vendors and sites can NEVER become visible (the gate always evaluates to hidden with an empty config), so the entity-tag visibility feature is ef
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.

**33. enrichment-runs-legacy-orphan-table**
The legacy autonomous-enrichment orchestrator's run-state table survives with orphaned rows after the orchestrator was deleted — nothing writes or reads it now; it awaits a drop decision that was never routed (the exact twin of the tracked enrichment_credit_usage item).
_State: lead (unverified). Evidence: app/models/enrichment_run.py:1-17 docstring states the orchestrator (scripts/enrich_orchestrator.py + enrichment-entrypoint.sh) 'was removed... so nothing writes these rows anymore' and the model is '_
Impact: None functional (dead legacy data); it is schema-drift residue carrying stale rows that should be surfaced as a one-line drop/keep decision alongside enrichment_credit_usage.
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.

**34. dead-schema-columns-enums-decide-then-drop**
A scoped 'decide-then-drop' cleanup for dead DB columns and dead enum members was planned but never routed to the user or executed.
_State: lead (unverified). Evidence: Dead columns still present on main: app/models/prospect_account.py:59 ProspectAccount.import_priority, app/models/crm.py:101 Company.import_priority, app/models/excess.py:115 ExcessLineItem.demand_mat_
Impact: No direct user-facing impact (internal cleanup), but the dead columns/enums persist as schema noise that the drift gate and any future migration-from-scratch DB rebuild must keep c
→ Relevance: drop the column/table OR was it for a planned feature? → decide-then-drop.


## G. Disabled / skipped tests

**35. zombie-performance-api-tests-masked-by-mvp-skip**
The buyer/sales performance scoring HTTP layer (Avail Score, Multiplier Score, bonus winners) — its API endpoints were deleted in March but the scores are still computed nightly and are now unviewable, while the endpoint tests that would catch this are silently skipped by a misleading gate.
_State: lead (unverified). Evidence: tests/test_avail_score.py:689 (TestAvailScoreAPI) and tests/test_multiplier_score.py:673 (TestMultiplierAPI) assert GET /api/performance/avail-scores and /api/performance/multiplier-scores return 200._
Impact: Buyer/sales Avail-Score, Multiplier-Score and bonus-winner data (money/comp-adjacent) is computed and stored every month but no page or API can display it; the two test classes tha
→ Relevance: re-enable (restores coverage) OR delete the dead test? → usually re-enable.

**36. openapi-contract-validation-silently-skipped-schemathesis-4x**
The OpenAPI contract test — the only check that API responses actually conform to their declared schemas — silently skips on every run because it was written for the schemathesis 3.x API but the pinned dependency is 4.x.
_State: lead (unverified). Evidence: tests/test_contract.py:53-60: test_contract_health branches on hasattr(schemathesis,'from_asgi') / hasattr(schemathesis,'from_dict') and otherwise hits pytest.skip('schemathesis version lacks from_asg_
Impact: API response/schema drift (a 200 whose body no longer matches its declared shape) is completely unguarded; the safety net reads green while asserting nothing. This is separate from
→ Relevance: re-enable (restores coverage) OR delete the dead test? → usually re-enable.

**37. migration-170-pg-trgm-index-test-never-runs**
The test that verifies migration 170 actually created the prospecting pg_trgm GIN indexes (warm-intro search) is gated on an env var that is set nowhere, so it never executes in any environment.
_State: lead (unverified). Evidence: tests/test_prospect_buyer_ready_persistence.py:126 test_warm_intro_trgm_indexes_exist_after_migration is skipif(not PROSPECTING_TRGM_TEST_DB_URL). A repo-wide grep (excluding .venv) finds PROSPECTING__
Impact: Low direct risk (backstopped by the drift gate), but it is a dead migration-verification test giving false comfort; a future migration that dropped/altered these prospecting search
→ Relevance: re-enable (restores coverage) OR delete the dead test? → usually re-enable.


## H. Config present but unwired

**38. dependabot-dependencies-label-never-created**
Dependabot is configured to tag every dependency-update PR with a `dependencies` label, but that label was never created in the repo — so the tag is silently applied to nothing and Dependabot prints a config error on every PR it opens.
_State: lead (unverified). Evidence: .github/dependabot.yml declares `labels: [dependencies]` on all three update groups (lines 10, 49, 77). The repo's label set (gh label list) contains bug/documentation/duplicate/enhancement/good first_
Impact: Low. No security/production bumps were dropped — dependency updates still merge. Impact is: (1) a recurring config-error comment on every dependabot PR (noise), and (2) any label-b
→ Relevance: wire the flag/feature OR remove the dead config?


## I. Stale claims (doc/config only)

**39. dead-behavior-toggle-env-vars-footgun**
Several feature/behavior toggles that read as active are silently ignored: the installer bakes three 'Behavior' knobs into every provisioned .env, and the staging .env sets four feature flags — none of which any code reads (config uses extra='ignore').
_State: lead (unverified). Evidence: scripts/bootstrap-server.sh:180-182 writes OUTREACH_COOLDOWN_DAYS=30, POLL_INTERVAL_MINUTES=5, AUTO_SIGHTING_CONFIDENCE=0.7 into the generated .env; grep finds no consumer for any of the three (POLL_I_
Impact: Operator-facing footgun: flipping DEEP_ENRICHMENT_ENABLED, MATERIAL_ENRICHMENT_ENABLED, AUTO_SIGHTING_CONFIDENCE, OUTREACH_COOLDOWN_DAYS, etc. changes nothing, giving a false sense
→ Relevance: fix the doc/config (cheap) — no product call needed.

---

# Section 2 — First-report items (17, already adversarially verified)

_Full detail in `2026-07-15-missing-incomplete-projects-report.md`. Five are already being fixed this session (PRs noted)._

## Security / launch
- **Public `/docs` `/redoc` `/openapi.json` unauth** — ✅ being fixed in **PR #742**. Relevance: confirm you want docs off in prod.
- **Global rate limit was dead code** — ✅ being fixed in **PR #743**.
- **Demoted admins re-promoted on login** — ✅ being fixed in **PR #744** (migration 190).
- **Password-login fail-boot guard (blocker #1)** — ✅ being fixed in **PR #745**.
- **Graph-webhook edge IP allowlist (HIGH-SEC-4)** — not built (Caddy matcher). Relevance: needed before multi-user go-live? (careful: don't lock out Microsoft IPs).
- **`ENCRYPTION_SALT` unset on staging** — ops secret; legacy static salt fallback. Relevance: set it (defense-in-depth) or accept the risk? (blocker: rotation script skips the system_config canary).
- **Single uvicorn worker + sync ORM** — app serializes under load; never triaged. Relevance: fix now (bump workers + pool) or accept for single-user staging?

## Requested features (never finished)
- **CRM honest-reporting page** — forecast engine built, the 3 rollups deleted, no route/nav. Relevance: build the Reporting destination, or formally drop Phase-5?
- **Vendor-facing QP share link** — spec deleted in #585 (was held, not shipped); hold-gate now cleared. Relevance: still want a revocable redacted share link?
- **Near-real-time calendar webhooks (Phase-4)** — built, lives only on archive tags (ba7ef84d). Relevance: webhooks vs. the simpler calendar-delta poll — which approach?
- **Buy Plans hub has no bottom-nav slot** — reachable only via Approvals. Relevance: dedicated nav item / in-page link / under "More"?

## Cleanup / debt
- **SQLAlchemy 1.x→2.0 migration** (~1,559 `db.query`) — XL, gated. Relevance: land the lint-guard now; schedule the mass migration?
- **`deploy.sh` has no rollback** on failed health check. Relevance: port the `deploy.yml` rollback? (low-risk yes).
- **Drop empty `enrichment_credit_usage` table** — decision-gated (drift gate protects it). Relevance: keep or drop?
- **`companies.account_type` missing index** — actively filtered, no index. Relevance: add index (cheap yes)?
- **Vendor reply-ranking (PR #444, closed unmerged)** — data-gated (email_health_score 0/1193 populated). Relevance: revive when data exists, or drop?
- **Stale CLAUDE.md config keys** (AZURE_REDIRECT_URI etc. don't exist) — doc-only footgun. Relevance: just fix (no product call).
