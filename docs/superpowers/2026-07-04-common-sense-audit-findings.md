# Common-Sense UX Audit — 2026-07-04 (46 findings)

Proactive audit across 10 surfaces for the issue classes the product owner keeps flagging:
dead/broken controls, slow-sync hangs, missing common actions, confusing empty states,
bloated toolbars, wasted space. Build status tracked inline. Full per-agent detail:
`subagents/workflows/wf_91e5e578-d82/journal.jsonl`.

Status key: ⬜ todo · 🔵 building · ✅ done+deployed

## 🔴 HIGH (9)

### Dead / broken controls
- ⬜ **Parts list inline edit wipes filters** — any qty/price/status/spec edit reloads the list from `window.location.search` (always empty) → resets search/status/sort/page. Fix: rebuild the reload URL from Alpine `filterVals` (incl. `q`). `parts/list.html:11-12`
- ⬜ **Dossier cache-hit shortlist checkboxes do nothing** — the sticky Create-RFQ/Add bar only lives in `results_shell.html`, not the cache-hit branch. Fix: `{% include "…/search/shortlist_bar.html" %}` in the cache-hit branch. `search/dossier_market.html:34`
- ⬜ **Resell triage cards "Offers to review" / "Take-all" don't filter** — both post `stage=''` → reset to All. Fix: give each a real query (`?needs=offers` / `?scope=take_all`) or make them non-interactive KPIs; fix the active-ring double-highlight. `resell/workspace.html:54-71`
- ⬜ **Vendor list sort / hide-blacklisted / show-archived / cards-table toggles all DEAD** — they `htmx.trigger(q,'changed')` but the input listens for `input`. Fix: `htmx.trigger(q,'input')` (or own hx-get). `vendors/list.html:112`

### Slow-sync (heavy work inline → UI hangs)
- 🔵 **Material Enrich ~30s inline** — enqueue on the worker + return immediately, let `enrich_status.html` poll. `materials.py:1068` *(fix agent in flight)*
- ⬜ **Material "Find Crosses" 30s Claude call inline** — move `ai_json` cross lookup to a background job, OOB-swap `#crosses-section`. `materials.py:1139`
- ⬜ **Sightings "Refresh" (row + bulk) runs supplier+AI search inline** (up to 50 reqs) — enqueue `search_requirement()`, return "Searching…", let the SSE stream swap results. `sightings.py:1107/1012`, `sightings/table.html:203,382`
- ⬜ **Account Enrich 20-40s inline** — SAM.gov + Clay/Explorium/Lusha + Anthropic + Hunter awaited in-request. Enqueue to worker + poll. `customers/detail.html:190` → `crm/enrichment.py:77-92`
- ⬜ **Vendor "Find Contacts" AI web-search inline >15s** → htmx 15s timeout aborts it. Enqueue + poll. `vendors.py:1278`

## 🟡 MEDIUM (19)
- ⬜ Accounts toolbar bloat — 8-control filter bar + saved-views + New/Export/Import rows above a 35%-wide list; collapse + "More filters" popover. `customers/list.html:32-156`
- ⬜ Prospecting: two controls both labeled "All" collide — relabel scope to Everyone/Mine. `prospecting/list.html:96`
- ⬜ Dossier "Add to Requisition" empty state "Create one first" has no create button. `search/requisition_picker_modal.html:25`
- ⬜ Sightings filtered-empty shows a dead "Clear Filters" label (no button). `sightings/table.html:138`
- ⬜ Prospecting un-scored prospects render an alarming red 0% Fit/Readiness bar — show gray "Not scored yet". `prospecting/_card.html:51`, `detail.html:151-167`
- ⬜ Approvals pipeline empty columns render bare "—" → reads broken; use "Nothing here yet". `approvals/_surface_pipeline.html:67,84`
- ⬜ Tasks/My Day zero-tasks shows italic "No tasks match filters" as a void — real empty state + New-task CTA. `tasks/_results.html:148`
- ⬜ Materials rows: in-row datasheet + WEB/OEM-SOURCED links also trigger row nav (no `@click.stop`). `materials/list.html:84`
- ⬜ Parts sort/pills/pagination drop the active search term — include `q` in `_fp()`. `parts/list.html:9`
- ⬜ Dossier lead-detail "Add to Shortlist" posts wrong key (`mpn_matched`→`mpn`) → de-syncs from row checkbox. `search/lead_detail.html:186`
- ⬜ Requisition detail: per-req Tasks board + Activity tab unreachable (no tab button). Add `('tasks','Tasks')`/`('activity','Activity')`. `requisitions/detail.html:45`
- ⬜ Vendor contact "Log Call" gives no feedback (`hx-swap=none` + bare JSON) — return refreshed row / HX-Trigger toast. `vendors/tabs/contact_row.html:45`
- ⬜ Materials list: no CSV export / no bulk multi-select. `materials/list.html:26`
- ⬜ Parts worklist: no CSV export (router already builds the filtered query). `parts/list.html:19-62`
- ⬜ Dossier Live-market: no sort/filter — the built `GET /v2/partials/search/filter` endpoint is orphaned (0 template refs). `htmx_views.py:1109`
- ⬜ Prospecting: no bulk claim/dismiss on the card grid. `prospecting/list.html:125`
- ⬜ Resell Lines: no per-line select → "offer these N lines" scope unreachable (backend supports `?line_ids=`). `resell/_lines.html:115-154`
- ⬜ Approvals lists: no CSV export on PO/Prepayment "Recently resolved" + Buy Plans tracking. `approvals/_tab_po_approval.html:80`
- ⬜ Sightings bulk Change-Status / Add-Note don't refresh the list (stale rows) — target `#sightings-table`. `sightings.py:1266/1316`
- ⬜ Account Contacts "Find contacts" provider waterfall inline. `customers/tabs/contacts_tab.html:67` → `companies.py:2993`
- ⬜ Resell "Offer to buyers" email send + N Graph lookups inline (modal hangs). `resell_outreach_service.py:389-437`

## 🟢 LOW (18) — polish / density / exports / bulk
- ⬜ Sightings: "Pending" counter + "Offered" pill duplicate status=offered. `table.html:34,48`
- ⬜ Prospecting toolbar 3 stacked rows; search alone on a full-width row. `prospecting/list.html:51`
- ⬜ Resell: triage strip + left pills both filter by status (duplicate). `resell/_lists.html:20-39`
- ⬜ Requisitions: two reset controls ("Clean & reset" + "Clear filters"). `requisitions/list.html:198`
- ⬜ Parts Offers tab: bare one-liner empty state (no icon/CTA). `parts/tabs/offers.html:66-70`
- ⬜ Search "All" tab silently caps groups at 10 with no "view all N". `search/full_results.html:166`
- ⬜ Parts: undo-archive toast is dead (nothing fires `part-archived`). `parts/list.html:281-288`
- ⬜ Materials workspace: "Materials" title in its own full-width row above search — fold into search row. `materials/workspace.html:277`
- ⬜ Prospecting detail: "Discovery Source" full card for one metadata line. `prospecting/detail.html:299`
- ⬜ Approvals Halted lane always rendered (min-h-120) even empty → dead 4th column. `approvals/_surface_pipeline.html:55,75-88`
- ⬜ Sightings: no bulk "Assign to buyer" though `POST /…/batch-assign` exists (orphaned). `table.html:358`, `sightings.py:1157`
- ⬜ Resell: no CSV export of collected offers / outreach tracker. `resell/_offers.html:67-199`
- ⬜ Approvals: no multi-select bulk-approve on pending queues. `approvals/_tab_buy_plan.html:24-61`
- ⬜ Requisitions list: no CSV export. `requisitions/list.html:24`
- ⬜ Vendors list / contacts: no CSV export or bulk actions. `vendors/list.html:18`
- ⬜ Two pages both titled "Approvals" (/v2/approvals vs /v2/buy-plans) — give buy-plans hub its own `<title>`. `buy_plans/hub.html:12`

## Patterns (build efficiently)
- **Slow-sync (7):** one shared "enqueue → return placeholder → poll/SSE" pattern (Enrich already establishes it). Apply to Find-Crosses, Sightings-Refresh, Account/Vendor/Contact enrich, Resell send.
- **Missing CSV export (7):** one shared streaming-CSV pattern (feature P establishes it on sightings). Apply to parts, materials, requisitions, vendors, approvals, resell.
- **Confusing empty states (7):** converge on the shared `empty_state.html` (icon + message + CTA).
- **Bloated toolbars / density (9):** the app-wide density pass (D/E/F/L/M/N).
