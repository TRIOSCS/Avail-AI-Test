# Remaining-Work Inventory (2026-07-04)

Exhaustive discovery sweep across all 5 audit docs, the backlog plan, memory, and code
markers. **The code is ahead of its tracking docs — treat the tree as truth.** Most of
the audits + master backlog (P0-P5, prepay closure, API-search Ph0-4) are BUILT + verified.
This lists what genuinely remains. Being worked down in waves (see status handoff).

## A. COMPLETABLE (bounded fixes)

### Tier 1 — high value / low effort
1. **Prospecting H8** — a duplicate domain rolls back the ENTIRE monthly discovery persist → 0 saved. `prospect_scheduler.py:52-69`. Fix: `ON CONFLICT (domain) DO NOTHING` / per-row; drop `.limit(10000)`.
2. **Prospecting M18** — non-string intent topic drops a 50-row page. `prospect_discovery_explorium.py:183`. Fix: `isinstance(t,str)` guard.
3. **Prospecting M19** — `ProviderQuotaError` swallowed; keeps burning slices. `prospect_discovery_explorium.py:141`. Fix: catch + short-circuit.
4. **Prospecting M1** — claim swallows the domain-collision warning. `prospecting.py:504,528`. Fix: surface `warning` toast.
5. **Prospecting M12 (authz)** — reclaim/reassign `/v2/partials/prospects` not in `_GUARDED_BASES`. `access_paths.py:43`. Fix: add the prefix.
6. **Prospecting M16** — email-discovery exclusion set truncated at 5000. `prospect_discovery_email.py:86`. Fix: drop the cap.
7. **Prospecting M15** — discovery `credits_used` never written (cost report always 0). `prospect_discovery_explorium.py:481`.
8. **Prospecting M8** — shared scorers dead; scheduler hand-codes the 60/40 composite (drift). `prospect_scoring.py:454`.
9. **Resell L2** — non-positive quantity → 500 not 400. `resell.py:831`.
10. **Resell M6** — no owner notification on inbound offer/reply. `excess_service.py:449`.
11. **Resell M9 (concurrency)** — `award_offer` no row lock → double-award race. `excess_service.py:728`.

### Tier 2 — medium value / medium effort
- Prospecting M5 (ai_match_desc loads whole pool), M6 (stats loads all SUGGESTED), M17 (dismiss logic in router + reason/confirm), M10 (send+park double-commit), M9 (find_similar N+1), M7/M13 (enrich no-lock / manual-add TOCTOU).
- Resell L1 (single-line offer entry), L3 (confirm_import trusts client rows — re-validate), L5 (rank_buyers loads vendor_cards).
- API-search O5 (~70 redundant SELECTs per Connectors render), O6 (per-call httpx/Anthropic clients bypass pool).
- Tasks L5 (in-template datetime math + inconsistent date formats), L2 (My Day view-only — snooze/reopen/edit), L4-CRM (priority uneditable from CRM/vendor create forms).
- CRM M4 (dup cadence_hero macro + tier select), L1 (4 hand-rolled empty states → shared).

### Tier 3 — nits (trivial)
Prospecting H3 (missing rollback; no live caller), L1-L8 (dupe queries, news-dedup, insufficient_data re-runs, SIZE_BRACKETS non-monotonic, batch left running, divergent Explorium key source). Resell L4 (offer_count meaning divergence). Tasks L7/L8 (consolidate 4 `create_*_task`; remove dead `/api/requirements/{id}/tasks`; completion_note=""; dupe `_parse_task_due_date`). CRM L3 (phone/email link color, redundant th classes). Code: `email_jobs.py:82` calendar /delta; `startup.py:1128` alias backfill.

## B. DEAD CODE (bounded cleanup pass — decide delete vs keep-as-API)
- Dead endpoint `sourcing_stream` (`sourcing.py:106`, no caller, no publisher).
- ~55 dead service functions (CRM Phase-5b forecast rollups `forecast_service.py:200/230/277`; `customer_enrichment_batch`; dead in ownership/knowledge/enrichment/excess/task_service etc.).
- Dead enum members (`constants.py`): ContactStatus.RETRIED, AttributionStatus.EXPIRED/CONVERTED, ExcessListStatus.ACTIVE/BIDDING, ExcessOfferStatus.EXPIRED, TicketSource.TICKET_FORM, whole RiskFlagType (only STALE_OFFER live), SpecCodeSource.MANUAL/CSV_IMPORT, ActivityType.TASK_REOPENED/BID_RECEIVED, QualityPlanStatus.IN_REVIEW/APPROVED/REJECTED, SourcingType.*, etc.
- Dead model columns (need migration): Offer.sourcing_type, ExcessLineItem.demand_match_count (+ index), ProspectAccount.import_priority, Company.import_priority, ApprovalStep.rule.

## C. ROADMAP — multi-day/week programs (separate projects, NOT sweep-completable)
Vendor-API parametric enrichment (MOSFET extractor + backfill --apply; blocked on inventory). CRM Phase-5b Reporting page (surfaces the forecast trio OR drop them). CRM-redesign remainder (Reporting + AI-suggest-tier). Supervise-lens redesign (audit vs Approvals rework first). Deferred-high-tier: HIGH-SEC-4 (Graph-webhook allowlist), HIGH-BE-11 (`db.query`→2.0, ~1567 callsites). SP4 reclamation UI (park buttons + wrapper). QualityPlan approval lifecycle (IN_REVIEW/APPROVED/REJECTED never built). KB insight refresh (job disabled for Anthropic cost — re-enable = cost decision).

## D. BLOCKED — credentials / external / user config
Customer-enrichment stubs (Apollo/Hunter/Lusha removed — need new providers). Sourcengine connector (live creds to confirm endpoint). Explorium + eBay dormant (keys). User-side: SFDC import, March enrichment recovery, launch config (disable password login, DO Spaces backup, 3 prepay notify keys, datasheet SharePoint).
