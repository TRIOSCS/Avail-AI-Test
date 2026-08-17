# AVAIL — Deep UX + Workflow Review

**Date:** 2026-08-10
**Scope:** Ten journeys reviewed end-to-end (Sourcing, Approvals, Quoting, Buy Plan/QP, Resell, Proactive/Matches, CRM/Prospecting, Navigation, Feedback/States, Consistency/Mobile). Every finding below was adversarially re-verified against current code — file:line citations are exact. 77 raw findings deduplicated to **62 unique: 12 blockers, 50 majors. Zero candidates were refuted.**

---

## 1. Executive read

AVAIL's underlying workflow machinery is genuinely strong — the approval pipeline, QP data model, prepayment lifecycle, proactive matching, and resell mirroring all exist and mostly work — but the surfaces users touch routinely misreport what that machinery did. The most dangerous pattern is **success theater**: the app shows green "sent/submitted/processed" states when nothing (or the wrong thing) happened — RFQs that were never emailed, quote tables that paste TRIO's cost under the "Sell" header, internal margin notes emailed to customers, trader bids that silently fall into an unmatched queue. The second through-line is that **this phone-first product is not usable on a phone**: three workspaces render side-by-side splits at 195px per pane, two more stack but load the detail off-screen so taps appear dead, every row action hides behind hover, and the 11-item bottom nav compresses to ~35px mis-tappable targets. Third, **the one-screen Approvals migration stranded critical affordances**: request-prepayment, mark-paid, reverse-payment, and flag-issue all have working backends with no rendered button, so money flows wedge. Fourth, **URLs lie**: filters push raw partial URLs that reload as unstyled fragments, and content swaps without push-url leave the address bar naming the wrong record. The good news: nearly every fix is small (an attribute, a button, a CSS rule) and the codebase already contains the correct pattern for almost all of them — the fixes are mostly "copy what the sibling screen already does."

---

## 2. Findings by journey

Severity order within each journey: blockers first. Cross-journey duplicates are merged into one entry with a note.

### 2.1 Sourcing core (9 findings, 2 blockers)

**S1 · BLOCKER — RFQ results screen misreports outcomes**
Screen: Send RFQ → results. `app/templates/htmx/partials/requisitions/rfq_results.html:14`; router `app/routers/htmx/offers/rfq.py:200-298`. *(Also surfaced by the Feedback/States review — merged.)*
A buyer selects 5 vendors (one flagged "No email contacts found") and sends. Failed Graph sends are passed to the template (`failed_results`, rfq.py:296-298) but never rendered; no-email vendors are silently dropped (`if not email: continue`, rfq.py:218/253/273); and with an expired Microsoft session, rfq.py:270-289 creates SENT contact rows without emailing anyone — the buyer sees a green "RFQ sent to N vendor(s)" banner while zero emails went out, then waits days for quotes never requested.
**Fix:** three-section result — Sent (green), Failed (rose, vendor + reason + retry), Skipped—no email (amber, link to add contact). No-token → amber "Not sent — email connection expired. Reconnect and resend." Exclude no-email vendors from Select All so the button count is honest.

**S2 · BLOCKER — Filters push raw partial URLs; reload = unstyled fragment**
Screen: Requisitions list (8 controls: `app/templates/htmx/partials/requisitions/list.html:65,104,122,132,154,184,199,293`) and Prospecting list (`prospecting/list.html:77,90,101,111,148,161`). Routes `app/routers/htmx/requisitions.py:172` and `prospecting.py:223` never check HX-Request. *(Sourcing + Navigation reviews — merged.)*
Search "acme", open a req, come back, pull-to-refresh: the address bar holds `/v2/partials/requisitions?q=acme` and the browser renders raw table text — no CSS, no nav, dead controls. Same on bookmark, shared link, or history-cache miss.
**Fix:** push canonical page URLs (`/v2/requisitions?view=list&q=…`, the pattern detail.html:15 already uses) and/or add the existing `full_page_shell()` content negotiation (`app/routers/htmx/_shared.py:92`, already used by quality_plans.py:253) to both partial routes.

**S3 · MAJOR — "Search All Sources" banner refreshes once at 8s, mid-search**
Screen: Requisition detail → Parts tab. `requisitions/tabs/parts.html:186-198`; `app/routers/htmx/requisitions.py:1262-1293`. *(Also Feedback/States — merged.)*
The banner promises auto-refresh, fires exactly one reload at 8s (one-shot `load delay:8s`; only the search POST sets `search_triggered`), while the sequential background job (per-part connector fan-out + ~30s AI calls) is still running. Buyer reads "0 sightings, banner gone" as "no stock anywhere" and starts phoning vendors; 40 sightings land 30s later, invisible. "Search Selected (N)" is worse: `hx-swap="none"` — toast only, table never updates. Cooldown-cached parts pretend to search.
**Fix:** keep a "Searching N parts…" state polling (or SSE, as the Sightings board already has, `sightings/list.html:124`) until the job completes; end with "Search complete — N new sightings"; say "searched 3h ago — cached" for cooldown parts.

**S4 · MAJOR — Header actions bypass tab state; tab tap silently destroys a drafted RFQ**
Screen: Requisition detail header vs tab bar. `requisitions/detail_header.html:55,64`; header is outside the Alpine scope opened at `detail.html:41`.
"Send RFQ"/"Search Sources" swap #tab-content without changing `activeTab`, so the old tab stays highlighted. Tapping that still-highlighted tab reloads it — instantly discarding a 5-minute composed RFQ email and vendor selection, no confirm, no recovery.
**Fix:** dispatch an event the tab x-data listens to (buttons inside #tab-content already do this, e.g. `tabs/build_quote.html:18`), or open the composer in the modal layer; guard tab swaps with "Discard RFQ draft?" while the body textarea is non-empty.

**S5 · MAJOR — Sightings count wears an eye icon but is inert**
Screen: Parts tab, Sightings column. `requisitions/tabs/req_row.html:53-68`.
After a search, the LM358DR row shows "12" with an eye icon; tapping does nothing. To act, the buyer must leave via bottom nav → Sightings and re-find the part, losing context. The target pane already exists (`GET /v2/partials/sightings/{req_id}/detail`, sightings.py:821) — a one-line deep link.
**Fix:** make the count open that sightings detail panel in a modal, or jump to the board with the requirement pre-selected. Eye icons only on things that show something.

**S6 · MAJOR — Row actions are hover-only: invisible on every touch device**
Screens: Offers kebab `requisitions/tabs/offers.html:193` (the ONLY path to edit/approve/reject/reconfirm/mark-sold/delete an offer); sightings Build RFQ `sightings/table.html:222` and offer kebab `sightings/_offer_row.html:38`; requirement files/delete `req_row.html:73,86`; quote-line remove `quotes/line_row.html:75`; task edit/delete `requisitions/tabs/_task_row.html:71,84`. *(Reported by 4 journey reviews — merged.)*
All use `opacity-0 group-hover:opacity-100`; no `@media (hover:none)` override exists anywhere, so on phones/tablets these columns render as blank space (yet stay invisibly tappable — stray taps open modals from controls never seen). The owner cannot visibly approve an offer from his phone.
**Fix:** one global rule — `@media (hover:none) { .group [class*='group-hover:opacity-100'] { opacity:1 } }` — or switch to `opacity-40 group-hover:opacity-100`; add aria-labels to the icon-only kebabs.

**S7 · MAJOR — Offer Edit/History open two screens above the viewport**
Screen: Offers tab. Edit `offers.html:203-204`, History `:240-241`, both swap into `#parse-area` (`offers.html:49`) at the top of the tab with no `show:` modifier or focus.
On a scrolled 30-offer list the tap appears to do nothing — the form has been open the whole time, off-screen. Users tap three times and give up.
**Fix:** edit in place in the offer's own row, or open in the modal layer (`_offer_row.html:46` pattern); at minimum `hx-swap="innerHTML show:top"` + focus the first field.

**S8 · MAJOR — Create Quote from Selected: quote appears, URL still says requisition**
Screen: Offers tab. `offers.html:104-112`; handler `app/routers/htmx/offers/crud.py:216-316` returns the quote detail with no HX-Push-Url.
Phone locks, Safari reloads → back on the requisition, quote "vanished" → buyer creates duplicate Q-12-2 (no dedupe guard, crud.py:248). Every other path into quote detail pushes the URL (e.g. `tabs/quotes.html:32`).
**Fix:** send `HX-Push-Url: /v2/quotes/{id}` from the create route + a "Quote Q-12-1 created" toast.

**S9 · MAJOR — First added part renders above a "No requirements yet" ghost**
Screen: Parts tab, add form. `parts.html:44-46` prepends into #parts-tbody; route (requisitions.py:1162-1235) returns only the row; placeholder `#parts-empty-state` (parts.html:234-250) is never removed.
The salesperson's new row sits directly above a big "No requirements yet" graphic, making her doubt the save; the ghost persists until a full tab reload.
**Fix:** include `<tr id="parts-empty-state" hx-swap-oob="delete"></tr>` in the add-requirement response.

### 2.2 Approvals workspace (10 findings, 2 blockers)

**A1 · BLOCKER — Mark-paid and reverse-payment are dead UI; a lost email wedges a wire forever**
Screen: Prepayments tab, approved pane. Routes exist and are tested (`app/routers/prepayments.py:324,355,398`; modal `mark_paid_modal.html`), but zero templates render a button for them — the approved pane (`approvals/_pane_prepayment.html:106-110`) shows only "Accounting confirms via the pay link". The pay token lives only in the (lost) email; no resend action exists either.
**Fix:** render "Mark paid manually" on the approved card (open the existing modal) and manager-only "Reverse payment" on the paid card; re-render via awListRefresh. Add a "Resend pay link" action.

**A2 · BLOCKER — "Request prepayment" doesn't exist anywhere in the workspace**
Screen: PO pane. `approvals/_pane_po_line.html:232` (verified branch = stamp only). *(Reported independently by the Approvals and Buy Plan/QP reviews — merged.)*
The button lives only on the legacy plan detail (`buy_plans/_detail_lines.html:263`); the workspace's sole "Open full plan" link renders only for DRAFT plans (`_pane_sales_order.html:105-110`) — but prepayments require a cut PO on an ACTIVE plan, so the link is gone exactly when the button becomes available. Old `/v2/buy-plans/{id}` URLs 308 back into the workspace. The buyer's only route is Requisitions → req → Buy Plans tab → plan → line — undiscoverable. This directly contradicts the one-screen model.
**Fix:** render the existing `request_prepayment_button` macro (gated by `can_request_prepayment`) in the PO pane's pending_verify/verified branches with `origin=approvals_workspace`; give ACTIVE plans the same "Open full plan" link drafts get.

**A3 · MAJOR — Approved-but-unpaid wires have no live surface**
Prepayments tab Live/Closed filter. `approvals_hub.py:1233-1236` (Live = REQUESTED only); APPROVED is terminal (`queue.py:50-55`); the Closed toggle is titled "Show completed and cancelled" (`_workspace_list.html:73`). Aggravator: the Closed feed caps at the 10 newest resolutions (`queue.py:56`), so an unpaid wire can fall off every list. Kanban risk lane fires only on PAID (`kanban_lanes.py:101`).
Manager approves a wire, checks next morning, sees a clean Live list, concludes it's settled — authorized money sits unconfirmed, invisible.
**Fix:** keep approved-awaiting-payment in Live with an amber "Approved — awaiting wire" chip aged on approved_at; reserve Closed for paid/void/rejected.

**A4 · MAJOR — Search box replaces itself mid-typing**
All four tabs. Input at `_workspace_list.html:46-53` targets #aw-list — its own container — and has no id, so htmx can't restore focus. 300ms after a pause the debounce swap wipes in-flight characters and dismisses the phone keyboard, on every search.
**Fix:** give the input a stable id (htmx restores focus + caret by id), or swap only the rows container below the filter bar.

**A5 · MAJOR — On a phone, tapping a work-list row appears to do nothing**
`approvals/_workspace_split.html:14,29-32` — below md the list stacks above the pane at full height; `select()` loads #aw-pane with no scroll; the 2px loading bar is also below the fold; aw-default even auto-loads a pane the user never sees. *(Reported by 4 journey reviews — merged.)* The manager taps the "Needs your approval" row, only the highlight changes, and he never finds the Approve button.
**Fix:** in select(), below md scroll #aw-pane into view after the swap (htmx.ajax returns a promise) — or a list⇄detail two-step with a Back control. Same fix applies to the Customers workspace (C5).

**A6 · MAJOR — "Export CSV" exports a different list than the one on screen**
PO + Prepayments tabs. Button tooltip says "Download this list" (`_workspace_list.html:85-87`); the route (`approvals_hub.py:1336-1346`) streams the resolved decision-history feed while the visible Live list shows pending work — none of the 12 rows the manager sees make the file.
**Fix:** thread q/scope/show_closed into the export and reuse the same row builders the list uses; or relabel "Export decision history".

**A7 · MAJOR — Stalled-plan warning missing from the default tab**
`approvals_hub.py:1090` computes stalled_ids only for lens == "buy-plans"; the identical rows on the default Sales Orders tab render the amber "No approver configured — stalled" flag (`_workspace_list.html:115-120`) never. A stalled plan is in nobody's badge or "Needs your approval" group — the exact case the code calls "silently stalled" is silent on the tab users land on.
**Fix:** compute stalled_ids for both plan lenses and group stalled rows into their own flagged section.

**A8 · MAJOR — Buyer's cut-the-PO work filed under "Needs your approval"**
`approvals_hub.py:1223` flags the viewer's own AWAITING_PO lines needs=True; the hardcoded header (`_workspace_list.html:136`) says "Needs your approval" — but opening the row says "Confirm the PO you cut in Acctivate". The tab badge merges "POs you can approve" and "POs to confirm" into one unexplained number.
**Fix:** split sections — "Needs your approval" (PENDING_VERIFY you can verify) vs "Your POs to confirm" (your AWAITING_PO); keep the badge as the sum.

**A9 · MAJOR — Any viewer gets the confirm-PO command form for someone else's line**
`_pane_po_line.html:77` gates only on status; `is_assigned_buyer` (computed at approvals_hub.py:513) is never used; backend accepts a confirm from anyone with plan access (`buyplan_po.py:24-69` never checks buyer_id). A manager clearing All-scope opens buyer X's line, is greeted with "Confirm the PO you cut in Acctivate", and one plausible submit fabricates a PO confirmation.
**Fix:** assigned buyer gets the form; everyone else a read-only "Awaiting PO — assigned to {buyer}" card with an explicit "Act for the buyer" expander for managers. (The pending_verify branch already gates on can_verify — apply the same pattern.)

**A10 · MAJOR — Every tab switch silently resets Mine → All**
Tab pills (`approvals_hub.html:24-27`) carry only ?tab in hx-get and hx-push-url; the shell route has no scope param, so the body route's default "all" always wins. A buyer on Mine flips to Prepayments and back — the list is All again, topped by other people's rows, every time; reloads forget scope too. Within-tab decide re-renders DO thread hub_scope, proving the plumbing exists.
**Fix:** thread scope through the pill URLs and let the shell/body routes default from the query.

### 2.3 Quoting (9 findings, 3 blockers)

**Q1 · BLOCKER — The field labeled "Internal Notes" is emailed to the customer**
Edit Terms modal `quotes/edit_form.html:54-56` → `quote.notes` → rendered into the branded customer email (`quote_send.py:282-286,363`) and the customer-facing quote document (`documents/quote_report.html:78-80`). The quote detail page never displays notes, so "cost from Shenzhen broker is $0.41, room to drop 10%" goes out with no chance to catch it.
**Fix:** split into Customer Notes (emailed/PDF) and Internal Notes (never leaves AVAIL), or relabel "Notes (shown to customer)"; surface the customer-facing notes on the detail page.

**Q2 · BLOCKER — Copy Table pastes TRIO's cost under the "Sell" header**
`quotes/detail.html:213-221`. Header is 6 columns (MPN/Manufacturer/Qty/Cost/Sell/Margin %) but the copy slices cells 0-5, which are MPN/Description/Manufacturer/Qty/Cost/Sell — every column shifts one left; cost lands under "Sell", sell under "Margin %". The "copied" confirmation also never fires: line 221 dispatches a window `toast` event nothing listens to. *(Both reviews — merged.)* Pasted into a customer email, the broker's buy price is presented as the offer.
**Fix:** build the copy from the live thead / data-col-keys; offer a customer-safe variant omitting Cost/Margin; use the real `showToast` bridge.

**Q3 · BLOCKER — Send Quote never shows or lets you choose the recipient**
`detail.html:118-126` (and twin `tabs/build_quote.html:197`). Confirm says only "Send this quote to the customer?"; `quote_send.py:101` silently resolves the site's default contact_email; the override_email hook exists only on a JSON route no template calls (`crm/quotes.py:439`); success discards `sent_to` — no toast ever names the address. If no contact_email exists, the error says "select a contact or enter one manually" — controls that don't exist anywhere.
**Fix:** replace hx-confirm with a small send dialog showing the resolved recipient (editable, seeded from site contacts) + CC, wired to the already-built override_email; success toast "Quote Q-12-3 sent to bob@acme.com".

**Q4 · MAJOR — Preview is not the email**
`quotes/preview.html:49-84` vs `quote_send.py:331-345`. Different columns (preview shows Description + internal Status; email shows Cond/Date Code/Pkg/Lead Time from linked offers the salesperson never saw), different data source (ORM lines vs line_items JSON), and sub-cent prices render as $0.0045 in preview but "$0" in the email (`_fmt_price`, quote_send.py:225-229). An accurate preview endpoint exists (`crm/quotes.py:381`) — orphaned, zero callers.
**Fix:** render the preview from the same `_build_quote_email_html` the send uses (iframe/sandboxed div), including recipient + subject; fix _fmt_price to keep 4 decimals under $1.

**Q5 · MAJOR — Pre-send advisories only exist on the Build Quote tab**
`build_quote.html:165-182` shows DNC/COO/MPN-drift warnings; the standalone quote detail's Send (the path after Revise/Reopen/deep link) shows none, and `/api/quotes/{id}/preflight` has zero UI callers. DNC still hard-blocks server-side, but non-US COO and MPN-drift go out with no flag at any point from detail.
**Fix:** run `quote_preflight` in `quote_detail_partial` for drafts and render the same amber card above the actions bar.

**Q6 · MAJOR — Mark Won / Mark Lost: adjacent, one-tap, no confirm; lost-reason is a dead feature**
`detail.html:130-147` — every sibling action carries hx-confirm, these two don't; a phone mis-tap flips a sent quote to LOST instantly (Reopen only regresses to draft). The endpoint reads `result_reason` (htmx/quotes.py:582) but no template ever sends it — win/loss review has nothing, forever. *(Both reviews — merged.)*
**Fix:** small result dialog: confirm outcome; for Lost, a reason picker/free-text posted as result_reason; on Won, offer "Build Buy Plan now?".

**Q7 · MAJOR — "Pricing History (Previous Revisions)" shows vendor cost, not revisions**
`detail.html:362,375` → `/v2/partials/pricing-history/{mpn}` queries the Offer table (buy-side cost rows). A rep preparing rev 3 gets vendor costs instead of what rev 1/2 quoted; revisions have no visible lineage anywhere on the page.
**Fix:** rename "Vendor Cost History" and add a revision strip (rev → rev with subtotal/status/link) on the header.

**Q8 · MAJOR — Three quote-building paths seed unsendable prices with no guard on Send**
Create Quote from Selected: sell = vendor cost, margin 0 (`offers/crud.py:263-266`); Add to Draft Quote same (`htmx/quotes.py:712-722`); detail-page offer gallery seeds sell = $0 (`quotes.py:502-504`) which the email renders as "—". The send path checks only recipient + DNC — a salesperson can email a quote at exact vendor cost in three taps. Only the Build Quote tab applies markup + a margin guardrail.
**Fix:** seed like the Build Quote tab (last-quoted, else cost × default markup) and add a send check: "2 lines have 0% margin / $0 sell — send anyway?".

**Q9 · MAJOR — Sending from Build Quote wipes the requisition workspace**
`build_quote.html:197-204` targets #main-content; success swaps the standalone quote detail over the whole requisition (tabs, parts, offers) with no push-url and no toast — URL still reads /v2/requisitions/{id}, back/refresh disagree with the screen. The router's own OQ-07 comments document this hazard on the error paths, then the success path does exactly it.
**Fix:** on success re-render the Build Quote tab with status SENT + toast "Quote Q-12-3 sent to bob@acme.com"; or push /v2/quotes/{id} if navigation is preferred.

### 2.4 Buy Plan / Quality Plan (7 findings)
*(This review also independently confirmed A2 — request-prepayment missing, blocker — and A5 — phone row-tap; merged above.)*

**B1 · MAJOR — Prepayment request modal closes on failure, discarding the form**
`prepayments/request_modal.html:38` closes on any 2xx; service errors (duplicate pending, no eligible approver) return HTTP 200 + error toast (`prepayments.py:152-162,270-275`), so the modal vanishes as if it worked — typed amount and approver remarks gone. The adjacent COD guard correctly returns a real 400 and keeps the modal open, proving the 200-error paths are the anomaly.
**Fix:** return non-2xx with the error in a modal-scoped slot, or gate close-modal on an explicit success signal.

**B2 · MAJOR — No "flag issue" in the awaiting-PO pane; a dead vendor stalls silently**
`_pane_po_line.html:77-131` offers only the confirm-PO form. The backend explicitly supports flagging AWAITING_PO lines (`buyplan_lines.py:253-286`, "sold out, price change") with a supervisor resolve flow — but the route's only template caller is the legacy plan detail the workspace doesn't link to past draft. The buyer whose vendor sold out can neither flag nor re-source; the deal sits until someone notices.
**Fix:** add "Problem with this vendor?" beside Confirm PO, posting the existing flag-issue route with origin=approvals_workspace, re-rendering the pane.

**B3 · MAJOR — The Quality Plan page is effectively hidden**
The app's only link to the QP native view is the button on the legacy plan detail (`buy_plans/detail.html:73`). Workspace panes embed QP answer grids but never link out; Serial/FRU tracking and section Mark-Reviewed live only at `qp/detail.html`. Ops must know to go Requisitions → req → Buy Plans tab → plan → Quality Plan.
**Fix:** a "Quality Plan" link (hx-get `/v2/qp/for-buy-plan/{id}`) in the SO and PO pane headers at every status.

**B4 · MAJOR — QP header chip says "Draft" forever**
`qp/detail.html:57`; `QualityPlan.status` is written only by create_qp (quality_plan_service.py:75) — the model docstring admits the lifecycle was never built. Both sections reviewed + plan approved still shows a slate "Draft" chip, actively contradicting the section chips below it.
**Fix:** derive the chip from the two reviewed_at stamps ("Sections reviewed 2/2" / Reviewed), or drop it.

**B5 · MAJOR — Mark Reviewed re-renders the whole page collapsed**
`qp/_section_sales.html:132` (and purchasing :49) post into #main-content; the full re-render resets Alpine to showSales:false (`qp/detail.html:50`) — everything the reviewer had open snaps shut, and a refused-incomplete error list is hidden inside the collapsed section.
**Fix:** target the section's own wrapper (outerHTML, like the field PATCH already does); OOB-update the header chip.

**B6 · MAJOR — QP question grid auto-save steals focus and eats keystrokes**
`_section_sales.html:67-71` — every field change PATCHes and outerHTML-swaps the entire 17-field grid; inputs have no ids so focus can never be restored; text typed into the next field during the round-trip is clobbered; phone keyboard dismisses; zero "saved" feedback. Same in `_section_purchasing.html:57-60`.
**Fix:** keep auto-save but respond `hx-swap="none"` (or OOB-swap only errors + the Mark-Reviewed button) plus a small "Saved" flash.

**B7 · MAJOR — Two confirm-PO forms collect different data**
Legacy quick form (`buy_plans/_detail_lines.html:175-201`) captures only PO# + ship date; the workspace pane requires a payment method and 16 QP-purchasing answers for the same endpoint. Via the legacy path the line lands with payment_method=None (no kanban chip, COD-contradiction guard has nothing to check) and an empty QP purchasing section that later blocks Mark Reviewed with errors nobody remembers causing.
**Fix:** make both surfaces collect the same data — add the required payment-method select to the quick form and include or link the QP grid.

### 2.5 Resell (5 findings, 2 blockers)

**R1 · BLOCKER — Offer modal is blind free-text; a typo'd bid silently loses**
`resell/offer_form.html:35-49` shows none of the posting's lines — one empty part-number box, one line per submit (resell.py:1796-1836). A transposed character stores match_status='unmatched' (excess_service.py:687-701) with a success toast; the trader's own Offers view renders it identically to a matched line (the unmatched queue is owner-only, hard-coded empty for non-owners at resell.py:731-740); the bid never enters the per-line comparison or best rollup.
**Fix:** render the posting's actual lines in the modal (checkbox + unit price + qty per row, one submit); keep a free-text row for substitutes but classify on submit and return an explicit "not on the list — owner will review manually" state, plus an 'unmatched' chip on the trader's own lines (the status is already stored).

**R2 · BLOCKER — Award buttons are clipped off-screen on phones**
Every resell table sits in an `overflow-hidden` wrapper with nowrap cells: per-line offers `_offers.html:210` (Award/Withdraw in the trailing column), Compare modal `offer_compare.html:70`, Lines `_lines.html:179`, Build Bid `_build_bid.html:51`. No ancestor scrolls horizontally, so on a ~390px phone (~240px detail pane) the action column is amputated with no scrollbar and nothing to tap — per-line awarding is impossible until a desktop. `import_preview.html:27` in the same module already uses overflow-x-auto.
**Fix:** change the wrappers to `overflow-x-auto`; longer term, collapse offer rows to stacked cards under a breakpoint with Award always visible.

**R3 · MAJOR — Left rail and triage cards go stale after every lifecycle action**
Publish/close/close-without-bid return only the detail (`resell.py:1763,1776,1789`); award/offer OOB-refreshes stay inside the detail root; the five stat cards render once at page load (`workspace.html:60-88`). Post a draft: detail says "open", the rail row two inches away still says "Draft", and the "Open" card doesn't move — the split view contradicts itself until a manual reload.
**Fix:** attach an `HX-Trigger: resellBoardChanged` to every status-changing response and have #resell-list-body + the strip re-fetch on it (same server-triggered pattern the toasts use).

**R4 · MAJOR — Anonymized rows carry zero content signal**
"Open to Me" lens: every row is "Excess listing #N / Anonymized posting / open / closes Xh" (`_list_rows.html:28-72`; title from resell.py:190-202). A browsing trader must click every posting; yet the line count is already shown to non-owners on the detail (`_header_chips.html:34`) and the Lines tab exposes full part+manufacturer — hiding it on the row protects nothing.
**Fix:** non-owner rows get a content summary from non-identifying line data — "12 lines · Xilinx, TI · New"; retitle the search placeholder ("Search by part or manufacturer…") since part search already works on this lens (resell.py:546-559).

**R5 · MAJOR — Asking price is collected three times and displayed nowhere**
Collected at `add_line_modal.html:46-47`, `edit_line_modal.html:46-48`, and the import preview (`import_preview.html:36`); rendered read-only in zero templates. The Lines tab, Compare modal, and Build Bid table all omit it — the owner pricing a bid-back cannot see the best bid is 30% under the ask they typed; once posted, even the edit-modal read path is gated away (Edit is draft-only, `_lines.html:195`).
**Fix:** owner-only "Ask" column beside "Best offer" on Lines, Compare, and Build Bid (behind the existing can_see_customer gate) — or stop collecting it.

### 2.6 Proactive / Matches (10 findings, 1 blocker)

**P1 · BLOCKER — Process silently drops a no-price line and shows a "." success banner**
`proactive_service.py:314-324` skips lines with no sell anchor (log only); `:649` drops the empty draft; no stat counts it, so the router (htmx/proactive.py:894-906) renders a green banner containing a lone period. The row returns with its checkbox cleared; the rep re-processes forever, and since Prepare is only reachable from a staged draft, that line is unsendable through the UI.
**Fix:** count skips ("1 line skipped — vendor gave no price and there's no prior quote; enter a sell price to offer it"), give the row an inline sell-price input, and never render a success banner with an empty message.

**P2 · MAJOR — Variant verify is one-way; human confirmation can never clear the amber flag**
`_offers_drilldown.html:42-54` offers only "Not the same part"; the endpoint accepts verdict=same (record_human_verdict, part_equivalence.py:266) but no template sends it, and kind is derived purely from key mismatch (proactive_matching.py:164) so a human-confirmed pair still renders amber "AI — verify" forever. Every rep re-verifies the same pair on every visit.
**Fix:** add "Confirm same part" beside the reject; thread verdict source through the rollup so human-confirmed renders as a neutral "verified same" chip. Amber should mean exactly "no human has looked yet".

**P3 · MAJOR — Chip says "expand the offers to verify" but the expander needs offer_count > 1**
`_match_row.html:47-51` (chip) vs `:66,123` (expander gated on count). A single-offer AI-pooled line — exactly where verification matters — offers no way to see the variant spelling, its reason, or reject it. The Supply cell can also name the seeding offer's vendor while qty/cost come from a different vendor's variant stock.
**Fix:** render the drilldown whenever variants exist (has_ai_variants), not only when offer_count > 1.

**P4 · MAJOR — Four paths silently drop the manager's "All salespeople" scope**
Refresh (htmx/proactive.py:127), verdict (:868), prepared Send (:929), prepared Discard (:948) all re-render with the default scope="mine"; only /process threads it. After every single send, the owner lands back in "My matches" — possibly an empty state he reads as "pipeline empty".
**Fix:** carry scope on all four exactly like /process (hidden input / param).

**P5 · MAJOR — Staged matches re-render looking untouched**
Send-processed matches stay status NEW (proactive_service.py:595-615) and reappear in the list (list.html:157) with checkboxes cleared and no staged indicator. Reps re-check, re-process, and hit a cryptic "5 already staged".
**Fix:** render draft-queued rows with a "Staged — see Prepared offers above" chip and disabled checkboxes (or collapse them until sent/discarded).

**P6 · MAJOR — "Prepare" throws away the reviewed draft**
The Prepare page (htmx/proactive.py:160-242) rebuilds from raw matches — empty body, cost×1.3 prices, rebuilt recipients — instead of hydrating the draft the rep just previewed (last-quote-anchored prices, chosen contact); sending then silently deletes the draft via supersede (proactive_service.py:538-549). The customer can get a materially different email at different prices than the one previewed.
**Fix:** when opened from a strip row, hydrate from the draft (subject, body, per-line prices, recipients) with "Editing prepared offer #N".

**P7 · MAJOR — "Don't offer" leaves a ghost in the Process count — and the ban isn't enforced at staging**
The row swap (`_match_row.html:107-119` → hidden tr) never prunes the Alpine sel map, so the bar still reads "Process (1 send)" with nothing visibly checked — and Process stages the banned MPN (build_draft_offers filters only status NEW; the DNO table is consulted nowhere in the draft/send pipeline), one tap from emailing the part the rep just protected.
**Fix:** clear sel[matchId] on the swap (HX-Trigger event) and filter build_draft_offers/send against ProactiveDoNotOffer.

**P8 · MAJOR — Manager's on-behalf sends vanish from every Sent view**
Sent tab is hard-scoped to the viewer (`proactive_service.py:1117-1123`, no toggle — list.html gates the scope chips to the matches tab) while sends are attributed to the rep. The manager can't confirm delivery or convert the win (convert raises "Not your proactive offer"); the rep's Sent tab shows an offer they never sent, with no sent-by attribution.
**Fix:** give Sent the same My/All toggle as Matches for managers and a "sent by" chip when actor ≠ owner.

**P9 · MAJOR — Digest email invites a reply instead of feeding the tracking loop**
`proactive_digest.py:157-182` closes with "Let me know what comes back" — no URL, no mention of recording outcomes in AVAIL. Reps reply by email, the per-line contacted/outcome cells stay empty, and the manager's "Last 7 days" panel shows 0% contact rate for reps who did the work.
**Fix:** end the digest with a link to the Digests tab + "Record each customer's answer on your digest lines in AVAIL — that's what rolls up to the weekly summary."

**P10 · MAJOR — Top picks are inert and stale for up to a day**
Cards are plain divs (`_picks.html:12-26`, no link) and the 24h cache is busted only by scan paths (proactive_matching.py:782,818) — never by process/send/dismiss. Right after acting on the #1 pick, the strip keeps advertising it; tapping does nothing; the rep hunts the groups for a match that no longer exists.
**Fix:** make each card a jump-link to its match row; invalidate the picks cache in process/send/dismiss.

### 2.7 CRM / Prospecting (8 findings, 1 blocker)

**C1 · BLOCKER — Bulk "Deactivate" archives instantly, no confirm, no rep undo**
`customers/_account_list.html:57-63` — sits beside "Send to prospecting", both btn-secondary, no hx-confirm (the single-account path demands "Are you sure?", detail.html:240). One mis-tap archives 8 accounts as Do-Not-Call, hidden from every active list; Reactivate is manager/admin-only (core.py:721). Same no-confirm pattern on the contacts bulk Archive/DNC (`contacts_list.html:108-119`). (Note: bulk archive keeps the owner assigned, unlike single archive which unassigns — the two paths also leave inconsistent state.)
**Fix:** hx-confirm on every destructive bulk action, rename to "Archive (DNC)" to match the single action, and either let the acting owner reactivate their own archives or offer an Undo toast.

**C2 · MAJOR — Bulk actions reset the list to blank filters while the controls show the old ones**
`companies/core.py:302-317` rebuilds the list with hardcoded defaults (search='', my_only=False, sort='oldest', offset=0) even though all three bulk forms post the live filter values via hx-include (`_account_list.html:59,67,84`) — the handler discards them. The rep's "My accounts + Needs a call" view explodes into all accounts oldest-first; the filter bar still shows the old selections; page-2 users snap to page 1.
**Fix:** read the posted filter fields and re-render honoring them, like the pagination links already do.

**C3 · MAJOR — Contact-form "Notes" save somewhere no screen ever shows**
`tabs/_contact_form.html:219-222` saves to SiteContact.notes; every notes display reads ActivityLog only (drawer footer via crm_service.py:691, "See all notes" modal). "Prefers WeChat, on leave until Sept" appears lost — visible only if the rep reopens the Edit modal.
**Fix:** one store — have the form field log an ActivityLog note, or render contact.notes as a pinned "Profile note" in the drawer. Two invisible-to-each-other "Notes" inputs must not coexist.

**C4 · MAJOR — Claiming a prospect dead-ends; no link to the account it just created**
`prospecting/detail.html:66` — claimed detail offers only Release/Enrich/Create Requisition; the card shows no actions at all past 'suggested' (`_card.html:138`). claim_prospect creates/links an owned Company (prospect_claim.py:65) announced only in a transient toast — on a domain collision, under a different name. The rep must hand-search CRM for the account they just took; revealed contacts land on a page the UI never links to.
**Fix:** "Open account" button → /v2/customers/{prospect.company_id} on both the card and detail header for claimed/converted prospects.

**C5 · MAJOR — Phone: tapping an account appears to do nothing**
`_account_list.html:117-127` swaps into #cdm-detail, which stacks below the up-to-50-row list on mobile (list.html:223-229), with no scroll — same dead-tap pattern as Approvals (A5).
**Fix:** same as A5 — scroll the detail into view on selection below md, or navigate to the full-page detail with a back link on phones.

**C6 · MAJOR — Workspace URL ends up naming the wrong company**
Detail tabs push `/v2/customers/{id}?tab=…` unconditionally (`customers/detail.html:346`) but row selection pushes nothing (`_account_list.html:117`) — after Acme → Quotes tab → click Beta, the bar still reads Acme's URL. Refresh/shared link opens Acme's standalone detail with the workspace (list, filters) gone entirely.
**Fix:** symmetric URL state — push /v2/customers/{id} on row selection too (and restore into the workspace on reload), or stop pushing full-page URLs from tabs inside #cdm-detail.

**C7 · MAJOR — Activity tab caps at 50 with no way to load older**
`get_company_activities` limits to the newest 50 across ALL types before bucketing (activity_service.py:855-860; `_shared_tabs.py:358`); auto-logged click-to-contact touches each burn a slot, so a three-week-old account note ("agreed 2% discount if PO > $50K") becomes unreachable — the footer literally says "older history may not appear" with no button (`activity_tab.html:179-183`), notes aren't searchable, and account-level notes have no other home.
**Fix:** "Load older" pagination, or at minimum fetch Notes outside the shared cap — a note a rep wrote must always be reachable from its account.

**C8 · MAJOR — CSV import succeeds but the list behind the modal doesn't refresh**
`companies/core.py:409-414` (and contacts.py:283-288) fire only showToast; create_company fires cdmListRefresh (core.py:522) but import doesn't, so 40 imported companies are invisible until some unrelated interaction — the import looks failed and gets re-run.
**Fix:** add "cdmListRefresh" to both import-confirm HX-Triggers.

### 2.8 Navigation / IA (3 findings, 1 blocker)
*(This review also independently confirmed S2 — partial-URL pushes, extended to Prospecting — and A5; merged above.)*

**N1 · BLOCKER — Customer-contact search results are inert; best-match card links to "#"**
`shared/search_results.html:201-210` renders site_contacts as plain divs while every other group is a link; site_contact is missing from bm_url_map (:31-50), so a contact best-match card gets href/hx-get "#" — clicking it swaps the current full page shell into #main-content, nesting a second top bar and bottom nav inside the page. A bread-and-butter CRM lookup either does nothing or visually breaks the app.
**Fix:** include company_id in the contact result (global_search_service.py:315) and link row + card to `/v2/customers/{company_id}?tab=contacts`; never emit an href="#" best-match — suppress the card for unmapped types.

**N2 · MAJOR — Bottom nav: 11 fixed-width items crushed into a phone**
`shared/mobile_nav.html:24,63` — 10 items + More at w-[62px] each (682px) in a non-scrolling flex row; on 375-430px phones each target shrinks to ~34-39px (well under 44px), 10px labels ("Sightings", "Materials", "Approvals") overflow and overlap, badges bleed into neighbors. The repo's own 44px rule (htmx_mobile.css:11-16) targets a class this nav never uses; the file header still says "8 primary nav items". *(3 reviews — merged.)*
**Fix:** below sm keep 4-5 highest-traffic items at full width and fold the rest into the existing More sheet — or make the strip overflow-x-auto with scroll-snap + edge fade. Truncate or drop labels before they overlap.

**N3 · MAJOR — CRM and Proactive tabs can't be deep-linked or survive reload**
CRM tab buttons push no URL and crm_shell hardcodes customers (`crm/views.py:27-31`; v2_page drops all query params for /v2/crm, htmx_views.py:228); worse, vendor filters push /v2/crm — a URL that reloads to the Customers tab with the vendor context gone. Proactive Matches/Sent/Digests same gap. Approvals, Settings, and customer detail all thread ?tab — the convention exists.
**Fix:** adopt the Approvals pattern: pills push ?tab=…, shells read it, v2_page threads it for both views.

### 2.9 Feedback / States
All five unique candidates from this review pass were the same defects found in other journeys and are merged above: S1 (RFQ results), Q2 (Copy Table + dead toast), S3 (Search All Sources), Q6 (Won/Lost), S6 (hover-only actions), N2 (bottom nav), M1 (non-stacking splits). The review added confirmed detail to each (e.g. the Copy Table toast that never fires; the Resell/Sourcing splits).

### 2.10 Consistency / Mobile (1 finding + merges)

**M1 · MAJOR — Three split workspaces never stack on a phone — including the landing page**
Sales Hub `parts/workspace.html:37` (plain flex, 50/50 inline widths, mouse-only divider — no touch handlers at all), Resell `resell/workspace.html:91`, Sourcing `sourcing/workspace.html:68` (both plain flex; their dividers accept touch but the 4px hover-only grip is undiscoverable). Login redirects to /v2/requisitions → the Sales Hub, so the owner's first screen on a phone is a ~195px parts table beside a ~195px "Select a part" empty state. The responsive pattern already exists and is deliberately applied on Customers (`list.html:224`, flex-col md:flex-row + innerWidth-gated width), Sightings (mobile card view + mobileDetailOpen), and Approvals (max-md:flex-col). *(3 reviews — merged.)*
**Fix:** copy the customers/list.html responsive split to all three: stack below md, scroll detail into view on select, hide/skip the divider on touch. The landing page should be the most phone-capable screen, not the least.

Other Consistency/Mobile candidates merged: N2 (bottom nav), A5/C5 (stacked-split dead taps), S6 (hover-only row actions — this pass added the sightings Build RFQ button and task rows, and confirmed there is no mobile card fallback for the sightings table at any width).

---

## 3. Cross-cutting themes — fix once, lift many screens

**T1 · Never show success the server didn't verify.**
Green banners and toasts fire on failure or partial completion across five journeys: RFQ "sent" with zero emails (S1), Process "." banner (P1), prepay modal closing on error (B1), resell "Offer submitted" on an unmatched bid (R1), import "success" over a stale list (C8), copy "confirmation" that never fires (Q2). Adopt two rules app-wide: (a) service errors return non-2xx or an explicit error partial — never 200 + HX-Reswap:none where a close-on-success hook listens; (b) every multi-item operation renders a Sent / Failed / Skipped breakdown, never just the success count.

**T2 · One mobile pass: stack, scroll, reveal, shrink the nav.**
Four one-pattern fixes cover ~15 findings: (1) copy the customers responsive split to Sales Hub/Resell/Sourcing (M1); (2) scroll the detail pane into view on row select in Approvals and Customers (A5/C5); (3) one `@media (hover:none)` rule to reveal all hover-hidden row actions (S6); (4) collapse the bottom nav to 4-5 items + More (N2). Plus `overflow-x-auto` on the resell tables (R2). The owner is phone-primary; today every core surface fails on his device.

**T3 · URLs must tell the truth.**
One convention: hx-push-url carries only canonical page URLs (never /v2/partials/*), partial routes that can be pushed get `full_page_shell()` fallback, every navigation-sized swap pushes its destination, and tab/scope state threads through ?tab/?scope everywhere (the Approvals pattern). Covers S2, S8, Q9, C6, N3, A10 and prevents the whole class recurring.

**T4 · Dead ends both directions: stored-but-never-shown, built-but-never-linked.**
Fields collected and displayed nowhere (asking price R5, contact notes C3, result_reason Q6, match_status R1); routes/flags built and rendered nowhere (mark-paid/reverse A1, request-prepayment A2, flag-issue B2, is_assigned_buyer A9, verdict=same P2, QP page link B3, preflight/preview/override endpoints Q3-Q5). Institute a cheap audit habit: every stored field names its display surface; every route names its button; grep for orphans after each migration (the workspace migration stranded at least four).

**T5 · Swap where the user is looking.**
Responses land off-viewport or destroy in-progress work: #parse-area at the top (S7), full-workspace replacement (Q9), search input replaced mid-type (A4), QP grid clobbering keystrokes (B6), sections collapsing on save (B5), ghost empty-state (S9), stale sibling panes (R3, C2). House rules: swap the smallest element that changed; OOB for adjacent state; `show:top` when the target can be off-viewport; stable ids on any input inside a swap target; a server-triggered refresh event for list+detail siblings.

---

## 4. Recommended sequence — top 8 by experience-lift per effort

1. **Stop the customer-facing money leaks (Q1 + Q2).** Split/relabel Internal Notes and fix the Copy Table column shift + toast. Two small template fixes; ends silent margin/cost disclosure to customers today.
2. **Make RFQ results honest (S1).** Render failed/skipped sections, amber no-token state, honest Select All. One template + one route; buyers stop waiting on quotes that were never requested.
3. **URL truth on the two list screens (S2).** Push canonical URLs on Requisitions + Prospecting filters and add full_page_shell to both partial routes. Mostly attribute changes; kills the app-looks-broken reload.
4. **The one-CSS-rule mobile unlock (S6 + N2).** Global hover:none reveal rule + bottom nav collapse to 5 + More. Makes offers approvable, tasks editable, and navigation tappable on the phone.
5. **Workspace phone pass (A5/C5 + M1 + R2).** scrollIntoView on select in Approvals/Customers; stack the three non-stacking splits (copy the customers pattern); overflow-x-auto on resell tables so awarding works. One focused mobile PR.
6. **Un-wedge the prepayment flow (A1 + A2 + A3 + B1).** Render mark-paid/reverse, add request-prepayment to the PO pane, keep approved-unpaid in Live, keep the modal open on errors. Completes the money loop on the surface it's supposed to live on.
7. **Honest send path for quotes (Q3 + Q5 + Q8).** Recipient dialog wired to override_email, preflight card on quote detail, 0-margin/$0 send check with Build-Quote-style seeding. Every Send button behaves the same and nothing leaves at cost.
8. **Proactive trust pass (P1 + P4 + P5 + P7).** Count and explain skips, thread scope through all re-renders, mark staged rows, prune sel on Don't-offer and enforce DNO at staging/send. The Matches tab stops lying to the person clicking Process.

---

## 5. Cleared-on-verification appendix

None. All 77 raw findings (62 after cross-journey dedup) were adversarially verified against current code and **confirmed real and reachable — zero were refuted or downgraded to non-issues.** Verification corrected minor details in several (exact file paths for the RFQ router and parts-row buttons; bulk archive keeps the owner assigned; recovery-by-search after CSV import; the resell/sourcing dividers technically accept touch; nav arithmetic 11×62px), and in four cases found the defect slightly *worse* than claimed (Graph batch-failure fallback also reports success in S1; the Closed feed's 10-row cap in A3; no MPN-chip fallback in S5; no sightings mobile card list in S6). Those corrections are folded into the entries above.
