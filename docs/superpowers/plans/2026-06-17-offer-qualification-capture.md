# Offer Qualification Capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a buyer converts a sighting to an offer, capture condition/packaging/provenance in a standardized, condition-driven way — auto-composing the standardized note, gating only the per-condition essentials, never adding form noise — so the data trustworthy-notes the next human, filters/reports, enforces rigor, and feeds RFQ follow-up.

**Architecture:** "Confirm, don't compose." Reuse existing `Offer` columns + **3 new columns** (`qualification_status`, `qualification_note`, `qualification` JSON). All condition logic lives in one pure-function service `app/services/offer_qualification.py`, called at both offer-persistence points. The condition `<select>` is the UI spine: an Alpine factory reveals only that condition's chips, live-previews the auto-note, and disables Save until the per-condition essentials are met (server 422 is the backstop).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL (JSON), Jinja2, HTMX, Alpine.js, Tailwind, pytest (+ in-memory SQLite).

## Global Constraints

- Stack is **HTMX + Alpine + Jinja2 — NOT React**. Server-render + HX swap.
- **No band-aids** — root-cause only.
- Migration: Alembic only; revision id **≤32 chars**; claim the number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; include a working `downgrade`; `alembic heads` must show one head.
- New offer-condition vocabulary is **lowercase** `new` / `new_no_pkg` / `pulls` / `refurb` (NOT the capitalized `MaterialCondition` card vocab). Legacy offer `condition='used'` → `pulls`.
- Alpine x-data carrying server data **must be single-quoted** with `|tojson` (double-quoted breaks init; a static guard enforces this).
- New Alpine.data factory registers in `app/static/htmx_app.js` **above** the final `Alpine.start()` (line ~2125).
- Input `name=` attributes in `_offer_form_fields.html` are the contract for BOTH POST handlers — keep existing names stable; new fields get new stable names.
- Render via `template_response(name, context)` from `app/template_env.py` (context MUST include `request`).
- Tests run on **in-memory SQLite** (`-n auto` xdist); PG-specific behavior (JSON filtering, the `used→pulls` data migration) is verified live after deploy.
- Every new file needs a header comment (what it does / what calls it / what it depends on).
- After code changes, update the relevant `docs/APP_MAP_*.md`.

---

## File Structure

**Create:**
- `app/services/offer_qualification.py` — pure qualification logic + `prefill_from_vendor` + `apply_qualification(offer)` + `request_template`.
- `app/templates/htmx/partials/offers/_qualification_fields.html` — condition spine + condition-gated chip panels + note preview + meter (the Alpine-wrapped section).
- `tests/test_offer_qualification.py` — pure-service unit tests.
- `tests/test_offer_qualification_routes.py` — integration tests (sightings create/update, #7 request, #8 prefill).
- `alembic/versions/108_offer_qualification.py` — 3 columns + index + `used→pulls` data update.

**Modify:**
- `app/constants.py` — add `OfferCondition`, `QualificationStatus` enums.
- `app/models/offers.py` — 3 columns + index + lenient condition validator + `qualification_summary` property.
- `app/schemas/crm.py` — `OfferCreate`/`OfferUpdate` add `qualification`; condition validator → `normalize_offer_condition`.
- `app/routers/crm/offers.py` — `create_offer`/`update_offer` set `qualification` + call `apply_qualification` (422 on essentials).
- `app/routers/sightings.py` — create/update handlers add new Form fields → payload `qualification`; catch 422 → re-render modal with errors; `sightings_offer_form` merges `prefill_from_vendor` (#8); add `#7` request route.
- `app/routers/htmx_views.py` — `add_offer`/`edit_offer` read new fields + `apply_qualification`.
- `app/templates/htmx/partials/offers/_offer_form_fields.html` — restructure (core line → condition spine include → collapsible "More details").
- `app/templates/htmx/partials/offers/_field_macros.html` — add `chip_select` + `qual_badge` macros.
- `app/templates/htmx/partials/sightings/offer_form_modal.html` — error banner; wrap include in the Alpine factory.
- `app/templates/htmx/partials/sightings/_offer_row.html` — qualification badge beside status pill.
- `app/templates/htmx/partials/sightings/offers_panel.html` (offer detail area) — standardized note + provenance + pending requests.
- `app/templates/htmx/partials/requisitions/add_offer_form.html` — inherits the restructure (verify Alpine wrap).
- `app/static/htmx_app.js` — register `offerQualification` factory.
- `MIGRATION_NUMBERS_IN_FLIGHT.txt` — claim `108`.
- `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_ARCHITECTURE.md`.

---

## Task 1: Data layer — constants, model columns, migration

**Files:**
- Modify: `app/constants.py` (after `AttributionStatus`, ~line 58)
- Modify: `app/models/offers.py` (Offer columns ~line 53–75; `__table_args__` ~137; validators ~99)
- Create: `alembic/versions/108_offer_qualification.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_offer_qualification_model.py`

**Interfaces — Produces:**
- `OfferCondition` StrEnum: `NEW="new"`, `NEW_NO_PKG="new_no_pkg"`, `PULLS="pulls"`, `REFURB="refurb"`.
- `QualificationStatus` StrEnum: `UNSET="unset"`, `INCOMPLETE="incomplete"`, `ESSENTIALS="essentials"`, `COMPLETE="complete"`.
- `Offer.qualification_status: str|None`, `Offer.qualification_note: str|None`, `Offer.qualification: dict|None`.

- [ ] **Step 1: Write the failing model test**

```python
# tests/test_offer_qualification_model.py
# What: asserts the 3 new qualification columns + the new condition enum exist and round-trip.
# Called by: pytest. Depends on: conftest db_session/test_requisition/test_user fixtures.
from app.constants import OfferCondition, QualificationStatus
from app.models.offers import Offer


def test_offer_qualification_columns_roundtrip(db_session, test_requisition, test_user):
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        condition=OfferCondition.PULLS.value,
        qualification={"usage": "systems", "requests": []},
        qualification_note="Pulls — packaged in Trays, pulled from systems.",
        qualification_status=QualificationStatus.ESSENTIALS.value,
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    assert o.qualification["usage"] == "systems"
    assert o.qualification_status == "essentials"
    assert OfferCondition("new_no_pkg") is OfferCondition.NEW_NO_PKG
```

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification_model.py -v --override-ini="addopts="`
Expected: FAIL — `AttributeError: ... 'qualification'` / `ImportError: OfferCondition`.

- [ ] **Step 3: Add the enums to `app/constants.py`** (immediately after `AttributionStatus`)

```python
class OfferCondition(StrEnum):
    """Offer-row condition vocabulary (lowercase; distinct from MaterialCondition).

    Drives the qualification capture spine. NOT the capitalized card/facet vocab.
    """

    NEW = "new"               # new, in original manufacturer packaging
    NEW_NO_PKG = "new_no_pkg"  # new, no original manufacturer packaging
    PULLS = "pulls"
    REFURB = "refurb"


class QualificationStatus(StrEnum):
    """Snapshot of how complete an offer's standardized qualification is."""

    UNSET = "unset"            # no condition chosen
    INCOMPLETE = "incomplete"  # an essential is missing (legacy/API only)
    ESSENTIALS = "essentials"  # essentials met, some recommended missing
    COMPLETE = "complete"      # essentials + recommended all present
```

- [ ] **Step 4: Add the 3 columns + index + lenient validator to `app/models/offers.py`**

Add columns right after `country_of_origin` (line 53):

```python
    # --- Qualification capture (standardized buyer qualification at offer entry) ---
    qualification_status = Column(String(20))  # QualificationStatus snapshot for filter/report
    qualification_note = Column(Text)          # system-composed standardized note (NOT free notes)
    qualification = Column(JSON)               # condition-specific detail + pending vendor requests
```

Add an index inside `__table_args__` (after `ix_offers_status`):

```python
        Index("ix_offers_qualification_status", "qualification_status"),
```

Add a lenient condition validator (mirrors the existing `_validate_status` warn-only style), after `_validate_qty_available`:

```python
    @validates("condition")
    def _validate_condition(self, _key, value):
        from ..constants import OfferCondition

        valid = {e.value for e in OfferCondition}
        if value and value not in valid:
            from loguru import logger

            logger.warning("Unexpected offer condition: {}. Expected one of {}", value, valid)
        return value
```

- [ ] **Step 5: Run the model test, verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification_model.py -v --override-ini="addopts="`
Expected: PASS (conftest builds tables from the models, so columns exist).

- [ ] **Step 6: Write the migration `alembic/versions/108_offer_qualification.py`**

```python
"""Add offer qualification capture columns + migrate legacy condition.

What: adds offers.qualification_status / qualification_note / qualification (JSON)
      and an index on qualification_status; migrates legacy condition 'used' -> 'pulls'.
Downgrade: drops the index + 3 columns. The 'used' -> 'pulls' data change is NOT
      reversed (legacy 'used' is unrecoverable post-merge) — documented one-way.
Called by: alembic (upgrade/downgrade).
Depends on: offers table.

Revision ID: 108_offer_qualification
Revises: 107_is_scratch_requisitions
Create Date: 2026-06-17
"""

import sqlalchemy as sa
from alembic import op
from loguru import logger
from sqlalchemy import text

revision = "108_offer_qualification"
down_revision = "107_is_scratch_requisitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("qualification_status", sa.String(length=20), nullable=True))
    op.add_column("offers", sa.Column("qualification_note", sa.Text(), nullable=True))
    op.add_column("offers", sa.Column("qualification", sa.JSON(), nullable=True))
    op.create_index("ix_offers_qualification_status", "offers", ["qualification_status"])

    conn = op.get_bind()
    result = conn.execute(text("UPDATE offers SET condition = 'pulls' WHERE condition = 'used'"))
    logger.info("108_offer_qualification: migrated {} legacy 'used' offers -> 'pulls'", result.rowcount)


def downgrade() -> None:
    op.drop_index("ix_offers_qualification_status", table_name="offers")
    op.drop_column("offers", "qualification")
    op.drop_column("offers", "qualification_note")
    op.drop_column("offers", "qualification_status")
    # Note: 'pulls' -> 'used' is intentionally NOT reversed (legacy value unrecoverable).
```

- [ ] **Step 7: Claim the migration number** — append to `MIGRATION_NUMBERS_IN_FLIGHT.txt`:

```
108  worktree-offer-qualification  offer qualification capture columns + used->pulls
```

- [ ] **Step 8: Verify single head**

Run: `PYTHONPATH=$PWD alembic heads`
Expected: exactly one head — `108_offer_qualification (head)`. If a newer head landed, re-point `down_revision` at it (keep number 108).

- [ ] **Step 9: Commit**

```bash
git add app/constants.py app/models/offers.py alembic/versions/108_offer_qualification.py \
        MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_offer_qualification_model.py
git commit -m "feat(offers): qualification columns + condition enum + migration 108"
```

---

## Task 2: Qualification service — pure logic + unit tests

**Files:**
- Create: `app/services/offer_qualification.py`
- Test: `tests/test_offer_qualification.py`

**Interfaces — Produces (imported by Tasks 3,5,6,7,8):**
- `normalize_offer_condition(raw: str|None) -> str|None`
- `validate_essentials(condition: str|None, data: dict) -> list[str]`
- `compose_note(condition: str|None, data: dict) -> str`
- `meter(condition: str|None, data: dict, has_images: bool) -> tuple[int, int]`
- `compute_status(condition: str|None, data: dict, has_images: bool) -> str`
- `apply_qualification(offer) -> None`  (raises `QualificationError`)
- `request_template(kind: str, mpn: str|None) -> str`
- `class QualificationError(Exception)` with `.errors: list[str]`
- Module constants: `PACKAGING_CHIPS`, `USAGE_OPTIONS`, `REFURB_BY_OPTIONS`, `REQUEST_KINDS`.

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_offer_qualification.py
# What: pure-function tests for the offer qualification service.
# Called by: pytest. Depends on: app.services.offer_qualification (no DB for these).
import pytest

from app.services.offer_qualification import (
    QualificationError,
    apply_qualification,
    compose_note,
    compute_status,
    meter,
    normalize_offer_condition,
    request_template,
    validate_essentials,
)


@pytest.mark.parametrize("raw,expected", [
    ("used", "pulls"), ("Used", "pulls"), ("pulled", "pulls"),
    ("refurbished", "refurb"), ("recertified", "refurb"),
    ("new", "new"), ("new_no_pkg", "new_no_pkg"), ("new no pkg", "new_no_pkg"),
    ("", None), (None, None), ("garbage", None),
])
def test_normalize_offer_condition(raw, expected):
    assert normalize_offer_condition(raw) == expected


def test_note_new():
    assert compose_note("new", {}) == "New — parts are in the original manufacturer's packaging."


def test_note_new_no_pkg():
    assert compose_note("new_no_pkg", {"packaging": "Trays"}) == \
        "New, no original manufacturer packaging. Packaged in Trays."


def test_note_pulls_full():
    note = compose_note("pulls", {"packaging": "Trays", "usage": "systems", "part_condition": "Light wear"})
    assert note == "Pulls — packaged in Trays, pulled from systems. Condition: Light wear."


def test_note_refurb_third_party_with_cert():
    note = compose_note("refurb", {"refurbished_by": "third_party", "refurb_process": "Reballed BGA", "cert_doc": "yes"})
    assert note == "Refurbished by a third party. Process: Reballed BGA. Certifying document on file."


def test_validate_blocks_bulk_for_pulls():
    errs = validate_essentials("pulls", {"packaging": "bulk", "usage": "boards"})
    assert errs and any("bulk" in e.lower() for e in errs)


def test_validate_pulls_requires_usage():
    errs = validate_essentials("pulls", {"packaging": "Trays"})
    assert any("usage" in e.lower() for e in errs)


def test_validate_new_no_essential_blocks_missing_manufacturer():
    assert validate_essentials("new", {"manufacturer": ""})
    assert validate_essentials("new", {"manufacturer": "TI"}) == []


def test_validate_refurb_requires_by_and_process():
    assert validate_essentials("refurb", {"refurbished_by": "supplier"})  # missing process
    assert validate_essentials("refurb", {"refurbished_by": "supplier", "refurb_process": "Cleaned"}) == []


def test_unset_condition_allowed_and_unset_status():
    assert validate_essentials(None, {}) == []
    assert compute_status(None, {}, has_images=False) == "unset"


def test_status_and_meter_pulls():
    data = {"packaging": "Trays", "usage": "systems", "part_condition": "Clean"}
    assert meter("pulls", data, has_images=True) == (4, 4)
    assert compute_status("pulls", data, has_images=True) == "complete"
    assert compute_status("pulls", data, has_images=False) == "essentials"  # images missing


def test_refurb_meter_excludes_cert_for_supplier():
    data = {"refurbished_by": "supplier", "refurb_process": "Cleaned"}
    assert meter("refurb", data, has_images=False) == (2, 3)  # by, process, images(0); no cert item


def test_request_template():
    assert "{mpn}" not in request_template("images", "LM317T")
    with pytest.raises(ValueError):
        request_template("nope", "X")


def test_apply_qualification_raises_on_missing_essential():
    class _O:
        condition = "pulls"
        packaging = "Trays"
        manufacturer = None
        date_code = None
        qualification = {}  # no usage
        attachments = []
        qualification_note = None
        qualification_status = None

    o = _O()
    with pytest.raises(QualificationError):
        apply_qualification(o)


def test_apply_qualification_sets_note_and_status():
    class _O:
        condition = "new"
        packaging = None
        manufacturer = "TI"
        date_code = None
        qualification = {}
        attachments = []
        qualification_note = None
        qualification_status = None

    o = _O()
    apply_qualification(o)
    assert o.qualification_note.startswith("New — parts are in the original")
    assert o.qualification_status in ("essentials", "complete")
```

- [ ] **Step 2: Run, verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification.py -v --override-ini="addopts="`
Expected: FAIL — `ModuleNotFoundError: app.services.offer_qualification`.

- [ ] **Step 3: Implement `app/services/offer_qualification.py`**

```python
"""Standardized offer qualification logic.

What: condition-driven validation, standardized-note composition, qualification
      status/meter computation, vendor-memory prefill, and RFQ-back request templates
      for the sighting->offer conversion flow.
Called by: app/routers/crm/offers.py, app/routers/sightings.py, app/routers/htmx_views.py,
      app/models/offers.py (qualification_summary property).
Depends on: app.models.offers.Offer (lazy import inside DB-touching functions only).
"""

from __future__ import annotations

from typing import Any

PACKAGING_CHIPS = ("Tape & Reel", "Reels", "Trays", "Tubes", "Antistatic bags", "Boxes")
USAGE_OPTIONS = ("boards", "systems")
REFURB_BY_OPTIONS = ("supplier", "third_party")
REQUEST_KINDS = ("images", "fpq", "cert", "pkg_qty")

_LEGACY_CONDITION = {
    "used": "pulls", "pull": "pulls", "pulls": "pulls", "pulled": "pulls",
    "refurbished": "refurb", "recertified": "refurb", "refurb": "refurb",
    "new": "new", "new_no_pkg": "new_no_pkg", "new_no_packaging": "new_no_pkg",
}
_VALID_CONDITIONS = {"new", "new_no_pkg", "pulls", "refurb"}

_USAGE_HUMAN = {"boards": "boards", "systems": "systems"}
_WHO_HUMAN = {"supplier": "the supplier", "third_party": "a third party"}

_REQUEST_TEMPLATES = {
    "images": "Please provide images of all angles, markings, contact points, and packaging for {mpn}.",
    "fpq": "Please confirm the factory package quantity (FPQ) for {mpn}.",
    "cert": "Please provide the third-party refurbishment certification document for {mpn}.",
    "pkg_qty": "Please confirm the package quantity and how the parts are packaged for {mpn}.",
}


class QualificationError(Exception):
    """Raised when an offer is missing a per-condition essential. Carries `.errors`."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def normalize_offer_condition(raw: str | None) -> str | None:
    if not raw:
        return None
    v = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    v = _LEGACY_CONDITION.get(v, v)
    return v if v in _VALID_CONDITIONS else None


def _s(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _require_packaging(data: dict, errors: list[str]) -> None:
    pkg = _s(data, "packaging")
    if not pkg:
        errors.append("Packaging is required; 'bulk' is not acceptable.")
    elif pkg.lower() in ("bulk", "loose") or pkg not in PACKAGING_CHIPS:
        errors.append(
            "Packaging must be one of "
            f"{', '.join(PACKAGING_CHIPS)} — 'bulk' is not acceptable."
        )


def validate_essentials(condition: str | None, data: dict) -> list[str]:
    errors: list[str] = []
    if not condition:
        return errors  # unset is allowed to save
    if condition == "new":
        if not _s(data, "manufacturer"):
            errors.append("Manufacturer is required for New (original packaging) offers.")
    elif condition == "new_no_pkg":
        _require_packaging(data, errors)
    elif condition == "pulls":
        _require_packaging(data, errors)
        if data.get("usage") not in USAGE_OPTIONS:
            errors.append("Usage (pulled from boards or systems) is required for Pulls.")
    elif condition == "refurb":
        if data.get("refurbished_by") not in REFURB_BY_OPTIONS:
            errors.append("Refurbished-by (supplier or 3rd-party) is required for Refurbs.")
        if not _s(data, "refurb_process"):
            errors.append("Refurbishment process is required for Refurbs.")
    return errors


def compose_note(condition: str | None, data: dict) -> str:
    pkg = _s(data, "packaging")
    if condition == "new":
        return "New — parts are in the original manufacturer's packaging."
    if condition == "new_no_pkg":
        note = (
            f"New, no original manufacturer packaging. Packaged in {pkg}."
            if pkg else "New, no original manufacturer packaging."
        )
        pc = _s(data, "part_condition")
        return f"{note} {pc}" if pc else note
    if condition == "pulls":
        usage = _USAGE_HUMAN.get(data.get("usage"), "")
        if pkg and usage:
            note = f"Pulls — packaged in {pkg}, pulled from {usage}."
        elif pkg:
            note = f"Pulls — packaged in {pkg}."
        elif usage:
            note = f"Pulls — pulled from {usage}."
        else:
            note = "Pulls."
        pc = _s(data, "part_condition")
        return f"{note} Condition: {pc}." if pc else note
    if condition == "refurb":
        who = _WHO_HUMAN.get(data.get("refurbished_by"), "")
        note = f"Refurbished by {who}." if who else "Refurbished."
        proc = _s(data, "refurb_process")
        if proc:
            note += f" Process: {proc}."
        if data.get("refurbished_by") == "third_party":
            cert = data.get("cert_doc")
            if cert == "yes":
                note += " Certifying document on file."
            elif cert == "no":
                note += " No certifying document."
        return note
    return ""


def _items_for(condition: str, data: dict, has_images: bool) -> dict[str, bool]:
    pkg = bool(_s(data, "packaging"))
    dc = bool(_s(data, "date_code"))
    if condition == "new":
        return {"manufacturer": bool(_s(data, "manufacturer")), "package_type": pkg, "date_code": dc}
    if condition == "new_no_pkg":
        return {"packaging": pkg, "images": has_images, "date_code": dc}
    if condition == "pulls":
        return {
            "packaging": pkg,
            "usage": data.get("usage") in USAGE_OPTIONS,
            "images": has_images,
            "part_condition": bool(_s(data, "part_condition")),
        }
    if condition == "refurb":
        items = {
            "refurbished_by": data.get("refurbished_by") in REFURB_BY_OPTIONS,
            "refurb_process": bool(_s(data, "refurb_process")),
            "images": has_images,
        }
        if data.get("refurbished_by") == "third_party":
            items["cert_doc"] = data.get("cert_doc") in ("yes", "no")
        return items
    return {}


def meter(condition: str | None, data: dict, has_images: bool) -> tuple[int, int]:
    if not condition:
        return (0, 0)
    items = _items_for(condition, data, has_images)
    return (sum(1 for ok in items.values() if ok), len(items))


def compute_status(condition: str | None, data: dict, has_images: bool) -> str:
    if not condition:
        return "unset"
    if validate_essentials(condition, data):
        return "incomplete"
    filled, total = meter(condition, data, has_images)
    return "complete" if filled >= total else "essentials"


def _data_from_offer(offer: Any) -> dict:
    q = dict(offer.qualification or {})
    return {
        "manufacturer": offer.manufacturer,
        "packaging": offer.packaging,
        "date_code": offer.date_code,
        "usage": q.get("usage"),
        "refurbished_by": q.get("refurbished_by"),
        "refurb_process": q.get("refurb_process"),
        "cert_doc": q.get("cert_doc"),
        "part_condition": q.get("part_condition"),
    }


def apply_qualification(offer: Any) -> None:
    """Validate essentials, compose the standardized note, compute status; set the columns.

    Raises QualificationError(list[str]) when a per-condition essential is missing.
    """
    data = _data_from_offer(offer)
    errors = validate_essentials(offer.condition, data)
    if errors:
        raise QualificationError(errors)
    has_images = bool(getattr(offer, "attachments", None))
    offer.qualification_note = compose_note(offer.condition, data)
    offer.qualification_status = compute_status(offer.condition, data, has_images)


def request_template(kind: str, mpn: str | None) -> str:
    tpl = _REQUEST_TEMPLATES.get(kind)
    if not tpl:
        raise ValueError(f"Unknown request kind: {kind}")
    return tpl.format(mpn=mpn or "this part")


def prefill_from_vendor(db, vendor_name_normalized: str | None) -> dict:
    """Vendor-memory: pull stable answers from this vendor's most-recent offer (#8)."""
    if not vendor_name_normalized:
        return {}
    from app.models.offers import Offer

    prev = (
        db.query(Offer)
        .filter(Offer.vendor_name_normalized == vendor_name_normalized)
        .order_by(Offer.created_at.desc())
        .first()
    )
    if not prev:
        return {}
    out: dict = {}
    if prev.country_of_origin:
        out["country_of_origin"] = prev.country_of_origin
    pq = prev.qualification or {}
    if pq.get("refurbished_by"):
        out["refurbished_by"] = pq["refurbished_by"]
    if pq.get("terms"):
        out["terms"] = pq["terms"]
    return out
```

- [ ] **Step 4: Run the unit tests, verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification.py -v --override-ini="addopts="`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/offer_qualification.py tests/test_offer_qualification.py
git commit -m "feat(offers): offer_qualification service (validate/compose/status/meter)"
```

---

## Task 3: Persistence wiring — schemas + both offer flows

**Files:**
- Modify: `app/schemas/crm.py` (`OfferCreate` ~135, `OfferUpdate` ~196, condition validator)
- Modify: `app/routers/crm/offers.py` (`create_offer` ~287, `update_offer` ~569)
- Modify: `app/routers/sightings.py` (`sightings_create_offer` ~2235, `sightings_update_offer` ~2421)
- Modify: `app/routers/htmx_views.py` (`add_offer` ~2111, `edit_offer` ~2235)
- Test: `tests/test_offer_qualification_routes.py`

**Interfaces — Consumes:** Task 2 (`apply_qualification`, `QualificationError`, `normalize_offer_condition`). **Produces:** offers persisted with `qualification`/`qualification_note`/`qualification_status`; HTTP 422 (JSON) / re-rendered form (HTMX) on missing essentials.

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/test_offer_qualification_routes.py
# What: integration tests for qualification capture across the sightings offer flow.
# Called by: pytest. Depends on: conftest client/db_session/test_requisition/test_user.
from app.models.offers import Offer


def _req(test_requisition):
    return test_requisition.id, test_requisition.requirements[0].id


def test_sightings_create_pulls_composes_note(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={
            "vendor_name": "Acme", "mpn": "LM317T", "condition": "pulls",
            "packaging": "Trays", "usage": "systems", "part_condition": "Clean",
        },
    )
    assert resp.status_code == 200
    o = db_session.query(Offer).filter_by(vendor_name="Acme").one()
    assert o.condition == "pulls"
    assert o.qualification["usage"] == "systems"
    assert o.qualification_note == "Pulls — packaged in Trays, pulled from systems. Condition: Clean."
    assert o.qualification_status in ("essentials", "complete")


def test_sightings_create_pulls_missing_usage_is_blocked(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "NoUsage", "mpn": "LM317T", "condition": "pulls", "packaging": "Trays"},
    )
    # No offer persisted; the buyer sees an inline error (re-rendered form, 200).
    assert db_session.query(Offer).filter_by(vendor_name="NoUsage").first() is None
    assert b"Usage" in resp.content


def test_sightings_create_bulk_packaging_rejected(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "Bulky", "mpn": "LM317T", "condition": "new_no_pkg", "packaging": "bulk"},
    )
    assert db_session.query(Offer).filter_by(vendor_name="Bulky").first() is None
    assert b"bulk" in resp.content.lower()


def test_legacy_used_normalizes_to_pulls(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "Legacy", "mpn": "LM317T", "condition": "used",
              "packaging": "Trays", "usage": "boards"},
    )
    o = db_session.query(Offer).filter_by(vendor_name="Legacy").one()
    assert o.condition == "pulls"
```

- [ ] **Step 2: Run, verify they fail**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification_routes.py -v --override-ini="addopts="`
Expected: FAIL (qualification not persisted; missing-usage currently saves).

- [ ] **Step 3: Extend the schemas (`app/schemas/crm.py`)**

In `OfferCreate` and `OfferUpdate`, add the field (place near `condition`):

```python
    qualification: dict | None = None
```

Find the existing condition `@field_validator` on each schema and set its body to delegate to the offer normalizer (import at top of file: `from app.services.offer_qualification import normalize_offer_condition`):

```python
    @field_validator("condition")
    @classmethod
    def _normalize_condition(cls, v):
        return normalize_offer_condition(v) or v  # keep raw if unmappable (lenient model validator warns)
```

> If `OfferCreate.condition` is a required non-optional `str` with default `"new"`, keep the default; the validator runs after default-filling.

- [ ] **Step 4: Wire `apply_qualification` into the canonical builders (`app/routers/crm/offers.py`)**

In `create_offer`, after the `Offer(...)` is built and its scalar fields (condition, packaging, manufacturer, date_code) are set, before the side-effects/commit, add:

```python
    from fastapi import HTTPException

    from app.services.offer_qualification import QualificationError, apply_qualification

    offer.qualification = payload.qualification or None
    try:
        apply_qualification(offer)
    except QualificationError as e:
        raise HTTPException(status_code=422, detail={"error": "; ".join(e.errors)})
```

In `update_offer`, after the `setattr` loop that applies `changes`, before `db.commit()`:

```python
    from fastapi import HTTPException

    from app.services.offer_qualification import QualificationError, apply_qualification

    if "qualification" in changes:
        offer.qualification = changes["qualification"] or None
    try:
        apply_qualification(offer)  # recompute note/status from current condition + qualification
    except QualificationError as e:
        raise HTTPException(status_code=422, detail={"error": "; ".join(e.errors)})
```

- [ ] **Step 5: Collect the new fields + handle 422 in the sightings handlers (`app/routers/sightings.py`)**

Add these `Form('')` params to BOTH `sightings_create_offer` and `sightings_update_offer` signatures:

```python
    usage: str = Form(""),
    refurbished_by: str = Form(""),
    refurb_process: str = Form(""),
    cert_doc: str = Form(""),
    part_condition: str = Form(""),
    provenance_story: str = Form(""),
    terms: str = Form(""),
    lead_time_reason: str = Form(""),
```

Build the qualification dict and attach to the payload before delegating. In `sightings_create_offer`, where it builds `OfferCreate(...)`, add `qualification=_qual_dict(...)`; define a local helper near the other `_parse_*` helpers:

```python
def _qual_dict(usage, refurbished_by, refurb_process, cert_doc, part_condition,
               provenance_story, terms, lead_time_reason) -> dict | None:
    q = {
        "usage": usage or None,
        "refurbished_by": refurbished_by or None,
        "refurb_process": refurb_process or None,
        "cert_doc": cert_doc or None,
        "part_condition": part_condition or None,
        "provenance_story": provenance_story or None,
        "terms": terms or None,
        "lead_time_reason": lead_time_reason or None,
        "requests": [],
    }
    return q if any(v for k, v in q.items() if k != "requests") else None
```

Wrap the delegated `await create_offer(...)` / `await update_offer(...)` to catch the 422 and re-render the modal with an error banner instead of bubbling a raw 422:

```python
    from fastapi import HTTPException

    try:
        await create_offer(requirement.requisition_id, payload, user=user, db=db)
    except HTTPException as e:
        if e.status_code == 422:
            errors = [(e.detail or {}).get("error")] if isinstance(e.detail, dict) else [str(e.detail)]
            ctx = {"request": request, "requirement": requirement, "offer": None,
                   "prefill": _echo_prefill(locals()), "errors": errors}
            return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)
        raise
```

Add `_echo_prefill` that re-builds a prefill dict from the submitted Form values (so the buyer's input survives the re-render — keys must match `_offer_form_fields.html` input names incl. the new qualification names). `sightings_update_offer` mirrors this (with `offer=db.get(Offer, offer_id)` in ctx).

> The `_qual_dict` (board condition) parts and `Offer.qualification` JSON store `requests: []` so Task 8 can append.

- [ ] **Step 6: Wire the requisitions flow (`app/routers/htmx_views.py`)**

In `add_offer` (reads `form = await request.form()`), after building the `Offer(...)` and before `db.commit()`:

```python
    from app.services.offer_qualification import QualificationError, apply_qualification

    qkeys = ("usage", "refurbished_by", "refurb_process", "cert_doc",
             "part_condition", "provenance_story", "terms", "lead_time_reason")
    qual = {k: (form.get(k) or None) for k in qkeys}
    qual["requests"] = []
    offer.qualification = qual if any(qual[k] for k in qkeys) else None
    offer.condition = normalize_offer_condition(form.get("condition")) or offer.condition
    try:
        apply_qualification(offer)
    except QualificationError as e:
        return HTMLResponse(f'<div class="text-sm text-rose-600 p-2">{"; ".join(e.errors)}</div>', status_code=400)
```

(import `normalize_offer_condition` alongside.) Apply the same in `edit_offer` after its per-field update loop (read the qkeys from `form`, merge into `offer.qualification`, `apply_qualification`).

- [ ] **Step 7: Run integration tests, verify pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_offer_qualification_routes.py -v --override-ini="addopts="`
Expected: all PASS.

- [ ] **Step 8: Run the broader offer suite (regression check)**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_sprint2_offer_mgmt.py tests/test_htmx_views_nightly3.py tests/test_sightings_offers.py tests/test_offers_overhaul.py -q`
Expected: PASS (no regressions from the condition-vocab change / new wiring). Fix any condition-label assertions that expected `used`.

- [ ] **Step 9: Commit**

```bash
git add app/schemas/crm.py app/routers/crm/offers.py app/routers/sightings.py \
        app/routers/htmx_views.py tests/test_offer_qualification_routes.py
git commit -m "feat(offers): persist + gate qualification across both offer flows"
```

---

## Task 4: Form UI — condition spine, chips, note preview, meter

**Files:**
- Modify: `app/templates/htmx/partials/offers/_field_macros.html` (add `chip_select`)
- Create: `app/templates/htmx/partials/offers/_qualification_fields.html`
- Modify: `app/templates/htmx/partials/offers/_offer_form_fields.html` (restructure)
- Modify: `app/templates/htmx/partials/sightings/offer_form_modal.html` (Alpine wrap + error banner)
- Modify: `app/static/htmx_app.js` (register `offerQualification`)
- Test: e2e console check (manual command below)

**Interfaces — Consumes:** Task 3 field names. **Produces:** progressive-disclosure form; Save disabled until essentials met; live note preview + meter.

- [ ] **Step 1: Add the `chip_select` macro to `_field_macros.html`**

```jinja
{%- macro chip_select(label, name, options, model) -%}
{# Radio chips that also drive an Alpine x-model (model = the Alpine state key). #}
<div>
  <label class="block text-xs text-gray-500 mb-1">{{ label }}</label>
  <div class="flex flex-wrap gap-1.5">
    {% for value, text in options %}
    <label class="cursor-pointer">
      <input type="radio" name="{{ name }}" value="{{ value }}" x-model="{{ model }}" class="peer sr-only">
      <span class="inline-flex px-2 py-1 text-xs rounded-full border border-gray-300 text-gray-600
                   peer-checked:bg-brand-500 peer-checked:text-white peer-checked:border-brand-500">{{ text }}</span>
    </label>
    {% endfor %}
  </div>
</div>
{%- endmacro -%}
```

- [ ] **Step 2: Create `_qualification_fields.html`** (the condition-gated panels + preview + meter)

```jinja
{# _qualification_fields.html — condition-driven qualification capture.
   Rendered inside the offerQualification() Alpine scope (x-data on the wrapping form).
   Consumes Alpine state: condition, usage, refurbished_by, cert_doc, part_condition, etc.
   Called by: _offer_form_fields.html. Depends on: _field_macros.chip_select. #}
{% from "htmx/partials/offers/_field_macros.html" import chip_select %}
{% set p = prefill or {} %}

<div class="space-y-3 rounded-md bg-gray-50 p-3">
  {# Condition spine #}
  <div>
    <label class="block text-xs text-gray-500 mb-1">Condition *</label>
    <select name="condition" x-model="condition"
            class="w-full px-2 py-1.5 text-sm border border-gray-300 rounded focus:ring-brand-500 focus:border-brand-500">
      <option value="">— choose —</option>
      <option value="new">New (mfr packaging)</option>
      <option value="new_no_pkg">New (no mfr packaging)</option>
      <option value="pulls">Pulls</option>
      <option value="refurb">Refurbs</option>
    </select>
  </div>

  {# New (no mfr pkg) #}
  <div x-show="condition === 'new_no_pkg'" x-cloak>
    {{ chip_select('Packaging *', 'packaging', PACKAGING_OPTS, 'packaging') }}
  </div>

  {# Pulls #}
  <div x-show="condition === 'pulls'" x-cloak class="space-y-2">
    {{ chip_select('Packaging *', 'packaging', PACKAGING_OPTS, 'packaging') }}
    {{ chip_select('Usage *', 'usage', [('boards','Pulled from boards'),('systems','Pulled from systems')], 'usage') }}
    <div>
      <label class="block text-xs text-gray-500 mb-1">Part condition</label>
      <input type="text" name="part_condition" x-model="part_condition" value="{{ p.get('part_condition','') }}"
             class="w-full px-2 py-1.5 text-sm border border-gray-300 rounded" placeholder="e.g. light wear, clean">
    </div>
  </div>

  {# Refurbs #}
  <div x-show="condition === 'refurb'" x-cloak class="space-y-2">
    {{ chip_select('Refurbished by *', 'refurbished_by', [('supplier','Supplier'),('third_party','3rd party')], 'refurbished_by') }}
    <div>
      <label class="block text-xs text-gray-500 mb-1">Process *</label>
      <input type="text" name="refurb_process" x-model="refurb_process" value="{{ p.get('refurb_process','') }}"
             class="w-full px-2 py-1.5 text-sm border border-gray-300 rounded" placeholder="What was done, and how">
    </div>
    <div x-show="refurbished_by === 'third_party'" x-cloak>
      {{ chip_select('Certifying doc?', 'cert_doc', [('yes','Yes'),('no','No')], 'cert_doc') }}
    </div>
  </div>

  {# Standardized note preview (read-only, auto-composed) + meter #}
  <div x-show="condition" x-cloak class="text-xs">
    <div class="text-gray-400 mb-0.5">Standardized note (auto)</div>
    <div class="rounded bg-white border border-gray-200 px-2 py-1 text-gray-700" x-text="noteText() || '—'"></div>
    <div class="mt-1 text-gray-500" x-show="meterTotal() > 0">
      Qualified <span x-text="meterFilled()"></span>/<span x-text="meterTotal()"></span>
    </div>
  </div>
</div>
```

Where `PACKAGING_OPTS` is provided to the template context, or inline as a Jinja set at the top of this file:

```jinja
{% set PACKAGING_OPTS = [('Tape & Reel','Tape & Reel'),('Reels','Reels'),('Trays','Trays'),('Tubes','Tubes'),('Antistatic bags','Antistatic bags'),('Boxes','Boxes')] %}
```

- [ ] **Step 3: Restructure `_offer_form_fields.html`**

Reorganize to: (1) core line (vendor, mpn, qty, unit_price — keep existing Row 1 inputs), (2) `{% include "htmx/partials/offers/_qualification_fields.html" %}` (replaces the inline condition `<select>` from old Row 2), (3) a collapsible "More details" `<details>` holding the remaining existing inputs (manufacturer, lead_time, date_code, moq, spq, packaging-for-`new`, firmware, hardware_code, warranty, country_of_origin, valid_until). Remove the old standalone condition `<select>`. Keep every input `name=` identical to today.

```jinja
<details class="mt-1">
  <summary class="text-xs text-gray-500 cursor-pointer">More details</summary>
  <div class="mt-2 space-y-3">
    {# ... existing Row 2 (minus condition), Row 3, Row 4 grids moved here verbatim ... #}
  </div>
</details>
```

- [ ] **Step 4: Wrap the form in the Alpine factory + add the error banner (`offer_form_modal.html`)**

On the `<form>` tag add a single-quoted x-data:

```jinja
  <form x-data='offerQualification({{ (prefill or {})|tojson }})'
        {% if offer %}hx-post="/v2/partials/sightings/{{ requirement.id }}/offers/{{ offer.id }}"
        {% else %}hx-post="/v2/partials/sightings/{{ requirement.id }}/offers"{% endif %}
        ...existing attrs...>
```

Above the field include, render server-side errors (from Task 3 re-render):

```jinja
    {% if errors %}
    <div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">
      {% for e in errors %}<div>{{ e }}</div>{% endfor %}
    </div>
    {% endif %}
```

Disable Save until essentials are met:

```jinja
      <button type="submit" :disabled="!essentialsMet()"
              class="px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded hover:bg-brand-600 disabled:opacity-50 disabled:cursor-not-allowed">
        Save Offer
      </button>
```

- [ ] **Step 5: Register the `offerQualification` factory in `app/static/htmx_app.js`** (ABOVE `Alpine.start()`)

```javascript
  Alpine.data('offerQualification', (prefill) => ({
    condition: (prefill && prefill.condition) || '',
    packaging: (prefill && prefill.packaging) || '',
    usage: (prefill && prefill.usage) || '',
    refurbished_by: (prefill && prefill.refurbished_by) || '',
    cert_doc: (prefill && prefill.cert_doc) || '',
    refurb_process: (prefill && prefill.refurb_process) || '',
    part_condition: (prefill && prefill.part_condition) || '',
    _pkgChips: ['Tape & Reel', 'Reels', 'Trays', 'Tubes', 'Antistatic bags', 'Boxes'],
    essentialsMet() {
      const c = this.condition;
      if (!c) return true; // unset is allowed to save
      if (c === 'new') return true; // manufacturer lives in More details; server backstops
      if (c === 'new_no_pkg') return this._pkgOk();
      if (c === 'pulls') return this._pkgOk() && (this.usage === 'boards' || this.usage === 'systems');
      if (c === 'refurb') return (this.refurbished_by === 'supplier' || this.refurbished_by === 'third_party') && !!this.refurb_process.trim();
      return true;
    },
    _pkgOk() { return this._pkgChips.includes(this.packaging); },
    noteText() {
      const c = this.condition, pkg = this.packaging;
      if (c === 'new') return "New — parts are in the original manufacturer's packaging.";
      if (c === 'new_no_pkg') return pkg ? `New, no original manufacturer packaging. Packaged in ${pkg}.` : 'New, no original manufacturer packaging.';
      if (c === 'pulls') {
        const u = this.usage;
        let n = (pkg && u) ? `Pulls — packaged in ${pkg}, pulled from ${u}.` : pkg ? `Pulls — packaged in ${pkg}.` : u ? `Pulls — pulled from ${u}.` : 'Pulls.';
        if (this.part_condition.trim()) n += ` Condition: ${this.part_condition.trim()}.`;
        return n;
      }
      if (c === 'refurb') {
        const who = this.refurbished_by === 'supplier' ? 'the supplier' : this.refurbished_by === 'third_party' ? 'a third party' : '';
        let n = who ? `Refurbished by ${who}.` : 'Refurbished.';
        if (this.refurb_process.trim()) n += ` Process: ${this.refurb_process.trim()}.`;
        if (this.refurbished_by === 'third_party') n += this.cert_doc === 'yes' ? ' Certifying document on file.' : this.cert_doc === 'no' ? ' No certifying document.' : '';
        return n;
      }
      return '';
    },
    _items() {
      const c = this.condition;
      if (c === 'new_no_pkg') return [this._pkgOk()];
      if (c === 'pulls') return [this._pkgOk(), this.usage === 'boards' || this.usage === 'systems', false, !!this.part_condition.trim()];
      if (c === 'refurb') { const a = [this.refurbished_by !== '', !!this.refurb_process.trim(), false]; if (this.refurbished_by === 'third_party') a.push(this.cert_doc !== ''); return a; }
      if (c === 'new') return [true, !!this.packaging, false];
      return [];
    },
    meterTotal() { return this._items().length; },
    meterFilled() { return this._items().filter(Boolean).length; },
  }));
```

> Note the client meter treats `images` as unknown (false) at entry time — matching the server (no attachments yet at create). It mirrors `noteText`/server `compose_note` exactly so preview == stored note.

- [ ] **Step 6: Build assets + headless console check**

Run: `npm run build`
Then load the sightings convert-to-offer modal authenticated and assert no console errors / Alpine init failures, switching condition through all four values (use the e2e session-cookie harness in `tests/e2e/conftest.py`):
Run: `npx playwright test --project=workflows -g "offer"` (or a new spec asserting `condition` panels toggle + no console errors).
Expected: panels show/hide per condition, note preview updates, zero console errors.

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/offers/_field_macros.html \
        app/templates/htmx/partials/offers/_qualification_fields.html \
        app/templates/htmx/partials/offers/_offer_form_fields.html \
        app/templates/htmx/partials/sightings/offer_form_modal.html app/static/htmx_app.js
git commit -m "feat(offers): condition-spine form, chips, live note preview + meter"
```

---

## Task 5: Qualification badge on offer row + detail

**Files:**
- Modify: `app/models/offers.py` (add `qualification_summary` property)
- Modify: `app/templates/htmx/partials/offers/_field_macros.html` (add `qual_badge`)
- Modify: `app/templates/htmx/partials/sightings/_offer_row.html` (badge beside status pill ~line 18–20)
- Modify: `app/templates/htmx/partials/sightings/offers_panel.html` (detail: standardized note + provenance + pending requests)
- Test: extend `tests/test_offer_qualification_model.py`

**Interfaces — Consumes:** Task 2 `meter`/`compute_status`. **Produces:** `Offer.qualification_summary -> {status, filled, total, note}`; a badge macro.

- [ ] **Step 1: Failing test for the property**

```python
def test_qualification_summary_property(db_session, test_requisition, test_user):
    from app.models.offers import Offer
    o = Offer(requisition_id=test_requisition.id, vendor_name="V", mpn="LM317T",
              condition="pulls", packaging="Trays", qualification={"usage": "boards"},
              entered_by_id=test_user.id)
    db_session.add(o); db_session.commit(); db_session.refresh(o)
    s = o.qualification_summary
    assert s["status"] in ("essentials", "complete", "incomplete")
    assert s["total"] == 4 and 0 <= s["filled"] <= 4
```

- [ ] **Step 2: Run, verify fail.** `... pytest tests/test_offer_qualification_model.py::test_qualification_summary_property -v --override-ini="addopts="` → FAIL.

- [ ] **Step 3: Add the property to `Offer`** (in `app/models/offers.py`, after the validators)

```python
    @property
    def qualification_summary(self) -> dict:
        """Live qualification badge/meter (display reads this; column is the snapshot)."""
        from app.services.offer_qualification import compute_status, meter

        data = {
            "manufacturer": self.manufacturer, "packaging": self.packaging, "date_code": self.date_code,
            **{k: (self.qualification or {}).get(k) for k in ("usage", "refurbished_by", "refurb_process", "cert_doc", "part_condition")},
        }
        has_images = bool(self.attachments)
        filled, total = meter(self.condition, data, has_images)
        return {"status": compute_status(self.condition, data, has_images),
                "filled": filled, "total": total, "note": self.qualification_note}
```

- [ ] **Step 4: Add the `qual_badge` macro** to `_field_macros.html`

```jinja
{%- macro qual_badge(offer) -%}
{% set s = offer.qualification_summary %}
{% set cmap = {'complete':'bg-emerald-50 text-emerald-700','essentials':'bg-amber-50 text-amber-700','incomplete':'bg-rose-50 text-rose-700','unset':'bg-gray-100 text-gray-500'} %}
<span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded-full {{ cmap.get(s.status, 'bg-gray-100 text-gray-600') }}"
      title="{{ s.note or 'Not qualified' }}">
  {% if s.status == 'unset' %}Unqualified{% elif s.total %}{{ s.filled }}/{{ s.total }}{% else %}{{ s.status|capitalize }}{% endif %}
</span>
{%- endmacro -%}
```

- [ ] **Step 5: Render the badge in `_offer_row.html`** — import the macro at top and drop `{{ qual_badge(o) }}` into the flex header row (line ~18) next to the status pill.

- [ ] **Step 6: Show the standardized note + provenance + requests in the offer detail** (`offers_panel.html`, in the per-offer expanded area): render `o.qualification_note`, and from `o.qualification`: `provenance_story`, `terms`, `lead_time_reason`, and pending `requests` (each as a small chip showing `kind` + `status`).

- [ ] **Step 7: Run model tests + build.** `... pytest tests/test_offer_qualification_model.py -v --override-ini="addopts="` PASS; `npm run build`.

- [ ] **Step 8: Commit**

```bash
git add app/models/offers.py app/templates/htmx/partials/offers/_field_macros.html \
        app/templates/htmx/partials/sightings/_offer_row.html \
        app/templates/htmx/partials/sightings/offers_panel.html tests/test_offer_qualification_model.py
git commit -m "feat(offers): qualification badge on row + standardized note on detail"
```

---

## Task 6: #8 vendor memory — prefill from last offer

**Files:**
- Modify: `app/routers/sightings.py` (`sightings_offer_form` ~2203)
- Test: extend `tests/test_offer_qualification_routes.py`

**Interfaces — Consumes:** Task 2 `prefill_from_vendor`. **Produces:** modal-open prefill merges vendor-stable answers.

- [ ] **Step 1: Failing test**

```python
def test_modal_open_prefills_country_from_last_vendor_offer(client, db_session, test_requisition, test_user):
    from app.models.offers import Offer
    from app.utils.normalization import normalize_vendor_name
    db_session.add(Offer(requisition_id=test_requisition.id, vendor_name="MemVendor",
                         vendor_name_normalized=normalize_vendor_name("MemVendor"),
                         mpn="LM317T", country_of_origin="JP", entered_by_id=test_user.id))
    db_session.commit()
    rid = test_requisition.requirements[0].id
    resp = client.get(f"/v2/partials/sightings/{rid}/offer-form", params={"vendor_name": "MemVendor"})
    assert resp.status_code == 200
    assert b"JP" in resp.content  # country prefilled into the form
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Merge prefill in `sightings_offer_form`** — after building `prefill` (only in the `vendor_name` truthy branch):

```python
    from app.services.offer_qualification import prefill_from_vendor
    from app.utils.normalization import normalize_vendor_name

    remembered = prefill_from_vendor(db, normalize_vendor_name(vendor_name))
    for k, v in remembered.items():
        prefill.setdefault(k, v)  # only fill empty keys; buyer overrides
```

(`refurbished_by`/`terms` land in `prefill` and are read by the Alpine factory / hidden inputs; `country_of_origin` lands in the More-details input.)

- [ ] **Step 4: Run test, verify pass. Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_offer_qualification_routes.py
git commit -m "feat(offers): #8 vendor-memory prefill on convert-to-offer modal open"
```

---

## Task 7: #7 one-tap vendor requests

**Files:**
- Modify: `app/routers/sightings.py` (new `POST .../offers/{offer_id}/request`)
- Modify: `app/templates/htmx/partials/sightings/offers_panel.html` (request chips + draft display)
- Test: extend `tests/test_offer_qualification_routes.py`

**Interfaces — Consumes:** Task 2 `request_template`, `REQUEST_KINDS`. **Produces:** logs a pending request on `offer.qualification['requests']` + returns the draft line.

- [ ] **Step 1: Failing test**

```python
def test_request_from_vendor_logs_pending_and_returns_draft(client, db_session, test_requisition, test_user):
    from app.models.offers import Offer
    o = Offer(requisition_id=test_requisition.id, vendor_name="V", mpn="LM317T",
              qualification={"requests": []}, entered_by_id=test_user.id)
    db_session.add(o); db_session.commit()
    rid = test_requisition.requirements[0].id
    resp = client.post(f"/v2/partials/sightings/{rid}/offers/{o.id}/request", data={"kind": "images"})
    assert resp.status_code == 200
    db_session.refresh(o)
    reqs = (o.qualification or {}).get("requests", [])
    assert reqs and reqs[-1]["kind"] == "images" and reqs[-1]["status"] == "pending"
    assert b"images" in resp.content.lower()
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Add the route to `app/routers/sightings.py`**

```python
@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/request", response_class=HTMLResponse)
async def sightings_offer_request(
    request: Request, requirement_id: int, offer_id: int,
    kind: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_buyer),
):
    from datetime import datetime, timezone

    from ..services.offer_qualification import REQUEST_KINDS, request_template

    offer = db.get(Offer, offer_id)
    if offer is None or kind not in REQUEST_KINDS:
        raise HTTPException(status_code=400, detail={"error": "invalid offer or request kind"})
    draft = request_template(kind, offer.mpn)
    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    reqs.append({"kind": kind, "status": "pending",
                 "requested_at": datetime.now(timezone.utc).isoformat(), "contact_id": None})
    q["requests"] = reqs
    offer.qualification = q
    db.commit(); db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), f"Logged request: {draft}")
```

> v1 logs the pending request + surfaces the draft line (buyer copies it into the existing solicit modal to actually send via `send_batch_rfq`). Auto-send wiring is a documented follow-up (`Contact.requisition_id` is NOT NULL — a real send needs a requisition/scratch-req).

- [ ] **Step 4: Render request chips in `offers_panel.html`** — for each `REQUEST_KINDS`, a small `hx-post` button to the route (use `hx-vals` as an object literal `'{"kind":"images"}'`), and list existing `requests` with their status.

- [ ] **Step 5: Run test, verify pass. Step 6: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/offers_panel.html \
        tests/test_offer_qualification_routes.py
git commit -m "feat(offers): #7 one-tap vendor requests (log pending + draft RFQ line)"
```

---

## Task 8: Docs + full verification

**Files:**
- Modify: `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_ARCHITECTURE.md`

- [ ] **Step 1: Update `APP_MAP_DATABASE.md`** — `offers` gains `qualification_status` / `qualification_note` / `qualification (JSON)`; document the JSON shape + the `OfferCondition`/`QualificationStatus` enums + `used→pulls`.
- [ ] **Step 2: Update `APP_MAP_INTERACTIONS.md`** — `offer_qualification` service; the gate (client disable + server 422/re-render); #7 request flow; #8 prefill.
- [ ] **Step 3: Update `APP_MAP_ARCHITECTURE.md`** — new `app/services/offer_qualification.py`.
- [ ] **Step 4: `pre-commit run --all-files`** — fix lint/format/mypy/static-guard (tojson/hx-vals) findings.
- [ ] **Step 5: Full suite** — `TESTING=1 PYTHONPATH=$PWD pytest tests/ -q`. Expected: green (fix any condition-vocab regressions surfaced).
- [ ] **Step 6: Build + console sweep** — `npm run build`; load the offer modal + sightings offers panel authenticated, assert no console errors across all four conditions.
- [ ] **Step 7: Commit docs**

```bash
git add docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_ARCHITECTURE.md
git commit -m "docs(offers): APP_MAP updates for qualification capture"
```

- [ ] **Step 8: Deploy + live-PG verify** (with user authorization) — `./deploy.sh`; then drive the deployed app on real PG: convert a sighting to an offer for each condition, confirm the standardized note persists, the `used→pulls` migration ran (`SELECT condition, count(*) FROM offers GROUP BY 1`), and the badge/meter render. (SQLite masks PG JSON behavior — this live pass is required.)

---

## Self-Review (against the spec)

**Spec coverage:** §3 data model → Task 1. §3.1 JSON shape (`usage`/`refurbished_by`/`refurb_process`/`cert_doc`/`part_condition`/`provenance_story`/`terms`/`lead_time_reason`/`requests`) → Tasks 2,3,7. §3.2 taxonomy + `used→pulls` → Tasks 1,2. §4 matrix + chips + auto-note → Tasks 2,4. §4.2 note composition → Task 2 (`compose_note`) + Task 4 (`noteText` mirror). §5 gate (client disable + server 422/re-render) → Tasks 3,4. §5.1 status/meter → Task 2. §6 service API → Tasks 2,6,7. §7 requests → Task 7. §8 vendor memory → Task 6. §9 form reorg + badge → Tasks 4,5. §10 routes → Tasks 3,6,7. §11 testing → every task + Task 8. §12 docs → Task 8.

**Placeholder scan:** none — all steps carry real code or exact commands.

**Type consistency:** field names (`usage`, `refurbished_by`, `refurb_process`, `cert_doc`, `part_condition`, `provenance_story`, `terms`, `lead_time_reason`) are identical across the service `data` dict, the schema/Form params, the `_qual_dict`/`qkeys` collectors, the Alpine state, and the JSON shape. `compose_note` (Py) and `noteText()` (JS) produce byte-identical strings per condition. `meter` denominators match between `_items_for` (Py) and `_items()` (JS) except `images` (always counted, false at entry on both sides).

**Known v1 boundaries (documented, not band-aids):** (a) attaching an image does not recompute the stored `qualification_status` until the next offer save — the displayed badge/meter (`qualification_summary`) is always live, the column is a refresh-on-save snapshot; (b) #7 logs the pending request + drafts the line but does not auto-send (the existing solicit modal sends); (c) `lead_time_reason`/`provenance_story`/`terms` are captured + displayed but are meter-neutral optional notes per the spec.
