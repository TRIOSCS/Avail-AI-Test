# Phased Plan — Finish the Email (Outlook) + Phone (8x8) Integration

**Date:** 2026-06-23
**Scope:** Complete the *automatic tracking* of calls (8x8) and emails+meetings (Outlook) into the CRM
timeline + cadence, to a "done" state. **Teams stays deferred** (needs Graph `CallRecords.Read.All`).
Grounded in the 3-way integration audit (8x8 / Outlook / Teams).

## Already shipped (Wave A, live on main `4e0303e0`)
- **8x8:** CDR now records outcome (answered→Connected / missed→No-answer), real call time
  (`occurred_at`), duration; `is_meaningful` gated on outcome; cadence (real-time + nightly backstop)
  uses `occurred_at`.
- **Outlook calendar:** meetings are first-class `ActivityType.MEETING` rows, attendee-matched to
  contacts/companies, bump cadence, dedup on Graph event id, unlinked-fallback row preserved.
- Click-to-call **outcome prompt** + **Out/In + outcome badges** across all timelines.

## Guiding principle
Reuse the spine — `ActivityLog`, `cadence_service.bump_clocks_from_activity` (+ the nightly
`materialize_*` backstop, which must use `coalesce(occurred_at, created_at)`), `match_email_to_entity`,
`log_call_activity`/`log_email_activity`/`log_meeting_activity`, the `CallOutcome`/`details` convention.
No new subsystems. Each phase independently shippable + tested + deployed + live-verified.

---

## Phase 1 — Phone matching & capture completeness  *(WS2; the biggest remaining phone gap; 1 migration)*
Calls today match on a weak last-10-digit string via TWO divergent matchers, and **calls by reps who
aren't onboarded are silently dropped**. Decision made: true E.164 (libphonenumber + index).
- Add **libphonenumber** prod dep; add a `normalized_phone` (E.164) column + index on the phone-bearing
  tables (`SiteContact.phone`, `Company.phone`, `CustomerSite.contact_phone`, `VendorCard.phones`,
  `VendorContact.phone`) — one migration + a backfill.
- Collapse `reverse_lookup_phone` (eight_by_eight_service.py:49) and `match_phone_to_entity`
  (activity_service.py:150) into **one unified matcher** over all five tables, E.164-keyed, O(1) index
  lookup; on multi-match stamp `details={"match_ambiguous": true, "candidates": [...]}` instead of
  first-wins. Preserve the open-requisition linking (eight_by_eight_jobs.py:182-212).
- **Log inbound + unmatched calls** — drop the skip at eight_by_eight_jobs.py:150-152; write the row
  with `company_id` NULL into the existing Unmatched feed instead of vanishing.
- **Files:** `requirements.in`/`.txt`, one `alembic/versions/*` (+ backfill), `app/utils/` new phone
  helper, `app/services/activity_service.py` (unified matcher), `app/services/eight_by_eight_service.py`
  / `app/jobs/eight_by_eight_jobs.py` (use it, log inbound/unmatched, delete the dead matcher).

## Phase 2 — Call idempotency + 8x8 hygiene  *(no migration)*
- **Idempotency:** a reconcile rule so the optimistic click-to-call log and the later 8x8 CDR for the
  *same* call don't double-log / double-bump cadence (match on normalized phone + occurred_at within N
  min; keep the richer CDR row, stamp `details.merged`).
- **8x8 OAuth token caching** — stop re-authing every poll (`get_access_token` per poll today).
- **Dead-config cleanup** — remove `get_extension_map` (unused) + the hardcoded `pbxId="allpbxes"`
  / required-but-unused `pbx_id`.
- **Recording/transcript slot** — land a `recording_url` slot in `details` now (fetch deferred — needs
  the 8x8 recording API surface; see open questions).

## Phase 3 — Email enrichment & matching  *(no migration)*
Outlook email is solid both directions; finish the quality:
- **Body / AI summary on the row** — email ActivityLog rows carry only subject + a templated summary;
  add the AI `clean_summary` (reuse `activity_quality_service` `_AI_SCORED_TYPES` + the 15-min pass) so
  the timeline shows *what was said*, not just the subject.
- **Matching disambiguation** — `match_email_to_entity` silently `.first()`s on a multi-Company/Vendor
  domain hit; prefer an exact `SiteContact` email over a domain match, tie-break with
  `fuzzy_score_vendor`.
- **Calendar delta + lifecycle** — switch the daily 30-day full re-scan to `/me/calendarView/delta`
  (SyncState delta token) for incremental scans that also capture meeting **updates/cancellations**;
  guard `calendar_scan` behind `activity_tracking_enabled` (today it's unconditional — inconsistent with
  the email jobs).
- **Files:** `app/services/activity_service.py` (matcher disambiguation),
  `app/services/activity_quality_service.py`, `app/services/calendar_intelligence.py` +
  `app/jobs/email_jobs.py` (delta + flag guard), `app/utils/graph_client.py` (delta reuse).

## Phase 4 — Near-real-time  *(latency; prerequisite-gated — needs your/IT input)*
Today an inbound reply takes ~45 min (30-min poll + 15-min AI pass).
- **Graph change-notifications (webhooks)** for `/me/messages` + `/me/events` → on notification, fetch
  the one changed item and run the existing inbox/sent/meeting writers; keep the 30-min poll as the
  reconcile backstop. Reuse the existing Teams-chat webhook plumbing
  (`webhook_service.create_teams_subscription`, the renewal loop) as the structural template.
- **8x8:** 8x8 likely has no webhook product (CDR-poll only) → tighten the poll interval to ~5 min +
  the token caching from Phase 2 (confirm whether 8x8 Work Analytics exposes event subscriptions).
- **PREREQUISITES (open questions to confirm before this phase):** (a) lift/carve-out `MVP_MODE` for
  the `/api/webhooks/*` endpoint (it 404s in MVP today); (b) a public HTTPS callback URL + a validation
  secret for Graph subscriptions; (c) whether 8x8 offers webhooks.

## Phase 5 — Reliability, observability & finish  *(no migration)*
- **Connector health** — surface poll success/error + the silent-403 pattern on the Settings/Connectors
  page (today a 403 is swallowed); add per-poll metrics.
- **Live-verify the loop end-to-end** on app.availai.net: place a real 8x8 call (in + out, answered +
  missed), send + receive a real email, hold a calendar meeting → confirm each lands on the right
  contact/company timeline, with the right outcome/direction, in the expected time.
- **Docs:** update `docs/APP_MAP_INTERACTIONS.md` (the integration data-flows).

---

## Explicitly deferred / out of scope
- **Teams calls** (WS4) — blocked on Graph `CallRecords.Read.All`; **8x8↔Teams dedup** (WS5) — moot
  while Teams calls are off.
- **8x8 recording/transcript fetch** — land the `details.recording_url` slot now, fetch later (separate
  8x8 API surface).
- **Compose-email-from-CRM** — keep the Outlook deeplink; not "tracking."
- **Manual backstops** (log-a-touch quick action, account/site notes feed) — valuable but they're
  *manual logging*, not integration; track separately.

## Open questions (gate Phase 4 + the recording slot)
1. Lift `MVP_MODE` (or carve out `/api/webhooks/*`) so Graph change-notifications can run?
2. Public HTTPS callback URL + validation secret available for Graph subscriptions?
3. Does the 8x8 Work Analytics subscription include a recording/transcript API and/or webhooks, or is it
   CDR-poll only?

## Suggested order & sizing
Phase 1 (migration, ~biggest) → Phase 2 (small, correctness) → Phase 3 (medium, email quality) →
Phase 4 (larger, prereq-gated) → Phase 5 (small, verify + docs). Phases 1–3 + 5 are buildable now with
no external blockers; Phase 4 waits on the three open questions.
