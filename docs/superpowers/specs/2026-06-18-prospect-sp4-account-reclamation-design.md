# Real Prospect Enrichment — SP4: Account Reclamation & Park Inflows

**Date:** 2026-06-18
**Status:** Architecture approved; detailed spec + plan finalized when reached in sequence
(after SP3).
**Owner:** prospecting
**Program:** Sub-project 4 of 4. Build order: SP1 → SP3 → **SP4** → SP2. Feeds accounts into
prospecting; SP3 (`2026-06-18-prospect-sp3-ai-screening-scoring-design.md`) screens + ranks
whatever SP4 surfaces.

## Goal

Get TRIO's *own* known-but-idle accounts back into play. Three inflows put accounts into the
prospecting pool, where SP1 enriches and SP3 screens them:

1. **Manual park** — a salesperson sends a promising account they can't work right now into
   prospecting for anyone to pick up.
2. **Auto-surface** — unassigned past customers are swept in automatically.
3. **90-day hardline auto-sweep** — any account dormant 90 days is moved to prospecting; the
   rep + manager are notified; the rep may contest/reclaim it.

## Model: parking a CRM account into prospecting

The CRM uses `Company`; prospecting uses `ProspectAccount` (which already has a
`company_id` FK). "Parking" an account creates/links a `ProspectAccount` to the `Company`
with `status='suggested'`, records the inflow source, and marks the `Company` unassigned
(owner cleared). Reclaiming reverses it (re-assign owner, remove from the pool).

New `ProspectAccount` provenance (stored on the row / `enrichment_data`):
`discovery_source ∈ {"sales_park", "reactivation", "auto_sweep"}`, plus
`swept_from_owner_id`, `swept_at`, `parked_by_id` as applicable.

## 1. Manual "Park in prospecting"

- New action on the CRM company/account page: **"Park in prospecting"** (buyer/rep gated).
- Creates the linked `ProspectAccount` (`discovery_source="sales_park"`,
  `parked_by_id=<rep>`), clears the Company owner, spawns enrichment → screen.
- New UI element → explicit approval at build time.

## 2. Auto-surface unassigned past customers

- Scheduled sweep: `Company` rows that are **past customers** (have quote / PO / buy-plan
  history) **and** currently **unassigned** → create `ProspectAccount`
  (`discovery_source="reactivation"`) → enrich → screen.
- Idempotent (skip companies already linked to an active prospect).

## 3. 90-day hardline auto-sweep + notify + reclaim

**Definition of "activity" (clock-reset):** any activity-timeline event on the account —
call, email, note, meeting, **quote, RFQ, SO, PO, buy-plan update**. Because open-deal
progress counts as activity, a genuinely live deal never reads as dormant, so no special
"exempt open deals" rule is needed — this keeps the policy **hardline** while never yanking
active work.

**Sweep job (APScheduler, daily):**
- Find `Company` accounts with an owner whose **last activity** is older than
  `account_sweep_inactivity_days` (default **90**).
- For each: clear the owner, create/flip a `ProspectAccount`
  (`discovery_source="auto_sweep"`, `swept_from_owner_id`, `swept_at`), spawn enrich → screen.
- **No pre-warning** (hardline). Send the loss notice at the moment of sweep.

**Notification (Microsoft Graph, via `email_service`):**
- **To:** the rep losing the account. **CC:** the manager (configurable
  `account_sweep_manager_email`; default = the admin / the program owner).
- Content: account name, that it was moved to prospecting after 90 days of inactivity, the
  last-activity date, and how to **reclaim** (deep link to the account + the in-app action).
- Idempotent: one notice per sweep event (guard via `swept_at`).

**Reclaim / contest:**
- New in-app **"Reclaim"** action on the prospect, available to the former owner and the
  manager. Re-assigns the Company to the reclaiming rep, removes the `ProspectAccount` from
  the pool, **resets the activity clock**, and logs the reclaim (+ optional justification)
  to the activity timeline.
- Optional email deep-link routes to this action. New UI element → approval at build time.

## Config

```
account_sweep_enabled: bool = False
account_sweep_inactivity_days: int = 90
account_sweep_manager_email: str = ""     # CC on loss notices; default admin/owner
account_reactivation_sweep_enabled: bool = True
```

## Dependencies (verified at build time)

- **Activity timeline** — the app already tracks activities (the source of "last activity").
  SP4 reads the latest activity timestamp per account; confirm the query covers all the
  event types listed above.
- **Microsoft Graph email** (`email_service`) — used for the loss notices.
- **User → manager relationship** — if the user model has no manager link, CC the
  configured `account_sweep_manager_email` (default admin/owner).

## Testing strategy

- Manual park: creates linked prospect, clears owner, spawns enrich/screen; rep-gated.
- Auto-surface: only unassigned past customers; idempotent.
- 90-day sweep: dormant account swept; account with recent quote/RFQ/call NOT swept (activity
  definition); owner cleared; provenance recorded; one notice per event (idempotent).
- Notification: correct To/CC; content includes last-activity date + reclaim link; mocked
  Graph send.
- Reclaim: re-assigns owner, removes from pool, resets clock, logs to timeline; permission
  (former owner or manager only).
- Migration (if provenance needs columns): upgrade → downgrade → upgrade clean.

## Out of scope (SP4)

- Pre-warning emails (hardline — notice is at sweep, per decision).
- Configurable per-tier windows (single global `account_sweep_inactivity_days` for now).
