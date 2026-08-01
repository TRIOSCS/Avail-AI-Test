# Plan — Teams agent + a more proactive AVAIL

Phased plan. Discovery-grounded; every current-state claim is cited
`file:line`. No code written yet. Ends with Open Questions — answer
those before any build.

## The idea in one line

AVAIL already computes scored inventory→customer matches but only
shows them in-app. Push those (and other already-computed signals)
into Teams, then make the Teams surface actionable, then — only if
warranted — a true interactive bot.

## Current state (what exists, so we don't rebuild it)

- Outbound Teams: `post_teams_channel`, `post_teams_channel_card`
  (full Adaptive Card, FactSet + `Action.OpenUrl`), `send_teams_dm`
  (Graph 1:1 chat) — `app/services/teams_notifications.py:17-135`.
- Inbound Teams webhook logs messages only, no reply/action —
  `app/routers/v13_features/activity.py:101-134`,
  `app/services/webhook_service.py:573-671`.
- No Bot Framework anywhere (no `/api/messages`, no adapter) — not found.
- Proactive engine: `ProactiveMatch` rows, recency/frequency/margin
  scoring, hotlist seed, scan — `app/services/proactive_matching.py`
  (`compute_match_score:83`, `run_proactive_scan:466`). Matches write
  only an in-app `ActivityLog` (`:445-455`); **no Teams/email push.**
- Scheduled scan `proactive_matching`, flag-gated
  `proactive_matching_enabled` — `app/jobs/offers_jobs.py:19-34`.
- Migration head: **203** (`203_outreach_recipient_email`). New = 204.

## Conflicts with the brief (code wins — confirm before building)

1. Per-user `teams_alert_config` (webhook, quiet-hours, threshold,
   digest hour) was created (`059`, `062`) then **DROPPED**
   (`a3f9c1d82e47_drop_dead_tables.py:25`). Only a single global
   `TEAMS_WEBHOOK_URL` credential remains. Per-user routing must be
   rebuilt if we want it.
2. Teams-Q&A agent was scaffolded but **never built** — `teams_qa_service.py`
   absent (`app/models/knowledge.py:10-15`); dead columns
   `KnowledgeEntry.nudged_at/delivered_at/answered_via` (`:62-65`).

## Phases

### Phase 1 — Proactive push (no bot; reuses the outbound card path)
Scheduled job that takes each salesperson's new `ProactiveMatch` rows
and posts a digest Adaptive Card (FactSet: customer, MPN, score,
last-bought) with an `Action.OpenUrl` deep-link into `/v2/proactive`.
Delivery via `send_teams_dm` (per-rep) or `post_teams_channel_card`
(shared channel). Respects the scan flag. **This alone makes AVAIL
proactive** — matches reach reps instead of waiting for a manual
refresh. New job in `offers_jobs.py`; likely a small `TeamsAlertConfig`
model + migration 204 if per-user routing/quiet-hours are wanted.

### Phase 2 — Actionable cards (still no bot transport)
Card buttons are `Action.OpenUrl` deep-links to existing endpoints
(prepare / send / dismiss in `htmx/proactive.py`). One tap in Teams →
lands in the app pre-filled. Cheap, no Azure Bot registration.

### Phase 3 — Interactive agent (real bot; the big lift — gated)
Azure Bot registration + Bot Framework `/api/messages` + adapter +
conversation-reference persistence. `Action.Execute` card buttons that
act in-place (send offer, dismiss, snooze, "why this match" using
`Sighting.evidence_tier` / `score_components`, `sourcing.py:284-287`).
Optionally revive or explicitly drop the dead Q&A scaffolding.

## Signals available to push (beyond matches)

Expiring strategic-vendor warnings (`offers_jobs.py:262-307`),
stale-offer flags (`:206-229`), vendor/company activity-health
red/yellow (`activity.py:589-648`), 12h score digests (`:110-160`) —
all computed today, none pushed.

## Open Questions (answer before build)

1. Delivery target: per-salesperson DM, a shared sales channel, or both?
2. How far: Phase 1–2 (push + deep links, no Azure Bot) — or Phase 3
   (interactive bot, which needs an Azure Bot registration you provision)?
3. First signal to push: proactive matches only, or also
   expiring-vendor / activity-health / score digests?
4. Config: rebuild a per-user `TeamsAlertConfig` (quiet hours,
   threshold), or start with the single global webhook?
5. Dead Teams-Q&A scaffolding: fold into this, or drop it separately?
