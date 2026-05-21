# RFQ Sightings Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the Sourcing tab to Sightings, add a derived vendor outreach status column, auto-search on requirement save, and remove manual search buttons.

**Architecture:** The status column is computed at query time in the route handler — no model changes. The route handler queries Contact, Offer, Sighting, and VendorCard tables to derive each vendor's status. Auto-search uses FastAPI BackgroundTasks to fire `search_requirement()` after saves.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2/HTMX, Tailwind CSS, pytest

**Spec:** `docs/superpowers/specs/2026-03-23-rfq-sightings-tab-design.md`

---

### Task 1: Rename Sourcing tab to Sightings

**Files:**
- Modify: `app/templates/htmx/partials/parts/workspace.html:85`

- [ ] **Step 1: Change tab label**

In `app/templates/htmx/partials/parts/workspace.html`, line 85, change `('sourcing', 'Sourcing')` to `('sourcing', 'Sightings')`:

```jinja2
{% for tab_key, tab_label in [('offers', 'Offers'), ('sourcing', 'Sightings'), ('notes', 'Sales Notes'), ('activity', 'Activity'), ('comms', 'Comms'), ('req-details', 'REQ Detail')] %}
```

The tab key stays `sourcing` so the route `/v2/partials/parts/{id}/tab/sourcing` doesn't need changing.

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/parts/workspace.html
git commit -m "feat: rename Sourcing tab to Sightings on RFQ workspace"
```

---

### Task 2: Add derived status column to sightings tab

**Files:**
- Modify: `app/routers/htmx_views.py:8963-8999` (route handler `part_tab_sourcing`)
- Modify: `app/templates/htmx/partials/parts/tabs/sourcing.html`
- Test: `tests/test_htmx_views_sourcing_tab_status.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_htmx_views_sourcing_tab_status.py`:

```python
"""Tests for derived vendor status in the sightings tab.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.offers import Contact, Offer
from app.models.vendors import VendorCard
from app.models.vendor_sighting_summary import VendorSightingSummary


def _make_requisition(db_session) -> Requisition:
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    return req


def _make_requirement(db_session, req: Requisition) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN-001",
        manufacturer="TestMfr",
    )
    db_session.add(r)
    db_session.flush()
    return r


def _make_summary(db_session, req_id: int, vendor: str, qty: int = 100) -> VendorSightingSummary:
    s = VendorSightingSummary(
        requirement_id=req_id,
        vendor_name=vendor,
        estimated_qty=qty,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.flush()
    return s


class TestDeriveVendorStatus:
    """Test the compute_vendor_statuses helper function."""

    def test_default_status_is_sighting(self, db_session):
        """Vendor with no buyer action should show 'sighting' status."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_contacted_status(self, db_session):
        """Vendor with a Contact record should show 'contacted'."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        contact = Contact(
            requisition_id=req.id,
            user_id=1,
            contact_type="email",
            vendor_name="Acme Corp",
            parts_included=["TEST-MPN-001"],
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "contacted"

    def test_offer_in_status(self, db_session):
        """Vendor with an Offer record should show 'offer-in'."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"

    def test_unavailable_status(self, db_session):
        """Vendor with all sightings marked unavailable should show 'unavailable'."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        sighting = Sighting(
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn_matched="TEST-MPN-001",
            is_unavailable=True,
        )
        db_session.add(sighting)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_blacklisted_overrides_all(self, db_session):
        """Blacklisted vendor should show 'blacklisted' even with offers."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Bad Vendor")

        # Create vendor card marked as blacklisted
        vc = VendorCard(
            normalized_name="bad vendor",
            display_name="Bad Vendor",
            is_blacklisted=True,
        )
        db_session.add(vc)

        # Also create an offer (should still show blacklisted)
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Bad Vendor",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Bad Vendor"] == "blacklisted"

    def test_offer_in_overrides_contacted(self, db_session):
        """Offer-in should take priority over contacted."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(Contact(
            requisition_id=req.id, user_id=1, contact_type="email",
            vendor_name="Acme Corp", parts_included=["TEST-MPN-001"], status="sent",
        ))
        db_session.add(Offer(
            requisition_id=req.id, requirement_id=r.id,
            vendor_name="Acme Corp", mpn="TEST-MPN-001",
        ))
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views_sourcing_tab_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.sighting_status'`

- [ ] **Step 3: Implement `compute_vendor_statuses` service**

Create `app/services/sighting_status.py`:

```python
"""Derive vendor outreach status for the sightings tab.

Computes a status per vendor for a given requirement by checking:
- VendorCard.is_blacklisted → "blacklisted"
- Offer exists for requirement + vendor → "offer-in"
- Contact sent to vendor for requisition → "contacted"
- All sightings marked is_unavailable → "unavailable"
- Default → "sighting"

Called by: htmx_views.part_tab_sourcing
Depends on: models (VendorCard, Offer, Contact, Sighting, VendorSightingSummary)
"""

from sqlalchemy.orm import Session

from ..models.offers import Contact, Offer
from ..models.sourcing import Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard


def compute_vendor_statuses(
    requirement_id: int,
    requisition_id: int,
    db: Session,
) -> dict[str, str]:
    """Return {vendor_name: status} for all vendors with sightings on this requirement.

    Priority order: blacklisted > offer-in > contacted > unavailable > sighting.
    """
    # Get all vendor names from summaries
    summaries = (
        db.query(VendorSightingSummary.vendor_name)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .all()
    )
    vendor_names = [s.vendor_name for s in summaries]
    if not vendor_names:
        return {}

    # Blacklisted vendors — match by normalized name
    blacklisted_names: set[str] = set()
    blacklisted_cards = (
        db.query(VendorCard.normalized_name)
        .filter(VendorCard.is_blacklisted.is_(True))
        .all()
    )
    bl_normalized = {c.normalized_name for c in blacklisted_cards}
    for vn in vendor_names:
        if vn.strip().lower() in bl_normalized:
            blacklisted_names.add(vn)

    # Vendors with offers for this requirement
    offer_vendors: set[str] = set()
    offers = (
        db.query(Offer.vendor_name)
        .filter(Offer.requirement_id == requirement_id)
        .all()
    )
    offer_vendors = {o.vendor_name for o in offers}

    # Vendors contacted for this requisition
    contacted_vendors: set[str] = set()
    contacts = (
        db.query(Contact.vendor_name)
        .filter(
            Contact.requisition_id == requisition_id,
            Contact.status.in_(["sent", "delivered", "opened"]),
        )
        .all()
    )
    contacted_vendors = {c.vendor_name for c in contacts}

    # Vendors with all sightings unavailable
    unavailable_vendors: set[str] = set()
    sightings = (
        db.query(Sighting.vendor_name, Sighting.is_unavailable)
        .filter(Sighting.requirement_id == requirement_id)
        .all()
    )
    vendor_avail: dict[str, list[bool]] = {}
    for s in sightings:
        vendor_avail.setdefault(s.vendor_name, []).append(bool(s.is_unavailable))
    for vn, flags in vendor_avail.items():
        if flags and all(flags):
            unavailable_vendors.add(vn)

    # Derive status per vendor (priority order)
    statuses: dict[str, str] = {}
    for vn in vendor_names:
        if vn in blacklisted_names:
            statuses[vn] = "blacklisted"
        elif vn in offer_vendors:
            statuses[vn] = "offer-in"
        elif vn in contacted_vendors:
            statuses[vn] = "contacted"
        elif vn in unavailable_vendors:
            statuses[vn] = "unavailable"
        else:
            statuses[vn] = "sighting"

    return statuses
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views_sourcing_tab_status.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Wire status into route handler**

In `app/routers/htmx_views.py`, modify `part_tab_sourcing` (line ~8963). After building `raw_by_vendor`, add:

```python
    # Derive vendor outreach statuses
    from ..services.sighting_status import compute_vendor_statuses

    vendor_statuses = compute_vendor_statuses(requirement_id, req.requisition_id, db)
```

And add `"vendor_statuses": vendor_statuses` to the `ctx.update()` dict.

- [ ] **Step 6: Update template with status column**

In `app/templates/htmx/partials/parts/tabs/sourcing.html`:

1. Remove the "Run Search" button and its wrapper div (lines 7-14). Replace with just the vendor count:

```jinja2
<div class="mb-2">
  <p class="text-xs text-gray-500">{{ summaries|length }} vendor{{ 's' if summaries|length != 1 else '' }}</p>
</div>
```

4. Update the empty state text (lines 128-131) to reflect auto-search:

```jinja2
<div class="p-8 text-center text-gray-400 border border-dashed border-gray-200 rounded-lg">
  <p class="text-sm">No sightings yet</p>
  <p class="text-xs mt-1">Sightings will appear automatically after the requirement is saved</p>
</div>
```

2. Add a Status header after the Vendor column in `<thead>`:

```html
<th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
```

3. Add a Status cell after the vendor name cell in each row:

```jinja2
<td class="px-3 py-2 whitespace-nowrap">
  {% set status = vendor_statuses.get(s.vendor_name, 'sighting') %}
  {% set status_styles = {
    'sighting': 'bg-gray-100 text-gray-600',
    'contacted': 'bg-blue-50 text-blue-700',
    'offer-in': 'bg-emerald-50 text-emerald-700',
    'unavailable': 'bg-gray-100 text-gray-500',
    'blacklisted': 'bg-red-50 text-red-700',
  } %}
  {% set status_labels = {
    'sighting': 'Sighting',
    'contacted': 'Contacted',
    'offer-in': 'Offer In',
    'unavailable': 'Unavailable',
    'blacklisted': 'Blacklisted',
  } %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ status_styles.get(status, 'bg-gray-100 text-gray-600') }}">
    {{ status_labels.get(status, status|capitalize) }}
  </span>
</td>
```

- [ ] **Step 7: Commit**

```bash
git add app/services/sighting_status.py app/routers/htmx_views.py app/templates/htmx/partials/parts/tabs/sourcing.html tests/test_htmx_views_sourcing_tab_status.py
git commit -m "feat: add derived vendor status column to sightings tab"
```

---

### Task 3: Auto-search on requirement save

**Files:**
- Modify: `app/routers/htmx_views.py:1094-1168` (HTMX add_requirement handler)
- Modify: `app/routers/htmx_views.py:2826-2905` (HTMX update_requirement handler)
- Modify: `app/routers/requisitions/requirements.py:352-469` (API add_requirements handler)

- [ ] **Step 1: Add BackgroundTasks param to HTMX add_requirement**

In `app/routers/htmx_views.py`, function `add_requirement` (line 1095), add `background_tasks: BackgroundTasks` parameter:

```python
async def add_requirement(
    request: Request,
    req_id: int,
    background_tasks: BackgroundTasks,
    primary_mpn: str = Form(...),
    # ... rest unchanged
```

After `db.refresh(r)` (line 1161), add:

```python
    # Auto-search: fire background search for the new requirement
    def _bg_search(requirement_id: int):
        import asyncio
        from ..database import SessionLocal
        from ..search_service import search_requirement as do_search

        bg_db = SessionLocal()
        try:
            req_obj = bg_db.get(Requirement, requirement_id)
            if req_obj:
                asyncio.run(do_search(req_obj, bg_db))
        except Exception:
            logger.debug("Auto-search failed for requirement %s", requirement_id, exc_info=True)
        finally:
            bg_db.close()

    background_tasks.add_task(_bg_search, r.id)
```

Add import at the top of file if not present: `from fastapi import BackgroundTasks`

- [ ] **Step 2: Add BackgroundTasks to HTMX update_requirement**

In `app/routers/htmx_views.py`, function `update_requirement` (line 2827), add same `background_tasks: BackgroundTasks` param and same `_bg_search` block after `db.refresh(item)` (line 2901):

```python
    # Auto-search: re-search after edit
    def _bg_search(requirement_id: int):
        import asyncio
        from ..database import SessionLocal
        from ..search_service import search_requirement as do_search

        bg_db = SessionLocal()
        try:
            req_obj = bg_db.get(Requirement, requirement_id)
            if req_obj:
                asyncio.run(do_search(req_obj, bg_db))
        except Exception:
            logger.debug("Auto-search failed for requirement %s", requirement_id, exc_info=True)
        finally:
            bg_db.close()

    background_tasks.add_task(_bg_search, item.id)
```

- [ ] **Step 3: Verify API handler already has auto-search**

Check `app/routers/requisitions/requirements.py` `add_requirements` (line 352). It already enqueues NC and ICS workers in background tasks (lines 467-469). The `search_requirement` function used elsewhere is a more comprehensive search that includes all connectors. Add it here too:

After the NC/ICS enqueue block (line 469), add:

```python
    # Auto-search: run full connector search in background
    def _bg_full_search(requirement_ids: list[int]):
        import asyncio
        from ...database import SessionLocal
        from ...search_service import search_requirement as do_search

        bg_db = SessionLocal()
        try:
            for rid in requirement_ids:
                req_obj = bg_db.get(Requirement, rid)
                if req_obj:
                    try:
                        asyncio.run(do_search(req_obj, bg_db))
                    except Exception:
                        logger.debug("Auto-search failed for requirement %s", rid, exc_info=True)
        finally:
            bg_db.close()

    if created:
        background_tasks.add_task(_bg_full_search, [r.id for r in created])
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py app/routers/requisitions/requirements.py
git commit -m "feat: auto-search requirements on save (add/edit)"
```

---

### Task 4: Remove search buttons from req_row

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/req_row.html`

- [ ] **Step 1: Remove Search button from actions column**

In `app/templates/htmx/partials/requisitions/tabs/req_row.html`, replace the actions `<td>` (lines 67-96) with just the delete button:

```jinja2
  <td class="px-4 py-2.5 text-center" x-show="!editing">
    <div class="flex items-center justify-center">
      <button hx-delete="/v2/partials/requisitions/{{ req.id }}/requirements/{{ r.id }}"
              hx-target="#req-row-{{ r.id }}"
              hx-swap="delete"
              hx-confirm="Delete this requirement?"
              class="inline-flex items-center px-1.5 py-1 text-xs text-rose-400 hover:text-rose-600 hover:bg-rose-50 rounded transition-colors opacity-0 group-hover:opacity-100"
              @click.stop>
        <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
        </svg>
      </button>
    </div>
  </td>
```

- [ ] **Step 2: Remove hidden search-results row**

Delete lines 199-202 (the hidden `search-results-{r.id}` `<tr>`):

```html
{# Search results appear here — hidden until populated by hx-target #}
<tr id="search-results-{{ r.id }}" class="bg-gray-50" style="display:none">
  <td colspan="16" class="p-0"></td>
</tr>
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/requisitions/tabs/req_row.html
git commit -m "feat: remove manual search button from requirement rows"
```

---

### Task 5: Run full test suite and verify

- [ ] **Step 1: Run related tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views_sourcing_tab_status.py tests/test_htmx_views.py -v --timeout=60
```

Expected: All pass

- [ ] **Step 2: Run full suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=120
```

Expected: No regressions

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: address test failures from sightings tab changes"
```
