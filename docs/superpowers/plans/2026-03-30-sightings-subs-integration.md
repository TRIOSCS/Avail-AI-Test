# Sightings Page — Substitute MPN Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface substitute MPN information throughout the sightings page so buyers can see which subs exist, search by sub MPN, and know which MPN each vendor sighting matched against.

**Architecture:** Three UI touchpoints (table badge, detail pills, vendor row tags) plus one search filter extension. No schema changes — uses existing `Requirement.substitutes`, `Requirement.substitutes_text`, and `Sighting.mpn_matched` columns. All changes are in the sightings router and its three template files.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX, Tailwind CSS, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/routers/sightings.py` | Modify | Add `substitutes_text` to search filter (line ~148); query `Sighting.mpn_matched` in detail endpoint (line ~513) |
| `app/templates/htmx/partials/sightings/table.html` | Modify | Add sub count badge in `render_row` (line ~153) and `render_card` (line ~228) |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Add sub pills below primary MPN (line ~18) |
| `app/templates/htmx/partials/sightings/_vendor_row.html` | Modify | Add "via SUB-MPN" tags (line ~43) |
| `tests/test_sightings_router.py` | Modify | Add tests for sub search, sub badge rendering, vendor matched MPN tags |

---

### Task 1: Search filter — extend to match substitute MPNs

**Files:**
- Modify: `tests/test_sightings_router.py`
- Modify: `app/routers/sightings.py:146-150`

- [ ] **Step 1: Write the failing test for sub MPN search**

Add to `tests/test_sightings_router.py` inside `TestSightingsFilters`:

```python
def test_search_by_substitute_mpn(self, client, db_session):
    """Search filter matches requirements by substitute MPN."""
    req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="PRIMARY-001",
        manufacturer="Mfr",
        target_qty=50,
        sourcing_status="open",
        substitutes=[{"mpn": "ALT-SUB-777", "manufacturer": "AltMfr"}],
        substitutes_text="ALT-SUB-777",
    )
    db_session.add(r)
    db_session.flush()
    db_session.add(VendorSightingSummary(
        requirement_id=r.id, vendor_name="V1", listing_count=1, score=50.0,
    ))
    db_session.commit()

    # Search by sub MPN should find this requirement
    resp = client.get("/v2/partials/sightings?q=ALT-SUB-777")
    assert resp.status_code == 200
    assert "PRIMARY-001" in resp.text

    # Search by primary MPN still works
    resp = client.get("/v2/partials/sightings?q=PRIMARY-001")
    assert resp.status_code == 200
    assert "PRIMARY-001" in resp.text

def test_search_by_sub_no_false_positive(self, client, db_session):
    """Sub search does not return unrelated requirements."""
    _seed_data(db_session)
    resp = client.get("/v2/partials/sightings?q=ALT-SUB-777")
    assert resp.status_code == 200
    assert "TEST-MPN-001" not in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsFilters::test_search_by_substitute_mpn -v --override-ini="addopts="`
Expected: FAIL — search by `ALT-SUB-777` won't find `PRIMARY-001` because the filter only checks `primary_mpn` and `customer_name`.

- [ ] **Step 3: Implement the search filter change**

In `app/routers/sightings.py`, find the search filter block (around line 146-150):

```python
# BEFORE:
    if filters.q:
        safe_q = escape_like(filters.q)
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{safe_q}%") | Requisition.customer_name.ilike(f"%{safe_q}%")
        )

# AFTER:
    if filters.q:
        safe_q = escape_like(filters.q)
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{safe_q}%")
            | Requisition.customer_name.ilike(f"%{safe_q}%")
            | Requirement.substitutes_text.ilike(f"%{safe_q}%")
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsFilters -v --override-ini="addopts="`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add tests/test_sightings_router.py app/routers/sightings.py
git commit -m "feat(sightings): extend search filter to match substitute MPNs"
```

---

### Task 2: Table rows — add sub count badge

**Files:**
- Modify: `tests/test_sightings_router.py`
- Modify: `app/templates/htmx/partials/sightings/table.html:153,228`

- [ ] **Step 1: Write the failing test for sub badge rendering**

Add a new test class to `tests/test_sightings_router.py`:

```python
class TestSightingsSubsBadge:
    def test_table_shows_sub_count_badge(self, client, db_session):
        """Table row shows '+N subs' badge when requirement has substitutes."""
        req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="HAS-SUBS-001",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "SUB-A", "manufacturer": "M1"},
                {"mpn": "SUB-B", "manufacturer": "M2"},
            ],
            substitutes_text="SUB-A SUB-B",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(
            requirement_id=r.id, vendor_name="V1", listing_count=1, score=50.0,
        ))
        db_session.commit()

        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "+2 subs" in resp.text

    def test_table_no_badge_without_subs(self, client, db_session):
        """Table row does not show sub badge when no substitutes."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "+0 subs" not in resp.text
        assert "subs" not in resp.text or "Unsubscri" in resp.text  # no false positive
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsSubsBadge::test_table_shows_sub_count_badge -v --override-ini="addopts="`
Expected: FAIL — `+2 subs` not in response text.

- [ ] **Step 3: Add sub count badge to table.html render_row**

In `app/templates/htmx/partials/sightings/table.html`, find line 153 (the MPN cell in `render_row`):

```html
{# BEFORE: #}
        <td class="px-3 py-2 font-mono font-medium text-gray-900">{{ r.primary_mpn }}</td>

{# AFTER: #}
        <td class="px-3 py-2 font-mono font-medium text-gray-900">
          {{ r.primary_mpn }}
          {% set sub_count = r.substitutes|sub_mpns|length %}
          {% if sub_count > 0 %}
          <span class="ml-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-amber-50 text-amber-600 border border-amber-200">+{{ sub_count }} sub{{ 's' if sub_count != 1 }}</span>
          {% endif %}
        </td>
```

- [ ] **Step 4: Add sub count badge to table.html render_card (mobile)**

In the same file, find line ~228 (the MPN span in `render_card`):

```html
{# BEFORE: #}
        <span class="font-mono font-semibold text-sm text-gray-900">{{ r.primary_mpn }}</span>

{# AFTER: #}
        <span class="font-mono font-semibold text-sm text-gray-900">{{ r.primary_mpn }}</span>
          {% set sub_count = r.substitutes|sub_mpns|length %}
          {% if sub_count > 0 %}
          <span class="ml-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-amber-50 text-amber-600 border border-amber-200">+{{ sub_count }} sub{{ 's' if sub_count != 1 }}</span>
          {% endif %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsSubsBadge -v --override-ini="addopts="`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/sightings/table.html tests/test_sightings_router.py
git commit -m "feat(sightings): show substitute count badge in table rows"
```

---

### Task 3: Detail header — add sub pills

**Files:**
- Modify: `tests/test_sightings_router.py`
- Modify: `app/templates/htmx/partials/sightings/detail.html:17-18`

- [ ] **Step 1: Write the failing test for sub pills in detail**

Add to `tests/test_sightings_router.py`:

```python
class TestSightingsDetailSubs:
    def test_detail_shows_sub_pills(self, client, db_session):
        """Detail panel shows substitute MPN pills below primary MPN."""
        req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DETAIL-PRIMARY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "DETAIL-SUB-A", "manufacturer": "M1"},
                {"mpn": "DETAIL-SUB-B", "manufacturer": "M2"},
            ],
            substitutes_text="DETAIL-SUB-A DETAIL-SUB-B",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(
            requirement_id=r.id, vendor_name="V1", listing_count=1, score=50.0,
        ))
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "DETAIL-SUB-A" in resp.text
        assert "DETAIL-SUB-B" in resp.text

    def test_detail_no_pills_without_subs(self, client, db_session):
        """Detail panel has no sub pills when requirement has no substitutes."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        # Should not contain sub pill markup
        assert "bg-amber-50 text-amber-700 border border-amber-200" not in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsDetailSubs::test_detail_shows_sub_pills -v --override-ini="addopts="`
Expected: FAIL — `DETAIL-SUB-A` not in response text.

- [ ] **Step 3: Add sub pills to detail.html**

In `app/templates/htmx/partials/sightings/detail.html`, find the manufacturer line (around line 17-18):

```html
{# BEFORE: #}
      {% if requirement.manufacturer %}
      <p class="text-xs text-gray-500">{{ requirement.manufacturer }}</p>
      {% endif %}
    </div>

{# AFTER: #}
      {% if requirement.manufacturer %}
      <p class="text-xs text-gray-500">{{ requirement.manufacturer }}</p>
      {% endif %}
      {% set sub_mpns = requirement.substitutes|sub_mpns %}
      {% if sub_mpns %}
      <div class="flex flex-wrap gap-1 mt-1">
        {% for mpn in sub_mpns %}
        <span class="px-1.5 py-0.5 text-[10px] font-mono font-medium rounded bg-amber-50 text-amber-700 border border-amber-200">{{ mpn }}</span>
        {% endfor %}
      </div>
      {% endif %}
    </div>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsDetailSubs -v --override-ini="addopts="`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/sightings/detail.html tests/test_sightings_router.py
git commit -m "feat(sightings): show substitute MPN pills in detail header"
```

---

### Task 4: Vendor rows — query matched MPNs and show "via" tags

**Files:**
- Modify: `tests/test_sightings_router.py`
- Modify: `app/routers/sightings.py:513-548`
- Modify: `app/templates/htmx/partials/sightings/_vendor_row.html:39-44`

- [ ] **Step 1: Write the failing test for vendor matched MPN tags**

Add to `tests/test_sightings_router.py`:

```python
class TestSightingsVendorMatchedMpns:
    def test_vendor_row_shows_via_sub_tag(self, client, db_session):
        """Vendor row shows 'via SUB-MPN' when vendor sighting matched a substitute."""
        req = Requisition(name="Match RFQ", status="active", customer_name="MatchCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MATCH-PRIMARY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[{"mpn": "MATCH-SUB-X", "manufacturer": "M1"}],
            substitutes_text="MATCH-SUB-X",
        )
        db_session.add(r)
        db_session.flush()
        # Vendor sighting summary
        db_session.add(VendorSightingSummary(
            requirement_id=r.id, vendor_name="SubVendor", listing_count=1, score=60.0,
        ))
        # Raw sighting matched against a substitute MPN
        db_session.add(Sighting(
            requirement_id=r.id,
            vendor_name="SubVendor",
            mpn_matched="MATCH-SUB-X",
            qty_available=100,
        ))
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via MATCH-SUB-X" in resp.text

    def test_vendor_row_no_via_tag_for_primary(self, client, db_session):
        """Vendor row does NOT show 'via' tag when sighting matched the primary MPN."""
        req = Requisition(name="Primary RFQ", status="active", customer_name="PrimaryCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="PRI-ONLY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(
            requirement_id=r.id, vendor_name="PriVendor", listing_count=1, score=60.0,
        ))
        db_session.add(Sighting(
            requirement_id=r.id,
            vendor_name="PriVendor",
            mpn_matched="PRI-ONLY",
            qty_available=100,
        ))
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via PRI-ONLY" not in resp.text

    def test_vendor_row_shows_multiple_via_tags(self, client, db_session):
        """Vendor row shows multiple 'via' tags when vendor matched multiple subs."""
        req = Requisition(name="Multi RFQ", status="active", customer_name="MultiCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MULTI-PRI",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "MULTI-SUB-1", "manufacturer": "M1"},
                {"mpn": "MULTI-SUB-2", "manufacturer": "M2"},
            ],
            substitutes_text="MULTI-SUB-1 MULTI-SUB-2",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(
            requirement_id=r.id, vendor_name="MultiVendor", listing_count=2, score=70.0,
        ))
        db_session.add(Sighting(
            requirement_id=r.id, vendor_name="MultiVendor",
            mpn_matched="MULTI-SUB-1", qty_available=50,
        ))
        db_session.add(Sighting(
            requirement_id=r.id, vendor_name="MultiVendor",
            mpn_matched="MULTI-SUB-2", qty_available=75,
        ))
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via MULTI-SUB-1" in resp.text
        assert "via MULTI-SUB-2" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsVendorMatchedMpns::test_vendor_row_shows_via_sub_tag -v --override-ini="addopts="`
Expected: FAIL — `via MATCH-SUB-X` not in response text.

- [ ] **Step 3: Add matched MPN query to sightings_detail endpoint**

In `app/routers/sightings.py`, in the `sightings_detail` function, add the matched MPN query just before the `ctx` dict (around line 514). Find this block:

```python
    # BEFORE (around line 514-515):
    activities = (
        db.query(ActivityLog)
```

Insert above it:

```python
    # ── Vendor Matched MPNs (substitute visibility) ──────────────
    matched_rows = (
        db.query(Sighting.vendor_name, Sighting.mpn_matched)
        .filter(
            Sighting.requirement_id == requirement_id,
            Sighting.mpn_matched.isnot(None),
        )
        .distinct()
        .all()
    )
    vendor_matched_mpns: dict[str, list[str]] = {}
    for vendor_name, mpn in matched_rows:
        vendor_matched_mpns.setdefault(vendor_name, []).append(mpn)

```

Then add `vendor_matched_mpns` to the `ctx` dict (around line 530-547):

```python
    ctx = {
        ...
        "vendor_matched_mpns": vendor_matched_mpns,
        ...
    }
```

Add it after the `"overlap_counts": overlap_counts,` line.

- [ ] **Step 4: Add "via" tags to _vendor_row.html**

In `app/templates/htmx/partials/sightings/_vendor_row.html`, find the overlap badge section (around line 39-44):

```html
{# BEFORE — after the overlap badge, before </div> that closes the badges div: #}
          {% if overlap > 1 %}
          <span class="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-indigo-50 text-indigo-700">
            Also on {{ overlap - 1 }} other req{{ 's' if overlap - 1 != 1 }}
          </span>
          {% endif %}
        </div>

{# AFTER: #}
          {% if overlap > 1 %}
          <span class="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-indigo-50 text-indigo-700">
            Also on {{ overlap - 1 }} other req{{ 's' if overlap - 1 != 1 }}
          </span>
          {% endif %}
          {% set matched = vendor_matched_mpns.get(s.vendor_name, []) if vendor_matched_mpns is defined else [] %}
          {% set sub_matches = matched|reject("equalto", requirement.primary_mpn)|list %}
          {% for mpn in sub_matches %}
          <span class="inline-flex px-1 py-0.5 text-[9px] font-mono rounded bg-amber-50 text-amber-600 border border-amber-100">via {{ mpn }}</span>
          {% endfor %}
        </div>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsVendorMatchedMpns -v --override-ini="addopts="`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd /root/availai
git add app/routers/sightings.py app/templates/htmx/partials/sightings/_vendor_row.html tests/test_sightings_router.py
git commit -m "feat(sightings): show 'via SUB-MPN' tags on vendor rows"
```

---

### Task 5: Run full test suite and verify no regressions

**Files:**
- All modified files from Tasks 1-4

- [ ] **Step 1: Run all sightings tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --override-ini="addopts="`
Expected: ALL PASS

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: ALL PASS, no regressions

- [ ] **Step 3: Run linter**

Run: `cd /root/availai && ruff check app/routers/sightings.py`
Expected: No errors

- [ ] **Step 4: Final commit if any fixes needed**

Only commit if Step 1-3 revealed issues that required fixes.
