# Sightings Offers Tab (Track A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) —
> this feature touches shared files (`sightings/detail.html`, `routers/sightings.py`)
> so it executes best in one session with TDD + checkpoints. Steps use `- [ ]`.

**Goal:** Add a part-centric Offers tab to the sightings detail pane, with
Convert-to-offer on each vendor row and a generic Enter-offer, reusing the existing
`Offer` model and the canonical `create_offer`/mutation functions.

**Architecture:** New thin HTMX endpoints on the (prefix-less) sightings router call
the canonical offer functions in `app/routers/crm/offers.py` directly (zero logic
duplication) and re-render an offers panel partial swapped into
`#sightings-offers-panel`. Offers are matched by part (MaterialCard id ∪ both
normalized-MPN forms) across primary + substitute MPNs, regardless of requirement.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind.

**Spec:** `docs/superpowers/specs/2026-06-05-sightings-offers-tab-design.md`

**Test command (single module, no xdist):**
`TESTING=1 PYTHONPATH=/root/availai-worktrees/sightings-offers pytest tests/test_sightings_offers.py -v --override-ini="addopts="`

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `app/services/part_offers.py` | Create | `part_offers_for(requirement, db)` — part-centric offer query helper |
| `app/routers/htmx_views.py` | Modify | Fix `add_offer` to write `normalize_mpn_key` (root-cause) |
| `app/routers/sightings.py` | Modify | `sightings_detail` ctx gains `part_offers`; new offer endpoints + `_render_offers_panel` helper |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Offers tab + panel; move pending block out of Vendors |
| `app/templates/htmx/partials/sightings/offers_panel.html` | Create | Part offers list + Enter-offer button + empty state |
| `app/templates/htmx/partials/sightings/_offer_row.html` | Create | Compact offer row + kebab actions |
| `app/templates/htmx/partials/sightings/_vendor_row.html` | Modify | Convert-to-offer button in line-2 action group |
| `app/templates/htmx/partials/offers/_offer_form_fields.html` | Create | Shared field grid (vendor…notes) |
| `app/templates/htmx/partials/sightings/offer_form_modal.html` | Create | Modal wrapper around the field grid (create + edit) |
| `app/templates/htmx/partials/requisitions/add_offer_form.html` | Modify | Include the shared field grid (kill the duplicate fields) |
| `tests/test_sightings_offers.py` | Create | All Track-A tests |
| `docs/APP_MAP_INTERACTIONS.md` | Modify | Document the new sightings offers endpoints + part-centric tab |

---

## Task 1: Part-offers query helper

**Files:** Create `app/services/part_offers.py`; Test `tests/test_sightings_offers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sightings_offers.py
"""Track-A: part-centric Offers tab on the sightings detail.

Called by: pytest. Depends on: conftest fixtures, Offer/Requirement/Requisition models.
"""
from app.constants import ActivityType, OfferStatus
from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.services.part_offers import part_offers_for


def _req(db, mpn="LM317T", subs=None, customer="Acme Corp"):
    rq = Requisition(name="RFQ", status="active", customer_name=customer)
    db.add(rq); db.flush()
    r = Requirement(requisition_id=rq.id, primary_mpn=mpn, manufacturer="TI",
                    target_qty=100, sourcing_status="open",
                    substitutes=subs or [])
    db.add(r); db.flush(); db.commit()
    return rq, r


def _offer(db, rq, r, vendor, mpn, normalized, status=OfferStatus.ACTIVE, price=1.0):
    o = Offer(requisition_id=rq.id, requirement_id=r.id, vendor_name=vendor,
              mpn=mpn, normalized_mpn=normalized, status=status, unit_price=price)
    db.add(o); db.commit()
    return o


def test_part_offers_includes_cross_req_and_substitute(db_session):
    rq1, r1 = _req(db_session, mpn="LM317T",
                   subs=[{"mpn": "LM317-ALT", "manufacturer": "ON"}])
    # offer for the same part on a DIFFERENT requisition (key form, no dashes)
    rq2, r2 = _req(db_session, mpn="LM317T", customer="Beta Inc")
    _offer(db_session, rq2, r2, "Mouser", "LM317T", "lm317t")
    # offer entered against a SUBSTITUTE mpn (display form, with dash)
    _offer(db_session, rq1, r1, "Arrow", "LM317-ALT", "LM317-ALT")
    # unrelated offer must NOT appear
    rq3, r3 = _req(db_session, mpn="NE555")
    _offer(db_session, rq3, r3, "Digi", "NE555", "ne555")

    offers = part_offers_for(r1, db_session)
    vendors = {o.vendor_name for o in offers}
    assert vendors == {"Mouser", "Arrow"}
```

- [ ] **Step 2: Run, expect ImportError/fail**

Run the test command above (`::test_part_offers_includes_cross_req_and_substitute`).
Expected: FAIL (`ModuleNotFoundError: app.services.part_offers`).

- [ ] **Step 3: Implement the helper**

```python
# app/services/part_offers.py
"""Part-centric offer lookup for the sightings Offers tab.

Returns every Offer for a requirement's part number — primary MPN plus
substitutes — regardless of which requirement/requisition it was entered against.

Called by: app/routers/sightings.py (detail view + offers panel re-render).
Depends on: Offer / Requirement / MaterialCard models, MPN normalization.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement
from app.utils.normalization import normalize_mpn, normalize_mpn_key, parse_substitute_mpns


def _part_mpns(requirement: Requirement) -> list[str]:
    """Primary MPN + substitute MPNs as display strings (deduped)."""
    subs = parse_substitute_mpns(requirement.substitutes or [], requirement.primary_mpn)
    mpns = [requirement.primary_mpn] + [s["mpn"] for s in subs]
    return [m for m in mpns if m]


def part_offers_for(requirement: Requirement, db: Session) -> list[Offer]:
    """All offers for the requirement's part (primary + substitutes), newest first.

    Matches on MaterialCard id OR normalized_mpn in BOTH normalization forms, because
    the two offer-creation paths historically wrote normalized_mpn differently.
    """
    mpns = _part_mpns(requirement)
    if not mpns:
        return []

    norm_keys: set[str] = set()
    for m in mpns:
        norm_keys.add(normalize_mpn_key(m))
        disp = normalize_mpn(m)
        if disp:
            norm_keys.add(disp)
    norm_keys.discard("")

    card_ids = {
        cid for (cid,) in db.query(MaterialCard.id)
        .filter(MaterialCard.normalized_mpn.in_({normalize_mpn_key(m) for m in mpns}))
        .all()
    }

    conds = [Offer.normalized_mpn.in_(norm_keys)]
    if card_ids:
        conds.append(Offer.material_card_id.in_(card_ids))

    return (
        db.query(Offer)
        .options(joinedload(Offer.requisition))
        .filter(or_(*conds))
        .order_by(Offer.created_at.desc())
        .all()
    )
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git add app/services/part_offers.py tests/test_sightings_offers.py && git commit -m "feat(sightings): part-centric offer query helper"`

---

## Task 2: Root-cause fix — `add_offer` normalized_mpn

**Files:** Modify `app/routers/htmx_views.py` (the `add_offer` POST handler ~line 2017-2084).

- [ ] **Step 1: Add the failing test**

```python
def test_add_offer_writes_dedup_key_normalized_mpn(client, db_session):
    """Requisitions manual add-offer must store the dash-stripped dedup key so the
    part-centric query matches it consistently with create_offer."""
    rq, r = _req(db_session, mpn="LM2596S-5.0")
    resp = client.post(
        f"/v2/partials/requisitions/{rq.id}/add-offer",
        data={"vendor_name": "Arrow", "mpn": "LM2596S-5.0", "requirement_id": r.id},
    )
    assert resp.status_code == 200
    o = db_session.query(Offer).filter(Offer.vendor_name == "Arrow").one()
    assert o.normalized_mpn == "lm2596s50"
```

- [ ] **Step 2: Run, expect FAIL** (currently writes display form `LM2596S-5.0`).

- [ ] **Step 3: Fix the handler** — in `add_offer`, change the normalized_mpn write
from `normalize_mpn(mpn)` to `normalize_mpn_key(mpn)`, and ensure
`from ..utils.normalization import normalize_mpn_key` is imported at top of
`htmx_views.py` (verify; add if absent). Exact edit:

```python
# was:  normalized_mpn=normalize_mpn(mpn),
normalized_mpn=normalize_mpn_key(mpn),
```

- [ ] **Step 4: Run, expect PASS.** Also run existing requisitions offer tests:
`pytest tests/ -k "add_offer or offers" --override-ini="addopts=" -q` — expect green.
- [ ] **Step 5: Commit** — `git commit -am "fix(offers): add_offer writes canonical normalize_mpn_key"`

---

## Task 3: Offers tab + panel + pending-block move

**Files:** Modify `app/routers/sightings.py` (`sightings_detail` ctx + a
`_render_offers_panel` helper); Modify `sightings/detail.html`; Create
`sightings/offers_panel.html`, `sightings/_offer_row.html`.

- [ ] **Step 1: Failing tests**

```python
def test_offers_tab_lists_part_offers_with_source_hint(client, db_session):
    rq1, r1 = _req(db_session, mpn="LM317T", customer="Acme Corp")
    rq2, r2 = _req(db_session, mpn="LM317T", customer="Beta Inc")
    _offer(db_session, rq2, r2, "Mouser", "LM317T", "lm317t", price=0.51)
    body = client.get(f"/v2/partials/sightings/{r1.id}/detail").text
    # Offers tab button present
    assert "activeTab = 'offers'" in body
    # cross-req offer + its source hint render
    assert "Mouser" in body
    assert "Beta Inc" in body

def test_pending_offer_in_offers_panel_not_vendors(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    _offer(db_session, rq, r, "PendVend", "LM317T", "lm317t",
           status=OfferStatus.PENDING_REVIEW)
    body = client.get(f"/v2/partials/sightings/{r.id}/detail").text
    panel = body.split('id="sightings-offers-panel"', 1)[1]
    assert "PendVend" in panel               # pending offer lives in offers panel
    assert "Approve" in panel and "Reject" in panel
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3a:** In `sightings.py`, add helper + context. Near the other helpers add:

```python
def _render_offers_panel(request, requirement, db):
    from ..services.part_offers import part_offers_for
    ctx = {
        "request": request,
        "requirement": requirement,
        "requisition": db.get(Requisition, requirement.requisition_id),
        "part_offers": part_offers_for(requirement, db),
    }
    resp = template_response("htmx/partials/sightings/offers_panel.html", ctx)
    resp.headers["X-Rendered-Req-Id"] = str(requirement.id)
    return resp
```

In `sightings_detail`, before building `ctx`, add:
`from ..services.part_offers import part_offers_for` and
`part_offers = part_offers_for(requirement, db)`; add `"part_offers": part_offers`
to the ctx dict.

- [ ] **Step 3b:** `sightings/offers_panel.html` (new):

```jinja
{# offers_panel.html — part-centric offers list for the sightings Offers tab.
   Receives: requirement, requisition, part_offers (list[Offer]).
   Called by: detail.html include + sightings offer endpoints (#sightings-offers-panel). #}
<div class="mb-3 flex items-center justify-between">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider">
    All offers for {{ requirement.primary_mpn }} · {{ part_offers|length }}
  </h4>
  <button @click.stop="$dispatch('open-modal', {url: '/v2/partials/sightings/{{ requirement.id }}/offer-form'})"
          class="px-2.5 py-1 text-[11px] font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600">
    + Enter Offer
  </button>
</div>
{% if not part_offers %}
<p class="text-xs text-gray-400 py-6 text-center border border-dashed border-gray-200 rounded-lg">
  No offers yet for this part — convert a vendor or enter one manually.
</p>
{% else %}
<div class="divide-y divide-gray-100 border border-gray-100 rounded-lg">
  {% for o in part_offers %}
    {% include "htmx/partials/sightings/_offer_row.html" %}
  {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 3c:** `sightings/_offer_row.html` (new) — compact row. Context: `o`
(Offer), `requirement`. Status pill colors mirror `requisitions/tabs/offers.html`.
Kebab actions hx-post to the sightings offer endpoints (Task 7) with
`hx-target="#sightings-offers-panel" hx-swap="innerHTML"`. Pending offers show
Approve/Reject; active offers show Mark Sold/Reconfirm; all show Edit/Delete.

```jinja
{# _offer_row.html — one compact offer row for the sightings Offers panel.
   Context: o (Offer), requirement. #}
{% set sc = {'active':'bg-emerald-50 text-emerald-700','pending_review':'bg-amber-50 text-amber-700',
   'approved':'bg-emerald-50 text-emerald-700','rejected':'bg-rose-50 text-rose-700',
   'sold':'bg-gray-100 text-gray-600','won':'bg-emerald-50 text-emerald-700'} %}
<div id="offer-row-{{ o.id }}" class="group px-2 py-1.5 hover:bg-gray-50/50" x-data="{ open: false }">
  <div class="flex items-center gap-2">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2">
        <span class="font-medium text-gray-900 text-sm truncate">{{ o.vendor_name or '—' }}</span>
        <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded-full {{ sc.get(o.status,'bg-gray-100 text-gray-600') }}">
          {{ (o.status or 'unknown')|replace('_',' ')|capitalize }}</span>
      </div>
      <div class="text-[11px] text-gray-400 mt-0.5">
        {{ '$%.4f'|format(o.unit_price) if o.unit_price is not none else 'RFQ' }}
        · {{ '{:,}'.format(o.qty_available) if o.qty_available else '—' }} pcs
        {% if o.lead_time %}· {{ o.lead_time }}{% endif %}
      </div>
      <div class="text-[10px] text-gray-300 mt-0.5">
        ↳ {{ o.requisition.customer_name or '—' if o.requisition else '—' }}
        {% if o.requisition_id %}· Req #{{ o.requisition_id }}{% endif %}
      </div>
    </div>
    <div class="relative shrink-0" @click.outside="open = false">
      <button @click.stop="open = !open" class="p-1 text-gray-300 hover:text-gray-600 opacity-0 group-hover:opacity-100">
        <svg class="h-4 w-4" fill="currentColor" viewBox="0 0 20 20"><path d="M10 6a2 2 0 110-4 2 2 0 010 4zm0 6a2 2 0 110-4 2 2 0 010 4zm0 6a2 2 0 110-4 2 2 0 010 4z"/></svg>
      </button>
      <div x-show="open" x-cloak class="absolute right-0 top-full mt-1 w-36 bg-white border border-gray-200 rounded-lg shadow-lg z-10 py-1 text-xs">
        <button hx-get="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}/edit-form"
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 hover:bg-gray-50">Edit</button>
        {% if o.status == 'pending_review' %}
        <button hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}/review" hx-vals='{"action":"approve"}'
                hx-target="#sightings-offers-panel" hx-swap="innerHTML" data-loading-disable
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 text-emerald-600 hover:bg-emerald-50">Approve</button>
        <button hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}/review" hx-vals='{"action":"reject"}'
                hx-target="#sightings-offers-panel" hx-swap="innerHTML" data-loading-disable
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 text-rose-600 hover:bg-rose-50">Reject</button>
        {% elif o.status in ('active','approved') %}
        <button hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}/reconfirm"
                hx-target="#sightings-offers-panel" hx-swap="innerHTML" data-loading-disable
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 text-brand-600 hover:bg-brand-50">Reconfirm</button>
        <button hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}/mark-sold"
                hx-target="#sightings-offers-panel" hx-swap="innerHTML" hx-confirm="Mark this offer as sold?" data-loading-disable
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 hover:bg-gray-50">Mark Sold</button>
        {% endif %}
        <button hx-delete="/v2/partials/sightings/{{ requirement.id }}/offers/{{ o.id }}"
                hx-target="#sightings-offers-panel" hx-swap="innerHTML" hx-confirm="Delete this offer?" data-loading-disable
                @click.stop="open=false" class="w-full text-left px-3 py-1.5 text-rose-600 hover:bg-rose-50 border-t border-gray-100">Delete</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 3d:** `sightings/detail.html` — change the tab list to include Offers,
add the offers panel, and **remove** the Pending Review block (lines ~146-177) from
the Vendors panel (it is replaced by the offers panel showing pending offers). Tab
loop becomes:
`[('vendors','Vendors'),('offers','Offers'),('activity','Activity')]`.
Add after the vendors panel:

```jinja
  {# ── Offers panel (part-centric) ─────────────────────────── #}
  <div x-show="activeTab === 'offers'" x-cloak>
    <div id="sightings-offers-panel">
      {% include "htmx/partials/sightings/offers_panel.html" %}
    </div>
  </div>
```

- [ ] **Step 4: Run the Task-3 tests + existing `test_renders_tab_structure`.** Update
`test_renders_tab_structure` in `tests/test_sightings_router.py` to also assert
`"activeTab = 'offers'"` and the Offers label. Expect PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(sightings): part-centric Offers tab + move pending block"`

---

## Task 4: Shared offer-form field grid + modal wrapper

**Files:** Create `offers/_offer_form_fields.html`; Modify
`requisitions/add_offer_form.html` to include it; Create
`sightings/offer_form_modal.html`.

- [ ] **Step 1:** Extract the field grid (vendor, mpn, qty, unit_price, manufacturer,
lead_time, date_code, condition, moq, spq, packaging, firmware, hardware_code,
warranty, country_of_origin, valid_until, notes) verbatim from
`requisitions/add_offer_form.html` into `offers/_offer_form_fields.html` as a
context-light include (accepts optional `prefill` dict + `mpn_default`). Each input
sets `value="{{ prefill.get('<field>','') if prefill else '' }}"`.
- [ ] **Step 2:** Replace those rows in `requisitions/add_offer_form.html` with
`{% include "htmx/partials/offers/_offer_form_fields.html" %}` (no prefill). Run the
existing requisitions add-offer tests — expect still green (no behavior change).
- [ ] **Step 3:** Create `sightings/offer_form_modal.html`: modal-content body (the
base modal already renders the ✕). Title = "Convert to Offer — {{ prefill.vendor }}"
when `prefill` else "Enter Offer". Form:

```jinja
{# offer_form_modal.html — offer entry/edit form loaded into #modal-content.
   Receives: requirement, prefill (dict|None), offer (Offer|None for edit). #}
<div class="p-4">
  <h3 class="text-sm font-semibold text-gray-900 mb-3">
    {% if offer %}Edit Offer{% elif prefill and prefill.get('vendor_name') %}Convert to Offer — {{ prefill.vendor_name }}{% else %}Enter Offer{% endif %}
  </h3>
  <form {% if offer %}hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ offer.id }}"
        {% else %}hx-post="/v2/partials/sightings/{{ requirement.id }}/offers"{% endif %}
        hx-target="#sightings-offers-panel" hx-swap="innerHTML"
        hx-on::after-request="if(event.detail.successful) $dispatch('close-modal')"
        data-loading-disable class="space-y-3">
    {% set mpn_default = requirement.primary_mpn %}
    {% include "htmx/partials/offers/_offer_form_fields.html" %}
    <div class="flex justify-end gap-2">
      <button type="button" @click="$dispatch('close-modal')" class="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800">Cancel</button>
      <button type="submit" class="px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded hover:bg-brand-600">Save Offer</button>
    </div>
  </form>
</div>
```

(For edit, `prefill` is built from the `offer`'s fields so inputs pre-populate.)

- [ ] **Step 4:** No new test here (covered by Tasks 5-7 integration). Commit —
`git commit -am "refactor(offers): shared offer-form field grid + sightings modal"`

---

## Task 5: Convert button + GET offer-form endpoint

**Files:** Modify `sightings/_vendor_row.html`; Modify `sightings.py` (GET offer-form).

- [ ] **Step 1: Failing tests**

```python
def test_convert_button_on_vendor_row_collapsed(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    db_session.add(VendorSightingSummary(requirement_id=r.id, vendor_name="Arrow",
                   listing_count=1, score=70.0, best_price=0.45, estimated_qty=5000))
    db_session.commit()
    body = client.get(f"/v2/partials/sightings/{r.id}/detail").text
    assert "Convert to offer" in body
    # sits on the collapsed row, before the expandable block
    assert body.index("Convert to offer") < body.index('x-show="expanded"')

def test_offer_form_prefill_from_vendor(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    body = client.get(
        f"/v2/partials/sightings/{r.id}/offer-form?vendor_name=Arrow&unit_price=0.45&qty=5000"
    ).text
    assert 'value="Arrow"' in body
    assert 'value="0.45"' in body
    assert "Convert to Offer" in body

def test_offer_form_blank_enter(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    body = client.get(f"/v2/partials/sightings/{r.id}/offer-form").text
    assert "Enter Offer" in body
    assert 'value="LM317T"' in body   # mpn prefilled to the part
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3a:** Add to `_vendor_row.html` line-2 action group (inside the existing
`{% if vs != 'blacklisted' and vs != 'unavailable' %}` span, after Mark Unavail):

```jinja
<button @click.stop="$dispatch('open-modal', {url: '/v2/partials/sightings/{{ requirement.id }}/offer-form?vendor_name={{ s.vendor_name|urlencode }}&unit_price={{ s.best_price or '' }}&qty={{ s.estimated_qty or '' }}&moq={{ s.min_moq or '' }}&lead_days={{ s.best_lead_time_days or '' }}'})"
        class="text-[10px] text-emerald-600 hover:text-emerald-800 font-medium">
  Convert to offer
</button>
```

- [ ] **Step 3b:** Add the GET endpoint to `sightings.py`:

```python
@router.get("/v2/partials/sightings/{requirement_id}/offer-form", response_class=HTMLResponse)
async def sightings_offer_form(
    request: Request,
    requirement_id: int,
    vendor_name: str = Query(""),
    unit_price: str = Query(""),
    qty: str = Query(""),
    moq: str = Query(""),
    lead_days: str = Query(""),
    manufacturer: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    prefill = None
    if vendor_name:
        prefill = {
            "vendor_name": vendor_name,
            "mpn": requirement.primary_mpn,
            "manufacturer": manufacturer or requirement.manufacturer or "",
            "unit_price": unit_price,
            "qty_available": qty,
            "moq": moq,
            "lead_time": f"{lead_days} days" if lead_days else "",
        }
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": None}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)
```

- [ ] **Step 4: Run, expect PASS** (incl. the Track-0 collapsed-row assertion holds).
- [ ] **Step 5: Commit** — `git commit -am "feat(sightings): Convert-to-offer button + offer-form modal endpoint"`

---

## Task 6: POST create offer (reuses create_offer)

**Files:** Modify `sightings.py`.

- [ ] **Step 1: Failing test**

```python
def test_create_offer_appears_in_panel_and_logs_activity(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={"vendor_name": "Arrow", "mpn": "LM317T", "qty_available": "5000",
              "unit_price": "0.45", "condition": "new"},
    )
    assert resp.status_code == 200
    assert "Arrow" in resp.text                       # panel re-rendered with new offer
    o = db_session.query(Offer).filter(Offer.vendor_name.ilike("%arrow%")).one()
    assert o.requirement_id == r.id
    assert db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == ActivityType.OFFER_CREATED,
        ActivityLog.requirement_id == r.id).count() == 1
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3:** Add the POST endpoint to `sightings.py`. It builds an `OfferCreate`
from form fields and calls the canonical `create_offer`:

```python
@router.post("/v2/partials/sightings/{requirement_id}/offers", response_class=HTMLResponse)
async def sightings_create_offer(
    request: Request,
    requirement_id: int,
    vendor_name: str = Form(...),
    mpn: str = Form(...),
    manufacturer: str = Form(""),
    qty_available: str = Form(""),
    unit_price: str = Form(""),
    lead_time: str = Form(""),
    date_code: str = Form(""),
    condition: str = Form("new"),
    packaging: str = Form(""),
    firmware: str = Form(""),
    hardware_code: str = Form(""),
    moq: str = Form(""),
    spq: str = Form(""),
    warranty: str = Form(""),
    country_of_origin: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    from ..routers.crm.offers import create_offer
    from ..schemas.crm import OfferCreate

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    def _int(v):
        try:
            return int(v) if str(v).strip() else None
        except ValueError:
            return None

    def _float(v):
        try:
            return float(v) if str(v).strip() else None
        except ValueError:
            return None

    payload = OfferCreate(
        mpn=mpn, vendor_name=vendor_name, requirement_id=requirement_id,
        manufacturer=manufacturer or None, qty_available=_int(qty_available),
        unit_price=_float(unit_price), lead_time=lead_time or None,
        date_code=date_code or None, condition=condition or "new",
        packaging=packaging or None, firmware=firmware or None,
        hardware_code=hardware_code or None, moq=_int(moq), spq=_int(spq),
        warranty=warranty or None, country_of_origin=country_of_origin or None,
        notes=notes or None, source="manual",
    )
    await create_offer(requirement.requisition_id, payload, user=user, db=db)
    db.expire_all()
    requirement = db.get(Requirement, requirement_id)
    resp = _render_offers_panel(request, requirement, db)
    _attach_toast(resp, "Offer saved", "success")   # see note
    return resp
```

> Note on the toast: prefer the existing OOB approach used in this router. If a
> helper like `_oob_toast` exists it returns an OOB div — append it to the panel
> HTML; otherwise set an `HX-Trigger: {"showToast": {...}}` header (as
> `sightings_refresh` does). Implement `_attach_toast` to set that header. Confirm
> the exact existing mechanism while implementing and match it.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(sightings): create offer endpoint via canonical create_offer"`

---

## Task 7: Offer mutation endpoints (thin wrappers)

**Files:** Modify `sightings.py` (review/reconfirm/mark-sold/edit-form/update/delete).

- [ ] **Step 1: Failing tests**

```python
def test_approve_pending_offer_via_panel(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "PendVend", "LM317T", "lm317t",
               status=OfferStatus.PENDING_REVIEW)
    resp = client.post(f"/v2/partials/sightings/{r.id}/offers/{o.id}/review",
                       data={"action": "approve"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id).status == OfferStatus.ACTIVE

def test_delete_offer_via_panel(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t")
    resp = client.delete(f"/v2/partials/sightings/{r.id}/offers/{o.id}")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id) is None

def test_edit_offer_updates_field(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t", price=1.0)
    resp = client.post(f"/v2/partials/sightings/{r.id}/offers/{o.id}",
                       data={"vendor_name": "Arrow", "mpn": "LM317T", "unit_price": "2.50"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert float(db_session.get(Offer, o.id).unit_price) == 2.50
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3:** Add endpoints to `sightings.py`. Each fetches the requirement (for
re-render), calls the canonical crm function, then returns `_render_offers_panel`:

```python
@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def sightings_review_offer(request: Request, requirement_id: int, offer_id: int,
        action: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_user)):
    from ..routers.crm.offers import approve_offer, reject_offer
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    if action == "approve":
        await approve_offer(offer_id, user=user, db=db)
    else:
        await reject_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _render_offers_panel(request, db.get(Requirement, requirement_id), db)

@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/reconfirm", response_class=HTMLResponse)
async def sightings_reconfirm_offer(request: Request, requirement_id: int, offer_id: int,
        db: Session = Depends(get_db), user: User = Depends(require_user)):
    from ..routers.crm.offers import reconfirm_offer
    requirement = db.get(Requirement, requirement_id)
    if not requirement: raise HTTPException(404, "Requirement not found")
    await reconfirm_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _render_offers_panel(request, db.get(Requirement, requirement_id), db)

@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/mark-sold", response_class=HTMLResponse)
async def sightings_mark_sold(request: Request, requirement_id: int, offer_id: int,
        db: Session = Depends(get_db), user: User = Depends(require_buyer)):
    from ..routers.crm.offers import mark_offer_sold       # verify exact fn name/sig
    requirement = db.get(Requirement, requirement_id)
    if not requirement: raise HTTPException(404, "Requirement not found")
    await mark_offer_sold(offer_id, user=user, db=db)
    db.expire_all()
    return _render_offers_panel(request, db.get(Requirement, requirement_id), db)

@router.delete("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_delete_offer(request: Request, requirement_id: int, offer_id: int,
        db: Session = Depends(get_db), user: User = Depends(require_buyer)):
    from ..routers.crm.offers import delete_offer
    requirement = db.get(Requirement, requirement_id)
    if not requirement: raise HTTPException(404, "Requirement not found")
    await delete_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _render_offers_panel(request, db.get(Requirement, requirement_id), db)

@router.get("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def sightings_offer_edit_form(request: Request, requirement_id: int, offer_id: int,
        db: Session = Depends(get_db), user: User = Depends(require_user)):
    requirement = db.get(Requirement, requirement_id)
    offer = db.get(Offer, offer_id)
    if not requirement or not offer: raise HTTPException(404, "Not found")
    prefill = {f: (getattr(offer, f) if getattr(offer, f) is not None else "") for f in
        ["vendor_name","mpn","manufacturer","qty_available","unit_price","lead_time",
         "date_code","condition","packaging","firmware","hardware_code","moq","spq",
         "warranty","country_of_origin","notes"]}
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": offer}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)

@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_update_offer(request: Request, requirement_id: int, offer_id: int,
        vendor_name: str = Form(""), mpn: str = Form(""), manufacturer: str = Form(""),
        qty_available: str = Form(""), unit_price: str = Form(""), lead_time: str = Form(""),
        date_code: str = Form(""), condition: str = Form(""), packaging: str = Form(""),
        firmware: str = Form(""), hardware_code: str = Form(""), moq: str = Form(""),
        spq: str = Form(""), warranty: str = Form(""), country_of_origin: str = Form(""),
        notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_buyer)):
    from ..routers.crm.offers import update_offer
    from ..schemas.crm import OfferUpdate
    requirement = db.get(Requirement, requirement_id)
    if not requirement: raise HTTPException(404, "Requirement not found")
    def _i(v):
        try: return int(v) if str(v).strip() else None
        except ValueError: return None
    def _f(v):
        try: return float(v) if str(v).strip() else None
        except ValueError: return None
    payload = OfferUpdate(vendor_name=vendor_name or None, mpn=mpn or None,
        manufacturer=manufacturer or None, qty_available=_i(qty_available),
        unit_price=_f(unit_price), lead_time=lead_time or None, date_code=date_code or None,
        condition=condition or None, packaging=packaging or None, firmware=firmware or None,
        hardware_code=hardware_code or None, moq=_i(moq), spq=_i(spq), warranty=warranty or None,
        country_of_origin=country_of_origin or None, notes=notes or None)
    await update_offer(offer_id, payload, user=user, db=db)
    db.expire_all()
    return _render_offers_panel(request, db.get(Requirement, requirement_id), db)
```

> Verify `mark_offer_sold`'s exact signature (it's a PATCH handler at offers.py:698);
> if it needs no extra body, the call above is correct; otherwise pass required args.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(sightings): offer mutation endpoints (approve/reject/reconfirm/sold/edit/delete)"`

---

## Task 8: Docs

- [ ] Update `docs/APP_MAP_INTERACTIONS.md`: add the sightings offers endpoints and
the part-centric Offers tab data flow (offers matched by part across requisitions).
Commit — `git commit -am "docs: APP_MAP sightings Offers tab"`

---

## Task 9: Full verification

- [ ] `pre-commit run --files <all changed>` — green.
- [ ] `TESTING=1 PYTHONPATH=$PWD pytest tests/test_sightings_offers.py tests/test_sightings_router.py -q --override-ini="addopts="` — green.
- [ ] `TESTING=1 PYTHONPATH=$PWD pytest tests/ -k "offer or sighting or requisition" -q --override-ini="addopts="` — green (no requisitions regression).
- [ ] Render-smoke the detail pane via the app Jinja env to eyeball the Offers tab +
Convert button placement.
- [ ] Run the PR-review agents (comment-analyzer, pr-test-analyzer,
type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer +
feature-dev:code-reviewer); fix all findings.

---

## Self-Review notes
- Spec coverage: Offers tab (T3), part-centric incl subs (T1), source hint (T3),
  Convert (T5), Enter (T4/T5/T6), modal (T4), pending move (T3), mutations (T7),
  DRY reuse of create_offer + field grid (T4/T6/T7), activity (T6), tests (all).
- Toast mechanism (T6) and `mark_offer_sold` signature (T7) are the two items to
  confirm against source during execution; both have an explicit verification note.
