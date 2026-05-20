# Search-button-only sourcing — design

**Date:** 2026-05-14
**Status:** Approved (brainstorm complete, awaiting implementation plan)
**Owner:** mkhoury

## Goal

Strip every automatic sourcing trigger from AvailAI. The system hits a connector only when a user clicks one of two surfaces on `/v2/sightings`:

1. The per-row refresh icon (currently conditional on stale rows; promoted to always visible)
2. A new "Search now" button on the detail panel (right pane)

Both surfaces fire the same backend path. Both honor a 48-hour cooldown enforced per normalized MPN (not per requirement).

## Why

Auto-search costs API quota and burns goodwill on rate limits without buyer intent driving the spend. The buyer is the human in the loop; sourcing should follow their attention, not a cron. Per-MPN cooldown (vs per-requirement) prevents the obvious waste where the same part on three different requisitions triggers three identical searches in an hour.

## Trigger surfaces (UI)

| Surface | Visibility | Behavior |
|---|---|---|
| Per-row refresh icon in `/v2/sightings` list | Always shown on every row. No "stale" gate. | `POST /v2/partials/sightings/{requirement_id}/refresh` |
| Detail-panel "Search now" button (new) | Always visible at top of the right pane, above the Vendors/Activity tabs | Same endpoint |
| Row click on a requirement | Cached `/detail` only — never fires `/refresh` | `GET /v2/partials/sightings/{requirement_id}/detail` |
| Batch-refresh button (existing) | Unchanged trigger; same per-MPN cooldown applies per req in batch | `POST /v2/partials/sightings/batch-refresh` |

The today's-shipped behavior where `selectReq` fired both GET `/detail` and POST `/refresh` in parallel is reverted. Row click is read-only.

## Cooldown semantics — per normalized MPN

`MaterialCard.last_searched_at` (existing column) is the source of truth for "when was this MPN last hit".

For a requirement R with `primary_mpn = A` and `substitutes = [B, C]`:

```
for mpn in [A, B, C]:
    card = MaterialCard.get_or_create(normalize_mpn(mpn))
    if card.last_searched_at is None or (now - card.last_searched_at) >= 48h:
        run_all_live_connectors(mpn)    # every connector with api_sources.status='live'
        enqueue_for_ics_search(req_id, mpn)
        enqueue_for_nc_search(req_id, mpn)
        card.last_searched_at = now
    # else: skip the connectors. Existing sightings linked to card.id remain visible.
```

`Sighting.material_card_id` is the cross-requirement linkage. The detail panel queries:

```sql
SELECT * FROM sightings
WHERE material_card_id IN (
    <primary_card_id>,
    <sub_card_ids>...
)
AND is_unavailable IS NOT TRUE
```

Cross-requirement visibility is automatic: a sighting created when req X searched MPN A is visible on req Y's panel whenever req Y has MPN A as primary or sub.

## Click response

`POST /v2/partials/sightings/{requirement_id}/refresh` returns:

1. The rendered detail panel HTML (200 OK), same as today.
2. An `HX-Trigger` header with a toast describing per-MPN results:
   - All MPNs cached: `"All MPNs searched within 48h — showing cached results."`
   - Some searched, some cached: `"Searched 2 MPNs, 1 cached (last refresh 14h ago)."`
   - All searched: `"Searched 3 MPNs."`

The toast wording is rendered server-side from the per-MPN results map.

## What's removed

| Removal | Location | Notes |
|---|---|---|
| Daily 3 AM cron `_job_refresh_stale_requisitions` | `app/jobs/sourcing_refresh_jobs.py` | File deleted entirely. Import + `register_sourcing_refresh_jobs()` call removed from `app/jobs/__init__.py` (or wherever it's wired). |
| Auto-enqueue ICS + NC on requirement creation | `app/routers/requisitions/requirements.py` | Inline `_nc_enqueue_batch`, `_ics_enqueue_batch`, and `_bg_full_search` background tasks deleted from the create-requirements path (around lines 455-485). The functions and their `background_tasks.add_task(...)` calls go away. |
| `_enqueue_ics_nc_batch` helper + its callers | Same file, lines 237-251 + 851 + 886 | Helper deleted; both callers (one batch endpoint, one per-req endpoint) deleted as part of the legacy-endpoint removal below. |
| Legacy `/api/requirements/{id}/search` | `app/routers/requisitions/requirements.py` | Route handler deleted entirely. No HTMX/v2 caller exists; only legacy `/api/...` consumers. |
| Legacy `/api/requirements/.../search-all` (batch) | Same file | Same — deleted. |
| `selectReq` POST /refresh on row click | `app/templates/htmx/partials/sightings/list.html` | `selectReq` reverts to GET `/detail` only. `clickPending += 2` becomes `+= 1`. The static-grep tests added today get inverted (assert POST is NOT fired on row click; assert GET `/detail` IS fired). |
| `REFRESH_RATE_LIMIT_SECONDS = 300` (5-min per-req cooldown) | `app/routers/sightings.py:80` | Constant removed. Per-req `last_searched_at` check (`_within_rate_limit`) deleted. Replaced by per-MPN cooldown in the search pipeline. |

## What changes in `/refresh`

`POST /v2/partials/sightings/{requirement_id}/refresh` after this PR:

- Does not consult `Requirement.last_searched_at` for cooldown. (That field becomes display-only; a follow-up PR can decide whether to remove it.)
- Calls a new internal helper `search_requirement_with_mpn_cooldown(req, db)` that:
  - Resolves the requirement's primary + substitutes into a list of `MaterialCard` rows
  - For each card with `last_searched_at` older than 48h or NULL: fires all connectors + enqueues ICS + NC, updates `last_searched_at`
  - For each card within cooldown: skips connector calls
  - Returns a per-MPN result map (`{mpn: "searched" | "cached"}`) used to build the toast
- Renders detail panel via `sightings_detail` (existing helper) which now queries by `material_card_id IN (...)` instead of `requirement_id`.

ICS and NC enqueue calls become first-class in this endpoint — they were previously absent from the v2 path (only the v1 legacy endpoints called them).

## Schema

No migration. `MaterialCard.last_searched_at` already exists. No new columns.

The cross-MPN sighting query relies on `Sighting.material_card_id`, which is already populated by `_save_sightings` in `search_service.py`. Index check: `Sighting.material_card_id` should be indexed; if it isn't, add an index in a follow-up (not part of this PR — verify and note).

## ICS / NC workers

Both workers stay running on systemd (`avail-ics-worker.service`, `avail-nc-worker.service`). No code change to the worker processes — they continue polling `ics_search_queue` / `nc_search_queue`. The only difference is that items now flow only from user-driven `/refresh` clicks (gated by the 48h cooldown), not from auto-enqueue at requirement creation.

`api_sources.icsource.status = 'live'` and `is_active = true` (flip via `UPDATE` in a startup-time idempotent seed or via Admin). Same for `netcomponents`.

The ics_worker_status singleton row is currently missing (the `update_worker_status` call silently skips when the row is absent). This PR seeds the singleton at startup so subsequent heartbeats land.

## Testing

| Layer | Test | Coverage |
|---|---|---|
| Unit | `_mpn_cooldown_partition(req, now)` returns `(to_search, cached)` correctly when MaterialCard.last_searched_at is None / <48h / >=48h | Cooldown helper |
| Unit | `search_requirement_with_mpn_cooldown` only invokes connectors for `to_search` MPNs; calls `MaterialCard.last_searched_at = now` per searched MPN | Pipeline logic |
| Integration | POST `/v2/partials/sightings/{id}/refresh` on a req with one fresh + one stale MPN returns toast with the right counts and updates only the stale card's `last_searched_at` | End-to-end |
| Integration | Row click only fires GET `/detail`; no POST hits `/refresh` | Frontend contract (static-grep test inverted from today's PR) |
| Integration | Detail panel surfaces sightings linked to substitute MaterialCards from prior searches on other requirements | Cross-MPN visibility |
| Regression | Deleted v1 endpoints (`/api/requirements/{id}/search`, `/api/requirements/.../search-all`) return 404 | Confirms removal |
| Regression | `sourcing_refresh_jobs` import path is absent; scheduler does not register `refresh_stale_requisitions` at startup | Confirms cron removal |
| Regression | Creating a new requirement does NOT trigger any background search or ICS/NC enqueue | Confirms auto-search removal |

## Non-goals (this PR)

- No admin override for the 48h limit. Forcing a re-search before 48h is not exposed in the UI. A future PR can add it if needed.
- No per-source cooldown. All connectors share the per-MPN window.
- No countdown display on the requirement row (e.g. "available in 23h"). Toast on click is the only surface.
- No changes to email mining / RFQ inbox flows.
- No changes to enrichment / CRM pipelines.
- `Requirement.last_searched_at` is not removed. It becomes effectively unused but stays for now; a follow-up PR can remove the column once we confirm nothing else reads it.

## Rollout

Single PR off `chore/gradient-vestiges-and-docfmt` (continuation of PR #107). No feature flag — the auto-search removal is a hard cutover. Deploy via `./deploy.sh --no-commit` after merge.

Operational note for the user: after deploy, the only way to see fresh sightings on a requirement is to click the row's refresh icon or the new detail-panel "Search now" button. Daily auto-refresh is gone.
