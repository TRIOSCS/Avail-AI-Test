"""qp_draft_service.py — draft the empty free-text Quality-Plan fields from the deal
(survey ideas #8 + #19, merged).

Pure, read-only suggestion builder — it NEVER writes the QP. The router renders the
suggestions into the section form (fill-empty-only) behind an amber "AI — verify" banner;
the human accepts and the existing section PATCH persists. Three clearly-separated sources:
  - "deal"  — deterministic copy from this buy plan's Requirement + chosen Offer + commodity
  - "prior" — carry-forward from the most-recent section-reviewed QP for the same
              company + commodity
  - "ai"    — Claude extraction from a pasted customer TSO/PO (extract_qp_fields_from_paste)

Called by: app/routers/quality_plans.py (the draft routes).
Depends on: models (quality_plan / buy_plan / sourcing / crm / intelligence), claude_client.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import MaterialCard
from app.models.quality_plan import QualityPlan
from app.models.sourcing import Requirement, Requisition
from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError

# The draftable (non-boolean) free-text/number fields per section. Booleans are deliberately
# excluded — a yes/no policy is never auto-suggested from a different deal. Mirrors the
# section PATCH whitelists in routers/quality_plans.py (the writer for accepted values).
_SALES_DRAFT_FIELDS = (
    "sales_condition",
    "sales_quantity",
    "sales_fw_hw_rev",
    "sales_product_commodity",
    "sales_testing_option",
    "sales_testing_specifics",
    "sales_test_location",
    "sales_routing_prescreening_whs",
    "sales_vendor_rating",
    "sales_pkg_requirements",
    "sales_bom_matrix_links",
    "sales_notes",
)
_PURCHASING_DRAFT_FIELDS = (
    "purchasing_po_number",
    "purchasing_condition",
    "purchasing_fw_hw_rev",
    "purchasing_product_commodity",
    "purchasing_testing_option",
    "purchasing_routing_prescreening_whs",
    "purchasing_packaging",
    "purchasing_tpo_notes",
)
_SECTION_FIELDS = {"sales": _SALES_DRAFT_FIELDS, "purchasing": _PURCHASING_DRAFT_FIELDS}


def _is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _fw_hw_string(*parts: str | None) -> str | None:
    """Join present FW/HW/date fragments into one 'FW1.2 / HWA' string (None if all
    blank)."""
    kept = [str(p).strip() for p in parts if p and str(p).strip()]
    return " / ".join(kept) or None


def _first_line(bp: BuyPlan) -> BuyPlanLine | None:
    """The first buy-plan line that carries a requirement (the deal source).

    Most QPs are single-line; a multi-line QP drafts from its first requirement-bearing
    line.
    """
    lines: list[BuyPlanLine] = bp.lines or []
    for line in sorted(lines, key=lambda ln: ln.id):
        if line.requirement is not None:
            return line
    return None


def _deal_context(db: Session, qp: QualityPlan) -> dict:
    """Gather the deal's draftable values (Requirement + chosen Offer + commodity)."""
    bp = qp.buy_plan
    if bp is None:
        return {}
    line = _first_line(bp)
    if line is None:
        return {}
    req = line.requirement
    offer = line.offer
    commodity = None
    if req is not None and req.material_card_id is not None:
        card = db.get(MaterialCard, req.material_card_id)
        commodity = card.category if card else None
    return {
        "commodity": commodity,
        "condition": req.condition if req else None,
        "quantity": (req.target_qty if req and req.target_qty else None) or line.quantity,
        "sales_fw_hw": _fw_hw_string(req.firmware, req.hardware_codes, req.date_codes) if req else None,
        "offer_condition": offer.condition if offer else None,
        "offer_packaging": offer.packaging if offer else None,
        "offer_fw_hw": _fw_hw_string(offer.firmware, offer.hardware_code) if offer else None,
        "po_number": line.po_number,
    }


def _deterministic_values(section: str, ctx: dict) -> dict:
    """Map the deal context onto this section's QP fields (deterministic, no AI)."""
    if section == "sales":
        return {
            "sales_condition": ctx.get("condition"),
            "sales_quantity": ctx.get("quantity"),
            "sales_product_commodity": ctx.get("commodity"),
            "sales_fw_hw_rev": ctx.get("sales_fw_hw"),
        }
    return {
        "purchasing_po_number": ctx.get("po_number"),
        "purchasing_condition": ctx.get("offer_condition") or ctx.get("condition"),
        "purchasing_product_commodity": ctx.get("commodity"),
        "purchasing_packaging": ctx.get("offer_packaging"),
        "purchasing_fw_hw_rev": ctx.get("offer_fw_hw") or ctx.get("sales_fw_hw"),
    }


def _prior_reviewed_values(db: Session, qp: QualityPlan, section: str, fields: list[str]) -> dict:
    """Carry-forward: values for *fields* from recent section-reviewed QPs (same company +
    commodity), most-recent first. Only fills a field once (first non-empty prior wins)."""
    bp = qp.buy_plan
    if bp is None or bp.requisition is None or bp.requisition.company_id is None:
        return {}
    ctx = _deal_context(db, qp)
    commodity = ctx.get("commodity")
    if not commodity:
        return {}  # no commodity → carry-forward would be too broad
    reviewed_at = getattr(QualityPlan, f"{section}_section_reviewed_at")
    # Match the commodity via an EXISTS semi-join, NOT a row-multiplying inner join on
    # BuyPlanLine: a prior QP whose buy plan has several matching (split) lines must count
    # once, or the SQL-level LIMIT would count fanned-out rows and starve older prior QPs.
    commodity_match = (
        select(BuyPlanLine.id)
        .join(Requirement, BuyPlanLine.requirement_id == Requirement.id)
        .join(MaterialCard, Requirement.material_card_id == MaterialCard.id)
        .where(BuyPlanLine.buy_plan_id == BuyPlan.id, MaterialCard.category == commodity)
        .exists()
    )
    stmt = (
        select(QualityPlan)
        .join(BuyPlan, QualityPlan.buy_plan_id == BuyPlan.id)
        .join(Requisition, BuyPlan.requisition_id == Requisition.id)
        .where(
            Requisition.company_id == bp.requisition.company_id,
            reviewed_at.isnot(None),
            QualityPlan.id != qp.id,
            commodity_match,
        )
        .order_by(reviewed_at.desc())
        .limit(3)
    )
    out: dict = {}
    for prior in db.execute(stmt).scalars():
        for field in fields:
            if field not in out:
                val = getattr(prior, field, None)
                if not _is_empty(val):
                    out[field] = val
    return out


def build_section_draft(
    db: Session, qp: QualityPlan, section: str, *, paste_values: dict | None = None
) -> dict[str, dict]:
    """Suggested values for the EMPTY fields of *section*.

    Precedence for a still-empty field:
    deterministic deal-copy ("deal") → pasted-document extraction ("ai") → carry-forward
    ("prior"). Returns {field: {"value": v, "source": ...}}. Never touches already-set fields.
    """
    fields = _SECTION_FIELDS.get(section, ())
    empty = [f for f in fields if _is_empty(getattr(qp, f, None))]
    if not empty:
        return {}

    draft: dict[str, dict] = {}
    for field, value in _deterministic_values(section, _deal_context(db, qp)).items():
        if field in empty and not _is_empty(value):
            draft[field] = {"value": value, "source": "deal"}

    for field, value in (paste_values or {}).items():
        if field in empty and field not in draft and not _is_empty(value):
            draft[field] = {"value": value, "source": "ai"}

    still_empty = [f for f in empty if f not in draft]
    for field, value in _prior_reviewed_values(db, qp, section, still_empty).items():
        draft[field] = {"value": value, "source": "prior"}
    return draft


def _paste_schema(section: str) -> dict:
    props = {f: {"type": ["string", "null"]} for f in _SECTION_FIELDS.get(section, ())}
    return {"type": "object", "properties": props}


def _paste_system(section: str) -> str:
    return (
        f"You extract Quality-Plan {section.upper()} fields from a pasted customer TSO/PO/quote. "
        "Return ONLY fields the document actually states; use null for anything not present — "
        "NEVER guess or infer. Copy values verbatim (trimmed). Fields: " + ", ".join(_SECTION_FIELDS.get(section, ()))
    )


async def extract_qp_fields_from_paste(raw_text: str, section: str) -> dict:
    """Claude-extract QP fields from a pasted customer document.

    Returns {field: value} for the whitelisted section fields only; {} on empty text or
    any AI failure (never raises).
    """
    if not raw_text or not raw_text.strip():
        return {}
    allowed = set(_SECTION_FIELDS.get(section, ()))
    if not allowed:
        return {}
    try:
        result = await claude_structured(
            raw_text.strip()[:8000],
            _paste_schema(section),
            system=_paste_system(section),
            model_tier="fast",
            max_tokens=1024,
            max_attempts=1,
            cost_bucket="qp_draft_paste",
        )
    except ClaudeError:
        return {}
    if not isinstance(result, dict):
        return {}
    out: dict = {}
    for field, value in result.items():
        if field in allowed and value is not None and str(value).strip():
            out[field] = str(value).strip()
    return out
