"""requirement_service.py — the ONE requirement-creation pipeline (spec §9, Wave 3).

Every surface that creates Requirement rows routes through ``create_requirements()``
(sync core) or ``create_requirements_ui()`` (async facade, adds datasheet-capture
enqueue). The pipeline carries the union of behaviors that previously lived only in
the W2-deleted JSON ``/api/requisitions/{id}/requirements`` path plus the shared
normalization the HTML paths already did:

- MPN display normalization (``normalize_mpn``) + canonical key (``normalize_mpn_key``)
  stored in ``normalized_mpn`` (lowercase, non-alphanumeric stripped — the join key
  part-history / material-card lookups assume; migration 206 remaps legacy rows).
- Condition / packaging vocabulary normalization (unmapped spellings store as NULL —
  fresh migration-built DBs enforce the vocabulary via chk_req_condition /
  chk_req_packaging, so a raw fallback would 500 there).
- Quantity / price coercion for string inputs (``normalize_quantity`` / ``normalize_price``).
- Canonical substitute parsing + dedup (``parse_substitute_mpns``, cap 20) and
  best-effort MaterialCard resolution for each substitute.
- Savepoint-guarded ``resolve_material_card`` for the primary MPN.
- Tag propagation to the customer site + company (best-effort).
- ``Source {mpn}`` task auto-gen via ``task_service.on_requirement_added`` (assigned to
  the requisition creator; skipped for scratch requisitions by design — quick-source
  scratch reqs are lightweight one-off actions, not sourcing commitments).
- 30-day same-customer-site duplicate detection (material-card match; informational —
  returned to the caller, never blocks).
- Datasheet-capture enqueue (async facade only; fire-and-forget, testing-suppressed).

Transaction contract: the core FLUSHES but does not commit; the caller owns the final
commit. Caveat inherited from task_service: ``on_requirement_added`` persists via
``_persist_task`` which commits — so on non-scratch requisitions the new rows are
committed as a side effect of task auto-gen (identical to the deleted JSON path,
which also committed mid-pipeline).

Called by: routers/htmx/requisitions.py (requisition_import_save, add_requirement),
           routers/htmx/search_views.py (add_to_requisition),
           services/requisition_service.py (clone_requisition),
           services/quick_source_service.py (_new_requirement)
Depends on: models.sourcing (Requirement, Requisition), models.crm (CustomerSite),
            search_service.resolve_material_card, services.task_service,
            services.tagging, services.datasheet_capture, utils.normalization
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import SourcingStatus
from ..models.crm import CustomerSite
from ..models.sourcing import Requirement, Requisition
from ..utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
    parse_substitute_mpns,
)

_UNSET = object()

# Optional passthrough fields: item key -> Requirement column (same names).
_PASSTHROUGH_TEXT_FIELDS = (
    "oem_pn",
    "brand",
    "sku",
    "firmware",
    "date_codes",
    "hardware_codes",
    "notes",
    "sale_notes",
    "description",
    "package_type",
    "revision",
    "customer_pn",
)


@dataclass
class RequirementsResult:
    """Outcome of one create_requirements() call."""

    created: list[Requirement] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)  # {"mpn", "req_id", "req_name"}
    skipped: list[dict] = field(default_factory=list)  # {"index", "error"}


# requirements.packaging vocabulary (chk_req_packaging, migration 048). Narrower
# than normalize_packaging's output: that map also emits sightings/offers-only
# values ('bag','box','each' — chk_sight_/chk_offer_packaging), which the
# requirements constraint rejects.
_REQ_PACKAGING_VOCAB = frozenset({"reel", "tube", "tray", "bulk", "cut_tape"})


def _norm_or_none(fn: Callable[[Any], str | None], raw: Any) -> str | None:
    """Vocabulary-normalize ``raw``; unmapped spellings store as NULL.

    Deliberately NOT a raw fallback: fresh migration-built DBs enforce the
    vocabulary via chk_req_condition / chk_req_packaging (NOT VALID still checks
    new writes), so an unmapped raw value would 500 there. Mirrors the deleted
    JSON path's semantics exactly.
    """
    if not raw:
        return None
    return fn(raw)


def req_packaging(raw: Any) -> str | None:
    """normalize_packaging clamped to the requirements vocabulary.

    Public: the requirement edit paths (JSON PUT, HTMX) reuse it so every
    requirements.packaging write obeys chk_req_packaging.
    """
    value = _norm_or_none(normalize_packaging, raw)
    return value if value in _REQ_PACKAGING_VOCAB else None


def _coerce_qty(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    return normalize_quantity(raw)


def _coerce_price(raw: Any) -> float | None:
    if raw is None or isinstance(raw, (int, float)):
        return raw
    return normalize_price(raw)


def _coerce_date(raw: Any) -> date | None:
    if raw is None or isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except (ValueError, TypeError):
        return None


def _resolve_card_safely(db: Session, mpn: str, manufacturer: str = ""):
    """Savepoint-guarded MaterialCard resolution — a card failure never kills the
    save."""
    try:
        # Lazy: search_service pulls in the whole connector stack (quick-source precedent).
        from ..search_service import resolve_material_card
    except Exception:  # noqa: BLE001 — pragma: no cover; import failure is environmental
        logger.warning("search_service import failed; skipping card resolve for {}", mpn, exc_info=True)
        return None
    nested = db.begin_nested()
    try:
        card = resolve_material_card(mpn, db, manufacturer or "")
        nested.commit()
        return card
    except Exception:  # noqa: BLE001 — card resolve is best-effort, never kills the save
        nested.rollback()
        logger.warning("resolve_material_card failed for {}", mpn, exc_info=True)
        return None


def create_requirements(
    db: Session,
    req: Requisition,
    items: list[dict],
) -> RequirementsResult:
    """Create Requirement rows on ``req`` from plain item dicts.

    ``req`` must already be flushed (id set). Flushes the new rows; does NOT issue the
    final commit (see module docstring for the task_service commit caveat). Items with
    a blank ``primary_mpn`` are skipped and reported, never fatal.
    """
    result = RequirementsResult()
    explicit_null_qty: list[Requirement] = []

    for idx, item in enumerate(items):
        mpn_raw = str(item.get("primary_mpn") or "").strip()
        if not mpn_raw:
            result.skipped.append({"index": idx, "error": "primary_mpn is required"})
            continue
        display = normalize_mpn(mpn_raw) or mpn_raw
        manufacturer = str(item.get("manufacturer") or "").strip()
        subs = parse_substitute_mpns(item.get("substitutes"), display)
        card = _resolve_card_safely(db, display, manufacturer)

        kwargs: dict[str, Any] = {
            "requisition_id": req.id,
            "primary_mpn": display,
            "normalized_mpn": normalize_mpn_key(display),
            "material_card_id": card.id if card else None,
            "manufacturer": manufacturer,
            "substitutes": subs,
            "condition": _norm_or_none(normalize_condition, item.get("condition")),
            "packaging": req_packaging(item.get("packaging")),
            "need_by_date": _coerce_date(item.get("need_by_date")),
            "sourcing_status": SourcingStatus.OPEN,
        }
        # target_qty distinguishes "absent" (model default 1) from an explicit None
        # (the search picker adds a part with no quantity — stored NULL). The ORM
        # applies Column(default=1) even to an explicitly-set None at INSERT, so
        # explicit NULLs are re-asserted after the flush below (defaults are
        # INSERT-only; the post-flush assignment UPDATEs to NULL).
        qty = item.get("target_qty", _UNSET)
        qty_is_explicit_null = qty is not _UNSET and _coerce_qty(qty) is None
        if qty is not _UNSET:
            kwargs["target_qty"] = _coerce_qty(qty)
        price = item.get("target_price", _UNSET)
        if price is not _UNSET:
            kwargs["target_price"] = _coerce_price(price)
        for fld in _PASSTHROUGH_TEXT_FIELDS:
            if fld in item and item[fld] is not None:
                kwargs[fld] = item[fld]

        r = Requirement(**kwargs)
        db.add(r)
        result.created.append(r)
        if qty_is_explicit_null:
            explicit_null_qty.append(r)
        for sub in subs:
            _resolve_card_safely(db, sub["mpn"], sub.get("manufacturer", ""))

    db.flush()

    if explicit_null_qty:
        for r in explicit_null_qty:
            r.target_qty = None
        db.flush()

    _propagate_tags(db, req, result.created)
    if not bool(getattr(req, "is_scratch", False)):
        _autogen_tasks(db, req, result.created)
    result.duplicates = _detect_duplicates(db, req, result.created)
    return result


async def create_requirements_ui(
    db: Session,
    req: Requisition,
    items: list[dict],
    *,
    actor_id: int,
) -> RequirementsResult:
    """Async facade for UI routers: the core pipeline + datasheet-capture enqueue."""
    result = create_requirements(db, req, items)
    try:
        from ..services.datasheet_capture import capture_datasheet
        from ..utils.async_helpers import safe_background_task

        for mpn in {r.primary_mpn for r in result.created if r.primary_mpn}:
            await safe_background_task(
                capture_datasheet(mpn, actor_id), task_name="datasheet_capture", suppress_in_testing=True
            )
    except Exception:  # noqa: BLE001 — fire-and-forget enqueue must never fail the save
        logger.warning("Datasheet capture enqueue failed for requirements", exc_info=True)
    return result


def _propagate_tags(db: Session, req: Requisition, created: list[Requirement]) -> None:
    """Best-effort MaterialTag propagation to the customer site + its company."""
    if not created or not req.customer_site_id:
        return
    try:
        from ..services.tagging import propagate_tags_to_entity

        site = db.get(CustomerSite, req.customer_site_id)
        for r in created:
            if r.material_card_id:
                propagate_tags_to_entity("customer_site", req.customer_site_id, r.material_card_id, 1.0, db)
                if site and site.company_id:
                    propagate_tags_to_entity("company", site.company_id, r.material_card_id, 1.0, db)
    except Exception:  # noqa: BLE001 — pragma: no cover; tag propagation is best-effort
        logger.warning("Tag propagation failed for requirements", exc_info=True)


def _autogen_tasks(db: Session, req: Requisition, created: list[Requirement]) -> None:
    """Best-effort 'Source {mpn}' task per new requirement (skipped for scratch
    reqs)."""
    if not created:
        return
    try:
        from ..services.task_service import on_requirement_added

        for r in created:
            on_requirement_added(db, req.id, r.primary_mpn, assigned_to_id=req.created_by)
    except Exception:  # noqa: BLE001 — task auto-gen must never fail the save
        logger.warning("Task auto-gen for requirements failed", exc_info=True)


def _detect_duplicates(db: Session, req: Requisition, created: list[Requirement]) -> list[dict]:
    """Same-part requirements on OTHER requisitions for this customer site, last 30
    days.

    Material-card match (the card is the part identity); informational only.
    """
    if not (req.customer_site_id and created):
        return []
    cutoff = datetime.now(UTC) - timedelta(days=30)
    card_ids = [r.material_card_id for r in created if r.material_card_id]
    if not card_ids:
        return []
    dup_rows = (
        db.query(Requirement.primary_mpn, Requisition.id, Requisition.name)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(
            Requirement.material_card_id.in_(card_ids),
            Requisition.customer_site_id == req.customer_site_id,
            Requisition.id != req.id,
            Requisition.created_at >= cutoff,
        )
        .all()
    )
    duplicates = []
    seen: set[str] = set()
    for mpn, rid, rname in dup_rows:
        key = f"{mpn}:{rid}"
        if key not in seen:
            seen.add(key)
            duplicates.append({"mpn": mpn, "req_id": rid, "req_name": rname})
    return duplicates
