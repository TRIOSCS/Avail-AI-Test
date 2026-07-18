# Resell Rework — Phase 3: Anonymization Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close every known Resell anonymization leak before multi-user go-live — apply D2 (offer counts/coverage PRIVATE) to all surfaces via ONE predicate, stop the open-lens search + outreach-subject de-anonymization oracles, and replace assert-200 theater with real inclusion/exclusion + owner-scoping tests.

**Architecture:** No schema change. Route every count/coverage/existence surface through the single existing ownership flag `can_see_customer` (== `el.owner_id == user.id`); reconcile the one code comment that declares the opposite. Search filters on part identity (indexed) in the open lens, never title. Subject prefill goes neutral.

**Tech Stack:** FastAPI, Jinja2/HTMX templates, pytest. **No migration** (code + template + test only).

## Global Constraints
- ONE policy: reuse `can_see_customer` everywhere — do NOT invent a second flag. It already means "viewer owns this list" (`resell.py:267`, aliased `is_owner` at `_lines.html:16`).
- The private-side precedent to mirror: `_lines.html:_line_offer_badge:20-34` (dash to non-owners) + the 403 guards (`resell.py:654-656`, `:557-561`). D2 generalizes this to the aggregate surfaces.
- Do not change UI layout/elements beyond gating visibility (UI guardrail) — gating an existing chip on ownership is a visibility change, allowed; adding/removing chips is not.
- Touching any resell test file forces its assertion-theater baseline entry to be burned down (CI lints changed files) — strengthen the test AND delete its `scripts/assertion_theater_baseline.txt` line in the same commit.

## OPEN CONFIRM (surface to user when Phase 3 starts)
- The adjacent **"N/M awarded" chip** (`_header_chips.html:34-40`) is declared *public* by the comment at `:32-33`. D2's "one policy everywhere" implies gating it too (a non-owner sees competitive progress), but D2 was scoped to offer counts/coverage. **Ask the user: gate the awarded chip on ownership too, or keep "deal done" public?** Recommend gate (consistency). Low-stakes, one quick confirm.

---

### Task 1: Apply D2 — gate the three count/coverage surfaces + reconcile the comment

**Files:** Modify `app/routers/resell.py:_list_cards:135-186` (null the counts when `not can_see_customer`; pass the flag into `_lists.html` context at `:393-405`); `app/templates/htmx/partials/resell/_header_chips.html:30-33` (gate offer chip; rewrite the public-position comment); `detail.html:101` (add `and can_see_customer`); `_lists.html:73-83` (wrap coverage meter + amber badge). Test `tests/test_resell_draft_offer_privacy.py`.

**Interfaces:** Consumes `can_see_customer`. Produces: header "N offers" chip, Offers-tab count badge, and open-lens coverage meter/badge all render ONLY for the owner; non-owner sees none of them (matching the already-private per-line badge).

- [ ] **Step 1:** Failing tests (extend RS-1 pattern `tests/test_resell_draft_offer_privacy.py:127-144`) — a non-owner viewing a posted list's **detail** sees no "N offers" header chip and no Offers-tab count badge; a non-owner in `lens=open` sees no coverage meter / amber offer badge on the list rows. Owner still sees all three.
- [ ] **Step 2:** Run — FAIL (surfaces currently leak).
- [ ] **Step 3:** In `_list_cards`, set `coverage_filled/coverage_total/offer_count = None` when `not can_see_customer`; pass `can_see_customer` into the `_lists.html` render context. Gate `_header_chips.html:31` offer chip + `detail.html:101` badge on `can_see_customer`. Wrap `_lists.html:73-83` on the passed flag. Rewrite the `_header_chips.html:32-33` comment to state the PRIVATE policy.
- [ ] **Step 4:** Run — PASS. **Step 5:** Commit.

---

### Task 2: #10 — open-lens search filters on part identity, not title

**Files:** Modify `app/routers/resell.py:resell_lists` q-filter `:387-388`; Test `tests/test_resell_routes.py`.

**Interfaces:** Produces: in `lens=open`, `q` matches `ExcessLineItem.normalized_part_number` / `.manufacturer` (both indexed, `models/excess.py:99-104`) via a subquery on `excess_list_id` — NEVER `ExcessList.title`. The title ILIKE stays only for `lens=mine`.

- [ ] **Step 1:** Failing tests — `GET /...lists?lens=open&q=<customer name>` returns NO rows (title no longer searchable by non-owners); `?lens=open&q=<real MPN>` returns the matching anonymized row; `lens=mine&q=<title fragment>` still matches (owner).
- [ ] **Step 2:** Run — FAIL (title currently searchable in both lenses).
- [ ] **Step 3:** Branch the `q` filter on lens: open → part-identity subquery; mine → existing title ILIKE.
- [ ] **Step 4:** Run — PASS. **Step 5:** Commit.

---

### Task 3: #11 — neutral outreach subject prefill (+ optional warn)

**Files:** Modify `app/templates/htmx/partials/resell/offer_buyers_modal.html:192` (subject prefill); review/neutralize internal ActivityLog subject `resell_outreach_service.py:251`; Test `tests/test_resell_outreach_routes.py` or a template-render test.

**Interfaces:** Produces: the outreach subject `value` no longer embeds `el.title` (the un-anonymized, customer-named field). Neutral part-based default (e.g. `Excess available: {{ line_count }} lines`). Reply-matching is unaffected (the send + `_find_sent_message` both use the same sent subject, `resell_outreach_service.py:485`). Optional: Alpine advisory warn (non-blocking, mirror the overlap-warning pattern `offer_buyers_modal.html:107-113`) if the typed subject contains the customer name.

- [ ] **Step 1:** Failing test — the rendered outreach modal's subject input default does NOT contain `el.title`; a neutral default is present.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Replace the prefill with the neutral default; review `:251` internal subject. **Step 4:** Run — PASS. **Step 5:** Commit.

---

### Task 4: #21 — real filter/owner-scoping tests + ratchet the baseline

**Files:** Modify `tests/test_nightly_resell_coverage.py:308-322` (strengthen stage/q); add `test_lists_mine_lens_excludes_other_owners`; strengthen `tests/test_resell_offers.py::test_confirm_import_resolves_material_card_id` (assert MAX232); drop redundant `tests/test_resell_outreach_routes.py::test_submit_outreach_posted_list_200:509`; Modify `scripts/assertion_theater_baseline.txt` (remove the strengthened/dropped entries: `:642`, `:643`, `:698`, `:699`, and any others touched from `:641-643,697-700`).

**Interfaces:** Model to copy: `tests/test_resell_routes.py:1091-1158` (override `require_user` → owner; `assert title in body` / `assert other_title not in body`).

- [ ] **Step 1:** Rewrite `test_lists_stage_filter` to seed collecting+awarded lists and assert `?stage=collecting` includes the collecting title, excludes the awarded one. Rewrite `test_lists_q_filter` with a title that actually contains the query term (inclusion) + a non-matching title (exclusion). Add `test_lists_mine_lens_excludes_other_owners` (another trader's list absent from `lens=mine`). Strengthen the confirm-import test to assert the MAX232 card. Drop the redundant outreach-200 test.
- [ ] **Step 2:** Run the new/changed tests — green.
- [ ] **Step 3:** Delete the corresponding lines from `scripts/assertion_theater_baseline.txt`; run `python3 scripts/lint_assertion_theater.py` (changed files) — passes with the entries burned down.
- [ ] **Step 4:** `pre-commit run --all-files`; update `docs/APP_MAP_INTERACTIONS.md` (Resell privacy policy). Commit. Open PR; pr-review-fleet; live-verify (non-owner sees no counts; open-lens name-search returns nothing).

---

## Self-Review
- **Coverage:** #9 (T1), #10 (T2), #11 (T3), #21 (T4). ✓
- **One policy:** all surfaces reuse `can_see_customer`; the contradicting comment rewritten (T1). ✓
- **Awarded-chip judgment** surfaced as an OPEN CONFIRM, not silently decided. ✓
- **No migration** (code/template/test only). ✓
- **Baseline ratchet** paired with each strengthened test (T4). ✓
