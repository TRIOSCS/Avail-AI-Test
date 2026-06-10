# Sightings Vendor Row Status Treatment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make unavailable sightings (soft red tint, dimmed text, red badge) and converted-to-offer sightings (mild green tint, emerald badge) visually obvious on the sightings workspace vendor rows.

**Architecture:** Presentation-only change in one Jinja2 partial, keyed off the already-computed vendor status `vs` (precedence resolved server-side in `app/services/sighting_status.py` — `blacklisted > offer-in > contacted > unavailable > sighting`). No route/model/schema changes.

**Tech Stack:** Jinja2 + Tailwind CSS 3 (JIT, content-scan over templates — class strings must be full literals), FastAPI route-render tests with pytest.

**Spec:** `docs/superpowers/specs/2026-06-10-sightings-status-row-treatment-design.md`

---

### Task 1: Status-aware row treatment in `_vendor_row.html`

**Files:**
- Modify: `app/templates/htmx/partials/sightings/_vendor_row.html` (row div ~line 20, vendor-name span ~line 24, `vs_styles` ~lines 25–31, qty/score spans ~lines 91–93)
- Test: `tests/test_sightings_router.py` (new class after `TestSightingsMarkUnavailable`, ~line 280)

- [ ] **Step 1: Write the failing tests**

Add after `TestSightingsMarkUnavailable` in `tests/test_sightings_router.py` (imports for `Offer` and `Sighting` already exist at the top of the file):

```python
class TestSightingsVendorRowStatusTreatment:
    """Row-level visual treatment keyed off computed vendor status (spec
    2026-06-10-sightings-status-row-treatment-design.md)."""

    def test_unavailable_vendor_gets_red_row_treatment(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-rose-50/60" in resp.text          # row tint
        assert "bg-rose-100 text-rose-700" in resp.text  # badge

    def test_offer_in_vendor_gets_green_row_treatment(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        db_session.add(
            Offer(
                requirement_id=r.id,
                requisition_id=req.id,
                vendor_name="Good Vendor",
                mpn="TEST-MPN-001",
                unit_price=1.50,
                qty_available=100,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-emerald-50/50" in resp.text           # row tint
        assert "bg-emerald-100 text-emerald-700" in resp.text  # badge

    def test_plain_sighting_row_has_no_status_tint(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-rose-50/60" not in resp.text
        assert "bg-emerald-50/50" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/availai/.claude/worktrees/sightings-status-rows && TESTING=1 PYTHONPATH=. pytest tests/test_sightings_router.py::TestSightingsVendorRowStatusTreatment -v --override-ini="addopts="`
Expected: first two tests FAIL on the class-string assertions (`bg-rose-50/60` / `bg-emerald-50/50` not in response); third PASSES (it encodes the current no-tint state).

- [ ] **Step 3: Implement the template change**

In `app/templates/htmx/partials/sightings/_vendor_row.html`:

(a) Replace the always-visible row div opener (currently
`<div class="flex items-center px-2 py-1.5 cursor-pointer hover:bg-gray-50/50 transition-colors"`) with:

```jinja
    {# Status-aware row treatment: unavailable = red tint + dimmed, offer-in = green tint #}
    {% set row_bg = {
      'unavailable': 'bg-rose-50/60 hover:bg-rose-50/80',
      'offer-in': 'bg-emerald-50/50 hover:bg-emerald-50/70',
    }.get(vs, 'hover:bg-gray-50/50') %}
    <div class="flex items-center px-2 py-1.5 cursor-pointer transition-colors {{ row_bg }}"
```

(b) Vendor name span — dim when unavailable. Replace
`<span class="font-medium text-gray-900 text-sm">{{ s.vendor_name }}</span>` with:

```jinja
          <span class="font-medium text-sm {{ 'text-gray-400' if vs == 'unavailable' else 'text-gray-900' }}">{{ s.vendor_name }}</span>
```

(c) `vs_styles` dict — stronger badges on tinted rows (50-shade chips vanish against 50-shade tints):

```jinja
          {% set vs_styles = {
            'sighting': 'bg-gray-100 text-gray-600',
            'contacted': 'bg-blue-50 text-blue-700',
            'offer-in': 'bg-emerald-100 text-emerald-700',
            'unavailable': 'bg-rose-100 text-rose-700',
            'blacklisted': 'bg-red-50 text-red-700',
          } %}
```

(d) Right-side qty + score — dim when unavailable. Replace:

```jinja
        <span class="font-mono text-xs text-gray-600">{{ s.estimated_qty or '—' }} pcs</span>
        {% set score_color = 'text-emerald-600' if s.score >= 70 else ('text-amber-600' if s.score >= 40 else 'text-gray-500') %}
```

with:

```jinja
        <span class="font-mono text-xs {{ 'text-gray-400' if vs == 'unavailable' else 'text-gray-600' }}">{{ s.estimated_qty or '—' }} pcs</span>
        {% set score_color = 'text-gray-400' if vs == 'unavailable' else ('text-emerald-600' if s.score >= 70 else ('text-amber-600' if s.score >= 40 else 'text-gray-500')) %}
```

Nothing else changes: expanded detail keeps `bg-gray-50/50`, `<tr>` border classes unchanged, action-button conditions unchanged.

- [ ] **Step 4: Run the new tests — all pass**

Run: `TESTING=1 PYTHONPATH=. pytest tests/test_sightings_router.py::TestSightingsVendorRowStatusTreatment -v --override-ini="addopts="`
Expected: 3 PASSED.

- [ ] **Step 5: Run the full sightings test file**

Run: `TESTING=1 PYTHONPATH=. pytest tests/test_sightings_router.py -v`
Expected: all PASS (no existing test asserts the old gray unavailable badge or the old row classes — verify; if one does, update it to the new spec classes).

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/sightings/_vendor_row.html tests/test_sightings_router.py
git commit -m "feat(sightings): row-level red/green treatment for unavailable and offer-in vendors"
```

### Task 2: APP_MAP doc touch-up

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (sightings section — only if it describes vendor-row status presentation; check with `grep -n "vendor row\|_vendor_row\|sighting status\|offer-in" docs/APP_MAP_*.md`)

- [ ] **Step 1: Check whether any APP_MAP doc describes the vendor-row status rendering**

Run: `grep -n "vendor row\|_vendor_row\|sighting_status\|offer-in" docs/APP_MAP_*.md`
If a hit describes the vendors panel/status badges, add one sentence noting the row-level tint treatment (unavailable = rose tint + dimmed, offer-in = emerald tint). If no hit, no doc change is needed (presentation detail below APP_MAP altitude) — note that in the task result.

- [ ] **Step 2: Commit (only if a doc changed)**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs: note sightings vendor-row status tint in APP_MAP"
```

### Task 3: Verification gate

- [ ] **Step 1: Full suite**

Run: `TESTING=1 PYTHONPATH=. pytest tests/ -q`
Expected: green (same pass/fail profile as main).

- [ ] **Step 2: Pre-commit on changed files**

Run: `pre-commit run --files app/templates/htmx/partials/sightings/_vendor_row.html tests/test_sightings_router.py`
Expected: all hooks pass (ruff/format don't touch templates; test file must pass ruff + format).
