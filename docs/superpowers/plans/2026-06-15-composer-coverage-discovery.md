# Composer Coverage-Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Surface every vendor-with-coverage (carded or not) in the composer's suggestions, ranked by coverage; non-RFQ-able vendors get a "no contact on file" badge + disabled checkbox + an "Add contact" action that reuses Track B's inline-create. No schema change, no new endpoint, no bulk CRM writes.

**Spec (authoritative):** `docs/superpowers/specs/2026-06-15-composer-coverage-discovery-design.md` — read fully. Also CLAUDE.md (Alpine attr landmines, thin routers, response formats).

---

### Task 1: Query — include cardless vendors + has_contact

**Files:** `app/routers/sightings.py` (`RankedVendor` :1327, `_coverage_ranked_vendor_rows` :1351, the affinity dedup consumer :1524, the modal context build ~:1451), `tests/test_sightings_router.py`.

- Outer join VSS→VendorCard via `_vss_vendor_card_join()`; group in Python by `card.id` (carded) else `normalize_vendor_name(vendor_name)` (cardless); accumulate distinct-requirement count, non-null scores → avg, representative card, deterministic display name (spec Part 1).
- Blacklist drop only when carded; exclusion drop by normalized name (keep belt-and-braces).
- `has_contact`: read `sightings_send_inquiry`/`send_batch_rfq` contact resolution FIRST and mirror it exactly (card + resolvable email); batch the contact lookup, no N+1.
- Rank: covered_count desc, has_contact desc, engagement desc nullslast, stable tiebreak; cap 20.
- New `RankedVendor(card: VendorCard | None, vendor_name, covered_count, avg_score, has_contact)`; update docstring + every consumer (affinity dedup keys on `normalize_vendor_name(r.vendor_name)`).
- Tests: spec "Query" list verbatim (cardless appears has_contact=False; carded+email True; carded-no-email False; cardless 2/2 outranks carded 1/2; contactable above non-contactable at equal coverage; name-variant merge; excluded cardless absent; blacklisted carded absent; cap 20).

- [ ] Failing tests first → implement → green → commit `feat(composer): include cardless vendors in coverage suggestions with has_contact`

### Task 2: Template + "Add contact" wiring

**Files:** `app/templates/htmx/partials/sightings/vendor_modal.html`, `app/static/htmx_app.js` (the `rfqVendorModal` factory — read it for the real inline-create state/method names), `tests/test_sightings_router.py` + `tests/frontend/rfq-vendor-modal.test.ts`.

- Contactable rows: unchanged (enabled checkbox, engagement badges). Non-contactable: disabled checkbox (reuse the excluded-vendor disabled pattern), `bg-gray-100 text-gray-500` "no contact on file" badge, "Add contact" link (full-literal Tailwind, single-quoted Alpine attrs).
- "Add contact" `@click`: pre-fill + reveal the existing inline "Add new vendor" form with the vendor name, focus the email input — reuse the real `rfqVendorModal` state/methods (no new endpoint, no invented names). No literal `"` inside double-quoted Alpine attrs.
- Tests: contactable→enabled+badge; non-contactable→disabled+badge+Add-contact link; MPN title still rendered; Vitest: "Add contact" sets the inline-form name + reveals it.

- [ ] Failing tests first → implement → green → commit `feat(composer): no-contact badge + Add-contact action for cardless vendors`

### Task 3: Docs + gate

- [ ] `docs/APP_MAP_INTERACTIONS.md`: update the composer vendor-panel section — coverage suggestions now include cardless vendors (outer join + Python group), `has_contact` mirrors send-skip, "Add contact" reuses inline-create. Note the deferred follow-up: vendor-email capture at scrape time (the long-tail lever).
- [ ] Full suite `TESTING=1 PYTHONPATH=. pytest tests/ -q --tb=line`; `npm run test:frontend`; `npm run lint`; `pre-commit run --files <changed>`.
- [ ] Commit `docs(composer): APP_MAP for coverage-discovery + cardless vendors`
