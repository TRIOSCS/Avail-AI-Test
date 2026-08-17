# QC audit mediums — verified worklist (2026-08-13 triage)

Triaged 82 findings vs current main: 44 open+safe, 17 open+gated, 21 already-fixed/not-found.

## Synthesis (ranked)

# QC Triage — Deduped, Ranked Implementation Worklist

**Input:** 44 findings, all gated `safe_autofix`. **After dedupe: 42 items** (two merges). No finding refuted. Ranked by value/risk ratio: user-facing correctness, security, confidentiality, and money integrity with mechanical fixes rank highest; subtle concurrency refactors and pure test-coverage rank lowest.

## Merges applied
- **M1** = `stocklist-midloop-rollback` + `invjobs-nullfk-rollback` — identical bug (mid-loop `db.rollback()` nukes the whole batch) and identical fix (`begin_nested()` SAVEPOINT + re-query). Two files, one change pattern, fix together: `stock_list_ingest.py:184` + `jobs/inventory_jobs.py:376`.
- **M2** = `oq-02-api-revise-no-lines-no-membership` + `oq-03-put-update-no-quoteline-sync` — same file, same root concern (legacy JSON quote path doesn't maintain the structured `QuoteLine`/membership the HTMX path keeps in sync). `crm/quotes.py:51` (revise) + `crm/quotes.py:341` (PUT). Both have **no live frontend caller**.

## Cross-cutting through-lines (bundle into shared PRs)
- **Claim-before-send / delete-in-flight (proactive):** #1, #2 share one pattern; do them as a pair — but see *Regate* below.
- **Watermark loss on 8x8 poll:** #28 (unbounded scan) + #41 (advance-on-failure) are the same file — one PR.
- **Event-loop offload vs cross-thread session:** #27 wants to *add* `run_in_executor(None, fn, db)`, but #42 says that exact "pass the job's Session into the executor" pattern is a bug (rollback/close races the worker thread). **Fix #42's pattern first (session created *inside* the executor fn), then apply #27 using the corrected pattern** — otherwise #27 reproduces #42.
- **Provenance ladder (enrichment):** #37, #38, #39, #40 all touch MaterialCard provenance; coordinate so they don't fight over `enrichment_provenance` merge semantics.
- **Possibly-dead paths → confirm callers, prefer DELETE over fix:** #11 (JSON prepay create), M2 (#17/#18 JSON quote routes), #16 (test-only helper). Cheaper and safer to remove than to mirror logic into an endpoint nobody calls.

## REGATE — NOT truly safe_autofix (need review/design gate, not blind auto-apply)
1. **`dc-buyplan-submit-no-lock` (#25)** — the sketch's own caveat kills the naive fix: `.with_for_update()` on the existing `joinedload(BuyPlan.lines)` query **errors on PG** (FOR UPDATE against the nullable outer-join side). Requires a *separate id-only locking SELECT* — a blind autofix ships a 500. Regate to reviewed.
2. **`pa-send-draft-no-atomic-claim` (#1)** — claim-before-send changes send semantics; crash-after-claim leaves rows SENT; conf 0.62. Delicate ordering, PG-only test. Review.
3. **`pa-digest-generate-deletes-inflight` (#2)** — cleanest fix adds a `SENDING` status enum value (schema-touching); the SENT-as-claim workaround briefly shows SENT pre-delivery. conf 0.60. Review.
4. **`oversized-value-poisons-enrichment-batch` (#39)** — *split it*: the `[:1000]` truncation at apply is safe autofix; the per-card `begin_nested()` batch-loop restructuring + `counts` reconciliation is a structural change → gate that half.
5. **`cross-thread-session-teardown` (#42)** — "threading refactor across several jobs; subtle." Session-lifecycle rework across offers_jobs/email_jobs, not a mechanical edit. Design-review gate.

Everything else stays `safe_autofix`. Money-touching row-locks (#10, #9) stay safe **only** with a PG concurrency test as acceptance (SQLite no-ops FOR UPDATE — feedback_sqlite_masks_postgres).

---

## RANKED WORKLIST (value/risk, highest first)

**Tier A — high value, trivial/low risk, mechanical**

1. **`unescaped-html-email-body`** — email_service.py:46 — *Fix:* `import html`; `html.escape(plain_text).replace('\n','<br>\n')` (mirror proactive_email.py:164). *Test:* `_build_html_body('price < 5 & >Q1')` contains `&lt;`/`&amp;`, still emits `<br>`. *Risk:* very low; both callers pass plain text.
2. **`js-bid-due-cap-return`** — task_service.py:753 (+ task_jobs.py:64) — *Fix:* add `return` before `auto_create_task(...)`; add `.order_by(Requisition.deadline.asc())` to the LIMIT-60 query. *Test:* seed >60 in-window reqs; assert cap=20 and nearest-deadline reqs get tasks. *Risk:* lexical order safe for ISO dates.
3. **`pa-send-mixed-site-leak`** — proactive_service.py:436 — *Fix:* after loading matches, `if len({m.customer_site_id for m in matches})>1: raise ValueError(...)`; mirror in send_draft_offer:774. *Test:* match_ids spanning two sites → ValueError, no Graph send. *Risk:* very low; **confidentiality** — closes cross-site email leak.
4. **`pa-mutating-routes-require-user-only`** — routers/htmx/proactive.py:476 (+559,113,133,164,281,356,634) — *Fix:* swap `Depends(require_user)`→`Depends(require_access(AccessKey.PROACTIVE))` (keep do-not-offer's can_manage_account). *Test:* non-PROACTIVE user POST → 403. *Risk:* low pure dependency swap; **authz gap**.
5. **`resell-3-publish-no-list-lock`** — excess_mirror.py:514 — *Fix:* `_lock_list_row(db, list_id)` right after get_excess_list, before the status read. *Test:* two concurrent publishes on one DRAFT → one succeeds, one 409, one set of mirror Sightings (PG). *Risk:* low; reuses proven helper.
6. **`resell-4-retry-card-none-deref`** — resell_outreach_service.py:1040 — *Fix:* build plan with `"card_id": row.target_vendor_card_id` (None-safe) or guard `if card is None → FAILED`. *Test:* retryable row w/ card=None, email set → no AttributeError, terminal status. *Risk:* low; stops sending↔interrupted crash loop.
7. **`resell-1-convert-dup-offer`** — resell_outreach_service.py:1171 — *Fix:* snapshot `already_terminal = rows[0].status in {BID,DECLINED}` before the loop; `if has_offer and not already_terminal: _link_inbound_offer(...)` (mirror record_manual_response:1216). *Test:* record_response(has_offer=True) twice on one conversation_id → exactly one ExcessOffer. *Risk:* low.
8. **`pa-scorecard-breakdown-no-draft-filter`** — proactive_service.py:1062 — *Fix:* add `.filter(*base_filter)` before `.group_by` on the per-rep query. *Test:* rep with 1 SENT+1 DRAFT+1 FAILED → breakdown sent==1. *Risk:* low; corrects inflated per-rep totals.
9. **`pa-scorecard-swallows-exceptions`** — routers/htmx/proactive.py:600 — *Fix:* narrow the except, `logger.exception(...)`, make fallback dict keys match get_scorecard (converted_revenue/gross_profit/anticipated_revenue). *Test:* patch get_scorecard to raise → logged + zeros on real template keys. *Risk:* low; error-path only.
10. **`sec-attachment-oom`** — attachment_service.py:217 — *Fix:* chunked `await file.read(65536)` into bytearray, raise 400 as soon as len>MAX_ATTACHMENT_BYTES, before whole body buffers. *Test:* >10MB UploadFile → HTTP 400, accumulator never exceeds max+1 chunk; boundary at exactly max succeeds. *Risk:* low; **DoS/OOM** hardening.

**Tier B — high value, low-moderate risk**

11. **`oq-04-offers-crud-count-numbering`** — htmx/offers/crud.py:248 — *Fix:* replace `count()+1` numbering with `next_quote_number(db)` + the 3-retry IntegrityError loop create_quote uses. *Test:* create 2 quotes from offers on one req, delete first, create third → no IntegrityError, unique Q-YYYY-NNNN. *Risk:* low-mod; **active HTMX path**, changes visible number format to canonical (identifier, not money).
12. **`quote-builder-revise-hijack`** — quote_builder_service.py:522 — *Fix:* load old_quote via `get_quote_for_user`; require `requisition_ids_for_quote(old_quote)` ∩ req_ids; carry full prior membership into link_quote_to_requisitions. *Test:* B posts assemble with A's quote_id + req_id=3 → 403/404, A's quote untouched. *Risk:* low; **cross-rep tamper + combined-quote data loss** (sibling HTMX path already guarded).
13. **`proactive-phantom-sent-banner`** — routers/htmx/proactive.py:534 — *Fix:* have send_proactive_offer return/raise on failure; branch router to error toast (400 on missing token like prepared-send). *Test:* patch post_json to raise, POST send → response lacks "Offer sent", shows error. *Risk:* low; DB already honest, stops false "sent" to salesperson.
14. **`proactive-zero-price-email`** — proactive_service.py:320 — *Fix:* in _build_line_items `continue` when resolved sell<=0 (not only None); stop prepare.html:66 pre-filling `0.0000`; surface needs_price. *Test:* match unit_price None, send with sell_price=0 → line excluded. *Risk:* low; stops $0 quote reaching customer.
15. **M1 `mid-loop-rollback-savepoint`** — stock_list_ingest.py:184 + jobs/inventory_jobs.py:376 — *Fix:* wrap the per-row flush in `with db.begin_nested():`; on IntegrityError re-query existing card by normalized_mpn and store in card_map (never bare `continue` with a None-id card). *Test (PG):* batch where a later row collides mid-loop → earlier rows' MVH/price persist, commit succeeds, no NULL-FK. *Risk:* low-mod; must re-fetch card after nested rollback; verify on PG.
16. **`8x8-watermark-advances-on-fetch-failure`** — jobs/eight_by_eight_jobs.py:118 — *Fix:* make get_cdrs raise / return `(records, ok)` on HTTPError/non-200; only `_update_watermark(until)` on a clean fetch (mirror teams_call_jobs any_fetch_failed). *Test:* mock get_cdrs failure → 8x8_last_poll unchanged; genuine-empty still advances. *Risk:* low; **prevents permanent call-record loss**.
17. **`graph-invalid-odata-sent-search`** — utils/graph_client.py:154 — *Fix:* replace `$filter contains(...)`+`$orderby` with `$search='"{query}"'`, `$select=id,subject,toRecipients,sentDateTime`, `$top`; sort by sentDateTime in Python. *Test:* patch get_json to capture params; assert `$search` present, no `$filter/$orderby`. *Risk:* mod — **cannot be CI-verified (Graph always mocked)**; wrong query still silently returns nothing (sqlite-masks-postgres analog). Verify against a live mailbox before trusting.
18. **`markpaid-unlocked-race`** — prepayment_confirm.py:128 — *Fix:* load row `.with_for_update().one_or_none()` (or re-fetch under lock in mark_prepayment_paid) and re-assert status==approved before mutating; same for in-app caller. *Test (PG):* two sessions mark-paid same token → one succeeds, one paid-notify. *Risk:* low-mod; **money** — stops wire_reference/paid_amount overwrite on double-click. PG-only test is acceptance.
19. **`prepay-json-create-no-notice`** — prepayments.py:102 — *Fix:* after commit, fire `run_prepayment_notify_bg(notify_prepayment_requested, prepayment.id)` (mirror HTMX:282) — **or delete the endpoint if it has no live caller**. *Test:* POST JSON create → notify scheduled once. *Risk:* low; confirm callers first.
20. **`stocklist-attachment-mining-dead`** — connectors/email_mining.py:440 — *Fix:* for each hasAttachments msg, `GET /me/messages/{id}/attachments?$select=id,name,size,contentType`; build stock_files from that (MSG_SELECT has no `$expand=attachments`). *Test:* mock attachments page → scan yields stock_files (rewrite test_connectors.py:2630). *Risk:* low; one extra Graph call/msg — bound fan-out.

**Tier C — moderate value / moderate risk (careful, but stays safe)**

21. **`js-cdr-candidates-no-timebound`** — jobs/eight_by_eight_jobs.py:277 — *Fix:* add SQL window bound on occurred_at (with generous created_at fallback margin) before `.all()`. *Test:* one in-window + many far-out rows → returns in-window, bounded rowcount. *Risk:* mod; too-tight bound could drop a valid match — bound occurred_at when set, wide created_at fallback.
22. **`resell-2-sent-lookup-no-time-floor`** — resell_outreach_service.py:991 (email_service.py:461) — *Fix:* add optional `sent_after: datetime|None` to _find_sent_message (skip older than floor); pass row's original created_at from retry; RFQ caller passes None. *Test:* mock older same-subject/recipient Sent msg → returns None. *Risk:* mod; shared with RFQ path (keep default None), Graph tz parsing.
23. **`pa-teams-watermark-top500-by-score`** — proactive_teams_push.py:117 — *Fix (conservative):* only advance watermark when returned count < _SCAN_CAP; or window by id.asc() for a contiguous max_id, keep display sorted by score. *Test (PG):* 600 mixed-score NEW matches → watermark contiguous, second run picks remainder, zero skips. *Risk:* mod; scan reorder changes first card body.
24. **`teardown-sweep-no-lock`** — buyplan_workflow/buyplan_approval.py:307 — *Fix:* add `.with_for_update()` to both selects in _cancel_open_prepayment_requests_for_plan (307-318, 325-331), re-check status in-loop. *Test (PG):* decide(approve) uncommitted in A while B sweeps → no APPROVED-with-live-token. *Risk:* narrow residual race; dominant harm already closed.
25. **`normalized-mpn-unique-drift`** — models/intelligence.py:30 — *Fix:* add `.filter(deleted_at.is_(None))` + restore to stock_list_ingest.py:173/185 (data fix); change model line 30 to partial UniqueConstraint `postgresql_where` + align 001 baseline (metadata-only, prod already partial). *Test (PG):* soft-delete card, re-ingest same MPN → restores, no active twin. *Risk:* low for the lookup filter; metadata half is test-fidelity only.
26. **`sf-import-invisible-to-matcher`** — management/import_proactive_export.py:234 — *Fix:* after `--apply`, call find_matches_for_offer over created offer ids, or rewind `_set_watermark` to oldest imported created_at. *Test:* import backdated offer, run scan → ProactiveMatch created. *Risk:* mod; management command, not hot path — but it's the feature's primary seed load.
27. **`auto-attribution-ai-dead`** — auto_attribution_service.py:149 (maintenance_jobs.py:72) — *Fix:* `await loop.run_in_executor(None, run_auto_attribution, db)` so the worker thread has no running loop and the AI pass fires (sibling auto_dedup pattern). *Test:* job under running loop, Claude mocked → ai_matched can exceed 0. *Risk:* **enables real Claude spend** (intended) — monitor cost. Heed #42 session caveat.
28. **`js-contact-status-eventloop`** — jobs/email_jobs.py:235 — *Fix:* extract sync body to module fn, `await run_in_executor(None, _compute_contact_statuses, db)` — **but create the Session inside the fn (per #42), don't share it across the loop**. *Test:* call helper with seeded rows → status transitions; assert wrapper offloads. *Risk:* low if session ownership handled; do after/with #42.
29. **`enrichment-clobbers-manual-provenance`** — authoritative_enrichment_service.py:214 — *Fix:* per-field skip (or record conflict) when existing provenance[field].source=='manual'; merge new entries into the dict instead of reassigning; stop NULLing provenance on not_found (526-527). *Test (PG):* stamp desc source=manual, run apply_web/oem/not_found → manual value+stamp survive. *Risk:* mod; touches all apply paths, merge must not resurrect stale entries.
30. **`raw-manufacturer-write-bypasses-ladder`** — tagging_ai_classify.py:136 (+7 sites) — *Fix:* route every `card.manufacturer=X` through `set_manufacturer(..., source=...)` with honest source; also fix routers/materials.py:88 write-commit-on-GET. *Test (PG):* classify/backfill → provenance.manufacturer.source set, normalized; tier-40 guess can't overwrite. *Risk:* mod; **changes stored (normalized) manufacturer string**, affects Brand facet grouping; multi-site.
31. **`approvals-notif-notoken-marked-sent`** — approvals/notifications.py:46 — *Fix:* raise (RuntimeError) instead of returning None on no Graph token, so dispatch_pending marks failed (_mark_failed) rather than stamping sent_at. *Test:* patch get_valid_token→None on email row → sent_at IS None, fail_count++. *Risk:* low-mod; adds retry/dead-letter log noise (arguably better); in_app twin still delivers.

**Tier D — legacy/dead paths, near-zero real-world blast (prefer DELETE)**

32. **M2 `legacy-json-quote-routes-diverge`** — crm/quotes.py:51 (revise) + :341 (PUT update) — *Fix:* mirror HTMX path (clone QuoteLine + link_quote_to_requisitions on revise; rebuild QuoteLine from JSON on PUT) — **or delete both; no live frontend caller**. *Test (PG):* revise → 2 cloned QuoteLine + both req links; PUT → QuoteLine matches JSON. *Risk:* low; mounted + test-covered but unused by UI.
33. **`resell-6-import-line-items-dead-unguarded`** — excess_service.py:276 — *Fix:* relocate as an explicit test fixture helper, or add a DRAFT guard (audit ~15 test callers first — they seed via create_excess_list=DRAFT). *Test:* import on non-DRAFT → 409; full resell suite still green. *Risk:* zero prod (unreachable by any route); guard could break fixtures.

**Tier E — perf residual, low value**

34. **`part-equivalence-repays-on-timeout`** — part_equivalence.py:262 — *Fix:* move O(n²) find_candidate_pairs (:214) to run_in_executor with an input cap (money bug already fixed via per-verdict commit). *Test:* ~10k keys → dispatched via executor, loop not blocked. *Risk:* low; perf-only residual.

**Tier F — REGATE (needs review/design, do not blind-autofix — see reasons above)**

35. **`dc-buyplan-submit-no-lock`** [REGATE] — buyplan_approval.py:62 — *Fix:* separate id-only `select(BuyPlan.id).where(...).with_for_update()` before the status gate, THEN joinedload + re-check (must NOT put FOR UPDATE on the joinedload query). *Test (PG):* two concurrent submits → one ValueError, exactly one open BUY_PLAN request. *Risk:* naive `.with_for_update()` on joinedload 500s on PG — reviewed only.
36. **`pa-send-draft-no-atomic-claim`** [REGATE] — proactive_service.py:741 — *Fix:* atomic claim (UPDATE…WHERE status=DRAFT RETURNING, or with_for_update skip_locked) then send; revert to DRAFT in the GraphAPIError branch; don't hold lock across await. *Test (PG):* two concurrent sends → one Graph send, other ValueError; send-vs-discard no StaleDataError. *Risk:* delicate ordering, crash-after-claim leaves SENT; conf 0.62.
37. **`pa-digest-generate-deletes-inflight`** [REGATE] — proactive_digest.py:217 — *Fix:* claim digest atomically before the await in send_digest (or skip recently-updated rows in the delete-all sweep). *Test (PG):* interleave send_digest + generate_digests → sent digest survives SENT. *Risk:* clean fix adds a status-enum value; conf 0.60.
38. **`oversized-value-poisons-enrichment-batch`** [REGATE-SPLIT] — enrichment_worker/worker.py:884 — *Safe half:* truncate description/datasheet_url to column length at apply (247/198). *Gated half:* per-card `begin_nested()` batch loop + reconcile `counts` to persisted rows. *Test (PG):* >1000-char desc → card persists truncated, batch commits. *Risk:* savepoint restructuring keeps web-call billing correct — gate that.
39. **`cross-thread-session-teardown`** [REGATE] — offers_jobs.py:137 (+ email_jobs.py:191/301) — *Fix:* create the Session INSIDE the executor fn (job holds none); never rollback/close cross-thread on wait_for timeout. *Test (PG):* force wait_for timeout mid-query → no InvalidRequestError/leak, no post-rollback partial commit. *Risk:* multi-job threading refactor; subtle — design gate (and it's a prerequisite for #28).

**Tier G — test coverage only (zero prod risk, do opportunistically)**

40. **`pg-search-no-pg-tests`** — global_search_service.py:227 — *Fix:* add a `@requires_postgres` class exercising fast_search PG/trgm branches (mirror test_score_nullslast_postgres pg_session). *Test:* seed rows, assert trgm-ordered results in the PG CI job. *Risk:* none; new coverage.
41. **`add-domain-overmocked-test`** — tests/test_htmx_views_nightly11.py:170 — *Fix:* drop `__wrapped__` patch, mock add_prospect_manually to return the real dict, assert rendered add_result content + success HX-Trigger. *Test:* wrong dict key now KeyErrors→500 instead of passing. *Risk:* none; test-only.
42. **`ratelimit-enforcement-untested`** — tests/test_rate_limiting.py:15 — *Fix:* add check_rate_limit unit tests via reset_rate_limit_state + `_now` seam (True×limit then False, window rollover); add an isolated enabled-Limiter 429 test. *Test:* (limit+1)th request → 429. *Risk:* none; must build an isolated enabled limiter (global TESTING disables it).

---
**Suggested sprint cut:** Tier A (1–10) is same-day, mechanical, high-value — ship first. Tier B (11–20) next. Route all five REGATE items (35–39) through code review with a mandatory PG concurrency test as acceptance. Before touching #11/M2(#32)/#33 dead paths, grep for live callers and prefer deletion.

---

## Open + gated (need owner/host/browser/data-model)

- **pa-match-no-unique-index** [gated_data_model] app/models/intelligence.py:375 — One-active-match-per-(part,company) enforced only in Python; no unique index means overlapping scheduler+Refresh scans duplicate matches
- **money-math-float** [gated_owner] app/services/buyplan_workflow/buyplan_approval.py:848 — Plan financials still computed in binary float (cent drift; at-limit routing edge)
- **unmark-paid-orphan-token** [gated_owner] app/routers/prepayments.py:418 — Unmark-paid re-mints a pay_token nobody is ever sent; accounting's confirm link is dead after an undo
- **oq-05-workspace-reopen-keeps-win-fields** [gated_owner] app/routers/htmx/quotes.py:190 — Workspace reopen sends quote to DRAFT but keeps result/result_at/won_revenue — reopened won deals still score as wins
- **oq-06-revenue90d-sums-all-quotes** [gated_owner] app/services/crm_service.py:776 — company revenue_90d sums EVERY quote on a WON requisition (revisions/draft/lost included) — double/triple counting
- **reenrich-clobbers-verified** [gated_owner] app/services/material_enrichment_service.py:105 — reenrich CLI overwrites verified description/lifecycle with Haiku guesses; status stays 'verified'
- **vendor-card-hxvals-e** [gated_browser] app/templates/htmx/partials/search/vendor_card.html:74 — vendor_card hx-vals uses |e not |tojson; a double-quote in mpn/vendor breaks JSON.parse -> empty RFQ/offer POST
- **dossier-xdata-quote** [gated_browser] app/templates/htmx/partials/search/dossier_shell.html:20 — dossier search bar inlines reflected ?mpn= into single-quoted x-data JS string; a quote kills Alpine (and is a DOM-XSS vector)
- **js-inmemory-jobstore** [gated_host] app/scheduler.py:41 — Scheduler uses default in-memory jobstore: interval jobs reset each deploy, missed monthly cron lost
- **js-qty-backfill-rerun** [gated_owner] app/startup.py:247 — 'One-time' proactive-qty backfill reruns every boot, can silently rewrite historical sent offers' $ totals
- **worker-concurrent-alembic** [gated_host] docker-entrypoint.sh:45 — enrichment-worker shares docker-entrypoint.sh and runs alembic upgrade concurrently with app
- **backup-verify-timer-uninstalled** [gated_host] scripts/verify-backup.sh:16 — Weekly backup-verify: units exist in repo but prod install unverifiable; --verify is checksum+TOC only, no test restore
- **repo-backup-sh-dead-real-path-tracked** [gated_host] backup.sh:26 — Repo-root backup.sh is a redundant daily-cron script; the real 6-hourly backup is now a TRACKED container path (finding partly stale)
- **db-backup-secrets-aws-unpinned** [gated_host] docker-compose.yml:183 — db-backup container gets full .env secret set and apk-installs unpinned aws-cli at runtime
- **rollback-restore-no-onerrorstop** [gated_host] .github/workflows/deploy.yml:174 — deploy.yml rollback DB restore runs psql without ON_ERROR_STOP/--single-transaction; prints success over partial restore
- **prod-py314-ci-py313-skew** [gated_host] Dockerfile:19 — Prod image runs Python 3.14 but CI tests and lockfile compile on 3.13
- **branch-cleanup-gh-fail-empty-protected** [gated_host] scripts/branch-cleanup.sh:34 — branch-cleanup.sh: gh pr list failure silently empties PROTECTED; --apply --remote then deletes open-PR branches
