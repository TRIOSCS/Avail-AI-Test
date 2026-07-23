# Resell Module — Deep Review #2 (2026-07-22) — Prioritized Findings Plan

> Second full-module audit, run after the 6-phase rework shipped (#753–#771) and alongside
> the bid-round-trip feature (PR #785). Method: 7 dimension reviewers + a 4-area completeness
> gap round (bid-back-export, bid-csv-upload, mirror-consumers, demo-seeder); every finding
> adversarially verified by a 3-lens skeptic panel (reproduce / impact / already-handled),
> 2-of-3 majority to survive. 65 raw candidates → **63 confirmed** (2 refuted): **2 P1, 28 P2, 33 P3**.
> Line anchors were re-verified by the panel against the current tree but WILL drift — re-ground
> every symbol before editing (CLAUDE.md Linear Development rule).

Severity: P0 corruption/breach/crash-loop · P1 broken workflow/wrong data, no recovery path ·
P2 silent failure, race, UX lie, perf · P3 polish/debt/doc drift.

## Sequencing

1. **Now (PR #785, before merge):** theme C — the three residuals in the new upload feature.
2. **Next PR (P1 pair):** findings 1–2 — both are single-service fixes with clear specs.
3. **Then:** theme A (lifecycle/concurrency, one PR), B (outreach truthfulness), D (UX), F (mirror).
4. **Background:** E/G/H/I/J as small focused PRs; P3 doc-drift items can batch.


## A. Lifecycle & concurrency correctness

### 1. [P1] Nightly expiry flips partially-awarded lists to terminal EXPIRED, stranding open offers
**Where:** `app/services/excess_service.py:1332` (dimension: services-core)

expire_overdue_lists selects every list with status in (open, collecting) whose close_at has passed, with no exclusion for lists that already hold a WON offer / AWARDED lines. A partial award deliberately keeps the list in COLLECTING (_apply_award_list_status only flips to AWARDED when ALL lines are decided — tested by test_resell_award.py::test_award_partial_does_not_flip_list). So: owner creates a list with a D1 'Offers close by' deadline, publishes, awards offer A on line 1 of 2 (list stays COLLECTING), and overnight the job flips the list to EXPIRED — a TERMINAL state (_TERMINAL_LIST_STATUSES at line 703). From then on award_offer raises 409 'This list is closed — it can't be reopened by awarding an offer' (line 955-956) for every remaining open/late offer, so the still-live bids on line 2 can never be resolved, and a list that actually SOLD parts is displayed as 'expired'. There is

**Fix:** In expire_overdue_lists, exclude lists that have any AWARDED line (or any WON offer) from the sweep — e.g. flip those to BID_OUT instead of EXPIRED, or skip them and log — so a partially-awarded list past deadline is never sent to a terminal state that blocks awarding its remaining live offers.

### 4. [P2] unaward_offer lacks the terminal-list guard award_offer has — unwinds awards on CLOSED/EXPIRED lists
**Where:** `app/services/excess_service.py:1048` (dimension: services-core)

award_offer 409s on a terminal list (line 955), but unaward_offer has no such guard: after the 403 owner check and the WON check (line 1048) it proceeds unconditionally. Reachable state: partial award leaves the list COLLECTING with a WON offer; the owner then runs close_list_without_bid (allowed — collecting is closeable, line 1261) → list CLOSED, or the nightly job expires it (finding above) → EXPIRED. Unaward on that terminal list then flips the winner back to OPEN, its awarded lines back to AVAILABLE, and _reopen_competing_offers (called unconditionally at line 1059) revives LOST offers to OPEN — all on a dead list where award_offer permanently 409s. The recorded sale is erased with no way to re-award it, leaving open offers frozen forever on a terminal list — exactly the D5 'terminal, no reopen' contract award enforces one function up.

**Fix:** Mirror award_offer's guard in unaward_offer: if excess_list.status in {s.value for s in _TERMINAL_LIST_STATUSES}: raise HTTPException(409, ...) immediately after the WON check.

**Resolution: FIXED.** `unaward_offer` now 409s ("This list is closed — the award can no longer be reversed") when `excess_list.status in _TERMINAL_LIST_STATUSES`, checked immediately after the not-WON guard and before any mutation; `bid_out` is not in that set and stays reversible. See `app/services/excess_service.py` (`unaward_offer`); tests in `tests/test_resell_award.py` (`test_unaward_on_terminal_list_409_no_reopen`, `test_unaward_on_bid_out_list_still_works`).

### 8. [P2] M9 award lock never refreshes the ExcessList row, so award's terminal-list guard and mirror sync read pre-lock stale status
**Where:** `app/services/excess_service.py:916` (dimension: lifecycle-concurrency)

_lock_list_for_award applies .populate_existing() to the line-item lock query and db.refresh(offer) to the offer, but the ExcessList lock query at line 916 has neither. award_offer loads the list (line 942) BEFORE taking the lock (line 946); if a concurrent close_list_without_bid / expiry commits while award blocks on the row lock, the FOR UPDATE select fetches the fresh 'closed'/'expired' row but SQLAlchemy returns the stale identity-mapped object without refreshing attributes. The terminal-list guard at line 955 then passes on the stale 'collecting' status, the award proceeds on a dead list (offer→WON, lines→AWARDED), and sync_list_mirror at line 994 — also reading the stale status — sees posting_closed=False and RE-MIRRORS the list's remaining lines as live Sightings that the close had just retired. The docstring explicitly claims the blocked-then-recheck behavior ('populate_existing

**Fix:** Add .populate_existing() to the ExcessList lock query in _lock_list_for_award (or db.refresh(excess_list) after the lock) so the terminal-status / window guards and sync_list_mirror read post-lock committed state.

**Resolution: FIXED.** `_lock_list_for_award` now composes the new shared `_lock_list_row` (`with_for_update().populate_existing()` on the ExcessList row) + `_lock_list_and_lines` (adds the line-item lock), so the ExcessList query itself refreshes the caller's identity-mapped object in place — the terminal-status guard and `sync_list_mirror` right after both read post-lock committed state. See `app/services/excess_service.py` (`_lock_list_row`, `_lock_list_and_lines`, `_lock_list_for_award`); test in `tests/test_resell_award.py::test_award_lock_refreshes_stale_excess_list_status` (a raw core UPDATE bypasses the ORM to simulate the race within one SQLite session).

### 9. [P2] submit_offer / _link_inbound_offer flip list status via unlocked last-write-wins, resurrecting a concurrently closed/expired list
**Where:** `app/services/excess_service.py:618` (dimension: lifecycle-concurrency)

submit_offer takes no _lock_list_for_award and never re-reads the list: it loads the list at line 545, stamps the offer's on-time/late status from that read (line 569), then at lines 618-619 writes status=COLLECTING if the stale in-session status is OPEN, committing at line 631. Failure scenario: T1 broker submits an offer (loads list status=open); T2 owner runs close_list_without_bid, which commits status=CLOSED (terminal, D5) + close_at=now and retires the mirror; T1 then commits UPDATE excess_lists SET status='collecting' — the terminal CLOSED list is resurrected to COLLECTING (the exact reopen the D5 contract forbids), while its mirror stays retired (drift), and the offer is stamped 'open' instead of 'late'. Because the close stamped a past close_at, the next 02:15 expiry sweep then flips the resurrected list to EXPIRED — a deliberately closed list ends up mislabeled 'expired'. The s

**Fix:** Take _lock_list_for_award (with the list-row refresh from finding 1) in submit_offer and _link_inbound_offer before reading excess_list.status, or make the flip a guarded UPDATE (`WHERE status='open'`) and re-derive the late stamp from the locked read.

**Resolution: FIXED.** Both `submit_offer` and `resell_outreach_service._link_inbound_offer` now take the new list-only `_lock_list_row` (populate_existing) BEFORE re-reading `excess_list.status` / re-validating the posted-status guard / stamping `offer_status_for_list` — a list closed between the caller's stale read and the lock can no longer be resurrected by the open→collecting flip. See `app/services/excess_service.py` (`submit_offer`), `app/services/resell_outreach_service.py` (`_link_inbound_offer`); tests in `tests/test_resell_offers.py` (`test_submit_offer_locks_list_before_status_read`, `test_submit_offer_stale_read_cannot_resurrect_closed_list`) and `tests/test_resell_outreach_service.py::test_reply_with_offer_locks_list_before_status_read`.

### 10. [P2] Offers landing after close_at but before the nightly sweep are stamped on-time 'open' — the late flag keys only on list status
**Where:** `app/services/excess_service.py:449` (dimension: lifecycle-concurrency)

offer_status_for_list decides LATE solely from list status membership in _CLOSED_LIST_STATUSES. A D1 posting window that lapses at e.g. 15:00 leaves the list in OPEN/COLLECTING until the 02:15 expire_overdue_lists cron (resell_jobs.py:33), so for up to ~11-24 hours every inbound offer (UI submit, emailed reply, manual log-bid) on the lapsed window is stamped 'open' — indistinguishable from an on-time bid — and the list keeps advertising in the 'Open to Me' lens and the Sighting mirror. The time-aware helper already exists (_posting_window_closed, line 1005, built in Phase 5 for unaward) but is not consulted at submit time, so the 'late = landed after the window closed' contract in the enum docstring is only enforced against the status column, not the actual deadline.

**Fix:** Have submit_offer / _link_inbound_offer pass the list to a window-aware check (`_posting_window_closed(excess_list) or status in _CLOSED_LIST_STATUSES`) so lateness reflects the real deadline, not the nightly sweep's lag.

**Resolution: FIXED.** `offer_status_for_list` now takes the `ExcessList` object (not a bare status string) and returns `late` when EITHER the status already reads closed OR `_posting_window_closed(excess_list)` is true — shared by `submit_offer`, `upload_bids`, and `_link_inbound_offer` so every offer-creation path stamps the same honest lateness even during the gap before the nightly sweep runs. See `app/services/excess_service.py` (`offer_status_for_list`); tests in `tests/test_resell_offers.py` (`test_submit_offer_past_close_at_is_late_even_though_status_still_open`, `test_submit_offer_before_close_at_is_open`), `tests/test_resell_bid_upload.py::test_upload_bids_past_close_at_stamps_late_even_though_status_still_collecting`, `tests/test_resell_outreach_service.py::test_reply_with_offer_past_close_at_stamps_late`.

### 11. [P2] _end_posting_window (close / close-without-bid) is an unlocked M9 sibling — a close racing an award clobbers the AWARDED status
**Where:** `app/services/excess_service.py:1261` (dimension: lifecycle-concurrency)

close_list / close_list_without_bid check `status in (open, collecting)` on an unlocked read (line 1261) and then unconditionally write the target status (line 1264). Failure scenario on PG: T1 award_offer takes the M9 lock and fully awards the list (commit flips it to AWARDED, offer WON, mirror retired); T2 close_list had already loaded the list as 'collecting' and passed the guard — its flush UPDATE merely blocks on T1's row lock, then applies status='bid_out' + close_at over AWARDED and commits. Result: a fully-awarded list (WON offer, all lines AWARDED) shown as bid_out; the workspace 'Awarded' glance loses it, and unaward_offer's `if excess_list.status == AWARDED` step-back (line 1074) no longer fires, so a later unaward leaves the status stuck at bid_out with lines back to available. Every other list-status writer in the award family goes through _lock_list_for_award; this one and

**Fix:** Take the same list+lines lock (with list refresh) at the top of _end_posting_window before evaluating the closeable guard, mirroring award/withdraw.

**Resolution: FIXED.** `_end_posting_window` now calls the shared `_lock_list_and_lines` BEFORE evaluating the closeable guard — the same M9 primitive `_lock_list_for_award` composes — so a close racing a concurrent award serializes instead of clobbering the just-awarded status. See `app/services/excess_service.py` (`_end_posting_window`, `_lock_list_and_lines`); test in `tests/test_resell_list_lifecycle.py::test_end_posting_window_locks_list_before_guard`.

### 12. [P2] assign_offer_line allows assigning an unmatched bid onto an already-AWARDED line, displacing the winner from best_offer_id
**Where:** `app/services/excess_service.py:777` (dimension: lifecycle-concurrency)

The target-line guard checks only existence and list membership — not the target's status. Concrete non-concurrent scenario: list is BID_OUT (allowed by _ASSIGN_BLOCKED_LIST_STATUSES, which blocks only closed/expired/awarded); line 1 is already AWARDED to winner W (partial award), line 2 undecided. The unmatched queue holds an open offer line from O with a higher unit_price. Owner assigns it to line 1: the guard passes, match_status flips MATCHED, and recompute_line_rollup(target.id) at line 790 — whose rollup set includes OPEN/LATE (line 426) — makes O the SOLD line's best_offer_id/best_offer_unit_price, displacing winner W from the 'Best' marker and inflating offer_count on a decided line. The salvaged bid is also a dead end: award_offer 409s ('already awarded — unaward the winner first'), and _close_competing_offers only runs on a future award, so O lingers open in the triage counts f

**Fix:** Reject a target line whose status is AWARDED/WITHDRAWN with a 409 ('unaward the winner first'), and take the M9 lock at the top of assign_offer_line.

**Resolution: FIXED.** `assign_offer_line` now takes `_lock_list_for_award` right after resolving the offer line (before any status guard), and 409s when the target line is `awarded` ("unaward the winner first") or `withdrawn` — the winner's `best_offer_id`/`best_offer_unit_price` are never displaced. See `app/services/excess_service.py` (`assign_offer_line`); tests in `tests/test_resell_award.py` (`test_assign_onto_awarded_target_line_409_winner_intact`, `test_assign_onto_available_line_on_bid_out_list_still_works`).

### 32. [P3] Award/unaward routes ignore the {list_id} path param — no offer.excess_list_id == list_id check (withdraw has one)
**Where:** `app/routers/resell.py:1535` (dimension: router-correctness)

resell_award_offer (line 1535) and resell_unaward_offer (line 1558) never verify the offer belongs to the list named in the URL — list_id is completely unused before the service call, and excess_service.award_offer/unaward_offer only check that the caller owns the offer's REAL list (excess_service.py:938-944, 1038-1044). The sibling withdraw route explicitly guards this (resell.py:1583 `if offer is None or offer.excess_list_id != list_id: raise HTTPException(404, ...)`), and assign_offer_line's service does too. Failure scenario: a user who owns lists A and B sends POST /api/resell/A/offers/{offer-on-B}/award (stale DOM after the detail pane switched lists, a replayed request, or a crafted URL) — the award executes against list B while the URL claims list A, and the response renders list B's _award_response.html (with OOB targets #tab-lines-B / #resell-chips-B that don't exist in list A'

**Fix:** In both routes load the offer first and 404 when `offer.excess_list_id != list_id`, exactly as resell_withdraw_offer does.

**Resolution: FIXED.** `resell_award_offer` and `resell_unaward_offer` both load the offer first and 404 ("Offer {id} not found on list {list_id}") when `offer.excess_list_id != list_id`, exactly mirroring `resell_withdraw_offer`. See `app/routers/resell.py`; tests in `tests/test_resell_award.py` (`test_award_route_wrong_list_id_404_nothing_mutated`, `test_unaward_route_wrong_list_id_404_nothing_mutated`).

### 37. [P3] LATE offer status silently flattened to OPEN on unaward / competitor revival
**Where:** `app/services/excess_service.py:1052` (dimension: services-core)

A LATE offer (landed after the window closed — a state the module explicitly preserves per constants.ExcessOfferStatus.LATE and offer_status_for_list) is awardable (line 696 _ACTIONABLE_OFFER_STATUSES includes LATE). unaward_offer unconditionally sets the reversed winner to OPEN (line 1052), and _reopen_competing_offers revives every LOST offer to OPEN (line 897) even if it was LATE before being closed by the award. Scenario: list is bid_out; late offer L arrives (status=late, amber 'late' pill in _offers.html line 73); owner awards W (take_all) → L marked LOST; owner unawards W → L becomes OPEN. The late flag — a user-visible review signal ('landed after your window closed') that also drives offer_status semantics — is silently destroyed on a round-trip that is supposed to be a pure reversal.

**Fix:** Recompute the revived status from the list's window instead of hardcoding OPEN: other.status = offer_status_for_list(excess_list.status) (and same for the unawarded winner), so an offer on a closed-window list reverts to LATE, not OPEN.

**Resolution: FIXED (same defect as #46 — one fix).** New `_revived_offer_status(excess_list, offer)` recomputes `late` vs `open` from `offer.created_at` vs `excess_list.close_at` (deterministic, no stored prior-status column, no migration); `unaward_offer` uses it for the reversed winner. See `app/services/excess_service.py` (`_revived_offer_status`, `unaward_offer`); tests in `tests/test_resell_award.py` (`test_unaward_revives_late_born_winner_as_late_not_open`, `test_unaward_revives_on_time_winner_as_open`).

### 46. [P3] Award→unaward round-trip erases LATE provenance: _reopen_competing_offers revives late offers as OPEN
**Where:** `app/services/excess_service.py:897` (dimension: lifecycle-concurrency)

_close_competing_offers correctly closes both OPEN and LATE competitors to LOST (lines 862, 870), but the inverse revives every qualifying LOST offer to OPEN unconditionally (line 897) — the original LATE status is not preserved anywhere. Failure scenario: list is bid_out; late offer L arrives (status='late' per offer_status_for_list); owner awards take_all winner W → L flips LOST; owner reverses the award → L flips OPEN. The 'landed after the window closed' provenance the enum docstring promises is silently lost, so the Offers tab and CSV export now show L as an on-time bid, and the owner reviewing after the unaward can no longer tell it was a post-deadline submission.

**Fix:** On revive, restore LATE when the offer originally was late — either persist the prior status before closing, or re-derive it (`offer_status_for_list`/_posting_window_closed) at reopen time.

**Resolution: FIXED (same defect as #37 — one fix).** `_reopen_competing_offers` uses the same `_revived_offer_status(excess_list, other)` recomputation instead of hardcoding `open`. See `app/services/excess_service.py` (`_revived_offer_status`, `_reopen_competing_offers`); test in `tests/test_resell_award.py::test_unaward_reopens_late_born_competitor_as_late_not_open`.

### 47. [P3] Posted-status guards live only in the router: service-level submit_offer accepts offers on drafts and outreach's _guard_owner skips the draft check
**Where:** `app/services/excess_service.py:545` (dimension: lifecycle-concurrency)

The Phase-1 architecture states 'Guards live in the SERVICE layer (routers stay thin)', and submit_offer's docstring enumerates its guards (404/403/self-offer) as if complete — but the posted-status requirement is enforced only in the router (resell.py:1473-1474 404s non-posted lists). A direct service call submits an offer on a DRAFT list: offer_status_for_list treats draft as on-time OPEN (line 449), the draft flips... nothing, but the offer persists. Same asymmetry on outreach: resell_outreach_service._guard_owner (lines 153-165) checks owner+can_post but not status, while the router 409s drafts at resell.py:1970-1971 — so a service-level outreach on a draft, followed by a reply, reaches _link_inbound_offer and attaches an offer to a private draft. That breaks a documented invariant: _get_list_for_user's expired-access relaxation (resell.py:313-316) reasons 'Drafts never carry offers,

**Fix:** Add a 404/409 non-posted guard inside submit_offer and _guard_owner (mirroring the close/award service-level guards) so direct service callers cannot attach offers/outreach to drafts.

**Resolution: FIXED.** `submit_offer` now 409s ("This list is not accepting offers") when `excess_list.status` is not in `_POSTED_LIST_STATUSES` (checked post-lock, see finding #9); `resell_outreach_service._guard_owner` 409s ("List is not posted") on a DRAFT list, mirroring the router's own guard message. See `app/services/excess_service.py` (`submit_offer`, `_POSTED_LIST_STATUSES`), `app/services/resell_outreach_service.py` (`_guard_owner`); tests in `tests/test_resell_offers.py` (`test_submit_offer_rejects_non_posted_list_service_level`, `test_submit_offer_works_on_every_posted_status`) and `tests/test_resell_outreach_service.py::TestSubmitOutreachGuards::test_draft_list_blocked`.


## B. Outreach & email truthfulness

### 2. [P1] Retry double-send guard's 'unknown' branch is unreachable — Graph outage triggers a resend
**Where:** `app/services/resell_outreach_service.py:899` (dimension: outreach-affinity)

retry_outreach_send treats an exception from email_service._find_sent_message as the UNKNOWN case (leave row INTERRUPTED, never resend). But _find_sent_message NEVER raises: every attempt's body is wrapped in 'except Exception' (email_service.py:488-489, sets api_error=True and keeps looping) and the function returns None at email_service.py:508 for BOTH a transient API failure and a genuine no-match. So the entire 'except Exception' branch at lines 900-917 is dead code, and a Graph 429/5xx/expired-token during the reconcile lookup is indistinguishable from 'positively not delivered'. Failure scenario: a row is FAILED because the Graph send call timed out AFTER the server accepted the message (the exact 'a failed row may have actually delivered' case the Phase-2 plan mandates guarding, plan line 16); the trader clicks Retry while the Sent-Items API is transiently erroring; all 3 lookup a

**Fix:** Give _find_sent_message a three-state contract (found dict | not-found | lookup-failed, e.g. raise on api_error or return a sentinel); in retry_outreach_send map lookup-failed to the existing INTERRUPTED no-resend branch and only resend on a positive not-found.

### 3. [P2] Degraded email outreach row (SENT, no graph ids) is a hard dead end — the 409 points at a route that 404s
**Where:** `app/routers/resell.py:2140` (dimension: router-correctness)

Phase 2 deliberately created a 'delivered, reply-matching degraded' state: a SENT email row with graph_conversation_id=None and send_error='delivered; reply-matching degraded (no Graph message id)' (resell_outreach_service.py:631-636). For that row every outcome-logging route is closed off: the reply viewer and convert-to-offer go through _load_outreach_for_owner which raises 404 'No email thread on this outreach' (resell.py:2120-2121); the manual log-response/log-bid routes go through _load_manual_outreach_for_owner which raises 409 'Use the reply viewer to log an email outreach's outcome' (resell.py:2140-2141) — directing the owner to the exact route that 404s for this row. The tracker template renders NO action for it either (_outreach.html: 'View reply' requires row.graph_conversation_id AND an engaged status; 'Log response'/'Log bid' require channel != 'email'; 'Retry' requires fail

**Fix:** Allow the manual log-response/log-bid path for an email row that has no graph_conversation_id (the degraded case): change _load_manual_outreach_for_owner's 409 condition to `outreach.channel == EMAIL and outreach.graph_conversation_id`, and render the Log actions in _outreach.html for email rows without a conversation id.

### 5. [P2] Recipient address is not persisted on ExcessOutreach — retry reconciles against the card's CURRENT primary email, defeating the guard
**Where:** `app/services/resell_outreach_service.py:879` (dimension: outreach-affinity)

enqueue_outreach_email sends to 'buyer.get("email") or _primary_email(card)' (line 450), but the actual recipient is never stored on the row (models/excess.py:342-366 has send_subject/send_body — persisted precisely so the retry guard can match the delivered message — but no recipient column). retry_outreach_send re-derives the address as '_primary_email(card) if card else None' (line 879) and runs the Sent-folder reconcile against THAT address. _find_sent_message requires the recipient among toRecipients (email_service.py:450-487), so if the card's emails JSON changed between send and retry (enrichment/merge prepending a new address is routine in this CRM), or the service-level per-buyer email override was used, the guard queries the WRONG mailbox address: an actually-delivered message is never found, the row is never reconciled to SENT, and the retry resends — the buyer organization re

**Fix:** Persist the send-time recipient (e.g. send_to Column(Text)) alongside send_subject/send_body in the next migration; retry should use row.send_to for both the reconcile lookup and the resend, falling back to _primary_email only for legacy rows.

### 7. [P2] 30-minute stale-'sending' threshold is only enforced by a once-nightly sweep — orphaned rows are unretryable and poll for up to ~24h
**Where:** `app/jobs/resell_jobs.py:38` (dimension: outreach-affinity)

sweep_stale_sending_outreach declares a row 'presumed orphaned' after _STALE_SENDING_MINUTES = 30 (resell_outreach_service.py:830), but the only caller is the cron job registered at CronTrigger(hour=2, minute=25) — once per day. Failure scenario: the app container restarts (deploy) at 09:00 while a background send job is mid-flight; its rows stay status='sending'. The retry route hard-409s on a SENDING row (app/routers/resell.py:2076-2077: 'Only a failed or interrupted outreach can be retried'), the tracker renders no Retry button for it, and while any row is 'sending' the Outreach tab self-polls every 3 seconds (_outreach.html:19-21, driven by any_sending at resell.py:1780). So the trader watches an un-actionable 'sending' badge — with the tab hammering the server every 3s whenever open — for up to ~17 hours until 02:25, even though the code's own threshold says the row was known-orphan

**Fix:** Run the sweep on an interval trigger (e.g. every 15-30 min — it is cheap and idempotent, selecting on the existing status index), or additionally sweep opportunistically inside the tracker render when a SENDING row is past the threshold.

### 36. [P3] resell_retry_outreach mutates outreach row state (including forging created_at) directly in the router
**Where:** `app/routers/resell.py:2090` (dimension: router-correctness)

The retry route performs the state transition itself — flips status to SENDING, clears send_error/sent_at, overwrites row.created_at with now(), and commits (resell.py:2087-2091) — business logic that belongs in resell_outreach_service (whose retry_outreach_send already performs the same reset internally when it decides to resend, per its docstring 'the row is reset to sending (with a refreshed created_at...)'). Beyond the thin-router violation, rewriting created_at destroys the row's true enqueue time unconditionally: even when the background job then reconciles the row to SENT without resending, or aborts (no buyer email -> FAILED), the original timestamp is gone. Failure scenario: a trader retries an hours-old failed touch that had actually delivered — the reconcile flips it back to SENT, but the tracker (ordered `created_at.desc()`, resell.py:1749) now shows that old touch pinned abo

**Fix:** Move the optimistic flip into a service function (e.g. mark_outreach_retrying) and track the sweep window in a dedicated column (or key the sweeper on updated_at) instead of overwriting created_at.

### 44. [P3] recompute_all_buyer_scores has no per-buyer error isolation — one bad buyer rolls back the entire nightly reconcile
**Where:** `app/services/buyer_affinity_service.py:614` (dimension: outreach-affinity)

The nightly BuyerScore backstop iterates every card and calls recompute_buyer_score (which flushes per card), then commits ONCE at the end (line 616). Any exception mid-batch — e.g. an IntegrityError on the unique ix_buyer_scores_vendor_card index (alembic/versions/133_resell_outreach_schema.py:88) when the batch's `.first()` at line 564 misses a row that a concurrent request-path recompute_buyer_score_on_win commits between the read and the flush, or any transient DB error at buyer N of M — propagates to _job_recompute_buyer_scores (app/jobs/resell_jobs.py:94-99), which rolls back EVERYTHING: zero buyers reconciled that night, with only a log line. This is the exact failure mode Phase 2 Task 7 fixed for the sibling expiry job — excess_service.expire_overdue_lists (lines 1339-1351) wraps each list in its own try/except + commit 'so a single bad list must never silently strand the whole n

**Fix:** Mirror expire_overdue_lists: wrap each card's recompute in try/except with a per-card (or small-batch) commit and rollback-and-continue on failure, returning the successful count.

### 45. [P3] no_response and opened statuses are never written by any production path; service docstrings still claim finalize advances rows to no_response
**Where:** `app/services/resell_outreach_service.py:22` (dimension: outreach-affinity)

ExcessOutreachStatus.NO_RESPONSE ('the terminal GENUINE-buyer-silence state used by the don't-forget nudge', constants.py:267-270) has no writer anywhere in app code — the only assignment is seed data (app/management/seed_sample_data.py:1404) — and there is no aging job flipping stale SENT rows to it, so a buyer who never replies shows 'sent' forever and the tracker/CSV badge mapping for 'no_response' (_outreach.html:100) is dead. Same for OPENED: it appears only in reader sets (_RESPONDED_STATUSES buyer_affinity_service.py:81, _RESPONDED_OUTREACH resell.py:1635) with no writer. Meanwhile the module docstring (line 22: 'advances each row to ``sent`` / ``no_response``') and submit_outreach_email's docstring (line 772: 'send + stamp + advance to ``sent`` / ``no_response``') still describe the pre-Phase-2 behavior — _finalize_outreach_send never writes NO_RESPONSE (correctly, per Phase 2),

**Fix:** Fix the two stale docstrings now; either add the aging job that flips aged SENT rows to NO_RESPONSE (giving the documented nudge semantics a writer) or remove NO_RESPONSE/OPENED from the enum docs, badge map, and reader sets as consciously dormant.


## C. New bid-upload feature residuals (fix in PR #785)

### 23. [P2] Bulk bid ingest silently nulls invalid or negative unit_price instead of rejecting the row — **FIXED in PR #785**
**Where:** `app/services/excess_service.py:854` (dimension: gap:bid-csv-upload)

_classify_bid_row builds accepted rows with "unit_price": _parse_price(row.get("unit_price")), and _parse_price (lines 156-165) returns None for any unparseable value (e.g. "1.2.5", "TBD", "call") AND for any negative price. Unlike a bad quantity — which rejects the row with a reason per the phase-6b 'never coerced' rule stated in the same docstring (line 798) — a bad price is silently dropped: the row stays ACCEPTED, the preview grid renders the price cell as '—' (bid_upload_preview.html:61) with no warning, and upload_bids persists an ExcessOfferLine with unit_price=None. recompute_line_rollup filters None prices (line 695 'priced = [r for r in rows if r.unit_price is not None]'), so that bid can never become best_offer_unit_price. This is the same defect class already found on the single-bid log path — the bulk path was not fixed.

**Fix:** In _classify_bid_row, distinguish blank (None is fine — price optional) from present-but-unparseable/negative: if the raw cell is non-blank and _parse_price returns None, reject the row with reason 'invalid unit price' so it appears in the preview's rejected list, mirroring the quantity rule.

**Resolution:** `_classify_bid_row` now rejects a non-blank/unparseable-or-negative price with reason "invalid unit price" before quantity/blank checks build the returned fields; a blank cell is unchanged (accepted, `unit_price=None`). See `app/services/excess_service.py` (`_classify_bid_row`); tests in `tests/test_resell_bid_upload.py` (`test_preview_invalid_unit_price_rejected_never_nulled`, `test_preview_blank_unit_price_accepted_with_none`, `test_upload_bids_invalid_unit_price_rejected*`).

### 24. [P2] Re-uploading a corrected bid sheet duplicates every previously ingested bid (no idempotency or duplicate warning) — **FIXED in PR #785**
**Where:** `app/services/excess_service.py:1005` (dimension: gap:bid-csv-upload)

upload_bids unconditionally creates a NEW ExcessOffer per bidder on every call (loop at 1005-1037) — nothing checks for an existing open offer from the same resolved VendorCard on the same list. The UI actively invites the trap: the preview shows 'N rejected' rows and offers an 'Upload another file' button (bid_upload_preview.html:90-94), so the natural flow after a partial reject is fix-the-file → re-upload the WHOLE corrected sheet → confirm. Every row that already ingested on the first confirm is ingested again: each bidder gets a second identical offer, item.offer_count doubles (recompute_line_rollup:693 counts distinct offer_ids), and the Offers tab shows two identical bids per bidder with no marker of which is stale. The owner can then award the duplicate. submit_offer has the same shape, but there the counterparty submits intentionally; here one owner action legitimately re-proces

**Fix:** On confirm, detect an existing open/late uploaded offer for the same card on the same list and either supersede it (withdraw + replace, then recompute rollups) or surface a preview-stage warning ('Broker A already has an uploaded offer on this list — confirming will add a second').

**Resolution:** `upload_bids` now supersedes (withdraw + replace) the design chosen in the review: before creating a bidder's new offer it looks up an earlier offer on this list for the same resolved VendorCard with `submitted_by == the uploading owner`, `notes == "Uploaded bid sheet"`, and status still open/late — withdraws it, and rolls both the withdrawn and new offer's matched lines into the rollup recompute. Manually-submitted offers (different notes) and won/lost offers are never touched. The result now carries a `superseded` count, surfaced in the confirm route's toast ("... replaced K earlier upload(s)") only when K>0; `preview_bid_upload` adds a cheap `supersedes_by_bidder` flag rendered as an inline note in `bid_upload_preview.html`. See `app/services/excess_service.py` (`upload_bids`, `preview_bid_upload`, `_UPLOAD_OFFER_NOTES`), `app/routers/resell.py` (`resell_bids_upload_confirm`); tests in `tests/test_resell_bid_upload.py` (`test_upload_bids_reupload_supersedes_old_offer`, `test_upload_bids_reupload_leaves_manual_offer_untouched`, `test_upload_bids_reupload_never_withdraws_won_offer`, `test_preview_flags_bidder_with_existing_upload`, `test_upload_confirm_toast_includes_superseded_count`).

### 58. [P3] Rejected-row numbers drift on sheets with blank separator rows, while the preview promises they match the file — **FIXED in PR #785**
**Where:** `app/services/excess_service.py:882` (dimension: gap:bid-csv-upload)

preview_bid_upload numbers rows with 'enumerate(rows, start=2)' assuming the parser output maps 1:1 to file rows after the header. But both parsers drop fully-blank rows before the service sees them: _parse_excel skips 'if not headers or not any(row): continue' (file_utils.py:124-125) and _parse_csv uses csv.DictReader (file_utils.py:134), which skips empty lines. The upload's own use case — 'several bidders' filled-in copies of the bid sheet, concatenated together' (upload_bids_modal.html:12-13) — makes blank separator rows between bidders likely. Every blank row shifts all subsequent rejection numbers, yet bid_upload_preview.html:29 asserts 'Row numbers match your file (the header row is row 1).' The owner opens Excel at 'Row 14', finds a valid row, and 'fixes' the wrong line. extract_mpns_with_rows documents this drift for the import path (file_utils.py:158-160); the bid path inherite

**Fix:** Have parse_tabular_file preserve source row numbers (emit them alongside each dict, as extract_mpns_with_rows already needs), or soften the template promise to 'Row numbers count non-blank rows (header = row 1)'.

**Resolution:** Scope decision taken (do not change `parse_tabular_file`'s shared contract in this PR): `bid_upload_preview.html`'s promise now reads "Row numbers count filled rows only (header = row 1) — blank rows in your file are skipped," and `preview_bid_upload`'s docstring no longer claims file-row parity. See `app/templates/htmx/partials/resell/bid_upload_preview.html`, `app/services/excess_service.py` (`preview_bid_upload` docstring); test in `tests/test_resell_bid_upload.py::test_upload_preview_blank_separator_row_shifts_numbering` (parses a real CSV with a blank separator line through `parse_tabular_file` to pin the drift).


## D. UX truthfulness & dead ends

### 13. [P2] Open-lens 'collecting' status badge + ungated stage filter is a surviving D2 offer-existence oracle
**Where:** `app/routers/resell.py:481` (dimension: security-access)

D2 hides every offer-existence signal from non-owners: _list_cards nulls coverage/offer_count (resell.py:248-250), the needs filter is force-cleared in the open lens (resell.py:472-473, whose comment explicitly names 'which anonymized Excess listing #N postings have already drawn a competing bid' as the protected signal), and the header offer chip / awarded chip are gated in _header_chips.html. But the list STATUS itself carries that exact signal: submit_offer flips a list open -> collecting on its first inbound offer (app/services/excess_service.py:617-619 'Any first offer on an OPEN list signals active collection'), and the status badge renders ungated to the open (non-owner) lens in _lists.html:64-72 ('collecting': 'bg-amber-50 ...') and _header_chips.html:21-29. Worse, the stage filter is applied in BOTH lenses (resell.py:481-482 'elif stage: query = query.filter(ExcessList.status ==

**Fix:** In the open lens, collapse the badge to a neutral 'open' for both open and collecting (template gate on can_see_customer, same predicate as D2), and restrict the open-lens stage filter to a whitelist that merges open+collecting (e.g. map stage in {open, collecting, live} -> _LIVE_STATUSES when not can_see_customer). Failure scenario: broker B loads GET /v2/partials/resell/lists?lens=open&stage=collecting and gets exactly the anonymized postings that have drawn a competing bid — the same signal t

### 14. [P2] Every resell 409 guard failure is completely silent in the UI
**Where:** `app/routers/resell.py:1139` (dimension: frontend-templates)

The global htmx error handler (app/static/htmx_app.js:586-593) deliberately suppresses the generic error toast for ALL 409 responses, on the assumption that 409s come from services/stale_guard.py which sends its own HX-Trigger showToast + HX-Reswap:none. The resell module never uses stale_guard: it raises dozens of bare HTTPException(409, ...) (resell.py:1052, 1139, 1186, 1207, 1382, 1971, 2075-2077, 2141; excess_service.py:731, 956-971, 1049, 1262) that return plain JSON via main.py:269's handler with no HX-Trigger. htmx does not swap 4xx responses by default, so every resell 409 produces zero feedback: no swap, no toast, modal stays open.

**Fix:** Attach the showToast HX-Trigger (the _toast helper pattern at resell.py:1510) to resell 409 responses — e.g. a router-level exception handler or reuse stale_guard's stale_conflict_response shape — so the suppressed-toast contract ("server owns 409 messaging") is actually honored.

### 15. [P2] Draft Delete button is dead on a deep-linked /v2/resell/{id} page
**Where:** `app/templates/htmx/partials/resell/detail.html:53` (dimension: frontend-templates)

The header Delete button targets #resell-list-body, which only exists inside the workspace shell (workspace.html:102 lazy_body('resell-list-body', ...)). A full-page load of /v2/resell/{id} (F5 after a row click pushed that URL, or a bookmark) renders detail.html alone into #main-content (htmx_views.py:142, 237-238, 286-289) — no workspace, no #resell-list-body. htmx resolves the target before sending; a missing target fires htmx:targetError and aborts, so the request is never sent.

**Fix:** Give the delete response a target that exists in both render contexts — e.g. target `closest [data-resell-detail-root]` and have the route answer with an HX-Redirect/HX-Location to /v2/resell (it already sets HX-Push-Url), or fall back to hx-target="body"-safe handling when #resell-list-body is absent.

### 16. [P2] Import-confirm toast fires 'Imported N lines' unconditionally, masking failures and clobbering the skipped-rows warning
**Where:** `app/templates/htmx/partials/resell/import_preview.html:67` (dimension: frontend-templates)

The confirm-import form's @htmx:after-request handler sets the success toast without the `if(event.detail.successful)` guard that every sibling resell form uses (offer_form.html:20, create_modal.html:14, add_line_modal.html:16, edit_line_modal.html:16, edit_list_modal.html:16). Two concrete failures: (a) on a 409 (list posted in another tab -> resell.py:1382) or any 4xx/5xx, the user sees a green 'Imported 47 lines' toast while nothing was imported and the stale preview stays on screen; (b) on success with server-side skips, Phase 6b Task 2's HX-Trigger warning toast ('N row(s) skipped (invalid quantity or blank part number)', resell.py:1397-1400) is dispatched first, then this handler overwrites the single global $store.toast with the success message — the just-shipped warning is never seen.

**Fix:** Wrap the handler in `if(event.detail.successful){...}` like its siblings, and skip the client-side toast entirely when the response carries the skipped-rows HX-Trigger (or move the success toast server-side into the same HX-Trigger channel so only one message is emitted).

### 17. [P2] List search input swaps itself away on every debounced keystroke — typing focus is lost
**Where:** `app/templates/htmx/partials/resell/_lists.html:34` (dimension: frontend-templates)

The search input targets #resell-list-body with innerHTML, but the input itself is rendered inside _lists.html, which IS the innerHTML of #resell-list-body (workspace.html:102). Every `input changed delay:300ms` request therefore replaces the element the user is typing in. htmx only restores focus to elements with a matching id, and this input has none — so after each 300ms pause the field loses focus and further keystrokes go nowhere. Every sibling list search in the app keeps the input OUTSIDE the swap target (sightings/table.html:59-64 targets #sightings-table, knowledge/list.html:13-16 targets #knowledge-list, parts/list.html:36-40 targets #parts-list with morph); resell deviates.

**Fix:** Move the filter/search bar out of the swapped region (swap only the rows container, matching the sightings/knowledge/parts pattern), or give the input a stable id so htmx's focus restoration applies, or use hx-swap="morph:innerHTML" like parts/list.html.

### 18. [P2] Header 'N offers' chip and Offers-tab badge count withdrawn/expired offers the tab never shows
**Where:** `app/routers/resell.py:352` (dimension: frontend-templates)

_detail_context computes offer_count with no status filter, but the Offers tab renders only _VISIBLE_OFFER_STATUSES (open/late/won/lost — resell.py:81-86); withdrawn and expired offers 'drop out of the tab entirely' per the module's own comment (resell.py:79-80). The count feeds both the header chip (_header_chips.html:34 '{{ offer_count }} offer{{ s }}') and the amber Offers-tab badge (detail.html:130), so after a withdrawal the owner sees a count that includes offers the tab cannot show.

**Fix:** Filter the offer_count query (and the take_all_count already does this via _UNACTIONED_OFFER_STATUSES) on _VISIBLE_OFFER_STATUSES so the chip/badge match what the tab renders.

### 20. [P2] _parse_close_at stamps UTC onto the naive datetime-local wall-clock — deadline saved shifted by the user's UTC offset, spurious 400 on near-term deadlines
**Where:** `app/routers/resell.py:167` (dimension: data-tests)

The D1 "Offers close by" input is an HTML datetime-local field, which submits the user's LOCAL wall-clock with no zone. _parse_close_at does parsed.replace(tzinfo=UTC), so a trader at UTC-4 entering "close by 17:00" persists a deadline of 17:00 UTC = 13:00 local — the posting window ends 4h early everywhere it is compared (expire_overdue_lists, the countdown chip). Worse, the create-side future check turns deterministic valid input into a hard error: a trader at 10:00 EDT (14:00 UTC) entering a deadline of 12:00 today (2h away, locally future) gets 12:00 UTC — already past — so _validate_draft_close_at raises 400 "must be in the future" for a genuinely future local deadline. The docstring calls the shift a "coarse urgency signal", but the 400-rejection branch is a hard user-facing failure, and no test pins either semantic.

**Fix:** Convert the naive wall-clock using the user/deployment timezone (e.g. a tz offset posted alongside the field via a hidden input populated from Date.getTimezoneOffset()), or at minimum document+test the intended semantics; add a test for the spurious-400 case.

### 50. [P3] 'Submit offer' button renders on closed/expired lists and dead-ends in a 'List not found' error
**Where:** `app/templates/htmx/partials/resell/detail.html:101` (dimension: frontend-templates)

_detail_context sets can_offer without any list-status check (resell.py:375 `excess_service.can_offer(user) and el.owner_id != user.id`), so a broker who reaches a now-closed/expired list (allowed when they hold an offer on it — the finding-#13 relaxation at resell.py:312-323, exactly so they can withdraw) still sees the primary 'Submit offer' button. Clicking it dispatches open-modal immediately (empty/stale modal shell opens) and the hx-get to /v2/partials/resell/{id}/offer-form 404s ('List not found', resell.py:1073-1074) — an error toast about a list the user is currently looking at.

**Fix:** Gate the context flag on posted status: `"can_offer": excess_service.can_offer(user) and el.owner_id != user.id and el.status in {s.value for s in _POSTED_STATUSES}` (is_posted is already computed in the same dict).

### 51. [P3] Open-lens empty state claims 'No postings are open to you' even when a search/filter is what emptied the list
**Where:** `app/templates/htmx/partials/resell/_lists.html:107` (dimension: frontend-templates)

The empty-state branch order tests `lens == 'open'` before `stage or needs or q`, so in the offerer lens an active search or stage filter that merely has no matches renders 'No postings are open to you right now.' instead of 'No lists match this filter.' — telling a broker the marketplace is empty when it is not.

**Fix:** Test the filter condition first: `{% if stage or needs or q %}No lists match this filter.{% elif lens == 'open' %}No postings are open to you right now.{% else %}...` so filtered-empty and genuinely-empty read differently in both lenses.

### 56. [P3] Invalid line id in assemble payload surfaces as 404 'Line item None is not part of list N' instead of 400
**Where:** `app/routers/resell.py:995` (dimension: gap:bid-back-export)

resell_assemble_bid coerces ids with `_to_int(str(s.get("excess_line_item_id")))`, which returns None for a missing key, garbage, or out-of-INT4 values. The None flows into build_bid_back, whose foreign-line guard then raises `404 "Line item None is not part of list {list_id}"` (bid_back_service.py:171). Scenario: POST selections_json `[{"customer_unit_price": "1.00"}]` (id key missing/typo'd) → the user gets a 404 with the literal word 'None' rather than the 400 'Invalid bid payload' every other malformed shape gets (resell.py:985, 987, 991).

**Fix:** In the router, 400 when any coerced excess_line_item_id is None, consistent with the adjacent payload-shape guards.


## E. Bid-back (CustomerBid) guards

### 21. [P2] No list-status guard: bid can be assembled and emailed from a DRAFT or terminal CLOSED list
**Where:** `app/services/bid_back_service.py:97` (dimension: gap:bid-back-export)

Neither build_bid_back nor send_bid_back checks excess_list.status; the only guards are owner (403), bid-is-draft, has-lines, and contact-email. The Build Bid tab is in the tab bar unconditionally (detail.html:119 `{% set tabs = [('lines','Lines'),('offers','Offers'),('build','Build Bid')] %}`) and resell_build_bid / resell_assemble_bid (resell.py:961-963, 980-981) also check only ownership. Scenario A: owner creates a DRAFT list (never posted, zero offers possible), opens Build Bid, assembles — every CustomerBidLine.customer_unit_price is None (best_offer_unit_price is None) — and clicks Send: the seller company's contact receives an official 'Bid BID-N' PDF with every price rendered as '—'. Scenario B: after 'Close without bidding' flips the list to CLOSED — which Phase 4 Task 3 defines as terminal with explicitly 'no bid-out-from-closed' (docs/superpowers/plans/2026-07-18-resell-phase

**Fix:** In build_bid_back and send_bid_back, 409 unless the list is in a biddable status set (e.g. collecting/bid_out/awarded — mirror the guard style of close_list/add-line), explicitly excluding DRAFT and the terminal CLOSED/EXPIRED states.

### 22. [P2] Emailed customer PDF is stamped 'Status: draft' — attachment rendered before the draft→sent flip
**Where:** `app/services/bid_back_service.py:357` (dimension: gap:bid-back-export)

send_bid_back renders the PDF attachment while the bid is still a draft (the 409 guard REQUIRES draft), and only flips status to SENT after the Graph send confirms (line 389). bid_back_export_context includes `"status": bid.status` and templates/documents/bid_report.html:42 renders it in the customer-facing meta table. So every bid the seller ever receives by email carries the internal token 'Status: draft' on the official purchase-bid document — wrong/confusing data on the one artifact the whole pipeline exists to keep clean. (The whitelist tests assert absent identity keys but never assert what `status` reads on the customer doc.)

**Fix:** Drop the internal status row from the customer PDF (or render a customer-appropriate label); the internal status already shows in _build_bid.html's pill.

### 55. [P3] Negative override prices accepted server-side and exported to the customer document
**Where:** `app/routers/resell.py:2576` (dimension: gap:bid-back-export)

The only floor on the per-line price is the client-side `min="0"` on the number input (_build_bid.html:84). Server-side, resell.py:_to_decimal (2571-2578) and bid_back_service._to_decimal (485-492) parse any Decimal including negatives, and neither build_bid_back nor the model validates customer_unit_price >= 0 (CustomerBidLine validates only quantity). Scenario: a crafted POST to /api/resell/{id}/bid with selections_json `[{"excess_line_item_id": 7, "customer_unit_price": "-5"}]` stores -5.0000, and the customer PDF/CSV then shows a negative unit price and negative extended, dragging the printed Total down.

**Fix:** Reject negative (and absurdly large) customer_unit_price in build_bid_back with a 400, mirroring the quantity>0 validator.

### 57. [P3] CustomerBid.status column default is the raw string "draft" instead of CustomerBidStatus.DRAFT
**Where:** `app/models/excess.py:255` (dimension: gap:bid-back-export)

CLAUDE.md mandates StrEnum constants from app/constants.py for all status values, 'never raw strings'. CustomerBid declares `status = Column(String(20), default="draft")` with a trailing raw-string comment, while the service layer right next to it uses CustomerBidStatus.DRAFT everywhere (bid_back_service.py:137, 148, 159). If CustomerBidStatus.DRAFT were ever renamed, the model default would silently diverge from the service writes; the @validates hook (excess.py:276-283) does use the enum, making the raw default the lone stray.

**Fix:** Use `default=CustomerBidStatus.DRAFT` (StrEnum value) for the column default, matching the constants convention used by the service.


## F. Mirror & sightings integration

### 25. [P2] Buyer sightings board shows phantom 'Customer Excess (list N)' scratch requirements
**Where:** `app/routers/sightings.py:391` (dimension: gap:mirror-consumers)

build_board_requirement_query (shared by the sightings board list, its stat counters, and the CSV export) filters only requisition status and sourcing status — it never excludes is_scratch requisitions. The mirror's virtual requisition is created with status=RequisitionStatus.OPEN, is_scratch=True (app/services/excess_mirror.py:102-108) and its virtual Requirement with sourcing_status=SourcingStatus.OPEN and primary_mpn=None (excess_mirror.py:117-122), so both board predicates pass. Concrete failure: publish excess list 42 → any buyer/admin opens the Sightings board → a permanent row with a blank MPN under requisition 'Customer Excess (list 42)' appears among active sourcing work (and in the CSV export and status stat-counters), one per published list. The excess_mirror.py header (lines 11-14) claims scratch reqs 'never pollute' sales views because htmx_views filters is_scratch — the sig

**Fix:** Add `.filter(Requisition.is_scratch.is_(False))` to build_board_requirement_query (and to the cached stat-count query at routers/sightings.py:487-492), matching the requisitions-list convention.

### 26. [P2] Mirror sightings are never scored/tiered — 'Customer Excess' ranks as tier 'Poor' yet sorts to the top on Postgres
**Where:** `app/services/sighting_ingest.py:34` (dimension: gap:mirror-consumers)

mirror_line builds its row via sighting_from_row, which defaults `score=item.get("score", 0)` and `evidence_tier=item.get("evidence_tier")` (None); the mirror's row dict (excess_mirror.py:186-195) supplies neither, and the mirror path bypasses _save_sightings' score_sighting_v2/tier_for_sighting entirely. tier_for_sighting also has no 'customer_excess' mapping (evidence_tiers.py:51 knows only 'excess_list' → T7; 'customer_excess' would fall through to T3), so no writer could tier it correctly anyway. Downstream, rebuild_vendor_summaries pulls mirror rows via material_card_id (sighting_aggregation.py:93-126) and produces a 'customer excess' summary with score=None (`round(max_score,1) if max_score else None`, 0 is falsy) and tier 'Poor' (_score_to_tier(0)). Consumers then order by `VendorSightingSummary.score.desc()` with no nullslast (routers/sightings.py:515 top-vendor chip, routers/sig

**Fix:** In mirror_line, set a real score (e.g. via score_sighting_v2 completeness inputs) and evidence_tier — add 'customer_excess' to tier_for_sighting (a live in-hand posting is arguably T6-level trust, not the T3 fallback); add .nullslast() to the three score.desc() order_bys.

### 27. [P2] Retiring the mirror never invalidates VendorSightingSummary — requirement vendor boards advertise closed excess supply forever
**Where:** `app/services/excess_mirror.py:228` (dimension: gap:mirror-consumers)

retire_line / sync_list_mirror / teardown_list_mirror delete the mirrored Sighting rows but never touch VendorSightingSummary, and summaries are upsert-only — rebuild_vendor_summaries (sighting_aggregation.py:197-206) updates/creates rows for vendors present in the current sighting set and there is no delete of VendorSightingSummary anywhere in app/ (grep confirms). Once a search on a requirement sharing the excess MPN materializes a 'customer excess' summary (with the posting's qty and asking price), awarding/closing/expiring the list retires the Sighting behind the summary's back and even a fresh re-search leaves the stale summary row in place ('customer excess' is simply absent from the new groups). Concrete failure: buyer searches requirement R for MPN X while list 42 is open → summary row 'customer excess, qty 500, $1.20' on R's vendor board; list 42 is awarded (mirror retired) → we

**Fix:** When retiring a mirror row, delete matching VendorSightingSummary rows (vendor_name='customer excess') for the affected requirement(s) — or make rebuild_vendor_summaries delete summary rows for vendors no longer present in the sighting set.

### 28. [P2] Global search surfaces mirror sightings and links users to the hidden scratch requisition
**Where:** `app/services/global_search_service.py:374` (dimension: gap:mirror-consumers)

fast_search's sightings group joins Sighting → Requirement and carries requisition_id 'for nav + read-gating', but unlike the requisition group (which filters `Requisition.is_scratch.is_(False)` at lines 227 and 575) it never excludes sightings hanging on scratch requisitions. Mirror rows match by normalized_mpn/mpn_matched, so a global search for a posted MPN returns a 'Customer Excess' sighting whose carried requisition_id is the hidden virtual requisition; the shared search-results template links every sighting hit to `/v2/requisitions/{{ requisition_id }}` (templates/htmx/partials/shared/search_results.html:39,49). Concrete failure: user global-searches 'LM317' while an excess posting of LM317 is live → result 'LM317 / Customer Excess' → click navigates to the requisition page for 'Customer Excess (list N)' — a system-owned scratch requisition that every other surface deliberately hi

**Fix:** Join Requisition in the sightings group (fast_search and the intent-query sighting branch at line 623) and add `Requisition.is_scratch.is_(False)` — or exclude `Sighting.source_type == 'customer_excess'` from global search results.

### 38. [P3] excess_mirror docs still describe the retired (source_company_id, material_card_id) upsert key
**Where:** `app/services/excess_mirror.py:21` (dimension: services-core)

Phase 5 (#18, migration 199) moved the mirror upsert to line-identity (Sighting.excess_line_item_id — implemented in _find_mirror at lines 128-145). But the module docstring's 'Dedup trap' paragraph still states 'The mirror upserts by (source_company_id, material_card_id)', and the update-path comment in mirror_line repeats it. A maintainer following the module header (the CLAUDE.md-mandated authoritative file docs) would reason about duplicate-part collapse and sibling-list wipes from the pre-199 model — the precise behavior finding #18 fixed.

**Fix:** Rewrite both comments to name the excess_line_item_id line-identity key (mirroring the accurate docstrings already on _find_mirror/mirror_line/retire_line).

### 59. [P3] L3 vendor-affinity aggregation counts mirror rows — suggests synthetic 'Customer Excess' as a supplier
**Where:** `app/services/vendor_affinity_service.py:187` (dimension: gap:mirror-consumers)

find_affinity_vendors_l3 aggregates ALL sightings joined to MaterialCard by category, grouped by (vendor_name_normalized, vendor_name), with no source_type exclusion. Mirror rows always have material_card_id set (it is the mirror's linkage key) and vendor_name='Customer Excess', so once excess postings exist in a category, the synthetic internal label enters the affinity vendor rows; _vendor_results_from_rows (lines 32-44) does not require a VendorCard (`vendor_id: vc.id if vc else None`) so it is emitted as a suggested vendor for the search fan-out. This is precisely the 'pollute vendor analytics' hazard the mirror's own comment (excess_mirror.py:51-54) says the design must avoid — it avoided VendorCard creation but not vendor-name-level aggregation. Concrete failure: several capacitor excess lines posted → L3 affinity for a capacitor MPN ranks 'Customer Excess' (mpn_count inflated by m

**Fix:** Add `Sighting.source_type != 'customer_excess'` (or an allowlist of connector source types) to the L3 aggregation.


## G. Buyer affinity & nudges

### 6. [P2] not_yet_offered_strip applies the limit BEFORE subtracting already-offered buyers — nudge starves right after a big campaign
**Where:** `app/services/buyer_affinity_service.py:767` (dimension: outreach-affinity)

The don't-forget nudge calls rank_buyers_for(db, excess_list_id=..., limit=limit) which slices to the top `limit` (default 20) at rank_buyers_for line 444, and only THEN subtracts the buyers already touched on this list (line 784). History/commodity buyers sort ahead of the engagement tier, so with more than `limit` commodity-history buyers the slice is entirely history buyers. Failure scenario: 25 reachable tier-2 commodity buyers exist; the trader runs a campaign to the ranked top 20; on the next render the strip computes ranked=those same top 20, all in `already`, and returns [] — 'every historical buyer has already been offered' — while buyers 21-25 (genuine commodity history, never offered) never surface. The starvation also propagates to the durable side: resell_not_yet_strip (app/routers/resell.py:1919-1931) creates My-Day follow-up tasks only for the strip's buyers, so those task

**Fix:** Compute the `already` set first and pass it into rank_buyers_for as an exclusion (or rank with a larger headroom, e.g. limit + len(already), then slice after subtraction) so the strip fills from not-yet-offered history buyers.

### 35. [P3] Offer-to-buyers panel accepts line_ids from ANY list — scope_lines query and rank_buyers_for are never scoped to the URL's list
**Where:** `app/routers/resell.py:1713` (dimension: router-correctness)

resell_offer_buyers_form parses line_ids from the query string (resell.py:1825) and _buyer_panel_context loads them with `db.query(ExcessLineItem).filter(ExcessLineItem.id.in_(line_ids)).all()` (1713) with no `excess_list_id == el.id` filter; _suggestion_rows likewise passes the raw ids to buyer_affinity_service.rank_buyers_for (1650-1654), which ranks buyers off those lines' material cards with no list check. The SEND path validates (`_target_line_ids` 422s on ids 'not on list', resell_outreach_service.py:186-188), so the panel and its submit disagree. Failure scenario: GET /v2/partials/resell/{A}/offer-buyers-form?line_ids={ids-от-list-B} (stale nudge-chip URL after lines were deleted/moved, or a crafted URL) renders a panel captioned 'N selected lines' whose ranked buyers are computed from lines that are not on list A — including lines of ANOTHER owner's private DRAFT (tier-1 'bought

**Fix:** In _buyer_panel_context filter the line query with `ExcessLineItem.excess_list_id == el.id` and 404/422 (or silently drop) ids not on the list before passing them to rank_buyers_for and the template.

### 49. [P3] resell_offer_buyers_form accepts line_ids from other lists (no ownership/scope validation)
**Where:** `app/routers/resell.py:1825` (dimension: security-access)

The offer-to-buyers panel parses ?line_ids= into ints and passes them straight into _buyer_panel_context without verifying they belong to the authorized list: scope_lines is fetched by bare id (resell.py:1713 `db.query(ExcessLineItem).filter(ExcessLineItem.id.in_(line_ids)).all()`) and the same ids are fed to buyer_affinity_service.rank_buyers_for (resell.py:1650-1654), whose _target_lines also fetches by bare id with no list scoping (buyer_affinity_service.py:141-142). The POST twin is properly guarded — resell_outreach_service._target_line_ids rejects foreign lines with 422 (resell_outreach_service.py:186-188: 'Line item(s) {bad} are not on list') — so the GET panel is the one hole. An owner of any list can pass another trader's line ids (including lines of a 404-masked private draft): the modal renders the foreign line count ('N selected lines on ...', offer_buyers_modal.html:56) and

**Fix:** In resell_offer_buyers_form, filter the parsed ids to lines on the authorized list (e.g. reuse resell_outreach_service._target_line_ids, or add `.filter(ExcessLineItem.excess_list_id == el.id)` and drop/422 mismatches) before building the panel context. Failure scenario: trader B (owner of list 50) requests GET /v2/partials/resell/50/offer-buyers-form?line_ids=777 where 777 is a line on trader A's private draft — the request succeeds and the panel/ranking is computed against A's draft line inste


## H. Data layer, migration & schema drift

### 19. [P2] Migration 199's clean-sweep DELETE strands already-open lists with no mirror and no rebuild path
**Where:** `alembic/versions/199_sighting_excess_line_fk.py:58` (dimension: data-tests)

Migration 199 deletes ALL customer_excess Sightings, claiming they are "rebuilt line-keyed on the next publish/sync". But sync_list_mirror only fires on publish (draft->open, excess_mirror.py:388), award/unaward (excess_service.py:994, 1081), close (1272), expire (1343), and the demo seed. A list that is already open/collecting at deploy time hits NONE of these while it is live — an idle open posting's live-supply rows vanish at upgrade and never come back for the remainder of its window (the events that would resync it are exactly the ones that end/retire it). The matcher and "Open to Me"/live-supply surfaces silently omit that supply. No startup.py idempotent backfill or nightly job resyncs open lists (app/jobs/resell_jobs.py registers only expiry, stale-send sweep, and buyer-score jobs), and no test covers the post-sweep resume behavior for an open list.

**Fix:** Add an idempotent backfill (startup.py backfills are explicitly permitted) or extend the nightly expiry job: for each open/collecting ExcessList with zero customer_excess Sightings, run sync_list_mirror. Add a test seeding an open mirrored list, deleting its sightings (simulating the 199 sweep), and asserting the backfill restores line-keyed rows.

### 52. [P3] ExcessLineItem.status has no @validates guard — the only resell status column that accepts arbitrary raw strings
**Where:** `app/models/excess.py:116` (dimension: data-tests)

Every other status-bearing resell model validates against its StrEnum via @validates: ExcessList.status (excess.py:77), ExcessOffer.status (177) and scope (168), ExcessOfferLine.match_status (225), CustomerBid.status (276), ExcessOutreach.channel/status (374/383). ExcessLineItem.status — for which ExcessLineItemStatus exists in app/constants.py:210 — has only a quantity validator (excess.py:124-128). A writer assigning a typo'd or legacy raw string (line.status = "availble") persists silently; the mirror's active-line predicate and the ix_excess_line_items_pn_status/status query paths then misclassify the line (an unknown status is treated as inactive, so its Sighting is silently retired), with no error anywhere.

**Fix:** Add the same @validates("status") pattern validating against ExcessLineItemStatus, plus a model test mirroring tests/test_resell_models.py's invalid-status cases.

### 53. [P3] app/schemas/excess.py is dead to app code and drifted — ExcessListUpdate still whitelists remapped legacy statuses, and tests lock the drift in
**Where:** `app/schemas/excess.py:44` (dimension: data-tests)

No router or service imports anything from app/schemas/excess.py — routers use Form fields and services take kwargs (grep: the only consumers are tests/test_models_excess.py and tests/test_resell_models.py), yet the header claims "Called by: routers/resell.py". The module has drifted from the reworked model: ExcessListUpdate.status is a raw-string Literal (violating the StrEnum convention) that still permits "active" and "bidding" — the exact legacy statuses migration 193 remapped away and the publish guard rejects — and ExcessListResponse omits open_at/close_at although the D1 posting window is now a real model feature. tests/test_models_excess.py:152-154 (test_update_with_valid_status) asserts ExcessListUpdate(status="active") is VALID, i.e. assertion theater that actively guards a contradiction of the phase-1 lifecycle cutover.

**Fix:** Either wire the schemas into the routes or delete the unused ones; at minimum drop "active"/"bidding" from the Literal (use ExcessListStatus values), fix the header comment, and retarget the tests so they stop enshrining legacy statuses.

### 54. [P3] Stale posting-window comment on the ExcessList model contradicts the phase-5 D1 behavior it sits next to
**Where:** `app/models/excess.py:62` (dimension: data-tests)

The model comment still describes the pre-phase-5 semantics: "open_at stamped on publish, close_at on close_list. Both nullable — a draft has neither". Since PR-phase-5 Task 1, close_at is set at CREATE (the optional "Offers close by" deadline), preserved by publish_list, and only additionally stamped by _end_posting_window — a draft very much can carry close_at (tests/test_resell_posting_window.py:67-75 proves it). The next engineer reading the model will reason from the wrong lifecycle (e.g. assume close_at != None implies the window ended, which is exactly the finding-#2 bug class the phase fixed).

**Fix:** Rewrite the comment: close_at is the optional owner-set deadline (create/update, draft scope), preserved on publish, and stamped at resolution by close/expire.

### 39. [P3] recompute_line_rollup docstring omits LATE from the counted statuses
**Where:** `app/services/excess_service.py:651` (dimension: services-core)

_ROLLUP_OFFER_STATUSES (line 426) is (OPEN, WON, LATE), with a lengthy comment explaining why LATE must be included, but recompute_line_rollup's docstring still says the rollup counts lines 'whose parent offer is in an active state (open/won)'. A reader trusting the function's own contract would conclude late bids are excluded from best_offer_unit_price/offer_count — the opposite of the implemented (and deliberate) behavior.

**Fix:** Change the docstring to '(open/won/late)' to match _ROLLUP_OFFER_STATUSES.


## I. Router/service discipline & guards

### 31. [P3] resell_import_confirm validates JSON syntax but not shape — non-list or non-dict payload reaches confirm_import and 500s
**Where:** `app/routers/resell.py:1389` (dimension: router-correctness)

resell_import_confirm only guards json.loads (lines 1385-1388) and then passes the parsed value straight to excess_service.confirm_import. confirm_import iterates `for raw in rows` and calls _parse_import_row -> _normalize_row, which does `raw.items()` (excess_service.py:130). Failure scenarios: rows_json='5' -> `for raw in 5` TypeError 500; rows_json='[1,2]' or '["x"]' -> `1.items()` AttributeError 500; rows_json='{"a":1}' -> iterates the dict's string keys -> `"a".items()` AttributeError 500. This is exactly the crash class Phase 6b fixed for the sibling resell_assemble_bid (the `isinstance(raw, list)` + all-dicts guard at resell.py:921-926) but the import-confirm route was not given the same guard, so a tampered/corrupted hidden all_valid_rows_json field produces an unhandled 500 instead of a 400.

**Fix:** After json.loads add: `if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows): raise HTTPException(400, "Invalid import payload")` (mirroring resell_assemble_bid).

### 33. [P3] resell_add_line inlines line-creation business logic in the router (private service helper, counter bump, commit) and accepts whitespace-only part numbers
**Where:** `app/routers/resell.py:1149` (dimension: router-correctness)

resell_add_line constructs the ExcessLineItem ORM row, calls the private excess_service._resolve_line_material_card, increments el.total_line_items and commits — all in the router (resell.py:1147-1162), violating the routers-thin/CLAUDE.md rule, while its siblings resell_update_line/resell_delete_line delegate to excess_service.update_line/delete_line where the guards live. Because it bypasses the service, it also misses the blank-part-number rejection every other write path has: the import parser skips 'blank part_number' rows (excess_service.py:174-176) and confirm_import re-validates, but the quick-add stores part_number verbatim. Failure scenario: POST /api/resell/{id}/lines with part_number=' ' (a single space passes both the HTML required attribute and FastAPI's empty-string-as-missing check) and quantity=5 -> a line is created with a whitespace part_number and normalized_part_numb

**Fix:** Move the creation into an excess_service.add_line(...) service function that strips and rejects blank part numbers (400), mirroring update_line; router keeps only the auth/draft/qty guards.

### 34. [P3] resell_import_preview lacks the draft-only 409 guard its confirm counterpart enforces — late failure after upload+review
**Where:** `app/routers/resell.py:1338` (dimension: router-correctness)

resell_import_confirm 409s on a non-draft list (resell.py:1381-1382: 'Posted lists are locked...'), but resell_import_preview (1329-1367) has no status check at all — it parses the upload and renders the preview grid for a posted/closed list. Failure scenario: an owner has the draft's Lines tab open in a stale tab (or replays the request) after the list was published elsewhere; the file upload succeeds, the preview grid renders fully with valid rows and a Confirm button, and only the final confirm click fails with the 409 — the whole upload/review effort is wasted at the last step instead of being rejected up front, and the preview implies an import is possible on a locked list.

**Fix:** Add the identical `if el.status != ExcessListStatus.DRAFT: raise HTTPException(409, ...)` guard to resell_import_preview.

### 40. [P3] update_excess_list unconditionally clears customer_site_id on every header edit
**Where:** `app/services/excess_service.py:1214` (dimension: services-core)

update_excess_list gives close_at a dedicated _UNSET_CLOSE_AT identity sentinel (lines 198-202) precisely because 'the draft-edit form carries no deadline input' and a plain None default 'cannot express both'. customer_site_id has the identical hazard but no sentinel: it defaults to None and is assigned unconditionally at line 1214, so any caller editing just title/notes silently NULLs a stored customer_site_id. Today the HTMX router never sets the field (resell_update_list passes title/notes/company_id only), so the wipe is latent — but the service's documented contract ('Updates title / notes / customer_site_id') is a data-loss trap for the first caller that creates a list with a site (create_excess_list accepts it; ExcessListCreate schema exposes it at app/schemas/excess.py:32).

**Fix:** Give customer_site_id the same unset-sentinel treatment as close_at (or drop the parameter until a form actually carries it).

### 41. [P3] confirm_import bypasses the service-level owned-draft guard the other line mutators enforce
**Where:** `app/services/excess_service.py:379` (dimension: services-core)

Phase 4 established _require_owned_draft (lines 1105-1117) 'so a direct service call is protected even if the router guard is bypassed', and delete_line/update_line/update_excess_list/delete_excess_list all use it. confirm_import (and import_line_items) mutate the same line rows but perform no owner or draft check at the service layer — get_excess_list only 404s a missing id. A direct call (job, management command, future second router) can inject line items into a posted/awarded/closed list owned by anyone; the new lines would also never be mirrored (nothing triggers sync_list_mirror), silently drifting the Sighting mirror from the line table. Only the router's guards (resell.py:1380-1382) currently prevent this over HTTP.

**Fix:** Thread the acting user into confirm_import/import_line_items and route the load through _require_owned_draft (keeping the router guard as the outer layer), matching the Phase-4 pattern.

### 42. [P3] resell_add_line builds the line item in the router with a bare db.commit() and a private service helper
**Where:** `app/routers/resell.py:1160` (dimension: services-core)

resell_add_line constructs the ExcessLineItem ORM row, normalizes the MPN, calls the private excess_service._resolve_line_material_card, maintains the total_line_items counter, and commits — all in the router (lines 1147-1162), violating the 'routers thin, business logic in services' rule its sibling endpoints follow (update/delete/import all delegate to service functions). It also commits with a bare db.commit() instead of the service's _safe_commit, so an IntegrityError surfaces as an unhandled 500 rather than the mapped 409 every other resell write path returns. The service already owns this exact logic (import_line_items/update_line build identical rows), so the duplication is also a drift risk — e.g. condition/date_code default handling now lives in two places.

**Fix:** Add an excess_service.add_line(db, list_id, owner, *, part_number, quantity, ...) that reuses _require_owned_draft + _safe_commit, and have the router delegate to it.

### 43. [P3] Dead 'Phase 4: Stats' / 'Phase 4: Normalization backfill' section headers with no code
**Where:** `app/services/excess_service.py:1358` (dimension: services-core)

The file ends (lines 1356-1363) with two full section-header banners — '# Phase 4: Stats' and '# Phase 4: Normalization backfill' — followed by nothing. The functions they once introduced no longer exist in the file (the router computes its own stat strip in _stat_strip, resell.py:261). The empty banners mislead a reader into hunting for stats/backfill logic here and violate the codebase's own file-header hygiene.

**Fix:** Delete the two empty section banners.

### 48. [P3] Draft-list existence oracle: owner-only endpoints return 403 (not 404) for another user's private draft
**Where:** `app/routers/resell.py:896` (dimension: security-access)

The documented draft-privacy policy is 404-masking: _get_list_for_user (resell.py:303-324) states 'drafts are private to the owner (404, not 403, to avoid revealing the list's existence)', and the Phase-1 edit paths explicitly added the mask ('404-mask a non-owner on a private draft (finding #3)' at resell.py:1231, 1257, 1275, 1296). But a large set of owner-only endpoints skip _get_list_for_user and call excess_service.get_excess_list + _require_owner (or the service equivalents) directly, so an existing-but-foreign list — including a private DRAFT — answers 403 while a nonexistent id answers 404. Affected: resell_build_bid :896-897, resell_assemble_bid :915-916, resell_bid_pdf :955-956, resell_add_line :1136-1137, resell_import_preview :1338-1339, resell_import_confirm :1379-1380, resell_publish :1412-1416, resell_offer_buyers_form :1823-1824, resell_outreach_tracker :1846-1847, resell

**Fix:** Route these endpoints through _get_list_for_user (which 404-masks non-owner drafts) before the owner check, or make _require_owner raise 404 when the list is still a draft. Failure scenario: trader B sweeps GET /v2/partials/resell/{id}/build-bid over ids — 403 vs 404 tells B exactly which ids exist as trader A's private drafts, defeating the 404 mask the detail/edit routes enforce for the same lists.


## J. Seeders & demo data

### 29. [P2] seed_test_data.py crashes: ExcessLineItem built with removed market_price/demand_score attributes
**Where:** `scripts/seed_test_data.py:614` (dimension: gap:demo-seeder)

seed_excess_lists constructs ExcessLineItem with market_price= and demand_score= kwargs, but the current model (app/models/excess.py:93-119) no longer defines those attributes (only the orphan DB columns remain — their drop is the deferred item; the ORM attributes are already gone). SQLAlchemy's declarative constructor raises TypeError ('market_price' is an invalid keyword argument for ExcessLineItem) on the very first line item. Because main() (scripts/seed_test_data.py:654-699) does a single db.commit() at line 670 after all seeders, the exception rolls back EVERYTHING — companies, requisitions, quotes, buy plans — so the whole seed script is dead, not just the excess section.

**Fix:** Delete the market_price/demand_score kwargs (and modernize the section to post-rework statuses while touching it).

### 30. [P2] scripts/seed_test_data.py has no production guard, unlike the resell demo seeder
**Where:** `scripts/seed_test_data.py:657` (dimension: gap:demo-seeder)

seed_resell_demo.main() refuses to run without ALLOW_SAMPLE_DATA_SEED (seed_resell_demo.py:372-378, covered by tests/test_seed_resell_demo_guard.py), and the header says it 'mirrors the AVSAMPLE seed guard'. But scripts/seed_test_data.py — which seeds excess lists, offers, quotes, buy plans and companies — opens app.database.SessionLocal directly with no opt-in check at all, and its own header (line 15) instructs running it inside the production app container: 'docker compose exec app python scripts/seed_test_data.py'. Whatever DATABASE_URL the container has (i.e. prod) gets synthetic data injected ungated. Today the TypeError in finding #1 accidentally aborts it, but once that is fixed a mistaken run seeds prod with pre-rework demo shapes.

**Fix:** Add the same ALLOW_SAMPLE_DATA_SEED refusal (checked before SessionLocal) to seed_test_data.main(), plus a guard test mirroring test_seed_resell_demo_guard.py.

### 60. [P3] Demo 'awarded' list is stamped awarded directly — zero offers, no WON offer, violating the M9 award invariant
**Where:** `app/management/seed_resell_demo.py:312` (dimension: gap:demo-seeder)

_build_awarded creates the 'Demo · Awarded FPGA lot' list with status=ExcessListStatus.AWARDED at construction and flips its 6 lines to ExcessLineItemStatus.AWARDED by direct assignment — it never goes through award_offer (app/services/excess_service.py:1297-1376), which is documented as 'the single chokepoint where an ExcessOffer becomes won'. Result: an awarded list with ZERO ExcessOffer rows, no WON offer, no best_offer_id/offer_count rollups, and awarded lines that no offer owns. This is a state unreachable in the post-Phase-4 app: the Offers tab of an 'awarded' demo renders empty (nothing shows the emerald won card in _offers.html:155-183, which keys on offer.status == 'won'), the 'Unaward' inverse cannot be demonstrated (unaward_offer, excess_service.py:1401-1423, 409s: 'This offer is not awarded — nothing to reverse' — and there is no offer to click at all), and the lines are perm

**Fix:** Seed the awarded list like the others (status OPEN + lines), submit a per-line offer from the demo broker, then call excess_service.award_offer(db, offer.id, trader) so the WON offer, rollups, list status, and mirror all derive through the real chokepoint.

### 61. [P3] Sample-data 'customer_excess' Sightings lack excess_line_item_id — invisible to the Phase-5 mirror lifecycle
**Where:** `app/management/seed_sample_data.py:1349` (dimension: gap:demo-seeder)

_seed_wf_e hand-rolls excess→demand Sightings with source_type='customer_excess' on its own AVSAMPLE scratch requisition via _mk_sighting, which never sets excess_line_item_id (see its defaults/key at seed_sample_data.py:1466-1484 — no excess_line_item_id) and never uses the list's 'Customer Excess (list N)' virtual requisition. The post-rework mirror machinery keys everything on excess_line_item_id (_find_mirror, app/services/excess_mirror.py:137-145) and retires/tears down by the list's virtual requirement. So these seeded sightings are unmanaged: if a user awards, closes, or expires ex1 in the app (or deletes it — delete_excess_list/teardown_list_mirror only delete customer_excess sightings hanging on the list's virtual req, excess_mirror.py:250,285), the AVSAMPLE customer_excess sightings stay live, advertising supply for a resolved list to search/matchers. Conversely, publishing ex1

**Fix:** Replace the hand-made sightings with a call to excess_mirror.sync_list_mirror(db, ex1) (as seed_resell_demo does), so the sightings carry excess_line_item_id and live on the managed virtual requisition.

### 62. [P3] Collecting demo list auto-expires after 3 nights and the 'idempotent' re-seed cannot restore it
**Where:** `app/management/seed_resell_demo.py:188` (dimension: gap:demo-seeder)

_build_collecting seeds the flagship 40-line list with close_at = now + 3 days (close_in_days=3). The nightly expiry job (_job_expire_resell_lists → expire_overdue_lists, excess_service.py:1703-1719) flips any open/collecting list past close_at to 'expired' and retires its Sighting mirror. So on staging, 3 days after seeding, the primary demo — the one the header says exists so 'the user can open /v2/resell ... and judge the look' of the collecting shape — silently becomes an expired list with a torn-down mirror. Re-running the seeder does NOT recover it: _get_or_create_list finds the list by title and returns immediately ('Sets status + close_at on create only', lines 136-139), and _list_has_offers short-circuits the rest, so the documented 'safe to re-run' idempotency leaves the demo permanently expired; only --reset then reseed restores it.

**Fix:** On find (created=False), refresh the demo window when the list has decayed: if status is expired/close_at past, reset status to COLLECTING, push close_at forward, and re-sync the mirror — or seed a longer window and document the decay.

### 63. [P3] seed_excess_lists seeds pre-rework legacy statuses, owner self-offers, and offers with zero rollups
**Where:** `scripts/seed_test_data.py:630` (dimension: gap:demo-seeder)

Beyond the crash in finding #1 (which currently masks this), seed_excess_lists writes shapes the 6-phase rework made unreachable: (a) lists in the legacy ACTIVE/BIDDING statuses (lines 564-565) that constants.py:171 marks 'kept for backward-compat ... a later cutover chunk retires them' — new seeded data should use the open/collecting/bid_out lifecycle; (b) every ExcessOffer has submitted_by=user.id where the SAME user owns every list (line 593 owner_id=user.id), a self-offer the Phase-1 guard forbids (excess_service.py:585-586: 'You cannot offer on your own excess list') and that _link_inbound_offer only permits with a buyer offerer_vendor_card attribution and outreach provenance; (c) offer statuses cycle through the whole enum (lines 626, 633) so WON offers land on lists whose line statuses were cycled independently (line 604) — a won offer whose lines aren't awarded, violating the M9

**Fix:** Rewrite the section on top of the fix for finding #1: use post-rework statuses, submit offers via excess_service.submit_offer under a distinct non-owner user, and derive WON via award_offer.


## Refuted by the skeptic panel (for the record)

2 findings were killed in verification (details in the run journal `wf_15b6d70f-a29`).


## Coverage notes

- Gap round confirmed the four originally-uncovered areas and contributed 19 of the 63 findings.
- Known-deferred items (market_price/demand_score orphan drop, excess_lists.version, total_line_items) were excluded by design.
