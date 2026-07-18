# Resell Rework — Phase 4: Workflow Completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the module's workflow dead-ends so it's a usable two-sided marketplace — give brokers a way to see/withdraw their own offers, give owners draft-editing + a true "close without bidding" + unmatched-line assignment + manual-channel logging, and fix the `stage=live` triage token. All 6 controls were **user-approved** (2026-07-18). **No migration** (all use existing schema/enum).

**Architecture:** New routes are thin (`app/routers/resell.py`); logic in `app/services/`. Reuse the established patterns: the Withdraw button, the convert-to-offer form (`_reply_viewer.html`), the award `_offer_action` macro, and the filter-token mechanism. Gate every owner action with `_require_owner`; keep the `can_see_customer` anonymization discipline from Phase 3 (never leak competitor data into the broker own-offers view).

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (2.0-style only — ratchet is down-only), HTMX/Alpine/Jinja2, pytest.

## Global Constraints
- Status/enum values ALWAYS from `app/constants.py`. `CLOSED = "closed"` already exists (Phase 1 kept it distinct, D5) — Task 3 gives it its first writer.
- No new UI *conventions* — reuse existing controls/macros (approved set only; do not add unapproved elements).
- Every new mutating route: `_get_list_for_user` (404) + `_require_owner` (403) where owner-only, draft/status guards as noted, 2.0-style DB access, `HTTPException` on guard failure.
- After changes, update `docs/APP_MAP_INTERACTIONS.md` (the new workflows).
- Re-verify every line number against the CURRENT worktree (Phases 1–3 moved things); work by symbol.

---

### Task 1: Broker own-offers view + Withdraw (finding #13)

**Files:** `app/routers/resell.py` (`_offers_context` non-owner branch ~473-482; `resell_submit_offer` re-render ~1138; `_get_list_for_user` ~244 expired-access); `app/templates/htmx/partials/resell/_offers.html` (~57-65 non-owner branch); Test `tests/test_resell_offers.py` + `tests/test_resell_draft_offer_privacy.py`.

**Interfaces:** Produces: a non-owner viewing a posted list's Offers tab sees ONLY their own offers (`submitted_by == user.id`, via 2.0 select) — no competitor data (keep `_broker_label`/anonymization) — each open/late own-offer with a **Withdraw** button (the withdraw route + submitter-authz already exist at ~1197-1227). The submitter can reach their own open offer even after the list expires (relax `_get_list_for_user` so a submitter with an offer isn't 404'd on a non-posted status). Post-submit re-render shows the submitter their own offers (not the empty state).

- [ ] Failing tests: non-owner Offers tab shows their own offer + Withdraw, and shows NO other broker's offer/count; submitter can withdraw after expiry; owner view unchanged. → red → implement (hydrate own-offers in `_offers_context` for non-owner; render in `_offers.html`; relax expired access for a submitter-with-offer) → green → commit.

---

### Task 2: Draft-edit set + honest 409 copy (finding #14, decision D4)

**Files:** `app/routers/resell.py` (4 NEW routes); `app/services/excess_service.py` (4 NEW functions); `_lines.html`, `detail.html` header, `_header_chips.html`; replace false copy at resell.py ~886/959/1036; Test `tests/test_resell_routes.py` + a new `tests/test_resell_draft_edit.py`.

**Interfaces (all DRAFT-only, `_require_owner` + 409-unless-draft; a draft has no offers/mirror so these are side-effect-free except `total_line_items`):**
- `DELETE /api/resell/{list_id}/lines/{line_id}` → `excess_service.delete_line` (delete + decrement `total_line_items`) → re-render `detail.html`.
- `PATCH /api/resell/{list_id}/lines/{line_id}` → `excess_service.update_line` (fields part_number/quantity/manufacturer/condition/date_code/asking_price; **re-validate quantity>0** — the model `@validates` raises else 500; re-resolve material card if MPN/mfr changed) → re-render.
- `PATCH /api/resell/{list_id}` → `excess_service.update_excess_list` (title/notes/company_id[+re-validate exists]/customer_site_id) → re-render `detail.html`.
- `DELETE /api/resell/{list_id}` → `excess_service.delete_excess_list` (draft-only; cascade cleans children) → return My-Lists partial + detail-pane reset + toast.
- **Honest 409 copy:** replace `"Posted lists are locked; revise as a new version"` (×3) with `"Posted lists are locked. Close this list and create a new one to make changes."`

- [ ] Per-route TDD: failing test (guard rejections: non-owner 403, non-draft 409, cross-list line 404, quantity=0 rejected; happy path mutates + re-renders) → red → implement service+route+template affordance → green → commit (may group the 4 into 1–2 commits given shared files).

---

### Task 3: "Close without bidding" → CLOSED terminal state (decision D5)

**Files:** `app/services/excess_service.py` (`close_list` gains a mode, or new `resolve_list_without_bid`); `app/routers/resell.py` (new/extended close route); `detail.html` header (~64-73, a second action alongside Close); `workspace.html` (~66 bid_out subtitle); `_lists.html`/`_header_chips.html` (CLOSED pill already styled); Test `tests/test_resell_list_lifecycle.py`.

**Interfaces:** Produces: an owner action "Close without bidding" that flips `open`/`collecting` → **CLOSED** (guarded exactly like `close_list`: 409 unless open/collecting; stamps `close_at`; retires the mirror via `sync_list_mirror`); CLOSED is terminal (no reopen, no bid-out-from-closed — mirror Phase-1's terminal reader-set treatment). Keep the existing Close→`bid_out` path for "bids went out." Fix the `bid_out` "Sent to the customer" subtitle to be accurate.

- [ ] Failing tests: close-without-bid on open/collecting → CLOSED + close_at + mirror retired; 409 on draft/terminal; CLOSED excluded from expiry/reopen. → red → implement → green → commit. **UI copy/affordance: mirror the existing Close button's confirm pattern; distinct label "Close (no bid)".**

---

### Task 4: "Assign to line" — unmatched-offer resolution (finding #15)

**Files:** `app/services/excess_service.py` (NEW `assign_offer_line`); `app/routers/resell.py` (NEW owner-only POST); `_offers.html` (~180-199 unmatched queue — the read-only "resolve manually" card gains an assign control mirroring the `_offer_action` macro); Test `tests/test_resell_award.py` pattern + new cases.

**Interfaces:** Produces: an owner assigns an unmatched/ambiguous `ExcessOfferLine` to a target posted `ExcessLineItem` — sets `excess_line_item_id`, flips `match_status` → MATCHED, recomputes the target line's rollup (`recompute_line_rollup`). Reject a target line not on this list (404). Now the salvaged offer is awardable.

- [ ] Failing tests: assign unmatched→matched updates target rollup; cross-list target 404; non-owner 403; re-assign moves it (recompute both old+new). → red → implement → green → commit.

---

### Task 5: Manual-channel "Log response / Log their bid" + checkbox fix (finding #12)

**Files:** `app/routers/resell.py` (NEW owner-only log routes keyed by outreach_id, reuse the convert-to-offer line form); `app/services/resell_outreach_service.py` (record manual RESPONSE/BID on the outreach row, create the ExcessOffer via the existing convert path); `_outreach.html` (manual-channel rows gain Log-response/Log-bid actions); `offer_buyers_modal.html` (~120-127 — enable the no-contact checkbox on phone/teams/marketplace channels); Test `tests/test_resell_outreach_*.py`.

**Interfaces:** Produces: for a manual-channel (`phone`/`teams`/`marketplace`, no `graph_conversation_id`) outreach row stuck at `sent`, the owner can Log a response (→ RESPONDED) or Log their bid (→ BID + create the ExcessOffer via the reused `_reply_viewer.html` convert form). The no-contact checkbox is enabled for manual channels (email-only disable stays).

- [ ] Failing tests: log-response flips status; log-bid creates the offer + flips BID; non-owner 403; checkbox enabled for manual channel. → red → implement → green → commit.

---

### Task 6: `stage=live` triage token (finding #16)

**Files:** `app/routers/resell.py` (`resell_lists` stage filter ~376-377 — add a `live` token → `status in (open, collecting)`); `workspace.html` (~63 glance card: `stage=live` + truthful subtitle); Test `tests/test_nightly_resell_coverage.py` (the stage-filter tests strengthened in Phase 3).

**Interfaces:** Produces: `stage=live` expands to `[open, collecting]`; the "Open" triage card links to `stage=live` (matching its open+collecting count) with an accurate subtitle. The strict `open` pill in `_lists.html` keeps meaning exactly `status=open` (do NOT overload it).

- [ ] Failing tests: `?stage=live` includes both an open and a collecting list, excludes bid_out/awarded; the strict `?stage=open` still returns only open. → red → implement → green → commit. Update `docs/APP_MAP_INTERACTIONS.md`. Open PR; adversarial review workflow; live-verify.

---

## Self-Review
- **Coverage:** #13 (T1), #14+D4 (T2), D5-CLOSE (T3), #15 (T4), #12 (T5), #16 (T6). ✓
- **No migration** (existing schema/enum). ✓
- **Anonymization preserved:** T1 own-offers view carries no competitor data (Phase-3 discipline). ✓
- **Reuse over invent:** Withdraw button, convert-to-offer form, `_offer_action` macro, filter-token — only assign-to-line + inline-edit are net-new interactions. ✓
