# AvailAI Codebase Audit — {'units': 19, 'total': 109, 'p0': 1, 'p1': 17, 'p2': 41, 'p3': 50}

## Executive summary
This audit surfaced ~90 verified findings. The dominant structural story: AvailAI built a hardened "v2" surface (requisitions2.py, sightings buyer handlers, Jinja templates) but parallel older HTMX/partial endpoints skipped the same authorization gates, escaping, and normalization — producing a cluster of confirmed IDOR, privilege-escalation, and stored-XSS bugs alongside the canonical paths that do it right. The single most dangerous issue is a daily 03:30 cron (_job_contact_dedup) that silently destroys contact attachments and open tasks via uncontrolled cascade deletes. Two confirmed crash paths (quote-delete cascade IntegrityError; legacy-substitute parse AttributeError) round out the release blockers. Below P0 sits a broad layer of silent-failure correctness bugs concentrated in scoring, the browser search workers, buy-plan PO completion, enrichment, and number/MPN normalization — plus pervasive raw-status-string and comment-rot cleanup themes. P0/P1 are kept strictly to real, actionable, reachable issues; merged where they share a root cause.

## Top themes
- Authorization is applied per-endpoint, not per-resource: the canonical v2 path enforces require_requisition_access / is_manager_or_admin / require_access gates that sibling legacy HTMX partials omit, yielding read-IDOR and write-side privilege escalation on the same data.
- Hand-built HTML f-string fragments returned via HTMLResponse bypass Jinja autoescaping wherever they interpolate user free-text (company/requisition/vendor/manufacturer names) -> recurring stored/reflected XSS; the fix is template_response or html.escape() at every such sink.
- Raw status/type string literals instead of app/constants.py StrEnums are pervasive, and at least two ('buyplan_cancelled', 'flagged') have no enum member at all -> values that silently drift or never match any filter.
- MPN normalization is inconsistent (.upper() vs normalize_mpn_key, display-form vs key-form lookups, stacked-suffix, substitute dict-vs-string) -> broken material-card joins, dedup misses, cross-MPN sighting contamination, and outright crashes on legacy rows.
- Automated/background paths skip the hardening their interactive twins have (verify_po_sent vs verify_po, Teams watermark advance-on-failure, knowledge-regen delete-before-AI, direct Anthropic calls with no timeout, AI-gate poison items) -> silent data loss and bypassed approval controls.
- Widespread comment/docstring rot and dead config: comments assert invariants the code does not hold and knobs (MAX_HOURLY_SEARCHES, BUSINESS_HOURS) are documented but unenforced.

## P0 — release-blocking

### app/jobs/maintenance_jobs.py:179 — Daily contact-dedup cron silently destroys child rows (cascade data loss)
**Why:** _job_contact_dedup runs in prod via register_maintenance_jobs (jobs/__init__.py:43, CronTrigger hour=3 minute=30). The merge loop copies only 5 scalar fields into the survivor then db.delete(other) with NO FK repoint. SiteContact.attachments is cascade='all, delete-orphan' so the loser's site_contact_attachments rows are DELETED; requisition_tasks.site_contact_id (ondelete=CASCADE) deletes its open tasks; activity_log.site_contact_id and companies.primary_contact_id are nulled. Two contacts sharing an email -> the lower-completeness one's CRM attachment + open task are destroyed daily, automatically, no backup, no guard. Confirmed and reachable.

**Fix:** Before db.delete(other), repoint site_contact_attachments.site_contact_id, requisition_tasks.site_contact_id, activity_log.site_contact_id, and companies.primary_contact_id from other.id to best.id (or route through a real contact-merge service).

### app/routers/htmx/companies.py:1142 — Stored/reflected XSS across hand-built HTML fragments (companies, materials, vendors)
**Why:** Multiple HTMX endpoints interpolate user-controlled free text into HTMLResponse f-strings with no escaping, bypassing Jinja autoescape. Confirmed: company_typeahead <option> (1142), check_company_duplicate warning (1167), and company_tab requisitions branch r.name/r.status (2574) all execute a payload like </option><img src=x onerror=...> when the fragment is HTMX-swapped. Same pattern (plausible) in manufacturer_add reflected name (materials.py:330) and the vendor offers tab o.mpn/o.lead_time (vendors.py:622). Sibling toast/template paths already escape; these sinks were missed.

**Fix:** html.escape() every interpolated field, or render these fragments via Jinja partials (template_response) so autoescaping applies. Audit all f-string HTMLResponse builders in these routers.

### app/routers/htmx/requisitions.py:664 — Requisition/requirement read-IDOR cluster (missing require_requisition_access)
**Why:** Several GET partials/APIs load a requisition/requirement by id and only 404 on missing, omitting the require_requisition_access / get_req_for_user that sibling mutators in the same files enforce. Confirmed: requisition_detail_partial (requisitions.py:664), requisition_tab (requisitions.py:910), and list_requirement_offers (requirements.py:1195) expose another rep's requirements, offers, pricing, vendor names, quotes and buy-plans to any SALES/TRADER user by id. Same gap (plausible) in list_requirement_notes (requirements.py:1302), list_requirement_tasks (requirements.py:1359), and list_requirement_history (requirements.py:1459).

**Fix:** Add require_requisition_access(db, <req_id>, user) immediately after the load in each of these GET endpoints, mirroring their POST counterparts.

### app/routers/htmx_views.py:534 — Ownership-reassignment authz bypass: BUYER can reassign requisition owner (single + bulk)
**Why:** The v2 requisitions2.py path returns 403 unless is_manager_or_admin for owner reassignment; the legacy htmx_views.py path omits that check entirely. requisition_inline_save sets req.created_by for field=='owner' (534) after only get_req_for_user (a no-op for buyer/manager/admin), so a BUYER can reassign the owner of ANY requisition. requisitions_bulk_action action=='assign' (441, route at 434) sets r.created_by for up to 200 requisitions with no manager/admin gate -> mass ownership reassignment. Both confirmed; is_manager_or_admin is not even imported in this file.

**Fix:** Import is_manager_or_admin and gate both the field=='owner' branch and action=='assign' on it (403 otherwise), matching requisitions2.py.

### app/models/quotes.py:73 — Quote.quote_lines missing delete cascade -> IntegrityError 500 on quote/requisition delete
**Why:** QuoteLine.quote_id is NOT NULL with DB ondelete=CASCADE, but Quote.quote_lines = relationship(...) sets neither cascade nor passive_deletes. On db.delete(quote) SQLAlchemy's unit-of-work nullifies children first (UPDATE quote_lines SET quote_id=NULL), violating NOT NULL before the DB cascade can fire — empirically reproduced as 'NOT NULL constraint failed: quote_lines.quote_id'. Reachable via the add-offers-to-draft-quote flow then delete_quote_htmx / crm quotes delete, and via Requisition deletion which cascades to its quotes -> 500. Existing delete tests only cover line-less quotes.

**Fix:** Add cascade="all, delete-orphan", passive_deletes=True to the Quote.quote_lines relationship.

### app/utils/normalization.py:430 — parse_substitute_mpns crashes (AttributeError 500) on legacy string-form substitutes
**Why:** Line 430 does sub.get('mpn','').strip() assuming every entry is a dict, but parts.py:370 calls parse_substitute_mpns(requirement.substitutes or [], ...) passing the raw DB column with no coercion. Legacy rows hold plain strings (e.g. ['LM338T']) -> str has no .get -> AttributeError -> 500 on a require_user GET (cross-req quote history). Other call sites (part_offers.py:27, requisition_list_service.py:116) coerce defensively and comment that legacy rows can crash; parts.py:370 is the lone unprotected caller. {'mpn': None} also reaches None.strip().

**Fix:** Coerce each sub before parsing: raw = sub if isinstance(sub,str) else (sub.get('mpn') or ''); raw_mpn = str(raw).strip().

## P1 — likely bugs / silent failures

### app/services/avail_score_service.py:562 — Sales b6 (Interaction Quality) summed into total but never persisted; sales scored 0-110 vs buyers 0-100
**Why:** compute_sales_avail_score adds b6 to behavior_total, but AvailScoreSnapshot only has b1-b5, _upsert_snapshot omits b6, and get_avail_scores reads range(1,6). So the persisted breakdown can't reconcile (b1-b5 won't sum to behavior_total) and sales runs a 0-110 scale while buyers run 0-100 — yet _rank_and_bonus applies the same absolute QUALIFY gates (60/50/40) with real $500/$250/$100 payouts, making sales easier to qualify. Reachable via the daily scheduler and routers/performance.py.

**Fix:** Add b6_score/label/raw to the model+migration and to _upsert_snapshot/get_avail_scores, or drop b6 from behavior_total, so both roles share one 0-100 scale.

### app/services/avail_score_service.py:127 — Score outcomes O2/O4 use .limit(10000) global scans that silently truncate
**Why:** quoted_offer_ids and bp/po_confirmed_offer_ids are built by materializing up to 10000 rows globally into Python sets. Once quotes or buy-plan lines exceed 10000, offers tied to rows beyond the cap drop out of the sets, silently undercounting a user's offers_in_quotes (O2) and bp_confirmed (O4) and lowering their score with no error.

**Fix:** Scope these with a DB-side JOIN/EXISTS against the user's offer ids instead of an unbounded .limit(10000) materialization.

### app/services/nc_worker/search_engine.py:51 — Browser fallback reuses Playwright objects across throwaway event loops -> works once per process
**Why:** worker.main() is synchronous and runs each browser fallback via asyncio.run(_search_browser(...)), which closes the loop each call. start_browser() binds _playwright/_context/_page to the first loop and is skipped thereafter; the 2nd+ fallback calls page.goto on a closed loop -> RuntimeError caught by the broad except -> returns None -> part recorded as 0 results. Fires for any part lacking result markers and reliably for EVERY part when NC blocks the HTTP API (the documented maintenance-mode purpose) -> silent false negatives. Tests patch asyncio.run so the real path is never exercised. Confirmed.

**Fix:** Run the worker browser path in a single persistent event loop, or detect a closed/foreign loop and rebuild the browser before reuse.

### app/services/search_worker_base/queue_manager.py:158 — Cross-requirement dedup links sightings by requirement_id, not by searched MPN
**Why:** A fresh enqueue that dedups against a recent completed search clones ALL sightings of recent.requirement_id filtered only by source_type, never by normalized_mpn. Because enqueue_search supports multiple MPNs per requirement (primary + AVL override), requirement B deduping against A's X row inherits A's Y-MPN sightings too -> vendor offers for an MPN B never requested are attributed to B's material card, corrupting buyer-facing sourcing data.

**Fix:** Scope the linked-sightings query with Sighting.normalized_mpn == norm_mpn so only the deduped MPN's sightings are cloned.

### app/services/search_worker_base/ai_gate.py:225 — Unguarded indexing of model output aborts the batch and skips the commit
**Why:** process_ai_gate indexes result_map[r['mpn']] and classification[search_field]/['commodity']/['reason'] directly. Tool-schema enforcement guarantees valid JSON but not strict presence of every required field; a single missing key raises KeyError that propagates out, aborting all remaining batches AND skipping the final db.commit() (242), so this cycle's already-applied cache-hit status updates are silently lost — inconsistent with the function's deliberate fail-open API handling.

**Fix:** Per-item guard the result access (skip + log items missing mpn/search_field/commodity/reason).

### app/services/nc_worker/ai_gate.py:189 — AI gate matches MPNs case-sensitively; un-returned items wedge and head-of-line-block the queue
**Why:** result_map keyed on exact r['mpn'] then result_map.get(item.mpn). If the model echoes different case/whitespace or omits an item, it stays status='pending', is re-sent every gate cycle with no retry cap, and (pending query LIMIT 30) permanently occupies a batch slot blocking newer items — ~30 poison items starve the gate and thus the search queue. Same in tbf_worker/ai_gate.py:189.

**Fix:** Match case-insensitively/normalized and cap reclassification attempts (mark items queued/gated_out after N misses).

### app/services/nc_worker/worker.py:203 — circuit_breaker_open flag set True on trip but never reset to False after self-heal
**Why:** All three workers write circuit_breaker_open=True when breaker.should_stop() is True, but the breaker auto-resets after cooldown and the worker resumes searching without ever writing circuit_breaker_open=False (the success-path update omits the field). Monitoring/UI shows the breaker permanently open though searching resumed. Same in ics_worker/worker.py:199 and tbf_worker/worker.py:199.

**Fix:** On the should_stop()==False / successful-search path, write circuit_breaker_open=False and clear circuit_breaker_reason.

### app/services/tbf_worker/worker.py:125 — Browser/Playwright leaked when session.start() fails partway through
**Why:** If launch_persistent_context() succeeds but the subsequent page.goto() in start() raises, the except logs, sets is_running=False and returns WITHOUT calling session.stop(), leaking _context/_playwright and the spawned Chrome. The adjacent login-failure path explicitly DOES call await session.stop(). Same in ics_worker/worker.py:125. Only partially mitigated by process exit under a restart loop.

**Fix:** Add await session.stop() inside the start() failure except block before updating status and returning.

### app/services/buyplan_workflow.py:818 — Auto-completion orphans the still-open deal-level PURCHASE_ORDER request and bypasses large-PO sign-off
**Why:** An over-threshold plan opens a PURCHASE_ORDER ApprovalRequest AND generates line buyer tasks; the line flow can run to COMPLETED before any PO approver decides the gate. _complete_plan never cancels the open PURCHASE_ORDER request (unlike cancel/halt, which call _cancel_open_engine_requests_for_plan), so a REQUESTED row is orphaned in the queue, a later approval hits the plan.status!=ACTIVE guard -> ValueError/400, and the SP-3 large-PO control is silently bypassed. Conversely if the PO gate is approved first, AWAITING_PO lines stall because confirm_po requires ACTIVE. Race is untested.

**Fix:** In _complete_plan, cancel any open PURCHASE_ORDER ApprovalRequest for the plan and reconcile the line-flow vs deal-PO completion paths so only one is live.

### app/services/buyplan_workflow.py:1375 — Scheduled verify_po_sent auto-verifies PO lines, bypassing the Phase-D manager PO-approver gate
**Why:** _job_po_verification -> verify_po_sent flips a PENDING_VERIFY line to VERIFIED (and can then auto-complete the plan) whenever the PO# appears in the buyer's Outlook sent folder, with NO can_approve_purchase_orders check and po_verified_by_id left NULL. The interactive verify_po was hardened in Phase D to require that manager right and stamp the approver; this automated path enforces neither, so a buyer emailing a PO can complete the deal with no PO approver signing off.

**Fix:** Restrict verify_po_sent to flagging/notification only; line verification must go through verify_po's can_approve_purchase_orders gate that stamps po_verified_by_id.

### app/services/knowledge_service.py:549 — _regenerate_insights deletes cached insights before the AI call; failure branches lose them with no rollback
**Why:** The pipeline db.delete()s every cached ai_insight + flush BEFORE awaiting claude_structured. The ClaudeError/no-result/unavailable branches return [] without recreating insights and without rolling back the flushed deletes; those deletes stay pending and get committed by the next sibling's create_entry(commit=True), so one transient Claude error permanently wipes that entity's insights with no replacement. Currently dormant (job disabled, htmx caller never awaits) but live in code and was exercised by the historical job.

**Fix:** Generate insights first, then delete-and-replace only on success, or wrap delete+regen in a savepoint and nested.rollback() on every failure branch.

### app/services/company_merge_service.py:166 — Company merge swallows FK-reassignment failure, then deletes the company anyway
**Why:** Step-8 FK reassign loop catches Exception and only logs (no re-raise), then proceeds to db.delete(remove)/flush. On Postgres a failed bulk UPDATE (e.g. unique-constraint conflict when both keep and remove have an EnrichmentQueue/ProactiveDoNotOffer row) poisons the transaction ('current transaction is aborted') or orphans/cascade-deletes the un-reassigned rows with the company. Sibling vendor_merge_service/delete_companies deliberately re-raise to fail closed.

**Fix:** Re-raise (raise ... from e) in the except so the caller aborts and rolls back, matching merge_vendor_cards.

### app/jobs/teams_call_jobs.py:108 — Teams call sync advances the shared watermark even when a user's fetch fails -> permanent window loss
**Why:** A per-user Graph fetch failure is swallowed with continue, but the single global teams_calls_last_poll watermark is advanced to now unconditionally and committed. If user A's callRecords request throws while B/C succeed, the next 6h poll starts past A's missed window; those records were never logged so no dedup can recover them — a transient per-user error becomes permanent data loss for that user's window.

**Fix:** Advance the watermark only when no user's fetch failed (track a failure flag), or keep a per-user last-poll timestamp.

### app/services/email_service.py:849 — VendorResponse appended to pending_parse before its savepoint commits
**Why:** pending_parse.append(vr) runs before nested.commit(). If the savepoint commit fails, the except rolls back and continues but the rolled-back vr stays in pending_parse; it is later AI-parsed and billed, and _auto_create_offers_from_parse can create Offers referencing the vanished vendor_response_id, poisoning the poll's final db.commit() which then rolls back ALL responses from the scan and re-raises.

**Fix:** Append to pending_parse only after nested.commit() succeeds, or remove vr from pending_parse in the except handler.

### app/search_service.py:1571 — is_obsolete lookup queries normalized_mpn with display-form MPN -> never matches
**Why:** pn comes from get_all_pns() in DISPLAY form (uppercase, dashes kept), but the normalized_mpn column stores normalize_mpn_key() output (lowercase, non-alphanumerics stripped). filter_by(normalized_mpn=pn) is case- and dash-mismatched, so it never returns a card -> is_obsolete is permanently False and the AI-search obsolescence trigger never fires. The sibling sighting lookup 12 lines below was fixed; this was missed. Also N+1 (one query per PN).

**Fix:** Use normalize_mpn_key(pn) and batch all pns into one .in_() query.

### app/routers/htmx_views.py:1333 — add_to_requisition writes non-canonical normalized_mpn (.upper()) and skips resolve_material_card
**Why:** Creating a Requirement sets normalized_mpn=mpn.strip().upper() (e.g. 'LM2596S-5.0') instead of the canonical normalize_mpn_key form ('lm2596s50') used by update_requirement, sightings, and material-card joins. @validates does not cover normalized_mpn so the bad value persists, breaking part-history/material-card joins; resolve_material_card is never called so material_card_id stays NULL.

**Fix:** Use normalized_mpn=normalize_mpn_key(mpn) and call resolve_material_card, as update_requirement does.

### app/routers/htmx/offers.py:262 — save_parsed_offers omits normalized_mpn and apply_qualification -> offers invisible + qualification NULL
**Why:** Offers built in save_parsed_offers set status ACTIVE directly but never set normalized_mpn (column has no default — models/offers.py:43) or material_card_id, and never call apply_qualification(). part_offers_for() matches only on normalized_mpn or material_card_id, so these AI-parsed offers are invisible in the sightings part-centric Offers panel; qualification_status/note stay NULL even for refurb/pulls, bypassing the qualification computation every other create path runs.

**Fix:** Set normalized_mpn=normalize_mpn_key(o['mpn']) and call apply_qualification(offer) before db.add(), mirroring add_offer.

### app/routers/htmx/vendors.py:611 — Vendor offers tab matches on display_name instead of vendor_name_normalized -> under-matches
**Why:** The offers branch filters Offer.vendor_name == vendor.display_name (exact raw-string match) while the same vendor detail view matches sightings on vendor_name_normalized == vendor.normalized_name. Offer has a dedicated vendor_name_normalized column; the exact display-name compare misses any offer stored under a slightly different name string and is inconsistent with the rest of the vendor-matching code.

**Fix:** Filter on Offer.vendor_name_normalized == vendor.normalized_name.

### app/routers/requisitions/requirements.py:89 — _substitute_keys silently drops dict-form (canonical) substitutes
**Why:** Requirement.substitutes is stored as the canonical list-of-dicts form, but _substitute_keys does `sub if isinstance(sub,str) else ""`, so every dict substitute yields an empty key and is skipped. Substitute material-card history (get_saved_sightings, list_requirement_sightings, list_requirement_offers) is therefore never found for modern requirements; the docstring falsely claims it handles dict form.

**Fix:** Extract the key from dicts too: sub_str = (sub if isinstance(sub,str) else sub.get('mpn','')).strip().

### app/services/sourcing_leads.py:476 — Re-sync overwrites buyer-feedback confidence adjustments
**Why:** update_lead_status mutates lead.confidence_score by buyer outcome (no_stock -14, bad_lead -18, etc.), but upsert_lead_from_sighting unconditionally resets confidence_score to the freshly source-computed value on every re-sync while leaving buyer_status untouched. After a buyer marks a lead 'no_stock' (confidence dropped), the next re-sight restores a high/medium band -> the UI shows a 'no_stock' lead with high confidence.

**Fix:** For a buyer-touched lead (buyer_status != 'new'), preserve the stored confidence_score or re-apply the feedback delta instead of overwriting.

### app/routers/htmx/companies.py:3788 — edit_company reassigns account_owner_id with no active-user validation
**Why:** When owner_id changes, edit_company sets company.account_owner_id = new_owner_id after only can_manage_account_team, never verifying the target user exists and is_active. create_company and bulk assign-owner both validate via db.get(User)+is_active. Result: ownership can be silently transferred to a deactivated user, or a non-existent id raises an unhandled FK IntegrityError on commit -> 500 instead of a clean 400.

**Fix:** Mirror create_company: target = db.get(User, new_owner_id); reject (400) if not target or not target.is_active before assigning.

### app/routers/htmx/companies.py:2368 — Cross-account contact PII readable by any logged-in user (company detail + site contacts)
**Why:** company_detail_partial/company_tab render full contact PII (emails, phones, activity) for the path company gated only by require_user, contradicting the documented SALES/TRADER can_manage scoping applied to the global /v2/contacts list. get_site_contacts (proactive.py:316) likewise returns SiteContact PII for any site_id with no account check. A rep can enumerate company/site ids and scrape every account's contacts, defeating the list-level scoping.

**Fix:** Apply can_manage_account scoping (404 on denial) to company_detail_partial/company_tab and get_site_contacts, consistent with the do-not-offer/attachments gates — or confirm open-view is intended and drop the list scoping.

### app/routers/htmx/materials.py:182 — Authz-gate gaps on materials/offer routes (Depends bypassed or wrong dependency)
**Why:** materials_list_partial is gated only by require_user then calls materials_workspace_partial as a plain function, so the inner Depends(require_access(MATERIALS)) is never evaluated -> a user without materials access gets the full workspace HTML (182). Related: review_offer approves/rejects offers with only require_requisition_access while the 3 sibling approve/reject routes require APPROVE_OFFERS (offers.py:393), and update_material_card is require_user while the add path is require_buyer (materials.py:915).

**Fix:** Gate materials_list_partial with Depends(require_access(MATERIALS)); add Depends(require_access(APPROVE_OFFERS)) to review_offer; align update_material_card with require_buyer.

### app/routers/resell.py:660 — Resell offer endpoints bypass the draft-privacy gate
**Why:** resell_offer_form and resell_submit_offer call excess_service.get_excess_list directly instead of _get_list_for_user, and submit_offer only guards exists/can_offer/self-owner with no posted-status check. A non-owner with can_offer can fetch the offer form for and submit an offer onto a DRAFT (unpublished) list, which the spec says must 404 for non-owners.

**Fix:** Gate both via _get_list_for_user and reject when el.status not in posted statuses (or add a posted-status guard in submit_offer).

### app/routers/htmx/requisitions.py:121 — List partial scopes only SALES, not TRADER, contradicting RESTRICTED_ROLES
**Why:** requisitions_list_partial filters created_by only when user.role == UserRole.SALES, but RESTRICTED_ROLES = {SALES, TRADER} and get_req_for_user / the core list both scope both roles. On this UI list a TRADER sees every non-scratch requisition, inconsistent with the API and the intended ownership boundary.

**Fix:** Change the guard to `if user.role in RESTRICTED_ROLES:`.

### app/utils/vendor_helpers.py:466 — SSRF guard bypassed by redirects in scrape_website_contacts
**Why:** is_private_url validates only the initial URL, but the actual fetch uses http_redirect (follow_redirects=True). A vendor page can 302 to an internal host or cloud metadata (http://169.254.169.254/...) and the scraper follows it, reaching internal services; the gethostbyname check is also a TOCTOU/DNS-rebinding window vs the request's own resolution.

**Fix:** Use the non-redirecting http client for scraping, or re-run is_private_url on each redirect hop's resolved IP via an httpx event hook.

### app/connectors/mouser.py:51 — Connector API keys leak into logs via exception strings (Mouser, element14, OEMSecrets)
**Why:** apiKey is sent as a URL query param. Any HTTP status not explicitly handled hits r.raise_for_status(), raising httpx.HTTPStatusError whose str() embeds the full URL incl. ?apiKey=SECRET; BaseConnector._search_with_retry logs `{e}` at WARNING (sources.py:156/163) to loguru stdout/file/docker sinks. The reassuring comment about Sentry before_send scrubbing is misleading — Sentry does not cover loguru sinks and the leak is via the exception string. Same in element14.py:90 and oemsecrets.py:37.

**Fix:** Send the key via header/body, or scrub query strings before logging the raised exception; fix the misleading comment.

### app/services/sighting_aggregation.py:65 — Direct Anthropic SDK calls with no timeout block sync worker paths (~600s)
**Why:** _estimate_qty_with_ai instantiates anthropic.Anthropic(...) and calls messages.create(...) with no explicit timeout, bypassing the shared claude_client (~30s). rebuild_vendor_summaries runs synchronously in the post-search _save_sightings flow, so a hung response blocks the thread-pool worker for the SDK default (~600s) on every part with 3+ qty listings. Same pattern in vendor_affinity_service.py:210 (_classify_mpn, L3 affinity lookup).

**Fix:** Route both through app.utils.claude_client, or pass an explicit timeout= to messages.create.

### app/routers/sightings.py:920 — Batch assign/status/notes parse requirement_ids JSON with no decode guard -> 500
**Why:** sightings_batch_assign (920), sightings_batch_status (961) and sightings_batch_notes (1020) call json.loads(req_ids_raw) with no try/except, so a malformed requirement_ids payload raises JSONDecodeError -> unhandled 500. sightings_batch_refresh wraps the identical parse and returns a clean 400, establishing the intended contract.

**Fix:** Wrap the json.loads in try/except (json.JSONDecodeError, ValueError) and raise HTTPException(400, ...).

### app/routers/htmx/offers.py:1818 — update_response_status stores out-of-vocabulary 'flagged' status
**Why:** update_response_status accepts {'reviewed','rejected','flagged','new'} and assigns vr.status as a raw string. VendorResponseStatus only defines new/parsed/reviewed/rejected — 'flagged' is not a member, so a flagged row holds a value outside the StrEnum vocabulary and never matches any VendorResponseStatus-based filter/report. send_reply_htmx already uses the enum, so the codebase intends it.

**Fix:** Add a FLAGGED member to VendorResponseStatus (or drop 'flagged') and assign from validated VendorResponseStatus constants.

### app/utils/normalization.py:296 — normalize_moq returns None for its own documented '10K minimum' example
**Why:** It only strips a leading moq/minimum/min prefix, then passes '10K minimum' to normalize_quantity, which strips spaces to '10Kminimum'; endswith('m') hits the 1e6 branch (float() fails) and int(float()) also fails -> None. Real vendor MOQ strings with a trailing qualifier word are silently dropped; the documented example case is untested.

**Fix:** Strip trailing qualifier words (minimum|min|pcs|units|each ...) or extract the leading numeric+suffix token before normalize_quantity.

### app/utils/normalization.py:211 — Compact day shorthand ('30d') misparsed as weeks -> 7x lead time
**Why:** Unit detection requires 'd ' (trailing space) to recognize days, so '30d'/'5d' miss every unit branch and fall into the ambiguous arm, which assumes weeks for a single number <=52 — '30d' returns 210 days. Compact day shorthand is common in vendor lead-time fields and silently inflates by 7x.

**Fix:** Match a trailing 'd' without requiring a following space (e.g. r'\d\s*d\b') before the ambiguous-weeks fallback.

### app/schemas/crm.py:74 — CompanyCreate skips hq_country/hq_state normalization that CompanyUpdate applies
**Why:** CompanyUpdate has normalize_hq_country/normalize_hq_state validators but CompanyCreate exposes the same fields with none. A company created with 'United States'/'California' stores those verbatim, while editing the same field rewrites to 'US'/'CA' — breaking country/state equality filters and grouping that assume ISO/abbrev codes.

**Fix:** Add the same normalize_country/normalize_us_state field_validators to CompanyCreate.hq_country and hq_state.

### app/services/activity_service.py:327 — match_phone_to_entity full-scans the VendorCard table on every phone activity log
**Why:** Priority 5 runs db.query(VendorCard).filter(is_blacklisted.is_(False)).all() then checks e164 membership in a Python loop, on the hot inbound/outbound call-logging path — a full-table scan + full materialization of every non-blacklisted vendor card per call, and it runs even when priorities 1-4 already matched.

**Fix:** Push the membership test into the DB (VendorCard.normalized_phones.contains([e164])) and short-circuit once a higher-priority match exists.

### app/templates/htmx/partials/requisitions/tabs/offers.html:77 — Alpine @click/@change handlers starting with `var` are dead (silent no-op)
**Why:** Alpine 3.15 only IIFE-wraps handler expressions matching if/let/const-leading; a `var`-leading body compiles to `__self.result = var ... = ...` -> SyntaxError -> handler no-ops. 'Add to Draft Quote' @click begins `var csrf = ...` so offers are never POSTed to the quote. The manufacturer-group 'Select all N' @change (sightings/table.html:225) begins `var ids = ...` so none of the group's ids reach the cross-requisition basket.

**Fix:** Change the leading `var` to `let` in both handlers.

### app/templates/htmx/partials/requisitions/tabs/req_row.html:161 — |tojson inside a DOUBLE-quoted attribute breaks the attr / handler
**Why:** tojson escapes ',<,>,& but NOT ". data-subs="{{ r.substitutes|tojson ... }}" (req_row.html:161) and the manufacturer-picker onclick="...{{ mfr.canonical_name|tojson }}..." (manufacturers/search_results.html:12) place tojson's leading " inside a double-quoted attribute, closing it early -> JSON.parse throws (substitutes editor dies for any row with substitutes) / onclick truncated (picking a typeahead result does nothing). Related apostrophe variant in sightings/table.html:64 x-data. CLAUDE.md mandates tojson in single-quoted attributes.

**Fix:** Single-quote these attributes so tojson's " is legal (tojson escapes '); seed the table.html x-data state via tojson in a single-quoted attr.

## P2 highlights
- encrypted_type.py:82 — process_result_value's bare `except Exception: return None` masks a wrong ENCRYPTION_SALT/secret_key as silently-empty credentials app-wide (M365 appears disconnected, API keys vanish) with only a warning; only InvalidToken should map to None, other errors should re-raise or alert so a misconfig is distinguishable from empty data.
- knowledge_service.py:126/255 — update_entry/delete_entry docstrings claim 'only the creator can update' but never check created_by (user_id is unused) -> a latent IDOR if ever wired to a router; capture_quote_fact uses create_entry(commit=True) without the begin_nested() savepoint isolation its sibling capture_offer_fact deliberately documents.
- activity_service.py:476 — log_email_activity direction is an open str mapped `'sent'->OUTBOUND else INBOUND`, so a plausible value like 'outbound' is silently classified INBOUND/EMAIL_RECEIVED; should be a Literal or routed through _normalize_direction like log_call_activity.
- Implicit-Optional signatures (`int = None`, `Session = None`, `str = None`) in email_service.py:575, requirements/email paths, and vendor_helpers.py:499 annotate non-Optional types with None defaults, misleading mypy and callers about nullability.
- enrichment_worker_status.py:66 — the hasattr-guarded setattr loop silently drops any unknown/misspelled column kwarg, so a typo'd counter (e.g. enriched_todays=5) is a no-op with no error and the heartbeat still commits.
- web_extractor.py:39 — WebExtractResult is left non-frozen while its OEM-module siblings are frozen=True specifically to protect their shared mutable _FAILED singleton; mutating result.source_urls would corrupt the singleton for all future failures (an aliasing footgun the siblings guard against).
- oemsecrets.py:135 — is_authorized defaults to True when no authorization signal is present, overstating trust for a 140+-distributor gray-market meta-aggregator; the unknown-authorization fallback should default False.
- requirement_status.py:24 — the per-part sourcing state machine is defined in two divergent tables (ALLOWED_TRANSITIONS vs status_machine.SOURCING_TRANSITIONS), so a transition's legality depends on which validator a caller happens to use.

## P3 cleanup / simplify themes
- Raw status/type string literals instead of app/constants.py StrEnums across ~10 modules (email_service, buyplan_notifications, avail_score_service, activity_service, requirement_status, startup.seed_api_sources, companies.build_account_timeline, offers) — including 'buyplan_cancelled' and 'flagged' that map to NO enum member at all, so they can silently drift or never match any filter.
- Primary-key lookups via db.query(Model).filter(Model.id==x).first() instead of the standardized db.get() — sourcing_leads.py alone has 5 such sites (855/911/804/572/738), with more scattered.
- Pervasive comment/docstring rot asserting invariants the code doesn't hold: PrometheusMiddleware 'outermost' (main.py:462), _NAV_ID_ALIAS 'Empty now' (htmx_views.py:65), attachments_extra access-model docstring, edit_company tax_id 'preserve on blank', has_price_below_target name/docstring, knowledge_jobs 'Mark expired' (only counts), circuit-breaker '30m' vs real 60m, build_worker_config 'returns object' vs dict, vendor_unavailability exclusion docstring, file_fingerprint unused `rows` param.
- Documented-but-unenforced config knobs across all three browser workers: *_MAX_HOURLY_SEARCHES (hourly cap does nothing) and *_BUSINESS_HOURS_START/END (worker actually runs 24h Mon-Thu) — dead env vars advertised in the READMEs.
- Dead / unreachable code: scoring.py:356 fallback branch can never execute, enrichment.py:735 semaphore is a no-op (and cross_validate_batch has no prod callers), companies.build_account_timeline is tests-only, inventory_jobs.py:305 carries a stale `# noqa: F841` on a used variable.
- Duplicated/divergent helpers instead of the single canonical one: three phone E.164 normalizers (the CRM schema validates through the lenient regex one), and inline fuzz.token_sort_ratio in auto_dedup_service.py:94 vs the shared fuzzy_score_vendor().
- N+1 / per-row query patterns: can_manage_account called per row in companies bulk/import loops (up to ~2000 round-trips), search_service is_obsolete per-PN, activity_service phone match.
- print() instead of loguru in backfill_cadence_clocks.py:36, violating the project logging rule that every sibling backfill CLI follows.

## Test-gap summary
"Coverage is thin precisely where the highest-severity bugs live. (1) Delete cascades: quote/requisition delete tests only exercise line-less quotes, so the Quote.quote_lines IntegrityError and the contact-dedup cron's child-row cascade loss are both untested. (2) Browser workers: tests patch search_engine.asyncio.run and use single-loop AsyncMocks, masking the real cross-loop Playwright reuse failure; the AI-gate poison-item/head-of-line-block path and the circuit_breaker_open reset are unexercised. (3) Buy-plan PO completion: only happy paths are covered — the line-flow-vs-deal-PO-gate race that orphans the PURCHASE_ORDER request and the automated verify_po_sent authz bypass have no tests. (4) Normalization edge cases: legacy string-form substitutes (the parse_substitute_mpns crash), normalize_moq '10K minimum', compact '30d' lead-time, and stacked-suffix MPN dedup are all untested despite being live crash/correctness paths. (5) Scoring: no test reconciles the avail-score breakdown (b6) or the sales-0-110-vs-buyer-0-100 scale that drives real payouts. (6) Authz/XSS: there are no negative tests asserting a restricted-role 403 on the htmx requisition read partials / ownership-reassignment routes, and none asserting that company/requisition/vendor/manufacturer names are HTML-escaped in the hand-built fragments. (7) Transaction-rollback discipline (knowledge regen delete-before-AI, company-merge fail-closed, Teams watermark-on-failure) is dormant or untested."
