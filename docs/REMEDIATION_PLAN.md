# Remediation Plan — Issue Backlog & Phased Fix Plan

<!-- What: Captured list of known issues/defects with a phased remediation plan.
     Who reads it: Claude Code sessions executing fixes; humans reviewing scope.
     Depends on: docs/APP_MAP_*.md for architecture context. -->

**Status:** COLLECTING — issues are being recorded from user notes. Phasing is
drafted only after collection closes and each item is verified against current code.

**Branch:** `claude/issues-remediation-plan-tdd147`
**Started:** 2026-07-21

---

## Issue Backlog

<!-- Template per issue:
### ISS-NNN: <short title>
- **Reported:** <date> (user note, verbatim gist)
- **Area:** backend | frontend | data | infra | integration
- **Severity:** P0 data-loss/security | P1 broken workflow | P2 degraded UX/perf | P3 cleanup
- **Symptom:** what the user sees
- **Suspected root cause:** with exact file paths (verified: yes/no)
- **Notes:** anything else relevant
-->

### ISS-001: Graph API 400 — OneOnOne chat creation with fewer than 2 members
- **Reported:** 2026-07-21 (Sentry AVAILAI-TC, 110 events, ongoing — last seen 1h ago)
- **Area:** integration (Microsoft Graph)
- **Severity:** P1
- **Symptom:** `app/utils/graph_client.py` `_request_with_retry` logs repeated
  `Graph 400: Creation of 'OneOnOne' chat requires 2 members`.
- **Suspected root cause:** a Teams chat-creation call is being built with only one
  member — likely the sender==recipient case or a missing/unresolved AAD user id.
  (verified: no)

### ISS-002: ENABLE_PASSWORD_LOGIN active outside tests (acknowledged auth bypass)
- **Reported:** 2026-07-21 (Sentry AVAILAI-TF / AVAILAI-DF, ongoing — last seen 1h ago)
- **Area:** backend (auth)
- **Severity:** P0 security
- **Symptom:** startup logs CRITICAL: password login enabled in non-test mode with
  `ALLOW_PASSWORD_LOGIN_RISK=true` (`app/startup.py:run_startup_migrations`).
- **Suspected root cause:** env flag left on in the deployed environment; needs either
  removal from prod `.env` or a hard fail in production mode. (verified: no)

### ISS-003: Graph API 400 — Contacts delta query uses unsupported $orderby
- **Reported:** 2026-07-21 (Sentry AVAILAI-JV, recurring — last seen 2 days ago)
- **Area:** integration (Microsoft Graph)
- **Severity:** P1
- **Symptom:** `Graph 400: ErrorInvalidUrlQuery — $orderby not supported with change
  tracking over the 'Contacts' resource`.
- **Suspected root cause:** the Contacts delta-sync request includes `$orderby`, which
  Graph change tracking rejects; the sync presumably never completes. (verified: no)

### ISS-004: Inbox scan timeout (90s) for specific user — scans silently skipped
- **Reported:** 2026-07-21 (Sentry AVAILAI-Q, recurring — last seen 4 days ago)
- **Area:** backend (jobs/email mining)
- **Severity:** P2
- **Symptom:** `app/jobs/core_jobs.py:_safe_scan` — "Inbox scan TIMEOUT for user 3
  (90s) — skipping"; that user's inbox mining repeatedly never runs.
- **Suspected root cause:** oversized mailbox or slow Graph paging exceeding the fixed
  90s budget; needs incremental checkpointing or a larger/adaptive budget. (verified: no)

### ISS-005: Test-run events polluting production Sentry
- **Reported:** 2026-07-21 (Sentry — many issues with `testserver` culprits, sqlite
  "no such table", `RuntimeError: boom`, MagicMock TypeErrors)
- **Area:** infra (observability)
- **Severity:** P2
- **Symptom:** the majority of unresolved Sentry issues are artifacts of test runs
  (e.g. AVAILAI-95, -K1/K2, -PR, -P0, -QS, -EP), burying real production errors.
- **Suspected root cause:** `SENTRY_DSN` reaches the app under `TESTING=1` (or CI);
  Sentry init in `app/main.py` lifespan needs a TESTING guard, and the stale test
  issues need bulk-resolving. (verified: no)

### ISS-006: "Task was destroyed but it is pending!" asyncio warnings
- **Reported:** 2026-07-21 (Sentry AVAILAI-PC/-PB/-K1/-K2, 28 days ago)
- **Area:** backend (async lifecycle)
- **Severity:** P3 (unless reproduced in prod — currently looks test-correlated)
- **Symptom:** asyncio tasks garbage-collected while pending, surfaced via Loguru→Sentry.
- **Suspected root cause:** fire-and-forget `asyncio.create_task` without holding a
  reference / without cancellation on shutdown (materials filters partial and others).
  (verified: no)

---

## Phased Plan

*(Drafted after collection closes.)*

- **Phase 0 — Data loss / correctness / security:** TBD
- **Phase 1 — Broken workflows:** TBD
- **Phase 2 — Degraded UX / performance:** TBD
- **Phase 3 — Cleanup / hardening:** TBD
