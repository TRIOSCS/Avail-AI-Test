# Wave-1 48h-quiet pre-flight — availai-simp log clustering

Date: 2026-08-04. Gate under test: spec §11 / §10 Wave-1 acceptance — "zero
recurring warnings in a 48h log window."

## Observation window

- `availai-simp-app-1`: 17:50:52 → 19:21:53 UTC (~91 min, full container
  history; 787 log lines, 121 WARNING/ERROR/CRITICAL).
- `availai-simp-enrichment-worker-1`: clean — 1 startup env-var warning,
  then hourly INFO only.
- The RUNNING container is a PRE-shrink build: it still schedules
  webhook_subs, poll_signature_batch, batch_parse_signatures, health_ping,
  quality_score_activities, po_verification, buyplan_nudge (all silent in
  the window except health_ping). The worktree already carries the W1
  shrink (e.g. `app/jobs/health_jobs.py` now registers nothing).

## Cluster table

| # | Message shape | Level | Count | Cadence | Root source | Covered by W1? |
|---|---|---|---|---|---|---|
| 1 | `Token refresh failed: 404 —` + `Token refresh failed for mkhoury@trioscs.com` (paired) | WARNING | 20 + 20 | every 5 min (+2 extra pairs per 15-min health cycle from the azure_oauth ping) | `token_refresh` job (`app/jobs/core_jobs.py:28`, KEEP — §3 kernel) → `app/utils/token_manager.py:_refresh_access_token`. `AZURE_TENANT_ID` unset ⇒ token URL `login.microsoftonline.com//oauth2/v2.0/token` ⇒ 404. Job selects `users WHERE refresh_token IS NOT NULL` (user 3 has a prod-restored refresh_token) and ignores `m365_connected=False`, so it retries forever. | **FLAG — NOT COVERED.** ~1,150 WARNING lines / 48h. Kernel job is kept; §7 keys-off honesty covers UI controls only, and W1_JOB_DISPOSITION adds no Azure-keys-off guard to this job. |
| 2 | `WORKER WATCHDOG: {ICS\|NetComponents\|The Broker Forum} worker heartbeat is stale (last seen Nm ago)` | ERROR | 2 × 3 workers | every ~65 min per worker (check 5 min, stale >15 min, Redis debounce 60 min — `config.py:360-362`) | `worker_liveness_check` (`app/jobs/worker_liveness_jobs.py`, KEEP — §3 kernel). The simp stack runs NO ICS/NC/TBF worker containers, while the restored-prod singletons say `is_running=t` with 13:12–13:13Z heartbeats — stale forever. Enrichment heartbeats fine (19:21Z). | **FLAG — NOT COVERED.** ~132 ERROR lines / 48h (44 firings × 3). Environment artifact — prod runs the workers — but the gate measured on THIS instance fails. |
| 3 | `Health ping failed for {hunter\|anthropic\|azure_oauth\|explorium\|clay\|lusha\|teams_notifications}: …` | WARNING | 42 (6 cycles × 7 sources) | every 15 min | `health_ping` job → `app/services/health_monitor.py` keyless probing of active ApiSources | **COVERED** — health_ping DELETE landed in the worktree (`health_jobs.py` registers no jobs; disposition + §5.5 "keys-off shown honestly, not polled"). |
| 4 | `ENCRYPTION_SALT not set — using legacy static salt for credentials` | WARNING | 18 (3 per health cycle) | rides the 15-min health cycle | `app/services/credential_service.py:_get_fernet`, invoked by health-ping credential reads | **COVERED** (recurrence dies with health_ping). Residual: still fires on any on-demand credential read; setting `ENCRYPTION_SALT` silences it at the source. |
| 5 | `API source health alert: [api_source_down] …` + `Source X transitioned live → error` | WARNING | 4 + 4 | one-time state transitions on the first ping cycle | health_monitor `_notify_admins` / `_check_status_transition` | **COVERED** (same health_ping delete); not recurring anyway. |
| 6 | Startup one-timers: missing env vars (shell + lifespan), ENABLE_PASSWORD_LOGIN CRITICAL (accepted non-prod risk), encrypted_type salt, connector erroring/not-configured pair, 28 non-canonical material categories | WARN/CRIT | 7 lines | once per boot | `app/startup.py`, `app/main.py`, `app/connector_status.py`, `app/utils/encrypted_type.py` | Not recurring — gate-neutral. The connectors-page honesty pass (§5.5/§7) addresses the display side. |

Line accounting: 68 recurring-covered (42+18+4+4) + 46 recurring-NOT-covered
(40+6) + 7 one-time = 121. Worker container contributes zero WARNING/ERROR.

## The two gate-failers (what W1 must still address)

1. **token_refresh 404 spam (every 5 min).** Options: (a) keys-off guard in
   `_job_token_refresh` — skip with a single startup notice when
   `azure_client_id`/`azure_tenant_id` are unset (same spirit as §7 honesty);
   (b) provide AZURE_* env to the simp stack; (c) also reasonable: skip users
   already marked `m365_connected=False` until re-connect. Doing nothing =
   ~1,150 warnings in the 48h window.
2. **Watchdog stale-heartbeat errors (every ~65 min × 3).** Options: (a) run
   the ICS/NC/TBF workers in the simp stack; (b) one-time DB fix on the
   parallel copy: set `is_running=false` on `ics/nc/tbf_worker_status`
   (clean-shutdown semantics — watchdog then never alerts; DB is read-only
   to this task, so flagging not executing); (c) accept and annotate the gate.
   Doing nothing = ~132 ERROR lines in the window.

## Caveats

- Window is 91 min, not 48h: nightly/cron jobs (cadence_materialize 4AM,
  remaining maintenance/resell/task schedules) never fired, so their noise is
  unobserved. Kept-kernel nightly job (cadence) is DB-only — low warn risk.
- Counts come from a pre-shrink container; re-run this clustering after the
  W1 build deploys to the simp stack to confirm clusters 3–5 actually vanish.
- Enrichment worker oddity (INFO, gate-neutral): resumed
  `enriched_today=3000` against `daily_cap=0` → logs "daily cap reached
  (3000/0), sleeping 1h" hourly; worker is permanently dormant in this stack.
